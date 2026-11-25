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
from gomoku.shared.rules import (
    BOARD_SIZE,
    NON_BOARD_ENDINGS,
    RESULT_REASON_FIVE,
    WIN_DIRECTIONS,
    WIN_LENGTH,
    Cell,
    Move,
    Player,
    RuleMode,
    find_winning_line,
    in_bounds,
    parse_player,
    parse_rule_mode,
)
from gomoku.shared.time_control import (
    TimeControl,
    time_control_from_snapshot,
)

PROTOCOL_VERSION = 1

#: request_id 的最大长度。服务端处理落子请求时也用同一个上限，
#: 过长的一律拒绝，避免超大字段占用带宽与日志。
MAX_REQUEST_ID_LENGTH = 64

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
# 同观：观战者之间的小频道，只在观战席内部广播，棋手永远收不到。
CLIENT_SPECTATOR_CHAT = "spectator_chat"

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
# 同观频道的一条消息，只发给同一房间的观战者。
SERVER_SPECTATOR_CHAT = "spectator_chat_message"

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
    CLIENT_SPECTATOR_CHAT,
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
    SERVER_SPECTATOR_CHAT,
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
        SERVER_SPECTATOR_CHAT,
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
    # 本局悔棋配额与冷却（由服务端按当前用户计算）。
    undo_remaining: int = 0
    undo_limit: int = 0
    undo_cooldown_until_ms: int | None = None
    undo_cooldown_seconds: int = 0
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
    # 本局时限设置。旧服务端不设置时，客户端用 move_timeout_seconds 推导。
    time_control: Optional[TimeControl] = None
    # Fischer 时钟双方当前余量（毫秒，快照生成瞬间的值）。
    # 每手模式不使用这两个字段，改用 turn_deadline_ms 显示本手倒计时。
    black_remaining_ms: Optional[int] = None
    white_remaining_ms: Optional[int] = None
    # 本局规则模式：freestyle（默认）或 renju。旧服务端不下发时按自由
    # 规则处理，保持向后兼容。
    rule_mode: RuleMode = RuleMode.FREESTYLE


def rule_mode_from_payload(value: object) -> RuleMode:
    """解析协议里的可选 ``rule_mode`` 字段。

    缺省（``None``）按自由规则处理；显式给出时必须是 ``"freestyle"``、
    ``"renju"`` 或 :class:`RuleMode`，其余值一律抛 :class:`ProtocolError`，
    避免服务端与客户端悄悄采用不同的规则。
    """
    if value is None:
        return RuleMode.FREESTYLE
    try:
        return parse_rule_mode(value)
    except ValueError as exc:
        raise ProtocolError("rule_mode 必须是 'freestyle' 或 'renju'") from exc


def _parse_player(value: Any, field: str) -> Player:
    return parse_player(value, ProtocolError, field)


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
    # bool 是 int 的子类，必须用精确类型判断，否则 True 会被当成第 1 行。
    if type(row) is not int or type(col) is not int:
        raise ProtocolError(f"{field} 坐标必须是整数")
    if not in_bounds(row, col):
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


# ---------------------------------------------------------------------------
# 快照字段族
#
# 快照字段很多，按“字段族”拆成几个小解析器：身份/状态/局面/房间信息/
# 时钟/悔棋/排位/成就。每族只关心自己那几个字段，出错时的提示也更精确。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _IdentityFields:
    """对局身份、执子方与步数。"""

    game_id: str
    current_player: Player
    your_color: Optional[Player]
    is_spectator: bool
    status: str
    move_no: int


@dataclass(frozen=True)
class _PositionFields:
    """棋盘、最后一手与胜利连线。"""

    board: tuple[tuple[Cell, ...], ...]
    last_move: Optional[Move]
    winning_line: tuple[tuple[int, int], ...]


@dataclass(frozen=True)
class _RoomFields:
    """房间与双方玩家的展示信息。"""

    room_code: str
    black_name: str
    white_name: str
    black_user_id: str
    white_user_id: str
    black_avatar: str
    white_avatar: str
    black_avatar_version: int
    white_avatar_version: int


@dataclass(frozen=True)
class _ClockFields:
    """时限设置与时钟余量。"""

    time_control: Optional[TimeControl]
    move_timeout_seconds: Optional[int]
    turn_deadline_ms: Optional[int]
    server_now_ms: Optional[int]
    black_remaining_ms: Optional[int]
    white_remaining_ms: Optional[int]


@dataclass(frozen=True)
class _UndoFields:
    """终局原因与悔棋配额。"""

    state_version: int
    result_reason: str
    undo_requester_id: str
    undo_remaining: int
    undo_limit: int
    undo_cooldown_until_ms: Optional[int]
    undo_cooldown_seconds: int


@dataclass(frozen=True)
class _RankingFields:
    """排位积分信息。"""

    rated: bool
    your_rating_before: Optional[int]
    your_rating_after: Optional[int]
    opponent_rating_before: Optional[int]
    opponent_rating_after: Optional[int]
    rating_delta: Optional[int]


def _optional_int(
    payload: Mapping[str, Any], field: str
) -> Optional[int]:
    """读取可选的非负整数字段；缺省或 null 返回 None。"""
    value = payload.get(field)
    if value is None:
        return None
    # bool 是 int 的子类，必须显式排除。
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError(f"{field} 必须是非负整数或 null")
    return value


def _optional_rating(
    payload: Mapping[str, Any], field: str
) -> Optional[int]:
    """读取可选的积分字段（同样只接受非负整数）。"""
    return _optional_int(payload, field)


def _parse_identity_fields(payload: Mapping[str, Any]) -> _IdentityFields:
    """解析对局身份、执子方、状态与步数。"""
    try:
        raw_game_id = payload["game_id"]
        raw_current_player = payload["current_player"]
        raw_move_no = payload["move_no"]
        raw_status = payload["status"]
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

    if not isinstance(raw_game_id, str) or not raw_game_id.strip():
        raise ProtocolError("game_id 必须是非空字符串")
    if type(raw_move_no) is not int or raw_move_no < 0:
        raise ProtocolError("move_no 必须是非负整数")
    if not isinstance(raw_status, str):
        raise ProtocolError("status 必须是 playing/black_won/white_won/draw")

    return _IdentityFields(
        game_id=raw_game_id,
        current_player=_parse_player(
            raw_current_player, "current_player"
        ),
        your_color=your_color,
        is_spectator=is_spectator,
        status=raw_status,
        move_no=raw_move_no,
    )


def _parse_status_fields(
    status: str,
) -> tuple[Optional[Player], bool, bool]:
    """把状态字符串翻译成 (胜者, 是否平局, 是否终局)。"""
    if status == "playing":
        return None, False, False
    if status == "black_won":
        return Player.BLACK, False, True
    if status == "white_won":
        return Player.WHITE, False, True
    if status == "draw":
        return None, True, True
    raise ProtocolError("status 必须是 playing/black_won/white_won/draw")


def _parse_position_fields(payload: Mapping[str, Any]) -> _PositionFields:
    """解析棋盘、最后一手与胜利连线。"""
    try:
        raw_board = payload["board"]
    except KeyError as exc:
        raise ProtocolError(f"game_state 缺少字段：{exc.args[0]}") from exc
    return _PositionFields(
        board=_parse_board(raw_board),
        last_move=_parse_last_move(payload.get("last_move")),
        winning_line=_parse_winning_line(payload.get("winning_line")),
    )


def _parse_room_fields(payload: Mapping[str, Any]) -> _RoomFields:
    """解析房间号、昵称、用户 id 与头像。"""
    room_code = payload.get("room_code", "")
    black_name = payload.get("black_name", "")
    white_name = payload.get("white_name", "")
    if (
        not isinstance(room_code, str)
        or not isinstance(black_name, str)
        or not isinstance(white_name, str)
    ):
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

    return _RoomFields(
        room_code=room_code,
        black_name=black_name,
        white_name=white_name,
        black_user_id=black_user_id,
        white_user_id=white_user_id,
        black_avatar=normalize_avatar(black_avatar, black_user_id),
        white_avatar=normalize_avatar(white_avatar, white_user_id),
        black_avatar_version=_optional_int(
            payload, "black_avatar_version"
        )
        or 0,
        white_avatar_version=_optional_int(
            payload, "white_avatar_version"
        )
        or 0,
    )


def _parse_clock_fields(payload: Mapping[str, Any]) -> _ClockFields:
    """解析时限设置、倒计时与 Fischer 时钟余量。"""
    move_timeout_seconds = _optional_int(payload, "move_timeout_seconds")
    time_control = time_control_from_snapshot(
        payload.get("time_control"), move_timeout_seconds
    )
    if time_control is not None and time_control.mode == "per_move":
        # 旧字段与 time_control 同时存在时以结构化字段为准。
        move_timeout_seconds = time_control.turn_seconds
    elif time_control is not None and time_control.is_fischer:
        move_timeout_seconds = None
    return _ClockFields(
        time_control=time_control,
        move_timeout_seconds=move_timeout_seconds,
        turn_deadline_ms=_optional_int(payload, "turn_deadline_ms"),
        server_now_ms=_optional_int(payload, "server_now_ms"),
        black_remaining_ms=_optional_int(payload, "black_remaining_ms"),
        white_remaining_ms=_optional_int(payload, "white_remaining_ms"),
    )


def _parse_undo_fields(payload: Mapping[str, Any]) -> _UndoFields:
    """解析终局原因、局面版本与悔棋配额。"""
    result_reason = payload.get("result_reason", "")
    undo_requester_id = payload.get("undo_requester_id", "")
    if not isinstance(result_reason, str) or not isinstance(
        undo_requester_id, str
    ):
        raise ProtocolError("result_reason/undo_requester_id 必须是字符串")
    return _UndoFields(
        state_version=_optional_int(payload, "state_version") or 0,
        result_reason=result_reason.strip(),
        undo_requester_id=undo_requester_id,
        undo_remaining=_optional_int(payload, "undo_remaining") or 0,
        undo_limit=_optional_int(payload, "undo_limit") or 0,
        undo_cooldown_until_ms=_optional_int(
            payload, "undo_cooldown_until_ms"
        ),
        undo_cooldown_seconds=(
            _optional_int(payload, "undo_cooldown_seconds") or 0
        ),
    )


def _parse_ranking_fields(payload: Mapping[str, Any]) -> _RankingFields:
    """解析排位标记与双方积分。"""
    rated = payload.get("rated", False)
    if not isinstance(rated, bool):
        raise ProtocolError("rated 必须是布尔值")
    rating_delta = payload.get("rating_delta")
    if rating_delta is not None and (
        not isinstance(rating_delta, int)
        or isinstance(rating_delta, bool)
    ):
        raise ProtocolError("rating_delta 必须是整数或 null")
    return _RankingFields(
        rated=rated,
        your_rating_before=_optional_rating(payload, "your_rating_before"),
        your_rating_after=_optional_rating(payload, "your_rating_after"),
        opponent_rating_before=_optional_rating(
            payload, "opponent_rating_before"
        ),
        opponent_rating_after=_optional_rating(
            payload, "opponent_rating_after"
        ),
        rating_delta=rating_delta,
    )


def _count_stones(board: tuple[tuple[Cell, ...], ...]) -> int:
    """统计棋盘上的棋子数。"""
    return sum(
        1 for row in board for cell in row if cell is not None
    )


def _board_has_five(
    board: tuple[tuple[Cell, ...], ...], player: Player
) -> bool:
    """扫描整块棋盘，判断 ``player`` 是否存在五连。"""
    for row_index, row in enumerate(board):
        for col_index, cell in enumerate(row):
            if cell is not player:
                continue
            if find_winning_line(board, row_index, col_index, player):
                return True
    return False


def _find_winner_line(
    board: tuple[tuple[Cell, ...], ...],
    winner: Player,
    last_move: Optional[Move],
) -> list[tuple[int, int]]:
    """找出 ``winner`` 在棋盘上的五连；优先用最后一手作为锚点。"""
    if last_move is not None and last_move.player is winner:
        line = find_winning_line(
            board, last_move.row, last_move.col, winner
        )
        if line:
            return line
    for row_index, row in enumerate(board):
        for col_index, cell in enumerate(row):
            if cell is not winner:
                continue
            line = find_winning_line(
                board, row_index, col_index, winner
            )
            if line:
                return line
    return []


def _validate_winning_line(
    board: tuple[tuple[Cell, ...], ...],
    winning_line: tuple[tuple[int, int], ...],
    winner: Optional[Player],
) -> None:
    """校验胜利连线：坐标在界内、颜色一致、连续且长度达标。"""
    if not winning_line:
        return
    if winner is None:
        raise ProtocolError("出现胜利连线但没有胜者")
    if len(winning_line) < WIN_LENGTH:
        raise ProtocolError("胜利连线长度不足五颗")
    for row, col in winning_line:
        if not in_bounds(row, col):
            raise ProtocolError("胜利连线坐标越界")
        if board[row][col] is not winner:
            raise ProtocolError("胜利连线上的棋子颜色与胜者不一致")

    # 连线必须沿同一个方向连续排列。
    step = (
        winning_line[1][0] - winning_line[0][0],
        winning_line[1][1] - winning_line[0][1],
    )
    reversed_step = (-step[0], -step[1])
    if step not in WIN_DIRECTIONS and reversed_step not in WIN_DIRECTIONS:
        raise ProtocolError("胜利连线不连续")
    for index in range(2, len(winning_line)):
        current = (
            winning_line[index][0] - winning_line[index - 1][0],
            winning_line[index][1] - winning_line[index - 1][1],
        )
        if current != step:
            raise ProtocolError("胜利连线不连续")


def _validate_snapshot_consistency(
    status: str,
    board: tuple[tuple[Cell, ...], ...],
    winner: Optional[Player],
    winning_line: tuple[tuple[int, int], ...],
    last_move: Optional[Move],
    move_no: int,
    result_reason: str,
) -> None:
    """交叉校验快照各字段是否互相自洽。

    服务端是权威方，正常情况下这些字段必然一致；这里做的是防御性校验，
    避免异常快照把界面置于矛盾状态（例如界面显示黑方获胜，棋盘上却是
    白方的五连）。

    校验口径完全对齐服务端真实行为：

    * ``move_no`` 是“已落子数”，悔棋会同时减少棋盘上的棋子和步数，
      因此两者必须相等；
    * 只有 ``result_reason == "five"``（棋盘分出胜负）时，棋盘上才必须
      真的有五连；认输/超时/逃跑结束的棋局，棋盘上本来就没有五连；
    * ``result_reason`` 为空是为了兼容不带该字段的旧服务端，此时只做
      “连线自洽 + 不能有另一方五连”的宽松校验。
    """
    stones = _count_stones(board)
    if move_no != stones:
        raise ProtocolError(
            f"move_no({move_no}) 与棋盘上的棋子数({stones})不一致"
        )

    if status in ("playing", "draw"):
        if winning_line:
            raise ProtocolError(
                "对局进行中不应出现胜利连线"
                if status == "playing"
                else "和棋不应出现胜利连线"
            )
        # 只检查 winning_line 还不够：只要服务端声明 playing/draw，
        # 就必须保证棋盘上确实没有五连，否则伪造的 status 会蒙混过关。
        if _board_has_five(board, Player.BLACK) or _board_has_five(
            board, Player.WHITE
        ):
            raise ProtocolError("对局尚未判定获胜，但棋盘上已有五连")

    if status in ("black_won", "white_won"):
        if winner is None:
            raise ProtocolError("终局状态缺少胜者")
        if result_reason == RESULT_REASON_FIVE:
            # 棋盘分出胜负：棋盘上必须真的有该颜色的五连。
            if not _find_winner_line(board, winner, last_move):
                raise ProtocolError(
                    "状态声明棋盘获胜，但棋盘上没有对应的五连"
                )
        elif result_reason in NON_BOARD_ENDINGS:
            # 认输/超时/逃跑：棋盘上不会有五连，也就不该有胜利连线。
            if winning_line:
                raise ProtocolError(
                    "认输/超时/逃跑结束的终局不应出现胜利连线"
                )
            if _board_has_five(board, winner.opponent):
                raise ProtocolError(
                    "状态声明的胜者与棋盘上的五连不一致"
                )
        else:
            # 兼容不带 result_reason 的旧服务端：连线本身仍要自洽，
            # 棋盘上也不能出现与胜者矛盾的另一方五连。
            if _board_has_five(board, winner.opponent):
                raise ProtocolError(
                    "状态声明的胜者与棋盘上的五连不一致"
                )

    _validate_winning_line(board, winning_line, winner)


def parse_game_state(payload: Mapping[str, Any]) -> GameSnapshot:
    """校验并转换服务器权威局面快照。

    逐字段校验之外还会做交叉校验（步数与棋子数、状态与胜利连线、胜者
    与棋盘），字段互相矛盾时抛 ``ProtocolError``。
    """
    if not isinstance(payload, Mapping):
        raise ProtocolError("game_state payload 必须是对象")

    identity = _parse_identity_fields(payload)
    winner, is_draw, is_game_over = _parse_status_fields(
        identity.status
    )
    position = _parse_position_fields(payload)
    room = _parse_room_fields(payload)
    clock = _parse_clock_fields(payload)
    undo = _parse_undo_fields(payload)
    ranking = _parse_ranking_fields(payload)

    opponent_online = payload.get("opponent_online")
    if opponent_online is not None and not isinstance(opponent_online, bool):
        raise ProtocolError("opponent_online 必须是布尔值或 null")

    black_achievements = normalize_achievement_keys(
        payload.get("black_achievements", ())
    )
    white_achievements = normalize_achievement_keys(
        payload.get("white_achievements", ())
    )
    your_new_achievements = normalize_achievement_keys(
        payload.get("your_new_achievements", ())
    )

    _validate_snapshot_consistency(
        status=identity.status,
        board=position.board,
        winner=winner,
        winning_line=position.winning_line,
        last_move=position.last_move,
        move_no=identity.move_no,
        result_reason=undo.result_reason,
    )

    return GameSnapshot(
        game_id=identity.game_id,
        board=position.board,
        current_player=identity.current_player,
        last_move=position.last_move,
        winner=winner,
        winning_line=position.winning_line,
        is_draw=is_draw,
        is_game_over=is_game_over,
        move_no=identity.move_no,
        your_color=identity.your_color,
        state_version=undo.state_version,
        result_reason=undo.result_reason,
        undo_requester_id=undo.undo_requester_id,
        undo_remaining=undo.undo_remaining,
        undo_limit=undo.undo_limit,
        undo_cooldown_until_ms=undo.undo_cooldown_until_ms,
        undo_cooldown_seconds=undo.undo_cooldown_seconds,
        room_code=room.room_code,
        black_name=room.black_name,
        white_name=room.white_name,
        black_user_id=room.black_user_id,
        white_user_id=room.white_user_id,
        black_avatar=room.black_avatar,
        white_avatar=room.white_avatar,
        black_avatar_version=room.black_avatar_version,
        white_avatar_version=room.white_avatar_version,
        black_achievements=black_achievements,
        white_achievements=white_achievements,
        your_new_achievements=your_new_achievements,
        turn_deadline_ms=clock.turn_deadline_ms,
        server_now_ms=clock.server_now_ms,
        move_timeout_seconds=clock.move_timeout_seconds,
        opponent_online=opponent_online,
        rated=ranking.rated,
        your_rating_before=ranking.your_rating_before,
        your_rating_after=ranking.your_rating_after,
        opponent_rating_before=ranking.opponent_rating_before,
        opponent_rating_after=ranking.opponent_rating_after,
        rating_delta=ranking.rating_delta,
        is_spectator=identity.is_spectator,
        time_control=clock.time_control,
        black_remaining_ms=clock.black_remaining_ms,
        white_remaining_ms=clock.white_remaining_ms,
        rule_mode=rule_mode_from_payload(payload.get("rule_mode")),
    )


def make_move_intent(row: int, col: int) -> dict[str, int]:
    """构造客户端落子意图 payload；服务端仍须重新校验。"""
    # bool 是 int 的子类，必须用精确类型判断。
    if type(row) is not int or type(col) is not int or not in_bounds(row, col):
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
        if (
            not isinstance(request_id, str)
            or not request_id
            or len(request_id) > MAX_REQUEST_ID_LENGTH
        ):
            raise ProtocolError(
                "request_id 必须是不超过 "
                f"{MAX_REQUEST_ID_LENGTH} 个字符的非空字符串"
            )
        message["request_id"] = request_id
    return message


def parse_message(raw: Any) -> dict[str, Any]:
    """校验 envelope 并返回原始 dict。

    未知消息类型一律抛 ``ProtocolError``：解析器只负责“认得的消息必须
    合法”。客户端需要向前兼容时，在调用本函数**之前**用
    :data:`SERVER_MESSAGE_TYPES` 过滤掉不认识的类型（``OnlineSession``
    就是这么做的），未知的服务端消息因此会被安静忽略，而不是打扰玩家。
    """
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
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > MAX_REQUEST_ID_LENGTH
    ):
        raise ProtocolError(
            "request_id 必须是不超过 "
            f"{MAX_REQUEST_ID_LENGTH} 个字符的非空字符串"
        )

    return dict(raw)


__all__ = [
    "MAX_REQUEST_ID_LENGTH",
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
    "CLIENT_SPECTATOR_CHAT",
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
    "SERVER_SPECTATOR_CHAT",
    "SERVER_MESSAGE_TYPES",
    "GameSnapshot",
    "rule_mode_from_payload",
    "parse_game_state",
    "make_move_intent",
    "make_message",
    "parse_message",
]
