"""联机房间的纯数据实体（连接、房间、邀请、队列项）。

这些 dataclass 不依赖 FastAPI 路由或数据库，便于 RoomManager 与
协议测试单独引用。``rooms.py`` 会重新导出它们，保持原有导入路径兼容。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

from starlette.websockets import WebSocket

from gomoku.shared.avatar import DEFAULT_AVATAR, normalize_avatar
from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Player
from gomoku.shared.time_control import TimeControl

logger = logging.getLogger(__name__)


@dataclass
class ClientConnection:
    """一条已认证的 WebSocket 连接及其发送锁。"""

    connection_id: str
    user_id: str
    username: str
    websocket: WebSocket
    client_ip: str = ""
    # 客户端在认证帧中声明的能力；未知能力由调用方忽略。
    capabilities: tuple[str, ...] = ()
    avatar: str = DEFAULT_AVATAR
    avatar_version: int = 0
    achievements: tuple[str, ...] = ()
    rating: int = 0
    connected: bool = True
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, message: dict) -> bool:
        if not self.connected:
            return False
        try:
            async with self._send_lock:
                await self.websocket.send_json(message)
            return True
        except Exception:
            logger.warning("WebSocket 发送失败 user=%s", self.user_id)
            self.connected = False
            return False

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if not self.connected:
            return
        self.connected = False
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:
            pass


@dataclass
class QueuedPlayer:
    """匹配队列中的一名玩家（内存状态）。"""

    conn: ClientConnection
    rating: int
    queued_at: float
    time_control: TimeControl


@dataclass
class Room:
    """一个好友房或排位房的服务器权威状态。"""

    code: str
    game_id: str
    black_user_id: str
    white_user_id: Optional[str]
    black_name: str
    white_name: str
    time_control: TimeControl
    black_avatar: str = DEFAULT_AVATAR
    white_avatar: str = DEFAULT_AVATAR
    black_avatar_version: int = 0
    white_avatar_version: int = 0
    black_achievements: tuple[str, ...] = ()
    white_achievements: tuple[str, ...] = ()
    # 排位结算后本局新解锁的成就；按用户 id 保存，供终局快照提示。
    new_achievements: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    game: GameModel = field(default_factory=GameModel)
    status: str = "waiting"  # waiting/playing/black_won/white_won/draw
    winner_user_id: Optional[str] = None
    result_reason: Optional[str] = None
    connections: dict[str, ClientConnection] = field(default_factory=dict)
    # 观战者：不是本局玩家，只接收权威快照，不能进行任何操作。
    # 刻意不放进 connections——那个字典会被聊天广播、房间关闭广播和再战
    # 投票逻辑遍历，放进去等于悄悄给观战者发了玩家权限。
    spectators: dict[str, ClientConnection] = field(default_factory=dict)
    rematch_votes: set[str] = field(default_factory=set)
    processed_moves: dict[str, int] = field(default_factory=dict)
    # 服务端局面版本。悔棋会让手数变小，客户端用版本号识别回滚快照。
    state_version: int = 0
    # 待回应的悔棋请求发起者；为空表示没有请求。
    undo_requester_id: Optional[str] = None
    # 本局每位玩家的悔棋次数与最近请求时间（epoch 秒）。
    undo_counts: dict[str, int] = field(default_factory=dict)
    undo_last_request: dict[str, float] = field(default_factory=dict)
    undo_task: Optional[asyncio.Task] = None
    undo_token: int = 0
    # 自建房可以让房主关闭观战；关闭后不出现在对战广场，也不能被旁观。
    allow_spectators: bool = True
    # 掉线宽限：按用户保存计时任务，重连时取消对应的逃跑判负。
    escape_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    escape_tokens: dict[str, int] = field(default_factory=dict)
    turn_deadline_ms: Optional[int] = None
    # 双方当前余量（毫秒）。per_move 模式只使用当前走子方的余量；
    # Fischer 模式在每手落子后把本手耗时从余量中扣除，再加固定加秒。
    clock_remaining_ms: dict[str, int] = field(default_factory=dict)
    # 本手开始时刻（epoch 毫秒）；为 None 表示计时暂停/尚未开始。
    clock_started_ms: Optional[int] = None
    timeout_task: Optional[asyncio.Task] = None
    turn_token: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 表情/评论专用锁：只用于串行化发言广播，绝不占用 lock，
    # 否则聊天会拖慢落子写库和超时判定，可能造成误判超时负。
    chat_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 每个用户最近若干次发言的时间戳（time.monotonic），用于限流。
    chat_sent_times: dict[str, list[float]] = field(default_factory=dict)
    rated: bool = False
    matchmade: bool = False
    rating_before: dict[str, int] = field(default_factory=dict)
    rating_after: dict[str, int] = field(default_factory=dict)
    rating_delta: dict[str, int] = field(default_factory=dict)
    rating_applied: bool = False

    def color_for(self, user_id: str) -> Optional[Player]:
        if user_id == self.black_user_id:
            return Player.BLACK
        if user_id == self.white_user_id:
            return Player.WHITE
        return None

    def opponent_id(self, user_id: str) -> Optional[str]:
        if user_id == self.black_user_id:
            return self.white_user_id
        if user_id == self.white_user_id:
            return self.black_user_id
        return None

    def username_for(self, user_id: str) -> str:
        if user_id == self.black_user_id:
            return self.black_name
        if user_id == self.white_user_id:
            return self.white_name
        return "对手"

    def avatar_for(self, user_id: str) -> str:
        if user_id == self.black_user_id:
            return normalize_avatar(self.black_avatar, user_id)
        if user_id == self.white_user_id:
            return normalize_avatar(self.white_avatar, user_id)
        return DEFAULT_AVATAR

    def avatar_version_for(self, user_id: str) -> int:
        if user_id == self.black_user_id:
            return max(0, self.black_avatar_version)
        if user_id == self.white_user_id:
            return max(0, self.white_avatar_version)
        return 0

    def achievements_for(self, user_id: str) -> tuple[str, ...]:
        if user_id == self.black_user_id:
            return self.black_achievements
        if user_id == self.white_user_id:
            return self.white_achievements
        return ()

    def participant_ids(self) -> list[str]:
        return [
            uid
            for uid in (self.black_user_id, self.white_user_id)
            if uid
        ]


@dataclass
class RoomInvite:
    """房主向在线好友发出的对局邀请（单进程内存状态）。"""

    invite_id: str
    room: Room
    inviter_id: str
    invitee_id: str
    created_at: float


__all__ = [
    "ClientConnection",
    "QueuedPlayer",
    "Room",
    "RoomInvite",
]
