"""服务器权威的好友房、排位房与快速匹配管理器。

设计要点：
* 单进程内存房间/匹配队列 + 每手数据库写穿；每房一个 asyncio.Lock 串行处理；
* 客户端只提交意图，服务器校验座位/回合/步序/坐标/时钟后广播完整快照；
* `(game_id, move_no)` 和 `(game_id, client_move_id)` 唯一，支持幂等重发；
* 断线不立刻判负：保留房间和时钟，玩家可在剩余时间内重连；
* 服务重启不伪造恢复：启动时未终局对局标记 abandoned，客户端回大厅；
* 好友房不计分；快速匹配创建排位房，终局由服务器按 Elo 结算一次，
  并把两名玩家的积分变化写入 rating_events 审计；
* 排位房不能通过房间码加入、不支持同一对手直接再战，降低刷分面。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import select, update
from starlette.websockets import WebSocket

from gomoku.shared.game_model import GameModel
from gomoku.shared.protocol import (
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_OPPONENT_STATUS,
    SERVER_QUEUE_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_CREATED,
    make_message,
)
from gomoku.shared.rules import BOARD_SIZE, Player
from server.app.config import Settings
from server.app.db import utcnow_naive
from server.app.models import Game, Move, RatingEvent, User, new_id
from server.app.ratings import calculate_pair, rank_for_rating

logger = logging.getLogger(__name__)

_ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _color_text(player: Player) -> str:
    return "black" if player is Player.BLACK else "white"


def _status_for_game(game: GameModel) -> str:
    if game.winner is Player.BLACK:
        return "black_won"
    if game.winner is Player.WHITE:
        return "white_won"
    if game.is_draw:
        return "draw"
    return "playing"


@dataclass
class ClientConnection:
    """一条已认证的 WebSocket 连接及其发送锁。"""

    connection_id: str
    user_id: str
    username: str
    websocket: WebSocket
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


@dataclass
class Room:
    """一个好友房或排位房的服务器权威状态。"""

    code: str
    game_id: str
    black_user_id: str
    white_user_id: Optional[str]
    black_name: str
    white_name: str
    turn_seconds: int
    game: GameModel = field(default_factory=GameModel)
    status: str = "waiting"  # waiting/playing/black_won/white_won/draw
    winner_user_id: Optional[str] = None
    result_reason: Optional[str] = None
    connections: dict[str, ClientConnection] = field(default_factory=dict)
    rematch_votes: set[str] = field(default_factory=set)
    processed_moves: dict[str, int] = field(default_factory=dict)
    turn_deadline_ms: Optional[int] = None
    timeout_task: Optional[asyncio.Task] = None
    turn_token: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
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

    def participant_ids(self) -> list[str]:
        return [
            uid
            for uid in (self.black_user_id, self.white_user_id)
            if uid
        ]


class RoomManager:
    """单进程内所有连接、房间与计时器的管理者。"""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._rooms_by_code: dict[str, Room] = {}
        self._room_by_user: dict[str, Room] = {}
        self._connections: dict[str, ClientConnection] = {}
        self._queue: dict[str, QueuedPlayer] = {}
        self._state_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 连接/发送辅助
    # ------------------------------------------------------------------
    def _is_current_connection(self, conn: ClientConnection) -> bool:
        return self._connections.get(conn.user_id) is conn

    async def _send_error(
        self,
        conn: ClientConnection,
        reason: str,
        request_id: Optional[str] = None,
    ) -> None:
        await conn.send(
            make_message(SERVER_ERROR, {"reason": reason}, request_id=request_id)
        )

    async def _send_room_closed(self, room: Room, reason: str) -> None:
        message = make_message(SERVER_ROOM_CLOSED, {"reason": reason})
        for conn in list(room.connections.values()):
            await conn.send(message)

    async def _send_opponent_status(
        self,
        room: Room,
        target_user_id: str,
        online: bool,
        who_name: str,
    ) -> None:
        conn = room.connections.get(target_user_id)
        if conn is not None:
            await conn.send(
                make_message(
                    SERVER_OPPONENT_STATUS,
                    {"username": who_name, "online": online},
                )
            )

    async def _send_room_created(self, conn: ClientConnection, room: Room) -> None:
        await conn.send(
            make_message(
                SERVER_ROOM_CREATED,
                {
                    "room_code": room.code,
                    "game_id": room.game_id,
                    "your_color": "black",
                    "black_name": room.black_name,
                    "white_name": room.white_name,
                    "turn_seconds": room.turn_seconds,
                },
            )
        )

    # ------------------------------------------------------------------
    # 匹配队列辅助
    # ------------------------------------------------------------------
    def _queue_state_payload(self, user_id: str) -> dict:
        queued = self._queue.get(user_id)
        rating = queued.rating if queued is not None else None
        return {
            "state": "queued" if queued is not None else "idle",
            "queue_size": len(self._queue),
            "your_rating": rating,
            "rank_name": (
                rank_for_rating(rating).name if rating is not None else ""
            ),
        }

    async def _send_queue_status(self, conn: ClientConnection) -> None:
        await conn.send(
            make_message(
                SERVER_QUEUE_STATUS,
                self._queue_state_payload(conn.user_id),
            )
        )

    def _match_found_payload(
        self, room: Room, user_id: str, opponent_id: Optional[str]
    ) -> dict:
        color = room.color_for(user_id)
        return {
            "game_id": room.game_id,
            "your_color": _color_text(color) if color else "black",
            "opponent_name": (
                room.username_for(opponent_id)
                if opponent_id
                else "对手"
            ),
            "your_rating": room.rating_before.get(user_id),
            "opponent_rating": (
                room.rating_before.get(opponent_id)
                if opponent_id
                else None
            ),
            "rated": True,
        }

    def _prune_queue_locked(self) -> None:
        for user_id, queued in list(self._queue.items()):
            if (
                not queued.conn.connected
                or self._connections.get(user_id) is not queued.conn
            ):
                self._queue.pop(user_id, None)

    async def _fetch_user_rating(self, user_id: str) -> Optional[int]:
        async with self._session_factory() as session:
            return await session.scalar(
                select(User.rating).where(
                    User.id == user_id, User.is_active.is_(True)
                )
            )

    def _room_state_payload(self, room: Room, user_id: str) -> dict:
        color = room.color_for(user_id)
        if color is None:
            raise RuntimeError("用户不属于该房间")
        opponent_id = room.opponent_id(user_id)
        last_move = room.game.last_move
        status = room.status
        return {
            "game_id": room.game_id,
            "board": [
                [
                    None if cell is None else _color_text(cell)
                    for cell in row
                ]
                for row in room.game.board
            ],
            "current_player": _color_text(room.game.current_player),
            "your_color": _color_text(color),
            "move_no": room.game.move_count,
            "status": status,
            "last_move": (
                None
                if last_move is None
                else {
                    "row": last_move.row,
                    "col": last_move.col,
                    "player": _color_text(last_move.player),
                }
            ),
            "winning_line": [list(cell) for cell in room.game.winning_line],
            "room_code": "" if room.matchmade else room.code,
            "black_name": room.black_name,
            "white_name": room.white_name,
            "turn_deadline_ms": room.turn_deadline_ms,
            "server_now_ms": _now_ms(),
            "move_timeout_seconds": room.turn_seconds,
            "opponent_online": (
                room.opponent_id(user_id) in room.connections
                if room.opponent_id(user_id) else False
            ),
            "rated": room.rated,
            "your_rating_before": room.rating_before.get(user_id),
            "your_rating_after": room.rating_after.get(user_id),
            "opponent_rating_before": room.rating_before.get(opponent_id),
            "opponent_rating_after": room.rating_after.get(opponent_id),
            "rating_delta": room.rating_delta.get(user_id),
        }

    async def _send_game_state(
        self,
        room: Room,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        try:
            payload = self._room_state_payload(room, conn.user_id)
        except RuntimeError:
            return
        await conn.send(
            make_message(SERVER_GAME_STATE, payload, request_id=request_id)
        )

    async def _broadcast_game_state(
        self,
        room: Room,
        request_user_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        for user_id, conn in list(room.connections.items()):
            await self._send_game_state(
                room,
                conn,
                request_id if user_id == request_user_id else None,
            )

    async def _send_rematch_status(self, room: Room) -> None:
        for user_id, conn in list(room.connections.items()):
            opponent = room.opponent_id(user_id)
            await conn.send(
                make_message(
                    SERVER_REMATCH_STATUS,
                    {
                        "your_requested": user_id in room.rematch_votes,
                        "opponent_requested": (
                            opponent in room.rematch_votes if opponent else False
                        ),
                    },
                )
            )

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    async def attach(self, conn: ClientConnection) -> None:
        """认证后的连接注册；若用户在房间中则重连并发送权威快照。"""
        old_conn: Optional[ClientConnection] = None
        queued: Optional[QueuedPlayer] = None
        async with self._state_lock:
            old_conn = self._connections.get(conn.user_id)
            self._connections[conn.user_id] = conn
            queued = self._queue.get(conn.user_id)
            if queued is not None:
                queued.conn = conn
        if old_conn is not None and old_conn is not conn:
            await old_conn.close(4001, "账号已在另一处连接")

        room = self._room_by_user.get(conn.user_id)
        if room is None:
            if queued is not None:
                await self._send_queue_status(conn)
            return

        async with room.lock:
            if conn.user_id not in room.participant_ids():
                return
            room.connections[conn.user_id] = conn
            if room.status == "waiting":
                if conn.user_id == room.black_user_id:
                    await self._send_room_created(conn, room)
            else:
                await self._send_game_state(room, conn)
                opponent = room.opponent_id(conn.user_id)
                if opponent:
                    await self._send_opponent_status(
                        room, opponent, True, conn.username
                    )

    async def detach(self, conn: ClientConnection) -> None:
        """连接断开；旧连接被新连接替换时不做任何房间变更。"""
        async with self._state_lock:
            if self._connections.get(conn.user_id) is not conn:
                return
            self._connections.pop(conn.user_id, None)
            self._queue.pop(conn.user_id, None)

        room = self._room_by_user.get(conn.user_id)
        if room is None:
            return

        current_task = asyncio.current_task()
        cleanup = False
        room_closed_reason = ""
        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            room.connections.pop(conn.user_id, None)
            room.rematch_votes.discard(conn.user_id)

            if room.status == "waiting":
                # 等待中的房主断线：房间没有对手，直接作废。
                cleanup = True
                room_closed_reason = "房主已离开，房间已关闭"
                await self._mark_waiting_game_abandoned(room, "host_disconnected")
            else:
                opponent = room.opponent_id(conn.user_id)
                if opponent and opponent in room.connections:
                    await self._send_opponent_status(
                        room, opponent, False, conn.username
                    )
                if room.status != "playing" and not room.connections:
                    cleanup = True

        if cleanup:
            if room_closed_reason:
                await self._send_room_closed(room, room_closed_reason)
            self._remove_room_locked(room)

    # ------------------------------------------------------------------
    # 房间创建/加入/离开
    # ------------------------------------------------------------------
    def _new_room_code(self) -> str:
        for _ in range(50):
            code = "".join(
                secrets.choice(_ROOM_ALPHABET)
                for _ in range(self._settings.room_code_length)
            )
            if code not in self._rooms_by_code:
                return code
        raise RuntimeError("无法生成唯一房间码")

    async def _create_game_row(
        self,
        room_code: str,
        black_id: str,
        white_id: Optional[str],
        status: str,
        started: bool,
        rated: bool = False,
    ) -> Game:
        game_row = Game(
            id=new_id(),
            room_code=room_code,
            black_user_id=black_id,
            white_user_id=white_id,
            status=status,
            turn_seconds=self._settings.turn_seconds,
            rated=rated,
            started_at=utcnow_naive() if started else None,
        )
        async with self._session_factory() as session:
            session.add(game_row)
            await session.commit()
        return game_row

    async def create_room(
        self, conn: ClientConnection, request_id: Optional[str] = None
    ) -> None:
        if not self._is_current_connection(conn):
            return
        room: Optional[Room] = None
        error: Optional[str] = None
        async with self._state_lock:
            self._queue.pop(conn.user_id, None)
            if conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            else:
                code = self._new_room_code()
                try:
                    game_row = await self._create_game_row(
                        code, conn.user_id, None, "waiting", False
                    )
                except Exception:
                    logger.exception("创建房间数据库写入失败")
                    error = "服务器无法创建房间"
                else:
                    room = Room(
                        code=code,
                        game_id=game_row.id,
                        black_user_id=conn.user_id,
                        white_user_id=None,
                        black_name=conn.username,
                        white_name="",
                        turn_seconds=self._settings.turn_seconds,
                        connections={conn.user_id: conn},
                    )
                    self._rooms_by_code[code] = room
                    self._room_by_user[conn.user_id] = room
        if error:
            await self._send_error(conn, error, request_id)
        elif room is not None:
            await self._send_room_created(conn, room)

    async def join_room(
        self,
        conn: ClientConnection,
        room_code: str,
        request_id: Optional[str] = None,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        code = room_code.strip().upper()
        error: Optional[str] = None
        joined_room: Optional[Room] = None

        async with self._state_lock:
            self._queue.pop(conn.user_id, None)
            if conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            else:
                room = self._rooms_by_code.get(code)
                if room is None:
                    error = "房间不存在或已关闭"
                else:
                    async with room.lock:
                        if room.matchmade:
                            error = "排位对局不能通过房间码加入"
                        elif (
                            room.status != "waiting"
                            or room.white_user_id is not None
                        ):
                            error = "房间已经开始或已满"
                        elif room.black_user_id == conn.user_id:
                            error = "不能加入自己创建的房间"
                        elif room.black_user_id not in room.connections:
                            error = "房主已离线，房间已关闭"
                            self._remove_room_locked(room)
                            await self._mark_waiting_game_abandoned(
                                room, "host_offline"
                            )
                        else:
                            try:
                                async with self._session_factory() as session:
                                    game_row = await session.get(
                                        Game, room.game_id
                                    )
                                    if game_row is None:
                                        raise RuntimeError("game row missing")
                                    game_row.white_user_id = conn.user_id
                                    game_row.status = "playing"
                                    game_row.started_at = utcnow_naive()
                                    await session.commit()
                            except Exception:
                                logger.exception("加入房间数据库写入失败")
                                error = "服务器无法开始对局"
                            else:
                                room.white_user_id = conn.user_id
                                room.white_name = conn.username
                                room.status = "playing"
                                room.connections[conn.user_id] = conn
                                self._room_by_user[conn.user_id] = room
                                self._start_timeout_locked(room)
                                joined_room = room

        if error:
            await self._send_error(conn, error, request_id)
        elif joined_room is not None:
            async with joined_room.lock:
                await self._broadcast_game_state(joined_room)

    # ------------------------------------------------------------------
    # 公开排位匹配
    # ------------------------------------------------------------------
    async def quick_match(
        self, conn: ClientConnection, request_id: Optional[str] = None
    ) -> None:
        """加入排位队列；队列中有第二名玩家时立即开一局积分赛。"""
        if not self._is_current_connection(conn):
            return
        if conn.user_id in self._room_by_user:
            await self._send_error(conn, "你已经在房间中", request_id)
            return

        room: Optional[Room] = None
        outgoing: list[tuple[ClientConnection, dict]] = []
        error: Optional[str] = None

        async with self._state_lock:
            self._prune_queue_locked()
            if conn.user_id in self._queue:
                self._queue[conn.user_id].conn = conn
                if len(self._queue) >= 2:
                    # 上一次配对写库失败时允许玩家重试入队命令触发配对。
                    room, outgoing, error = (
                        await self._match_queued_locked(conn)
                    )
                else:
                    outgoing.append(
                        (
                            conn,
                            make_message(
                                SERVER_QUEUE_STATUS,
                                self._queue_state_payload(conn.user_id),
                            ),
                        )
                    )
            else:
                rating = await self._fetch_user_rating(conn.user_id)
                if rating is None:
                    error = "账号不存在或已停用"
                else:
                    self._queue[conn.user_id] = QueuedPlayer(
                        conn=conn,
                        rating=rating,
                        queued_at=time.monotonic(),
                    )
                    if len(self._queue) >= 2:
                        room, outgoing, error = (
                            await self._match_queued_locked(conn)
                        )
                    else:
                        outgoing.append(
                            (
                                conn,
                                make_message(
                                    SERVER_QUEUE_STATUS,
                                    self._queue_state_payload(conn.user_id),
                                ),
                            )
                        )

        if error:
            await self._send_error(conn, error, request_id)
        for target, message in outgoing:
            await target.send(message)
        if room is not None:
            await self._broadcast_game_state(room)

    async def _match_queued_locked(
        self, conn: ClientConnection
    ) -> tuple[
        Optional[Room],
        list[tuple[ClientConnection, dict]],
        Optional[str],
    ]:
        """为入队玩家选择积分最接近的对手并创建排位房。

        调用方必须持有 ``_state_lock``。写库失败时本次入队的玩家退出队列
        并收到错误，其他等待者保留；避免客户端认为未入队而服务器仍在队列。
        """
        queued = self._queue.get(conn.user_id)
        if queued is None:
            return None, [], "你不在匹配队列中"
        candidates = [
            player
            for user_id, player in self._queue.items()
            if user_id != conn.user_id
        ]
        if not candidates:
            return (
                None,
                [
                    (
                        conn,
                        make_message(
                            SERVER_QUEUE_STATUS,
                            self._queue_state_payload(conn.user_id),
                        ),
                    )
                ],
                None,
            )

        opponent = min(
            candidates, key=lambda player: abs(player.rating - queued.rating)
        )
        if secrets.randbelow(2) == 0:
            black, white = queued, opponent
        else:
            black, white = opponent, queued

        code = self._new_room_code()
        try:
            game_row = await self._create_game_row(
                code,
                black.conn.user_id,
                white.conn.user_id,
                "playing",
                True,
                rated=True,
            )
        except Exception:
            logger.exception("创建排位对局数据库写入失败")
            self._queue.pop(conn.user_id, None)
            return None, [], "服务器无法创建排位对局，请稍后重试"

        self._queue.pop(black.conn.user_id, None)
        self._queue.pop(white.conn.user_id, None)
        room = Room(
            code=code,
            game_id=game_row.id,
            black_user_id=black.conn.user_id,
            white_user_id=white.conn.user_id,
            black_name=black.conn.username,
            white_name=white.conn.username,
            turn_seconds=self._settings.turn_seconds,
            status="playing",
            connections={
                black.conn.user_id: black.conn,
                white.conn.user_id: white.conn,
            },
            rated=True,
            matchmade=True,
            rating_before={
                black.conn.user_id: black.rating,
                white.conn.user_id: white.rating,
            },
        )
        self._rooms_by_code[code] = room
        self._room_by_user[black.conn.user_id] = room
        self._room_by_user[white.conn.user_id] = room
        self._start_timeout_locked(room)

        outgoing: list[tuple[ClientConnection, dict]] = []
        for player, rival in ((black, white), (white, black)):
            outgoing.append(
                (
                    player.conn,
                    make_message(
                        SERVER_QUEUE_STATUS,
                        self._queue_state_payload(player.conn.user_id),
                    ),
                )
            )
            outgoing.append(
                (
                    player.conn,
                    make_message(
                        SERVER_MATCH_FOUND,
                        self._match_found_payload(
                            room,
                            player.conn.user_id,
                            rival.conn.user_id,
                        ),
                    ),
                )
            )
        return room, outgoing, None

    async def cancel_match(
        self, conn: ClientConnection, request_id: Optional[str] = None
    ) -> None:
        """退出匹配队列；不在队列中时也返回 idle，供客户端恢复状态。"""
        if not self._is_current_connection(conn):
            return
        async with self._state_lock:
            self._queue.pop(conn.user_id, None)
            payload = self._queue_state_payload(conn.user_id)
        await conn.send(make_message(SERVER_QUEUE_STATUS, payload))

    async def leave_room(self, conn: ClientConnection) -> None:
        """等待中或终局后可离开；进行中的对局请使用认输。"""
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            return

        error: Optional[str] = None
        message = make_message(SERVER_ROOM_CLOSED, {"reason": "对局房间已关闭"})
        async with room.lock:
            if room.status == "playing":
                error = "进行中的对局请先认输"
            else:
                if room.status == "waiting":
                    await self._mark_waiting_game_abandoned(room, "left")
                for other in list(room.connections.values()):
                    await other.send(message)
                self._remove_room_locked(room)

        if error:
            await self._send_error(conn, error)

    # ------------------------------------------------------------------
    # 落子/认输/再战
    # ------------------------------------------------------------------
    async def move(
        self,
        conn: ClientConnection,
        request_id: Optional[str],
        row: int,
        col: int,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        if not request_id or len(request_id) > 64:
            await self._send_error(conn, "落子请求缺少有效的 request_id")
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._send_error(conn, "你不在房间中")
            return

        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            if room.status != "playing" or room.game.is_game_over:
                await self._send_error(conn, "当前对局已结束")
                return
            color = room.color_for(conn.user_id)
            if color is None:
                await self._send_error(conn, "你不是本局玩家")
                return
            if room.game.current_player is not color:
                await self._send_move_rejected(conn, request_id, "还没有轮到你")
                return
            if not (
                isinstance(row, int)
                and isinstance(col, int)
                and 0 <= row < BOARD_SIZE
                and 0 <= col < BOARD_SIZE
            ):
                await self._send_move_rejected(conn, request_id, "落子坐标非法")
                return
            if room.game.board[row][col] is not None:
                await self._send_move_rejected(conn, request_id, "该位置已有棋子")
                return

            # 客户端重试同一 move_id：返回当前权威状态，不重复落子。
            if request_id in room.processed_moves:
                await self._send_game_state(room, conn, request_id=request_id)
                return

            if not room.game.place_stone(row, col):
                await self._send_move_rejected(conn, request_id, "落子不合法")
                return

            new_move_no = room.game.move_count
            status = _status_for_game(room.game)
            winner_id: Optional[str] = None
            if room.game.winner is Player.BLACK:
                winner_id = room.black_user_id
            elif room.game.winner is Player.WHITE:
                winner_id = room.white_user_id

            try:
                async with self._session_factory() as session:
                    game_row = await session.get(Game, room.game_id)
                    if game_row is None:
                        raise RuntimeError("game row missing")
                    session.add(
                        Move(
                            game_id=room.game_id,
                            move_no=new_move_no,
                            user_id=conn.user_id,
                            color=_color_text(color),
                            row=row,
                            col=col,
                            client_move_id=request_id,
                        )
                    )
                    game_row.move_no = new_move_no
                    game_row.status = status
                    game_row.winner_user_id = winner_id
                    if status != "playing":
                        game_row.result_reason = (
                            "five" if winner_id is not None else "draw"
                        )
                        game_row.ended_at = utcnow_naive()
                    await session.commit()
            except Exception:
                logger.exception("落子数据库写入失败，撤销内存落子")
                room.game.undo()
                await self._send_error(conn, "服务器保存落子失败，请重试")
                return

            room.processed_moves[request_id] = new_move_no
            room.status = status
            room.winner_user_id = winner_id
            room.result_reason = None
            self._cancel_timeout_locked(room)

            if status == "playing":
                self._start_timeout_locked(room)
            else:
                room.result_reason = (
                    "five" if winner_id is not None else "draw"
                )
                room.turn_deadline_ms = None
                await self._settle_rated_game_locked(room)

            await self._broadcast_game_state(
                room,
                request_user_id=conn.user_id,
                request_id=request_id,
            )
            if room.status != "playing" and not room.connections:
                self._remove_room_locked(room)

    async def _send_move_rejected(
        self,
        conn: ClientConnection,
        request_id: str,
        reason: str,
    ) -> None:
        await conn.send(
            make_message(
                SERVER_MOVE_REJECTED,
                {"reason": reason},
                request_id=request_id,
            )
        )

    async def resign(self, conn: ClientConnection) -> None:
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            return
        async with room.lock:
            if room.status != "playing":
                await self._send_error(conn, "当前没有进行中的对局")
                return
            opponent = room.opponent_id(conn.user_id)
            if opponent is None:
                return
            await self._end_game_locked(room, opponent, "resign")

    async def rematch(self, conn: ClientConnection) -> None:
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            return
        error: Optional[str] = None
        async with room.lock:
            if room.rated:
                error = "排位赛不支持再战，请重新匹配"
            elif room.status == "playing" or room.status == "waiting":
                error = "当前对局还没有结束"
            elif conn.user_id not in room.connections:
                error = "请先重新连接到房间"
            else:
                room.rematch_votes.add(conn.user_id)
                participants = set(room.participant_ids())
                if participants and participants.issubset(room.rematch_votes):
                    if not all(uid in room.connections for uid in participants):
                        await self._send_rematch_status(room)
                    else:
                        try:
                            new_game = await self._create_game_row(
                                room.code,
                                room.white_user_id,  # 交换黑白
                                room.black_user_id,
                                "playing",
                                True,
                            )
                        except Exception:
                            logger.exception("再战新对局写入失败")
                            error = "服务器无法开始下一局"
                        else:
                            old_black = room.black_user_id
                            old_white = room.white_user_id
                            old_black_name = room.black_name
                            old_white_name = room.white_name
                            room.game_id = new_game.id
                            room.black_user_id = old_white
                            room.white_user_id = old_black
                            room.black_name = old_white_name
                            room.white_name = old_black_name
                            room.game = GameModel()
                            room.status = "playing"
                            room.winner_user_id = None
                            room.result_reason = None
                            room.rematch_votes.clear()
                            room.processed_moves.clear()
                            self._start_timeout_locked(room)
                            await self._broadcast_game_state(room)
                else:
                    await self._send_rematch_status(room)

        if error:
            await self._send_error(conn, error)

    # ------------------------------------------------------------------
    # 时钟
    # ------------------------------------------------------------------
    def _cancel_timeout_locked(self, room: Room) -> None:
        task = room.timeout_task
        room.timeout_task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def _start_timeout_locked(self, room: Room) -> None:
        self._cancel_timeout_locked(room)
        if room.status != "playing":
            room.turn_deadline_ms = None
            return
        room.turn_token += 1
        token = room.turn_token
        seconds = room.turn_seconds
        room.turn_deadline_ms = _now_ms() + seconds * 1000
        room.timeout_task = asyncio.create_task(
            self._timeout_after(room, token, seconds)
        )

    async def _timeout_after(self, room: Room, token: int, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            async with room.lock:
                if room.status != "playing" or room.turn_token != token:
                    return
                loser_id = None
                if room.game.current_player is Player.BLACK:
                    loser_id = room.black_user_id
                elif room.game.current_player is Player.WHITE:
                    loser_id = room.white_user_id
                if loser_id is None:
                    return
                winner_id = room.opponent_id(loser_id)
                if winner_id is None:
                    return
                await self._end_game_locked(room, winner_id, "timeout")
        except asyncio.CancelledError:
            return

    async def _end_game_locked(
        self,
        room: Room,
        winner_user_id: str,
        reason: str,
    ) -> None:
        color = room.color_for(winner_user_id)
        if color is None:
            return
        room.status = "black_won" if color is Player.BLACK else "white_won"
        room.winner_user_id = winner_user_id
        room.result_reason = reason
        room.turn_deadline_ms = None
        room.rematch_votes.clear()
        self._cancel_timeout_locked(room)

        try:
            async with self._session_factory() as session:
                game_row = await session.get(Game, room.game_id)
                if game_row is not None:
                    game_row.status = room.status
                    game_row.winner_user_id = winner_user_id
                    game_row.result_reason = reason
                    game_row.ended_at = utcnow_naive()
                    game_row.move_no = room.game.move_count
                    await session.commit()
        except Exception:
            logger.exception("终局结果数据库写入失败")

        await self._settle_rated_game_locked(room)
        await self._broadcast_game_state(room)
        if not room.connections:
            self._remove_room_locked(room)

    async def _settle_rated_game_locked(self, room: Room) -> None:
        """结算排位积分；同一局只应用一次，好友房直接跳过。

        调用方持有 ``room.lock``。积分与 RatingEvent、Game.rating_applied
        在同一数据库事务中提交，失败则保留未结算状态并记录日志。
        """
        if not room.rated or room.rating_applied:
            return
        black_id = room.black_user_id
        white_id = room.white_user_id
        if not black_id or not white_id:
            return
        if room.winner_user_id is None:
            score_black = 0.5
        elif room.winner_user_id == black_id:
            score_black = 1.0
        elif room.winner_user_id == white_id:
            score_black = 0.0
        else:
            return

        try:
            async with self._session_factory() as session:
                users = {
                    user.id: user
                    for user in (
                        await session.scalars(
                            select(User).where(
                                User.id.in_((black_id, white_id))
                            )
                        )
                    ).all()
                }
                black_user = users.get(black_id)
                white_user = users.get(white_id)
                game_row = await session.get(Game, room.game_id)
                if (
                    black_user is None
                    or white_user is None
                    or game_row is None
                ):
                    logger.warning(
                        "排位积分结算缺少玩家或对局 room=%s", room.code
                    )
                    return
                if game_row.rating_applied:
                    room.rating_applied = True
                    return

                black_change, white_change = calculate_pair(
                    black_user.rating,
                    black_user.rated_games,
                    white_user.rating,
                    white_user.rated_games,
                    score_black,
                )
                black_user.rating = black_change.after
                white_user.rating = white_change.after
                black_user.peak_rating = max(
                    black_user.peak_rating, black_change.after
                )
                white_user.peak_rating = max(
                    white_user.peak_rating, white_change.after
                )
                black_user.rated_games += 1
                white_user.rated_games += 1

                if score_black == 1.0:
                    black_user.wins += 1
                    white_user.losses += 1
                    black_score, white_score = "win", "loss"
                elif score_black == 0.0:
                    white_user.wins += 1
                    black_user.losses += 1
                    black_score, white_score = "loss", "win"
                else:
                    black_user.draws += 1
                    white_user.draws += 1
                    black_score = white_score = "draw"

                session.add_all(
                    [
                        RatingEvent(
                            game_id=room.game_id,
                            user_id=black_id,
                            opponent_id=white_id,
                            score=black_score,
                            rating_before=black_change.before,
                            rating_after=black_change.after,
                            delta=black_change.delta,
                        ),
                        RatingEvent(
                            game_id=room.game_id,
                            user_id=white_id,
                            opponent_id=black_id,
                            score=white_score,
                            rating_before=white_change.before,
                            rating_after=white_change.after,
                            delta=white_change.delta,
                        ),
                    ]
                )
                game_row.rated = True
                game_row.rating_applied = True
                game_row.status = room.status
                game_row.winner_user_id = room.winner_user_id
                game_row.result_reason = room.result_reason or (
                    "five" if room.winner_user_id else "draw"
                )
                game_row.ended_at = game_row.ended_at or utcnow_naive()
                game_row.move_no = room.game.move_count
                await session.commit()
        except Exception:
            logger.exception("排位积分结算失败 room=%s", room.code)
            return

        room.rating_before = {
            black_id: black_change.before,
            white_id: white_change.before,
        }
        room.rating_after = {
            black_id: black_change.after,
            white_id: white_change.after,
        }
        room.rating_delta = {
            black_id: black_change.delta,
            white_id: white_change.delta,
        }
        room.rating_applied = True

    # ------------------------------------------------------------------
    # 持久化清理/关闭
    # ------------------------------------------------------------------
    async def _mark_waiting_game_abandoned(
        self, room: Room, reason: str
    ) -> None:
        if room.status != "waiting":
            return
        try:
            async with self._session_factory() as session:
                game_row = await session.get(Game, room.game_id)
                if game_row is not None and game_row.status == "waiting":
                    game_row.status = "abandoned"
                    game_row.result_reason = reason
                    game_row.ended_at = utcnow_naive()
                    await session.commit()
        except Exception:
            logger.exception("作废等待房间数据库写入失败")

    def _remove_room_locked(self, room: Room) -> None:
        self._cancel_timeout_locked(room)
        self._rooms_by_code.pop(room.code, None)
        for uid in room.participant_ids():
            if self._room_by_user.get(uid) is room:
                self._room_by_user.pop(uid, None)
        room.connections.clear()

    async def shutdown(self) -> None:
        """停止时作废未完成对局并关闭连接，避免数据库残留 playing。"""
        rooms = list(self._rooms_by_code.values())
        for room in rooms:
            self._cancel_timeout_locked(room)
        active_game_ids = [
            room.game_id
            for room in rooms
            if room.status in ("waiting", "playing")
        ]
        if active_game_ids:
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(Game)
                        .where(
                            Game.id.in_(active_game_ids),
                            Game.status.in_(("waiting", "playing")),
                        )
                        .values(
                            status="abandoned",
                            result_reason="server_shutdown",
                            ended_at=utcnow_naive(),
                        )
                    )
                    await session.commit()
            except Exception:
                logger.exception("服务关闭时作废对局失败")
        for conn in list(self._connections.values()):
            await conn.close(1001, "服务器正在关闭")
        self._rooms_by_code.clear()
        self._room_by_user.clear()
        self._connections.clear()
        self._queue.clear()


__all__ = ["ClientConnection", "Room", "RoomManager"]
