"""客户端/服务端共享的联机协议草稿（协议版本 1）。

Stage 0 只定义最小可测试的消息封装、落子意图和服务器权威局面快照，为后续
FastAPI/WSS 服务端和 OnlineSession 提供共同语言。真实服务端落地时可以在保持
版本号/向后兼容策略的前提下扩展房间、认证、匹配和再战等消息。

协议原则：
* 客户端只发送意图（坐标），不能提交胜负或积分；
* 服务端发送带步序和完整棋盘的权威快照；
* 所有消息都是 JSON 兼容的 dict，带 ``protocol`` 版本号和 ``payload``；
* 未知/畸形消息应被拒绝并作为非致命错误处理。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from gomoku.shared.achievements import normalize_achievement_keys
from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.errors import ProtocolError
from gomoku.shared.rules import BOARD_SIZE, Cell, Move, Player

PROTOCOL_VERSION = 1

# 客户端 → 服务端
CLIENT_AUTH = "auth"
CLIENT_CREATE_ROOM = "create_room"
CLIENT_JOIN_ROOM = "join_room"
CLIENT_MOVE_INTENT = "move_intent"
CLIENT_RESIGN = "resign"
CLIENT_LEAVE_ROOM = "leave_room"
CLIENT_REMATCH = "rematch"
CLIENT_UNDO_REQUEST = "undo_request"
CLIENT_UNDO_RESPONSE = "undo_response"
CLIENT_QUICK_MATCH = "quick_match"
CLIENT_CANCEL_MATCH = "cancel_match"
CLIENT_PING = "ping"
CLIENT_CHAT = "chat"
CLIENT_INVITE_TO_ROOM = "invite_to_room"
CLIENT_RESPOND_ROOM_INVITE = "respond_room_invite"
# 观战：旁观者以对局编号进入别人正在进行的一局，只接收权威快照。
CLIENT_WATCH_GAME = "watch_game"

# 服务端 → 客户端
SERVER_AUTHENTICATED = "authenticated"
SERVER_ROOM_CREATED = "room_created"
SERVER_GAME_STATE = "game_state"
SERVER_MOVE_REJECTED = "move_rejected"
SERVER_OPPONENT_STATUS = "opponent_status"
SERVER_ROOM_CLOSED = "room_closed"
SERVER_REMATCH_STATUS = "rematch_status"
SERVER_UNDO_STATUS = "undo_status"
SERVER_QUEUE_STATUS = "queue_status"
SERVER_MATCH_FOUND = "match_found"
SERVER_PONG = "pong"
SERVER_ERROR = "server_error"
SERVER_CHAT = "chat_message"
SERVER_ROOM_INVITATION = "room_invitation"
SERVER_ROOM_INVITE_RESULT = "room_invite_result"
SERVER_FRIEND_EVENT = "friend_event"
SERVER_DIRECT_MESSAGE = "direct_message"
# 进行中的对局列表发生变化（有人开局或终局），客户端据此刷新对战广场。
SERVER_LIVE_GAMES_CHANGED = "live_games_changed"

_ALLOWED_MESSAGE_TYPES = {
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_RESIGN,
    CLIENT_LEAVE_ROOM,
    CLIENT_REMATCH,
    CLIENT_UNDO_REQUEST,
    CLIENT_UNDO_RESPONSE,
    CLIENT_QUICK_MATCH,
    CLIENT_CANCEL_MATCH,
    CLIENT_PING,
    CLIENT_CHAT,
    CLIENT_INVITE_TO_ROOM,
    CLIENT_RESPOND_ROOM_INVITE,
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_ROOM_CREATED,
    SERVER_GAME_STATE,
    SERVER_MOVE_REJECTED,
    SERVER_OPPONENT_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_REMATCH_STATUS,
    SERVER_UNDO_STATUS,
    SERVER_QUEUE_STATUS,
    SERVER_MATCH_FOUND,
    SERVER_PONG,
    SERVER_ERROR,
    SERVER_CHAT,
    SERVER_ROOM_INVITATION,
    SERVER_ROOM_INVITE_RESULT,
    SERVER_FRIEND_EVENT,
    SERVER_DIRECT_MESSAGE,
    SERVER_LIVE_GAMES_CHANGED,
}

# 服务端 → 客户端的消息类型集合。
#
# 客户端只认得自己这一版支持的服务端消息；遇到不认识的服务端类型时
# 应当安静忽略，而不是当成协议错误弹给玩家。这样服务端以后新增消息
# 类型（哪怕协议版本不变）也不会让旧客户端满屏报错。
SERVER_MESSAGE_TYPES = frozenset(
    {
        SERVER_AUTHENTICATED,
        SERVER_ROOM_CREATED,
        SERVER_GAME_STATE,
        SERVER_MOVE_REJECTED,
        SERVER_OPPONENT_STATUS,
        SERVER_ROOM_CLOSED,
        SERVER_REMATCH_STATUS,
        SERVER_UNDO_STATUS,
        SERVER_QUEUE_STATUS,
        SERVER_MATCH_FOUND,
        SERVER_PONG,
        SERVER_ERROR,
        SERVER_CHAT,
        SERVER_ROOM_INVITATION,
        SERVER_ROOM_INVITE_RESULT,
        SERVER_FRIEND_EVENT,
        SERVER_DIRECT_MESSAGE,
        SERVER_LIVE_GAMES_CHANGED,
    }
)


@dataclass(frozen=True)
class GameSnapshot:
    """服务器下发的权威局面快照。

    ``your_color`` 由服务端按当前连接的用户生成；``ack_request_id`` 通过
    消息 envelope 的 request_id 携带，与客户端的 pending move 对齐。
    """

    game_id: str
    board: tuple[tuple[Cell, ...], ...]
    current_player: Player
    last_move: Optional[Move]
    winner: Optional[Player]
    winning_line: tuple[tuple[int, int], ...]
    is_draw: bool
    is_game_over: bool
    move_no: int
    # 执子颜色。观战者没有棋子，服务器下发 None，并用 is_spectator 标记。
    your_color: Optional[Player]
    # 服务端局面版本：悔棋会让 move_no 变小，客户端据此区分回滚与过期快照。
    state_version: int = 0
    # 终局原因：five/draw/resign/timeout/escape；对局中为空。
    result_reason: str = ""
    # 当前待回应的悔棋请求发起者；空字符串表示没有请求。
    undo_requester_id: str = ""
    # 联机房间 UI 的可选元数据（Stage 1 服务端会提供）。
    room_code: str = ""
    black_name: str = ""
    white_name: str = ""
    black_user_id: str = ""
    white_user_id: str = ""
    black_avatar: str = ""
    white_avatar: str = ""
    black_avatar_version: int = 0
    white_avatar_version: int = 0
    # 双方已解锁成就 key；对局卡片只展示其中最稀有的几枚。
    black_achievements: tuple[str, ...] = ()
    white_achievements: tuple[str, ...] = ()
    # 本局结算后当前玩家新解锁的成就（仅终局快照携带）。
    your_new_achievements: tuple[str, ...] = ()
    turn_deadline_ms: int | None = None
    server_now_ms: int | None = None
    move_timeout_seconds: int | None = None
    opponent_online: bool | None = None
    # Stage 2 排位元数据：好友房 rated=False 且积分为 null。
    rated: bool = False
    your_rating_before: int | None = None
    your_rating_after: int | None = None
    opponent_rating_before: int | None = None
    opponent_rating_after: int | None = None
    rating_delta: int | None = None
    # 观战标记：True 表示这条快照属于旁观者，没有执子颜色，也不能操作。
    is_spectator: bool = False


def _parse_player(value: Any, field: str) -> Player:
    if value == "black":
        return Player.BLACK
    if value == "white":
        return Player.WHITE
    raise ProtocolError(f"{field} 必须是 'black' 或 'white'")


def _parse_board(value: Any) -> tuple[tuple[Cell, ...], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProtocolError("board 必须是二维数组")
    if len(value) != BOARD_SIZE:
        raise ProtocolError(f"board 必须有 {BOARD_SIZE} 行")

    board: list[tuple[Cell, ...]] = []
    for row in value:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            raise ProtocolError("board 的每一行必须是数组")
        if len(row) != BOARD_SIZE:
            raise ProtocolError(f"board 每一行必须有 {BOARD_SIZE} 格")
        parsed_row: list[Cell] = []
        for cell in row:
            if cell is None:
                parsed_row.append(None)
            elif cell == "black":
                parsed_row.append(Player.BLACK)
            elif cell == "white":
                parsed_row.append(Player.WHITE)
            else:
                raise ProtocolError("board 格子只能是 null/black/white")
        board.append(tuple(parsed_row))
    return tuple(board)


def _parse_coord(value: Any, field: str) -> tuple[int, int]:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) != 2
    ):
        raise ProtocolError(f"{field} 必须是 [row, col]")
    row, col = value
    if not isinstance(row, int) or not isinstance(col, int):
        raise ProtocolError(f"{field} 坐标必须是整数")
    if not 0 <= row < BOARD_SIZE or not 0 <= col < BOARD_SIZE:
        raise ProtocolError(f"{field} 坐标越界")
    return row, col


def _parse_last_move(value: Any) -> Optional[Move]:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ProtocolError("last_move 必须是对象或 null")
    try:
        row = value["row"]
        col = value["col"]
        player = _parse_player(value["player"], "last_move.player")
    except KeyError as exc:
        raise ProtocolError("last_move 缺少字段") from exc
    row, col = _parse_coord([row, col], "last_move")
    return Move(row=row, col=col, player=player)


def _parse_winning_line(value: Any) -> tuple[tuple[int, int], ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProtocolError("winning_line 必须是坐标数组")
    return tuple(_parse_coord(item, "winning_line[]") for item in value)


def parse_game_state(payload: Mapping[str, Any]) -> GameSnapshot:
    """校验并转换服务器权威局面快照。"""
    if not isinstance(payload, Mapping):
        raise ProtocolError("game_state payload 必须是对象")

    try:
        game_id = payload["game_id"]
        board = _parse_board(payload["board"])
        current_player = _parse_player(
            payload["current_player"], "current_player"
        )
        move_no = payload["move_no"]
        status = payload["status"]
    except KeyError as exc:
        raise ProtocolError(f"game_state 缺少字段：{exc.args[0]}") from exc

    # 观战者没有执子颜色：只有 is_spectator 为真时才允许 your_color 缺省。
    is_spectator = payload.get("is_spectator", False)
    if not isinstance(is_spectator, bool):
        raise ProtocolError("is_spectator 必须是布尔值")
    raw_color = payload.get("your_color")
    if raw_color is None:
        if not is_spectator:
            raise ProtocolError("your_color 必须是 'black' 或 'white'")
        your_color = None
    else:
        your_color = _parse_player(raw_color, "your_color")

    if not isinstance(game_id, str) or not game_id.strip():
        raise ProtocolError("game_id 必须是非空字符串")
    if not isinstance(move_no, int) or move_no < 0:
        raise ProtocolError("move_no 必须是非负整数")

    if status == "playing":
        winner = None
        is_draw = False
        is_game_over = False
    elif status == "black_won":
        winner = Player.BLACK
        is_draw = False
        is_game_over = True
    elif status == "white_won":
        winner = Player.WHITE
        is_draw = False
        is_game_over = True
    elif status == "draw":
        winner = None
        is_draw = True
        is_game_over = True
    else:
        raise ProtocolError("status 必须是 playing/black_won/white_won/draw")

    last_move = _parse_last_move(payload.get("last_move"))
    winning_line = _parse_winning_line(payload.get("winning_line"))

    room_code = payload.get("room_code", "")
    black_name = payload.get("black_name", "")
    white_name = payload.get("white_name", "")
    if not isinstance(room_code, str) or not isinstance(black_name, str) or not isinstance(white_name, str):
        raise ProtocolError("room_code/black_name/white_name 必须是字符串")

    black_user_id = payload.get("black_user_id", "")
    white_user_id = payload.get("white_user_id", "")
    black_avatar = payload.get("black_avatar", "")
    white_avatar = payload.get("white_avatar", "")
    if not all(
        isinstance(value, str)
        for value in (
            black_user_id,
            white_user_id,
            black_avatar,
            white_avatar,
        )
    ):
        raise ProtocolError("用户 id/头像字段必须是字符串")
    black_avatar = normalize_avatar(black_avatar, black_user_id)
    white_avatar = normalize_avatar(white_avatar, white_user_id)

    def optional_int(field: str) -> int | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, int) or value < 0:
            raise ProtocolError(f"{field} 必须是非负整数或 null")
        return value

    opponent_online = payload.get("opponent_online")
    if opponent_online is not None and not isinstance(opponent_online, bool):
        raise ProtocolError("opponent_online 必须是布尔值或 null")

    rated = payload.get("rated", False)
    if not isinstance(rated, bool):
        raise ProtocolError("rated 必须是布尔值")

    def optional_rating(field: str) -> int | None:
        value = payload.get(field)
        if value is None:
            return None
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ProtocolError(f"{field} 必须是非负整数或 null")
        return value

    rating_delta = payload.get("rating_delta")
    if rating_delta is not None and (
        not isinstance(rating_delta, int)
        or isinstance(rating_delta, bool)
    ):
        raise ProtocolError("rating_delta 必须是整数或 null")

    state_version = optional_int("state_version") or 0
    result_reason = payload.get("result_reason", "")
    undo_requester_id = payload.get("undo_requester_id", "")
    if not isinstance(result_reason, str) or not isinstance(
        undo_requester_id, str
    ):
        raise ProtocolError("result_reason/undo_requester_id 必须是字符串")

    black_avatar_version = optional_int("black_avatar_version") or 0
    white_avatar_version = optional_int("white_avatar_version") or 0
    black_achievements = normalize_achievement_keys(
        payload.get("black_achievements", ())
    )
    white_achievements = normalize_achievement_keys(
        payload.get("white_achievements", ())
    )
    your_new_achievements = normalize_achievement_keys(
        payload.get("your_new_achievements", ())
    )

    return GameSnapshot(
        game_id=game_id,
        board=board,
        current_player=current_player,
        last_move=last_move,
        winner=winner,
        winning_line=winning_line,
        is_draw=is_draw,
        is_game_over=is_game_over,
        move_no=move_no,
        your_color=your_color,
        state_version=state_version,
        result_reason=result_reason.strip(),
        undo_requester_id=undo_requester_id,
        room_code=room_code,
        black_name=black_name,
        white_name=white_name,
        black_user_id=black_user_id,
        white_user_id=white_user_id,
        black_avatar=black_avatar,
        white_avatar=white_avatar,
        black_avatar_version=black_avatar_version,
        white_avatar_version=white_avatar_version,
        black_achievements=black_achievements,
        white_achievements=white_achievements,
        your_new_achievements=your_new_achievements,
        turn_deadline_ms=optional_int("turn_deadline_ms"),
        server_now_ms=optional_int("server_now_ms"),
        move_timeout_seconds=optional_int("move_timeout_seconds"),
        opponent_online=opponent_online,
        rated=rated,
        your_rating_before=optional_rating("your_rating_before"),
        your_rating_after=optional_rating("your_rating_after"),
        opponent_rating_before=optional_rating("opponent_rating_before"),
        opponent_rating_after=optional_rating("opponent_rating_after"),
        rating_delta=rating_delta,
        is_spectator=is_spectator,
    )


def make_move_intent(row: int, col: int) -> dict[str, int]:
    """构造客户端落子意图 payload；服务端仍须重新校验。"""
    if (
        not isinstance(row, int)
        or not isinstance(col, int)
        or not 0 <= row < BOARD_SIZE
        or not 0 <= col < BOARD_SIZE
    ):
        raise ProtocolError("落子坐标非法")
    return {"row": row, "col": col}


def make_message(
    message_type: str,
    payload: Mapping[str, Any],
    request_id: Optional[str] = None,
) -> dict[str, Any]:
    """构造协议 envelope。"""
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("消息类型不能为空")
    if not isinstance(payload, Mapping):
        raise ProtocolError("payload 必须是对象")

    message: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "type": message_type,
        "payload": dict(payload),
    }
    if request_id is not None:
        if not isinstance(request_id, str) or not request_id:
            raise ProtocolError("request_id 必须是非空字符串")
        message["request_id"] = request_id
    return message


def parse_message(raw: Any) -> dict[str, Any]:
    """校验 envelope 并返回原始 dict；未知类型交由会话决定是否忽略。"""
    if not isinstance(raw, Mapping):
        raise ProtocolError("协议消息必须是对象")
    if raw.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("协议版本不兼容")

    message_type = raw.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise ProtocolError("消息缺少 type")
    if message_type not in _ALLOWED_MESSAGE_TYPES:
        raise ProtocolError(f"未知消息类型：{message_type}")

    payload = raw.get("payload")
    if not isinstance(payload, Mapping):
        raise ProtocolError("消息 payload 必须是对象")

    request_id = raw.get("request_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id
    ):
        raise ProtocolError("request_id 必须是非空字符串")

    return dict(raw)


__all__ = [
    "PROTOCOL_VERSION",
    "CLIENT_AUTH",
    "CLIENT_CREATE_ROOM",
    "CLIENT_JOIN_ROOM",
    "CLIENT_MOVE_INTENT",
    "CLIENT_RESIGN",
    "CLIENT_LEAVE_ROOM",
    "CLIENT_REMATCH",
    "CLIENT_UNDO_REQUEST",
    "CLIENT_UNDO_RESPONSE",
    "CLIENT_QUICK_MATCH",
    "CLIENT_CANCEL_MATCH",
    "CLIENT_PING",
    "CLIENT_CHAT",
    "CLIENT_INVITE_TO_ROOM",
    "CLIENT_RESPOND_ROOM_INVITE",
    "CLIENT_WATCH_GAME",
    "SERVER_AUTHENTICATED",
    "SERVER_ROOM_CREATED",
    "SERVER_GAME_STATE",
    "SERVER_MOVE_REJECTED",
    "SERVER_OPPONENT_STATUS",
    "SERVER_ROOM_CLOSED",
    "SERVER_REMATCH_STATUS",
    "SERVER_UNDO_STATUS",
    "SERVER_QUEUE_STATUS",
    "SERVER_MATCH_FOUND",
    "SERVER_PONG",
    "SERVER_ERROR",
    "SERVER_CHAT",
    "SERVER_ROOM_INVITATION",
    "SERVER_ROOM_INVITE_RESULT",
    "SERVER_FRIEND_EVENT",
    "SERVER_DIRECT_MESSAGE",
    "SERVER_LIVE_GAMES_CHANGED",
    "SERVER_MESSAGE_TYPES",
    "GameSnapshot",
    "parse_game_state",
    "make_move_intent",
    "make_message",
    "parse_message",
]
