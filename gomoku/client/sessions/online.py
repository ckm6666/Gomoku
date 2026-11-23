"""联机会话：登录后的好友房、服务器确认落子与断线状态。

Stage 1 已接入真实 WebSocket 协议（登录/建房/加入/落子/认输/再战），但
仍坚持以下约束：
* 客户端不先落子，必须等服务器权威快照；
* 同一时刻最多一个在途落子；
* 服务器拒绝/断线/换房时清空 pending，避免 UI 永久卡住；
* 本地模型不参与联机胜负判定。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Mapping, Optional

from PySide6.QtCore import QObject, Signal

from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.client.sessions.online_state import OnlineSessionPhase
from gomoku.client.transport import Transport
from gomoku.shared.achievements import normalize_achievement_keys
from gomoku.shared.chat import (
    CHAT_INTERVAL_SECONDS,
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    build_chat_payload,
    parse_chat_message,
)
from gomoku.shared.capabilities import (
    CLIENT_CAPABILITIES,
    normalize_capabilities,
)
from gomoku.shared.errors import ProtocolError, TransportError
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_CHAT,
    CLIENT_CREATE_ROOM,
    CLIENT_INVITE_TO_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_LEAVE_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_PING,
    CLIENT_QUICK_MATCH,
    CLIENT_REMATCH,
    CLIENT_RESPOND_ROOM_INVITE,
    CLIENT_RESIGN,
    CLIENT_UNDO_REQUEST,
    CLIENT_UNDO_RESPONSE,
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_CHAT,
    SERVER_DIRECT_MESSAGE,
    SERVER_ERROR,
    SERVER_FRIEND_EVENT,
    SERVER_GAME_STATE,
    SERVER_LIVE_GAMES_CHANGED,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_MESSAGE_TYPES,
    SERVER_OPPONENT_STATUS,
    SERVER_PONG,
    SERVER_QUEUE_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_UNDO_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_ROOM_INVITATION,
    SERVER_ROOM_INVITE_RESULT,
    GameSnapshot,
    make_message,
    make_move_intent,
    parse_game_state,
    parse_message,
)
from gomoku.shared.rules import BOARD_SIZE, Cell, Player
from gomoku.shared.social import (
    SocialError,
    parse_direct_message,
    parse_friend_event,
)

logger = logging.getLogger(__name__)

#: 已发出的发言请求在本地保留多久，等待服务器的回执或过期淘汰。
CHAT_REQUEST_TTL_SECONDS = 30.0

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


def _empty_view() -> BoardView:
    return BoardView(
        board=_EMPTY_BOARD,
        current_player=Player.BLACK,
        last_move=None,
        winner=None,
        winning_line=(),
        is_draw=False,
        is_game_over=False,
        move_count=0,
    )


def _view_from_snapshot(snapshot: GameSnapshot) -> BoardView:
    return BoardView(
        board=snapshot.board,
        current_player=snapshot.current_player,
        last_move=snapshot.last_move,
        winner=snapshot.winner,
        winning_line=snapshot.winning_line,
        is_draw=snapshot.is_draw,
        is_game_over=snapshot.is_game_over,
        move_count=snapshot.move_no,
    )


class OnlineGameSession(GameSession):
    """好友房联机会话；网络传输由外部注入，便于 fake 测试。"""

    authenticated = Signal(object)  # 服务端返回的用户信息 dict
    room_created = Signal(str)  # 房间码
    opponent_status_changed = Signal(bool, str)  # online, username
    room_closed = Signal(str)
    rematch_status_changed = Signal(bool, bool)  # yours, opponent
    # 对手发来悔棋请求；参数为发起者昵称。
    undo_request_received = Signal(str)
    # 悔棋状态变化：state(requested/accepted/declined/expired/cancelled), reason。
    undo_status_changed = Signal(str, str)
    queue_status_changed = Signal(bool, int)  # queued, queue_size
    matched = Signal(str, str)  # game_id, opponent_name
    chat_received = Signal(object)  # ChatMessage
    chat_failed = Signal(str)  # 发言被拒/发送失败的中文提示
    room_invited = Signal(object)  # 收到自建房邀请 payload
    room_invite_result = Signal(object)  # 邀请发送/接受/拒绝结果
    friend_event = Signal(object)  # FriendEvent
    direct_message = Signal(object)  # DirectMessage
    # 进行中的对局列表发生变化（有人开局或终局）。
    live_games_changed = Signal()

    def __init__(
        self,
        transport: Transport,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._view = _empty_view()
        self._snapshot: Optional[GameSnapshot] = None
        self._authenticated = False
        self._server_capabilities: frozenset[str] = frozenset()
        self._user_id = ""
        self._username = ""
        self._room_code = ""
        self._waiting = False
        self._your_color: Optional[Player] = None
        self._game_id: Optional[str] = None
        # 是否以旁观者身份观战：观战者没有执子颜色，也不能操作棋局。
        self._spectating: bool = False
        self._active = False
        self._paused = False
        self._closed = False
        self._disconnected = False
        self._pending_move_id: Optional[str] = None
        self._last_move_no = 0
        self._opponent_online = False
        self._opponent_username = ""
        self._rematch_yours = False
        self._rematch_opponent = False
        self._undo_requester_id = ""
        self._undo_requester_name = ""
        self._pending_undo_request_id: Optional[str] = None
        self._undo_remaining = 0
        self._undo_limit = 0
        self._undo_cooldown_seconds = 0
        self._undo_cooldown_deadline: Optional[float] = None
        self._last_state_version = 0
        self._queued = False
        self._queue_size = 0
        self._rated = False
        self._your_rating_before: Optional[int] = None
        self._your_rating_after: Optional[int] = None
        self._opponent_rating_before: Optional[int] = None
        self._opponent_rating_after: Optional[int] = None
        self._rating_delta: Optional[int] = None
        self._last_chat_at: Optional[float] = None
        # 已发出但尚未收到服务器回执的发言请求，用于把错误精确归位。
        # 发言成功时服务器只广播消息、不回执 request_id，所以这里必须
        # 有界，否则长时间对局会一直累积。
        self._pending_chat_ids: dict[str, float] = {}

        transport.message_received.connect(self._on_transport_message)
        transport.disconnected.connect(self._on_transport_disconnected)
        transport.error.connect(self._on_transport_error)

    # ------------------------------------------------------------------
    # 认证/大厅状态
    # ------------------------------------------------------------------
    @property
    def is_authenticated(self) -> bool:
        return self._authenticated

    @property
    def is_connected(self) -> bool:
        return (
            self._authenticated
            and not self._disconnected
            and not self._closed
        )

    @property
    def phase(self) -> OnlineSessionPhase:
        """当前界面阶段；连接断开由 :attr:`is_connected` 单独表达。"""
        if self._closed:
            return OnlineSessionPhase.CLOSED
        if not self._authenticated:
            return OnlineSessionPhase.CONNECTING
        if self._spectating:
            return OnlineSessionPhase.SPECTATING
        if self._active:
            return OnlineSessionPhase.PLAYING
        if self._waiting:
            return OnlineSessionPhase.WAITING
        if self._queued:
            return OnlineSessionPhase.QUEUED
        return OnlineSessionPhase.LOBBY

    @property
    def server_capabilities(self) -> frozenset[str]:
        return self._server_capabilities

    def supports(self, capability: str) -> bool:
        return capability in self._server_capabilities

    @property
    def user_id(self) -> str:
        return self._user_id

    @property
    def username(self) -> str:
        return self._username

    @property
    def room_code(self) -> str:
        return self._room_code

    @property
    def waiting_for_opponent(self) -> bool:
        return self._waiting

    @property
    def in_room(self) -> bool:
        """是否已在等待房间或进行中的对局里。"""
        return self._waiting or self._active

    @property
    def snapshot(self) -> Optional[GameSnapshot]:
        return self._snapshot

    @property
    def opponent_online(self) -> bool:
        return self._opponent_online

    @property
    def is_spectator(self) -> bool:
        """当前是否只是旁观者（观战），而不是对局中的玩家。"""
        return self._spectating

    @property
    def opponent_username(self) -> str:
        """对局中的另一方昵称。

        旁观者不是任何一方，不能把其中一人说成自己的对手，因此观战时
        返回两名玩家的昵称。
        """
        if self._opponent_username:
            return self._opponent_username
        if self._snapshot is None:
            return "对手"
        if self._spectating:
            names = [
                name
                for name in (
                    self._snapshot.black_name,
                    self._snapshot.white_name,
                )
                if name
            ]
            return "、".join(names) if names else "观战双方"
        if self._your_color is None:
            return "对手"
        return (
            self._snapshot.white_name
            if self._your_color is Player.BLACK
            else self._snapshot.black_name
        ) or "对手"

    @property
    def your_username(self) -> str:
        """自己的昵称；旁观者返回旁观者自己的昵称。"""
        if self._snapshot is None or self._your_color is None:
            return self._username
        return (
            self._snapshot.black_name
            if self._your_color is Player.BLACK
            else self._snapshot.white_name
        ) or self._username

    @property
    def rematch_state(self) -> tuple[bool, bool]:
        return self._rematch_yours, self._rematch_opponent

    @property
    def undo_pending(self) -> bool:
        """是否存在等待回应的悔棋请求。"""
        return bool(self._undo_requester_id)

    @property
    def undo_requested_by_you(self) -> bool:
        return bool(
            self._undo_requester_id
            and self._undo_requester_id == self._user_id
        )

    @property
    def undo_requester_name(self) -> str:
        return self._undo_requester_name

    @property
    def undo_remaining(self) -> int:
        return max(0, self._undo_remaining)

    @property
    def undo_limit(self) -> int:
        return max(0, self._undo_limit)

    @property
    def undo_cooldown_seconds(self) -> int:
        return max(0, self._undo_cooldown_seconds)

    @property
    def undo_cooldown_remaining(self) -> int:
        """距离下次可请求悔棋的剩余秒数（向上取整）。"""
        deadline = self._undo_cooldown_deadline
        if deadline is None:
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 0
        return int(remaining + 0.999)

    @property
    def queued(self) -> bool:
        return self._queued

    @property
    def queue_size(self) -> int:
        return self._queue_size

    @property
    def rated(self) -> bool:
        return self._rated

    @property
    def your_rating(self) -> Optional[int]:
        return self._your_rating_before

    @property
    def opponent_rating(self) -> Optional[int]:
        return self._opponent_rating_before

    @property
    def rating_delta(self) -> Optional[int]:
        return self._rating_delta

    @property
    def your_rating_after(self) -> Optional[int]:
        return self._your_rating_after

    @property
    def chat_available(self) -> bool:
        """聊天输入是否可用（已认证、连接正常、已经进入对局）。

        这里**不**包含发言冷却：冷却只用来限制发送频率，如果拿它去
        禁用输入框，Qt 会把焦点从被禁用的控件上摘走，用户每发一条
        消息都要重新点回输入框。
        """
        if self._closed or not self._authenticated:
            return False
        if self._spectating:
            # 观战者只是旁观，不参与对局内的发言。
            return False
        if self._snapshot is None:
            # 还没进入对局（等待对手）时不可发言。
            return False
        return not self._disconnected

    @property
    def can_send_chat(self) -> bool:
        """当前是否可以真的发出这条消息（chat_available 且冷却已过）。"""
        if not self.chat_available:
            return False
        now = time.monotonic()
        if (
            self._last_chat_at is not None
            and now - self._last_chat_at < CHAT_INTERVAL_SECONDS
        ):
            return False
        return True

    # ------------------------------------------------------------------
    # GameSession 只读接口
    # ------------------------------------------------------------------
    @property
    def view(self) -> BoardView:
        return self._view

    @property
    def human_player(self) -> Optional[Player]:
        return self._your_color

    @property
    def busy(self) -> bool:
        return self._pending_move_id is not None

    @property
    def accepts_human_input(self) -> bool:
        return (
            self._authenticated
            and self._active
            and not self._closed
            and not self._paused
            and not self._disconnected
            and not self._view.is_game_over
            and not self._spectating
            and self._your_color is not None
            and self._view.current_player is self._your_color
            and self._pending_move_id is None
            and self._pending_undo_request_id is None
            and not self._undo_requester_id
        )

    @property
    def can_undo(self) -> bool:
        """当前是否可以请求悔棋（自己的回合、已有落子且对手在线）。"""
        return (
            self.accepts_human_input
            and self._opponent_online
            and self._has_your_stone()
            and self._undo_limit > 0
            and self._undo_remaining > 0
            and self.undo_cooldown_remaining == 0
        )

    def _has_your_stone(self) -> bool:
        color = self._your_color
        if color is None:
            return False
        return any(cell is color for row in self._view.board for cell in row)

    # ------------------------------------------------------------------
    # 通信辅助
    # ------------------------------------------------------------------
    def _send(self, message: Mapping[str, Any]) -> bool:
        try:
            self._transport.send_message(dict(message))
            return True
        except (TransportError, RuntimeError, OSError) as exc:
            logger.warning("发送联机会话消息失败：%s", exc)
            self.error.emit("网络发送失败，请检查连接")
            return False

    def authenticate(self, access_token: str) -> bool:
        """建立 WebSocket 后发送首帧认证。"""
        if self._closed:
            return False
        return self._send(
            make_message(
                CLIENT_AUTH,
                {
                    "access_token": access_token,
                    "capabilities": sorted(CLIENT_CAPABILITIES),
                },
            )
        )

    def create_room(self, allow_spectators: bool = True) -> bool:
        if not self._authenticated or self._closed:
            return False
        return self._send(
            make_message(
                CLIENT_CREATE_ROOM,
                {"allow_spectators": bool(allow_spectators)},
            )
        )

    def join_room(self, code: str) -> bool:
        if not self._authenticated or self._closed:
            return False
        code = code.strip().upper()
        if not code:
            self.error.emit("请输入房间码")
            return False
        return self._send(
            make_message(CLIENT_JOIN_ROOM, {"room_code": code})
        )

    def watch_game(self, game_id: str) -> bool:
        """以旁观者身份观战一局进行中的对局。

        观战者没有执子颜色，只能接收服务器的权威快照，不能落子、悔棋、
        认输、再战或发言；服务器会用带 ``is_spectator`` 的快照回应。
        """
        if not self._authenticated or self._closed:
            return False
        if self._waiting or self._active or self._spectating:
            # 已经在房间里（作为玩家或旁观者），不能再开新的一局。
            return False
        game_id = game_id.strip()
        if not game_id:
            self.error.emit("对局编号无效")
            return False
        if not self._send(make_message(CLIENT_WATCH_GAME, {"game_id": game_id})):
            return False
        self._spectating = True
        return True

    def invite_to_room(self, user_id: str) -> bool:
        """邀请一位在线好友加入自己的等待房间。"""
        if not self._authenticated or self._closed or not user_id:
            return False
        return self._send(
            make_message(
                CLIENT_INVITE_TO_ROOM,
                {"user_id": user_id},
                request_id=uuid.uuid4().hex,
            )
        )

    def respond_room_invite(self, invite_id: str, accept: bool) -> bool:
        """接受/拒绝收到的自建房邀请。"""
        if not self._authenticated or self._closed or not invite_id:
            return False
        return self._send(
            make_message(
                CLIENT_RESPOND_ROOM_INVITE,
                {"invite_id": invite_id, "accept": bool(accept)},
                request_id=uuid.uuid4().hex,
            )
        )

    def quick_match(self) -> bool:
        """加入公开排位匹配队列。"""
        if not self._authenticated or self._closed:
            return False
        return self._send(make_message(CLIENT_QUICK_MATCH, {}))

    def cancel_match(self) -> bool:
        """退出排位匹配队列。"""
        if not self._authenticated or self._closed:
            return False
        return self._send(make_message(CLIENT_CANCEL_MATCH, {}))

    def send_ping(self) -> bool:
        if not self._authenticated or self._closed:
            return False
        return self._send(make_message(CLIENT_PING, {}))

    # ------------------------------------------------------------------
    # 落子/认输/再战/离房
    # ------------------------------------------------------------------
    def request_move(self, row: int, col: int) -> bool:
        """发送落子意图；本地棋盘只等服务器权威快照更新。"""
        if self._spectating:
            # 观战者不能落子（纵深防御，正常情况下 accepts_human_input 已拦截）。
            return False
        if not self.accepts_human_input:
            return False
        try:
            payload = make_move_intent(row, col)
        except ProtocolError:
            return False

        request_id = uuid.uuid4().hex
        message = make_message(
            CLIENT_MOVE_INTENT,
            payload,
            request_id=request_id,
        )
        self._pending_move_id = request_id
        if not self._send(message):
            self._pending_move_id = None
            return False
        self.state_changed.emit()
        return True

    def request_undo(self) -> bool:
        """向对手发起悔棋请求；实际回滚等服务器确认后广播。"""
        if self._spectating:
            # 观战者不参与悔棋。
            return False
        if not self.can_undo:
            return False
        request_id = uuid.uuid4().hex
        self._pending_undo_request_id = request_id
        if not self._send(
            make_message(CLIENT_UNDO_REQUEST, {}, request_id=request_id)
        ):
            self._pending_undo_request_id = None
            return False
        self.state_changed.emit()
        return True

    def respond_undo(self, accept: bool) -> bool:
        """回应对手的悔棋请求；仅被请求的一方可以操作。"""
        if (
            self._closed
            or self._disconnected
            or self._spectating
            or not self._undo_requester_id
            or self._undo_requester_id == self._user_id
        ):
            return False
        return self._send(
            make_message(
                CLIENT_UNDO_RESPONSE,
                {"accept": bool(accept)},
                request_id=uuid.uuid4().hex,
            )
        )

    def request_restart(self) -> bool:
        return False

    def resign(self) -> bool:
        if self._spectating:
            # 观战者没有棋子，不能认输。
            return False
        if self._snapshot is None or self._snapshot.is_game_over:
            return False
        return self._send(make_message(CLIENT_RESIGN, {}))

    def request_rematch(self) -> bool:
        if self._spectating:
            # 观战者不是对局双方，不能申请再战。
            return False
        if self._rated:
            return False
        if self._snapshot is None or not self._snapshot.is_game_over:
            return False
        return self._send(make_message(CLIENT_REMATCH, {}))

    def leave_room(self) -> bool:
        """离开房间；对观战者而言这是“退出观战”，不影响两名玩家。"""
        if self._snapshot is None and not self._waiting:
            return False
        sent = self._send(make_message(CLIENT_LEAVE_ROOM, {}))
        if sent and self._spectating:
            # 观战者离开只是停止观看：本地立刻回到大厅状态，不必等服务器
            # 再推一次 room_closed，也不会打扰房间里的两名玩家。
            self._reset_room_state()
            self.state_changed.emit()
        return sent

    # ------------------------------------------------------------------
    # 表情/评论
    # ------------------------------------------------------------------
    def send_chat(self, kind: str, content: str) -> bool:
        """发送表情或评论；失败时发出 chat_failed 信号并返回 False。"""
        if self._closed or not self._authenticated:
            return False
        if not self.chat_available:
            self.chat_failed.emit("当前不能发言")
            return False
        try:
            payload = build_chat_payload(kind, content)
        except ProtocolError as exc:
            self.chat_failed.emit(str(exc))
            return False
        # 客户端冷却：两次发送之间至少间隔 CHAT_INTERVAL_SECONDS。
        now = time.monotonic()
        if (
            self._last_chat_at is not None
            and now - self._last_chat_at < CHAT_INTERVAL_SECONDS
        ):
            # 明确告诉用户被冷却挡下来了，不要让点击悄悄消失。
            self.chat_failed.emit("发言太快，请稍候")
            return False
        self._last_chat_at = now
        # 带 request_id，服务器拒绝这条发言时才能把错误精确归到聊天，
        # 不会混进对局的红色错误条。
        request_id = uuid.uuid4().hex
        sent = self._send_chat_message(
            make_message(CLIENT_CHAT, payload, request_id=request_id)
        )
        if not sent:
            self.chat_failed.emit("网络发送失败，请检查连接")
        else:
            self._remember_chat_request(request_id)
        return sent

    def _send_chat_message(self, message: Mapping[str, Any]) -> bool:
        """发送聊天消息，且不触发对局的通用错误信号。

        发言失败只提示在聊天区域；混进对局的红色错误条会让玩家以为
        是棋局出了故障。
        """
        try:
            self._transport.send_message(dict(message))
            return True
        except (TransportError, RuntimeError, OSError) as exc:
            logger.warning("发送聊天消息失败：%s", exc)
            return False

    def send_emoji(self, emoji: str) -> bool:
        """发送一个预设表情。"""
        return self.send_chat(CHAT_KIND_EMOJI, emoji)

    def send_comment(self, text: str) -> bool:
        """发送一条手动输入或预设的评论。"""
        return self.send_chat(CHAT_KIND_TEXT, text)

    def _remember_chat_request(self, request_id: str) -> None:
        """记录一条已发出的发言请求，并淘汰过期的记录。

        服务器只在**拒绝**发言时才回执 request_id，成功的发言只会收到
        一条广播，因此这里必须限制上限，避免长时间对局无限累积。
        """
        now = time.monotonic()
        stale = [
            key
            for key, stamp in self._pending_chat_ids.items()
            if now - stamp > CHAT_REQUEST_TTL_SECONDS
        ]
        for key in stale:
            self._pending_chat_ids.pop(key, None)
        self._pending_chat_ids[request_id] = now

    def pause(self) -> None:
        if not self._closed and not self._paused:
            self._paused = True
            self.state_changed.emit()

    def resume(self) -> None:
        if not self._closed and self._paused:
            self._paused = False
            self.state_changed.emit()

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending_move_id = None
        try:
            self._transport.close()
        except Exception:
            logger.exception("关闭联机传输失败")

    def _reset_room_state(self) -> None:
        """认证/换房时清空旧房间快照，等待服务器下发新房间状态。"""
        self._snapshot = None
        self._view = _empty_view()
        self._room_code = ""
        self._waiting = False
        self._your_color = None
        self._game_id = None
        self._spectating = False
        self._active = False
        self._pending_move_id = None
        self._last_move_no = 0
        self._opponent_online = False
        self._opponent_username = ""
        self._rematch_yours = False
        self._rematch_opponent = False
        self._undo_requester_id = ""
        self._undo_requester_name = ""
        self._pending_undo_request_id = None
        self._undo_remaining = 0
        self._undo_limit = 0
        self._undo_cooldown_seconds = 0
        self._undo_cooldown_deadline = None
        self._last_state_version = 0
        self._queued = False
        self._queue_size = 0
        self._rated = False
        self._your_rating_before = None
        self._your_rating_after = None
        self._opponent_rating_before = None
        self._opponent_rating_after = None
        self._rating_delta = None

    # ------------------------------------------------------------------
    # 传输消息
    # ------------------------------------------------------------------
    def _on_transport_message(self, raw: Any) -> None:
        if self._closed:
            return
        # 服务端以后新增消息类型时（协议版本不变），旧客户端应该安静
        # 忽略，而不是当成协议错误打扰玩家。
        if isinstance(raw, Mapping):
            raw_type = raw.get("type")
            if (
                isinstance(raw_type, str)
                and raw_type not in SERVER_MESSAGE_TYPES
            ):
                logger.warning("忽略未知服务器消息：%s", raw_type)
                return
        try:
            message = parse_message(raw)
        except ProtocolError as exc:
            logger.warning("丢弃非法服务器消息：%s", exc)
            self.error.emit("收到无法解析的服务器消息")
            return

        message_type = message["type"]
        if message_type == SERVER_GAME_STATE:
            self._handle_game_state(message)
        elif message_type == SERVER_AUTHENTICATED:
            self._handle_authenticated(message)
        elif message_type == SERVER_ROOM_CREATED:
            self._handle_room_created(message)
        elif message_type == SERVER_MOVE_REJECTED:
            self._handle_move_rejected(message)
        elif message_type == SERVER_OPPONENT_STATUS:
            self._handle_opponent_status(message)
        elif message_type == SERVER_REMATCH_STATUS:
            self._handle_rematch_status(message)
        elif message_type == SERVER_UNDO_STATUS:
            self._handle_undo_status(message)
        elif message_type == SERVER_QUEUE_STATUS:
            self._handle_queue_status(message)
        elif message_type == SERVER_MATCH_FOUND:
            self._handle_match_found(message)
        elif message_type == SERVER_ROOM_CLOSED:
            self._handle_room_closed(message)
        elif message_type == SERVER_ERROR:
            self._handle_server_error(message)
        elif message_type == SERVER_PONG:
            return
        elif message_type == SERVER_CHAT:
            self._handle_chat(message)
        elif message_type == SERVER_ROOM_INVITATION:
            self._handle_room_invitation(message)
        elif message_type == SERVER_ROOM_INVITE_RESULT:
            self._handle_room_invite_result(message)
        elif message_type == SERVER_FRIEND_EVENT:
            self._handle_friend_event(message)
        elif message_type == SERVER_DIRECT_MESSAGE:
            self._handle_direct_message(message)
        elif message_type == SERVER_LIVE_GAMES_CHANGED:
            self.live_games_changed.emit()
        else:
            logger.warning("忽略未知服务器消息：%s", message_type)

    def _handle_authenticated(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        user = payload.get("user") if isinstance(payload, Mapping) else None
        if not isinstance(user, Mapping):
            self.error.emit("服务器认证响应格式异常")
            return
        user_id = user.get("id")
        username = user.get("username")
        if not isinstance(user_id, str) or not isinstance(username, str):
            self.error.emit("服务器认证响应格式异常")
            return
        self._authenticated = True
        self._disconnected = False
        self._user_id = user_id
        self._username = username
        try:
            server_capabilities = frozenset(
                normalize_capabilities(payload.get("capabilities", ()))
            )
        except ProtocolError as exc:
            logger.warning("服务器能力声明非法：%s", exc)
            server_capabilities = frozenset()
        self._server_capabilities = server_capabilities
        self._reset_room_state()
        rating = user.get("rating")
        avatar_version = user.get("avatar_version", 0)
        if (
            not isinstance(avatar_version, int)
            or isinstance(avatar_version, bool)
            or avatar_version < 0
        ):
            avatar_version = 0
        self.authenticated.emit(
            {
                "id": user_id,
                "username": username,
                "avatar": user.get("avatar", ""),
                "avatar_version": avatar_version,
                "achievements": list(
                    normalize_achievement_keys(
                        user.get("achievements")
                    )
                ),
                "rating": (
                    rating if isinstance(rating, int) else None
                ),
                "rank_name": user.get("rank_name", ""),
                "capabilities": sorted(server_capabilities),
            }
        )
        self.state_changed.emit()

    def _handle_room_created(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            self.error.emit("服务器房间响应格式异常")
            return
        code = payload.get("room_code")
        if not isinstance(code, str) or not code:
            self.error.emit("服务器返回了无效房间码")
            return
        self._room_code = code
        self._waiting = True
        self._active = False
        self._snapshot = None
        self._view = _empty_view()
        self._your_color = Player.BLACK
        self._spectating = False
        self._rematch_yours = False
        self._rematch_opponent = False
        self._undo_requester_id = ""
        self._undo_requester_name = ""
        self._pending_undo_request_id = None
        self._undo_remaining = 0
        self._undo_limit = 0
        self._undo_cooldown_seconds = 0
        self._undo_cooldown_deadline = None
        self._last_state_version = 0
        self._queued = False
        self._queue_size = 0
        self._rated = False
        self._your_rating_before = None
        self._your_rating_after = None
        self._opponent_rating_before = None
        self._opponent_rating_after = None
        self._rating_delta = None
        black_name = payload.get("black_name")
        if isinstance(black_name, str) and black_name:
            self._username = black_name
        self.room_created.emit(code)
        self.state_changed.emit()

    def _handle_game_state(self, message: Mapping[str, Any]) -> None:
        try:
            snapshot = parse_game_state(message["payload"])
        except (ProtocolError, KeyError) as exc:
            logger.warning("服务器局面快照非法：%s", exc)
            self.error.emit("服务器返回了无效棋局")
            return

        same_game = self._game_id == snapshot.game_id
        if same_game:
            # 新版服务端用 state_version 识别回滚；没有版本号时退回手数比较，
            # 兼容旧快照并拒绝迟到消息。
            if snapshot.state_version or self._last_state_version:
                if snapshot.state_version < self._last_state_version:
                    logger.warning(
                        "丢弃过期服务器快照：version=%s",
                        snapshot.state_version,
                    )
                    return
            elif snapshot.move_no < self._last_move_no:
                logger.warning(
                    "丢弃过期服务器快照：move_no=%s", snapshot.move_no
                )
                return

        was_over = self._view.is_game_over
        game_changed = self._game_id != snapshot.game_id
        # 新对局（再战/新匹配）不继承上一局的手数基线，只把首帧当作开局。
        previous_move_no = 0 if game_changed else self._last_move_no
        self._game_id = snapshot.game_id
        self._room_code = snapshot.room_code or (
            "" if snapshot.rated else self._room_code
        )
        self._your_color = snapshot.your_color
        # 观战者的快照没有执子颜色（your_color 为 None），由 is_spectator 标记。
        self._spectating = bool(snapshot.is_spectator)
        self._snapshot = snapshot
        self._active = True
        self._waiting = False
        self._disconnected = False
        self._queued = False
        self._queue_size = 0
        self._rated = snapshot.rated
        self._your_rating_before = snapshot.your_rating_before
        self._your_rating_after = snapshot.your_rating_after
        self._opponent_rating_before = snapshot.opponent_rating_before
        self._opponent_rating_after = snapshot.opponent_rating_after
        self._rating_delta = snapshot.rating_delta
        self._view = _view_from_snapshot(snapshot)
        self._last_move_no = snapshot.move_no
        self._last_state_version = snapshot.state_version
        self._undo_requester_id = snapshot.undo_requester_id
        if snapshot.undo_requester_id:
            self._undo_requester_name = (
                snapshot.black_name
                if snapshot.undo_requester_id == snapshot.black_user_id
                else (
                    snapshot.white_name
                    if snapshot.undo_requester_id == snapshot.white_user_id
                    else "对手"
                )
            )
        else:
            self._undo_requester_name = ""
            self._pending_undo_request_id = None
        self._undo_remaining = snapshot.undo_remaining
        self._undo_limit = snapshot.undo_limit
        self._undo_cooldown_seconds = snapshot.undo_cooldown_seconds
        if (
            snapshot.undo_cooldown_until_ms is not None
            and snapshot.server_now_ms is not None
        ):
            remaining = max(
                0,
                snapshot.undo_cooldown_until_ms
                - snapshot.server_now_ms,
            ) / 1000.0
            self._undo_cooldown_deadline = time.monotonic() + remaining
        else:
            self._undo_cooldown_deadline = None
        self._opponent_online = (
            snapshot.opponent_online
            if snapshot.opponent_online is not None
            else True
        )
        self._rematch_yours = False
        self._rematch_opponent = False

        request_id = message.get("request_id")
        if self._pending_move_id is not None and (
            request_id == self._pending_move_id
            or snapshot.move_no > previous_move_no
        ):
            self._pending_move_id = None

        self.state_changed.emit()
        if snapshot.move_no > previous_move_no:
            self.stone_placed.emit()
        if self._view.is_game_over and not was_over:
            self.game_over.emit()

    def _handle_move_rejected(self, message: Mapping[str, Any]) -> None:
        request_id = message.get("request_id")
        if self._pending_move_id is None:
            return
        if request_id is not None and request_id != self._pending_move_id:
            return
        self._pending_move_id = None
        payload = message.get("payload", {})
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        self.error.emit(
            reason if isinstance(reason, str) and reason else "服务器拒绝了本次落子"
        )
        self.state_changed.emit()

    def _handle_opponent_status(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        online = payload.get("online")
        if not isinstance(online, bool):
            return
        self._opponent_online = online
        name = payload.get("username")
        if isinstance(name, str) and name:
            self._opponent_username = name
        self.opponent_status_changed.emit(online, self.opponent_username)
        self.state_changed.emit()

    def _handle_rematch_status(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        yours = payload.get("your_requested")
        opponent = payload.get("opponent_requested")
        self._rematch_yours = bool(yours)
        self._rematch_opponent = bool(opponent)
        self.rematch_status_changed.emit(
            self._rematch_yours, self._rematch_opponent
        )
        self.state_changed.emit()

    def _handle_undo_status(self, message: Mapping[str, Any]) -> None:
        """更新悔棋请求状态；请求与结果都只认服务器广播。"""
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        state = payload.get("state")
        if state not in (
            "requested",
            "accepted",
            "declined",
            "expired",
            "cancelled",
        ):
            logger.warning("丢弃非法悔棋状态：%s", state)
            return
        requester_id = payload.get("requester_id")
        if not isinstance(requester_id, str):
            requester_id = ""
        requester_name = payload.get("requester_name")
        if not isinstance(requester_name, str) or not requester_name:
            requester_name = "对手"
        was_mine = bool(requester_id) and requester_id == self._user_id
        reason = payload.get("reason")
        reason_text = reason if isinstance(reason, str) else ""
        if state == "accepted":
            reason_text = "对手已同意悔棋" if was_mine else "你已同意悔棋"
        elif state == "declined":
            reason_text = (
                "对手拒绝了悔棋请求" if was_mine else "你已拒绝悔棋请求"
            )
        elif state == "expired":
            reason_text = "悔棋请求已超时"
        elif state == "cancelled" and not reason_text:
            reason_text = "悔棋请求已取消"

        if state == "requested":
            self._undo_requester_id = requester_id
            self._undo_requester_name = requester_name
            self._pending_undo_request_id = None
            if requester_id and requester_id != self._user_id:
                self.undo_request_received.emit(requester_name)
        else:
            self._undo_requester_id = ""
            self._undo_requester_name = ""
            self._pending_undo_request_id = None
        self.undo_status_changed.emit(state, reason_text)
        self.state_changed.emit()

    def _handle_queue_status(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        state = payload.get("state")
        if state not in ("queued", "idle"):
            self.error.emit("服务器返回了无效的匹配状态")
            return
        size = payload.get("queue_size", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            size = 0
        self._queued = state == "queued"
        self._queue_size = size
        self.queue_status_changed.emit(self._queued, size)
        self.state_changed.emit()

    def _handle_match_found(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            self.error.emit("服务器匹配消息格式异常")
            return
        game_id = payload.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            self.error.emit("服务器匹配消息缺少对局")
            return
        color_text = payload.get("your_color")
        if color_text == "black":
            color = Player.BLACK
        elif color_text == "white":
            color = Player.WHITE
        else:
            self.error.emit("服务器匹配消息缺少执子颜色")
            return

        def rating_value(key: str) -> Optional[int]:
            value = payload.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return None

        opponent_name = payload.get("opponent_name")
        self._queued = False
        self._queue_size = 0
        self._rated = True
        self._your_color = color
        self._spectating = False
        self._room_code = ""
        self._waiting = False
        self._active = False
        self._snapshot = None
        self._view = _empty_view()
        self._game_id = game_id
        self._last_move_no = 0
        self._rematch_yours = False
        self._rematch_opponent = False
        self._undo_requester_id = ""
        self._undo_requester_name = ""
        self._pending_undo_request_id = None
        self._undo_remaining = 0
        self._undo_limit = 0
        self._undo_cooldown_seconds = 0
        self._undo_cooldown_deadline = None
        self._last_state_version = 0
        self._opponent_username = (
            opponent_name if isinstance(opponent_name, str) else ""
        )
        self._your_rating_before = rating_value("your_rating")
        self._opponent_rating_before = rating_value("opponent_rating")
        self._your_rating_after = None
        self._opponent_rating_after = None
        self._rating_delta = None
        self._pending_move_id = None
        self.matched.emit(game_id, self._opponent_username or "对手")
        self.state_changed.emit()

    def _handle_room_closed(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        reason_text = (
            reason if isinstance(reason, str) and reason else "房间已关闭"
        )
        self._reset_room_state()
        self.room_closed.emit(reason_text)
        self.state_changed.emit()

    def _handle_server_error(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        reason = payload.get("reason") if isinstance(payload, Mapping) else None
        request_id = message.get("request_id")
        # 发言被服务器拒绝（限流、白名单等）走 chat_failed，
        # 不混进对局的红色错误条。
        if isinstance(request_id, str) and request_id in self._pending_chat_ids:
            self._pending_chat_ids.pop(request_id, None)
            self.chat_failed.emit(
                reason if isinstance(reason, str) and reason else "发言未发送"
            )
            return
        if (
            self._pending_move_id is not None
            and request_id == self._pending_move_id
        ):
            self._pending_move_id = None
            self.state_changed.emit()
        if (
            self._pending_undo_request_id is not None
            and request_id == self._pending_undo_request_id
        ):
            self._pending_undo_request_id = None
            self.state_changed.emit()
        if self._spectating and self._snapshot is None:
            # 观战请求被服务器拒绝（对局不存在、已在房间中等）：观战没有
            # 成立，必须回到普通玩家身份，否则界面会一直显示只读视图。
            self._spectating = False
            self.state_changed.emit()
        self.error.emit(reason if isinstance(reason, str) and reason else "服务器错误")

    def _handle_chat(self, message: Mapping[str, Any]) -> None:
        """解析服务端广播的表情/评论并转发给界面。"""
        try:
            chat = parse_chat_message(message.get("payload", {}))
        except ProtocolError as exc:
            logger.warning("丢弃非法聊天消息：%s", exc)
            return
        self.chat_received.emit(chat)

    def _handle_room_invitation(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if not isinstance(payload, Mapping):
            return
        invite_id = payload.get("invite_id")
        inviter = payload.get("inviter")
        if not isinstance(invite_id, str) or not isinstance(inviter, Mapping):
            logger.warning("丢弃非法自建房邀请")
            return
        self.room_invited.emit(dict(payload))

    def _handle_room_invite_result(self, message: Mapping[str, Any]) -> None:
        payload = message.get("payload", {})
        if isinstance(payload, Mapping):
            self.room_invite_result.emit(dict(payload))

    def _handle_friend_event(self, message: Mapping[str, Any]) -> None:
        try:
            event = parse_friend_event(message.get("payload", {}))
        except SocialError as exc:
            logger.warning("丢弃非法好友事件：%s", exc)
            return
        self.friend_event.emit(event)

    def _handle_direct_message(self, message: Mapping[str, Any]) -> None:
        try:
            direct = parse_direct_message(message.get("payload", {}))
        except SocialError as exc:
            logger.warning("丢弃非法私聊消息：%s", exc)
            return
        self.direct_message.emit(direct)

    def _on_transport_disconnected(self, reason: str) -> None:
        if self._closed:
            return
        self._disconnected = True
        self._pending_move_id = None
        self._undo_requester_id = ""
        self._undo_requester_name = ""
        self._pending_undo_request_id = None
        was_queued = self._queued
        self._queued = False
        self._queue_size = 0
        if was_queued:
            self.queue_status_changed.emit(False, 0)
        self.error.emit(reason or "联机连接已断开")
        self.state_changed.emit()

    def _on_transport_error(self, message: str) -> None:
        if self._closed:
            return
        self._pending_move_id = None
        self._pending_undo_request_id = None
        self.error.emit(message or "网络错误")
        self.state_changed.emit()


__all__ = ["OnlineGameSession"]
