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
import uuid
from typing import Any, Mapping, Optional

from PySide6.QtCore import QObject, Signal

from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.client.transport import Transport
from gomoku.shared.errors import ProtocolError, TransportError
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_LEAVE_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_PING,
    CLIENT_QUICK_MATCH,
    CLIENT_REMATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_OPPONENT_STATUS,
    SERVER_PONG,
    SERVER_QUEUE_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_CREATED,
    GameSnapshot,
    make_message,
    make_move_intent,
    parse_game_state,
    parse_message,
)
from gomoku.shared.rules import BOARD_SIZE, Cell, Player

logger = logging.getLogger(__name__)

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
    queue_status_changed = Signal(bool, int)  # queued, queue_size
    matched = Signal(str, str)  # game_id, opponent_name

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
        self._user_id = ""
        self._username = ""
        self._room_code = ""
        self._waiting = False
        self._your_color: Optional[Player] = None
        self._game_id: Optional[str] = None
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
        self._queued = False
        self._queue_size = 0
        self._rated = False
        self._your_rating_before: Optional[int] = None
        self._your_rating_after: Optional[int] = None
        self._opponent_rating_before: Optional[int] = None
        self._opponent_rating_after: Optional[int] = None
        self._rating_delta: Optional[int] = None

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
    def snapshot(self) -> Optional[GameSnapshot]:
        return self._snapshot

    @property
    def opponent_online(self) -> bool:
        return self._opponent_online

    @property
    def opponent_username(self) -> str:
        if self._opponent_username:
            return self._opponent_username
        if self._snapshot is None or self._your_color is None:
            return "对手"
        return (
            self._snapshot.white_name
            if self._your_color is Player.BLACK
            else self._snapshot.black_name
        ) or "对手"

    @property
    def your_username(self) -> str:
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
            and self._your_color is not None
            and self._view.current_player is self._your_color
            and self._pending_move_id is None
        )

    @property
    def can_undo(self) -> bool:
        """联机首版不支持悔棋。"""
        return False

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
            make_message(CLIENT_AUTH, {"access_token": access_token})
        )

    def create_room(self) -> bool:
        if not self._authenticated or self._closed:
            return False
        return self._send(make_message(CLIENT_CREATE_ROOM, {}))

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
        return False

    def request_restart(self) -> bool:
        return False

    def resign(self) -> bool:
        if self._snapshot is None or self._snapshot.is_game_over:
            return False
        return self._send(make_message(CLIENT_RESIGN, {}))

    def request_rematch(self) -> bool:
        if self._rated:
            return False
        if self._snapshot is None or not self._snapshot.is_game_over:
            return False
        return self._send(make_message(CLIENT_REMATCH, {}))

    def leave_room(self) -> bool:
        if self._snapshot is None and not self._waiting:
            return False
        return self._send(make_message(CLIENT_LEAVE_ROOM, {}))

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
        self._active = False
        self._pending_move_id = None
        self._last_move_no = 0
        self._opponent_online = False
        self._opponent_username = ""
        self._rematch_yours = False
        self._rematch_opponent = False
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
        self._reset_room_state()
        self.authenticated.emit({"id": user_id, "username": username})
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
        self._rematch_yours = False
        self._rematch_opponent = False
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

        if (
            self._game_id == snapshot.game_id
            and snapshot.move_no < self._last_move_no
        ):
            logger.warning("丢弃过期服务器快照：move_no=%s", snapshot.move_no)
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
        self._room_code = ""
        self._waiting = False
        self._active = False
        self._snapshot = None
        self._view = _empty_view()
        self._game_id = game_id
        self._last_move_no = 0
        self._rematch_yours = False
        self._rematch_opponent = False
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
        if (
            self._pending_move_id is not None
            and message.get("request_id") == self._pending_move_id
        ):
            self._pending_move_id = None
            self.state_changed.emit()
        self.error.emit(reason if isinstance(reason, str) and reason else "服务器错误")

    def _on_transport_disconnected(self, reason: str) -> None:
        if self._closed:
            return
        self._disconnected = True
        self._pending_move_id = None
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
        self.error.emit(message or "网络错误")
        self.state_changed.emit()


__all__ = ["OnlineGameSession"]
