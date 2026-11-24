"""五子棋核心规则：棋手、坐标、胜负判定与棋盘约束。

本模块完全不依赖 Qt，可被 Windows 客户端、AI 和未来 FastAPI 服务端复用。
坐标约定：``row`` 从上到下为 0～14，``col`` 从左到右为 0～14。

规则模式
--------

- :attr:`RuleMode.FREESTYLE`（默认）：休闲自由规则，任意一方连续 5 颗及
  以上即获胜，不设禁手。现有对局、AI 与棋谱都沿用该默认值。
- :attr:`RuleMode.RENJU`（可选）：日式连珠规则，黑棋禁止长连（6 颗及
  以上）、四四和禁手三三；白棋不设禁手，长连仍算获胜。黑棋只有恰好
  5 颗才算胜利。

连珠禁手检测是**简化近似**：只检查通过候选落点的连续线段，扫描范围
限定在落点周围 ±4 格，覆盖长连、“恰好五连”的四四威胁和“活四”的三三
威胁；不识别跳三、跳四等带空隙的复合棋形，因此可能漏判部分真实禁手。
本实现只拒绝能够明确判定的禁手，不引入半套棋形去误伤普通合法落子。
若以后需要完整连珠规则，应替换 :func:`is_forbidden_move` 的实现而保持
调用方接口不变。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Sequence

# ---------------------------------------------------------------------------
# 棋盘规则常量
# ---------------------------------------------------------------------------
BOARD_SIZE = 15
WIN_LENGTH = 5
BOARD_CELL_COUNT = BOARD_SIZE * BOARD_SIZE

# 判定五连时检查的四个方向：横、竖、主对角线、副对角线。
WIN_DIRECTIONS: tuple[tuple[int, int], ...] = (
    (0, 1),
    (1, 0),
    (1, 1),
    (1, -1),
)

#: 终局原因：棋盘上形成五连。
RESULT_REASON_FIVE = "five"
#: 终局原因：棋盘下满且无人获胜。
RESULT_REASON_DRAW = "draw"
#: 终局原因：认输。
RESULT_REASON_RESIGN = "resign"
#: 终局原因：超时判负。
RESULT_REASON_TIMEOUT = "timeout"
#: 终局原因：掉线逃跑判负。
RESULT_REASON_ESCAPE = "escape"

#: 全部已知的终局原因。
RESULT_REASONS = frozenset(
    {
        RESULT_REASON_FIVE,
        RESULT_REASON_DRAW,
        RESULT_REASON_RESIGN,
        RESULT_REASON_TIMEOUT,
        RESULT_REASON_ESCAPE,
    }
)

#: 棋盘没有分出胜负、由外部事件结束的原因。这类对局可以有胜者，但棋盘上
#: 不会出现五连。
NON_BOARD_ENDINGS = frozenset(
    {
        RESULT_REASON_RESIGN,
        RESULT_REASON_TIMEOUT,
        RESULT_REASON_ESCAPE,
    }
)


class Player(Enum):
    """棋手（同时也是棋子颜色）。"""

    BLACK = "black"
    WHITE = "white"

    @property
    def label(self) -> str:
        """返回适合界面展示的中文名称。"""
        return "黑棋" if self is Player.BLACK else "白棋"

    @property
    def opponent(self) -> "Player":
        """返回另一方棋手。"""
        return Player.WHITE if self is Player.BLACK else Player.BLACK


class RuleMode(Enum):
    """对局采用的规则模式。

    - ``FREESTYLE``：休闲自由规则，连续 5 颗及以上即获胜，无禁手。
    - ``RENJU``：日式连珠规则，只对黑棋施加长连、四四、三三禁手。
    """

    FREESTYLE = "freestyle"
    RENJU = "renju"


#: 默认规则模式：保持既有自由规则行为不变。
DEFAULT_RULE_MODE = RuleMode.FREESTYLE


def parse_rule_mode(value: object) -> RuleMode:
    """把 ``"freestyle"`` / ``"renju"``（或 :class:`RuleMode`）解析成规则模式。

    非法输入统一抛出 ``ValueError``，避免调用方悄悄退回某个默认模式而
    产生规则分歧。
    """
    if isinstance(value, RuleMode):
        return value
    if value == "freestyle":
        return RuleMode.FREESTYLE
    if value == "renju":
        return RuleMode.RENJU
    raise ValueError("规则模式必须是 'freestyle' 或 'renju'")


@dataclass(frozen=True)
class Move:
    """一手棋的不可变记录，用于悔棋与“最后一手”标记。"""

    row: int
    col: int
    player: Player


# 单个交叉点：空位为 None，否则为黑/白棋。
Cell = Optional[Player]
# 只读或可变的二维棋盘都可使用。
BoardLike = Sequence[Sequence[Cell]]


def in_bounds(row: int, col: int, size: int = BOARD_SIZE) -> bool:
    """判断坐标是否落在棋盘范围内。

    坐标必须是精确的 ``int``：``bool`` 是 ``int`` 的子类，用
    ``type(row) is int`` 判定可以把它挡在棋盘之外，避免 ``True`` 被当成
    第 1 行。
    """
    if type(size) is not int:
        return False
    if type(row) is not int or type(col) is not int:
        return False
    return 0 <= row < size and 0 <= col < size


def find_winning_line(
    board: BoardLike,
    row: int,
    col: int,
    player: Optional[Player],
) -> list[tuple[int, int]]:
    """检查指定落子是否形成五连，并返回完整连续线。

    该函数只依赖棋盘与最后一手，不修改状态，供客户端、AI 和未来服务端
    共用。规则采用休闲自由五子棋：连续 5 颗及以上同色棋子都算获胜。

    参数：
        board: 二维棋盘，空位为 None。
        row: 最后落子的行号。
        col: 最后落子的列号。
        player: 最后落子的棋手。传入 ``None`` 表示“不知道是谁落的子”，
            此时一律返回空列表——否则空点会被误当成同色棋子，伪造出
            一条并不存在的胜利连线。

    返回：
        若存在五连，返回从一端到另一端的所有连续同色棋子坐标；
        否则返回空列表。
    """
    # 没有棋手就没有判定依据：宁可判定“没有五连”，也不能凭空造一条。
    if player is None:
        return []
    if not in_bounds(row, col):
        return []
    # 该函数既用于真实落子，也可能被 AI 用于试探；目标点必须确属该棋手。
    if board[row][col] is not player:
        return []

    def is_same_stone(r: int, c: int) -> bool:
        """判断棋盘内某一点是否属于当前检查的棋手。"""
        return (
            player is not None
            and in_bounds(r, c)
            and board[r][c] is player
        )

    for dr, dc in WIN_DIRECTIONS:
        backward: list[tuple[int, int]] = []
        forward: list[tuple[int, int]] = []

        r, c = row - dr, col - dc
        while is_same_stone(r, c):
            backward.append((r, c))
            r -= dr
            c -= dc

        r, c = row + dr, col + dc
        while is_same_stone(r, c):
            forward.append((r, c))
            r += dr
            c += dc

        line = list(reversed(backward)) + [(row, col)] + forward
        if len(line) >= WIN_LENGTH:
            return line

    return []


def would_win(
    board: BoardLike,
    row: int,
    col: int,
    player: Optional[Player],
) -> bool:
    """判断在 ``(row, col)`` 落下 ``player`` 是否能形成五连。

    直接在原棋盘上按四个方向统计连续同色棋子，不复制棋盘、不修改状态，
    判定口径与 :func:`find_winning_line` 一致：连续 5 颗及以上即获胜
    （自由规则不设禁手）。目标点已有棋子时视为非法落子，返回 ``False``。

    这是 AI、威胁搜索与规则层共用的唯一“落子即胜”实现；热路径上一局
    要调用成千上万次，因此必须保持无分配、无复制的形态。
    """
    if player is None:
        return False
    if not in_bounds(row, col):
        return False
    if board[row][col] is not None:
        return False

    for dr, dc in WIN_DIRECTIONS:
        count = 1
        r, c = row + dr, col + dc
        while in_bounds(r, c) and board[r][c] is player:
            count += 1
            r += dr
            c += dc
        r, c = row - dr, col - dc
        while in_bounds(r, c) and board[r][c] is player:
            count += 1
            r -= dr
            c -= dc
        if count >= WIN_LENGTH:
            return True
    return False


#: 禁手检测沿每个方向、从落点向两侧扫描的候选格数。
#: 五连/活四的完成点与落点最多相距 4 格，窗口再大不会增加判定能力。
_FORBIDDEN_SCAN_RADIUS = 4


def _run_span(
    board: BoardLike,
    row: int,
    col: int,
    player: Player,
    dr: int,
    dc: int,
) -> tuple[int, int]:
    """返回 ``(row, col)`` 所在连续同色段沿方向的起止偏移。

    偏移以 ``(row, col)`` 为原点、``(dr, dc)`` 为步长，满足
    ``lo <= 0 <= hi``。调用方必须保证 ``board[row][col]`` 已经是
    ``player`` 的棋子。遍历只沿一条线进行（最多 15 格），不做全盘扫描。
    """
    lo = 0
    while True:
        r, c = row + (lo - 1) * dr, col + (lo - 1) * dc
        if not in_bounds(r, c) or board[r][c] is not player:
            break
        lo -= 1
    hi = 0
    while True:
        r, c = row + (hi + 1) * dr, col + (hi + 1) * dc
        if not in_bounds(r, c) or board[r][c] is not player:
            break
        hi += 1
    return lo, hi


def _creates_exact_five(
    probe: list[list[Cell]],
    row: int,
    col: int,
    dr: int,
    dc: int,
) -> bool:
    """判断沿 ``(dr, dc)`` 是否有让黑棋形成恰好五连的候选空点。

    在 ``probe``（已模拟本手落子）上逐个试落候选点，若通过候选点的
    连续段恰好 5 颗、且包含本手 ``(row, col)``，说明本手在该方向造出
    了一个“四”。连续 6 颗及以上是长连，不算四的威胁。
    """
    for offset in range(-_FORBIDDEN_SCAN_RADIUS, _FORBIDDEN_SCAN_RADIUS + 1):
        if offset == 0:
            continue
        cand_row = row + offset * dr
        cand_col = col + offset * dc
        if not in_bounds(cand_row, cand_col):
            continue
        if probe[cand_row][cand_col] is not None:
            continue
        probe[cand_row][cand_col] = Player.BLACK
        lo, hi = _run_span(probe, cand_row, cand_col, Player.BLACK, dr, dc)
        probe[cand_row][cand_col] = None
        # ``-offset`` 是本手相对候选点的偏移，落在连续段内才算本手参与。
        if hi - lo + 1 == WIN_LENGTH and lo <= -offset <= hi:
            return True
    return False


def _creates_open_four(
    probe: list[list[Cell]],
    row: int,
    col: int,
    dr: int,
    dc: int,
) -> bool:
    """判断沿 ``(dr, dc)`` 是否存在把三变成活四的候选空点。

    候选点试落黑子后，通过它的连续段必须恰好 4 颗、包含本手
    ``(row, col)``，并且两端都在棋盘内且为空。满足时本手在该方向
    构成了一个“活三”威胁。
    """
    for offset in range(-_FORBIDDEN_SCAN_RADIUS, _FORBIDDEN_SCAN_RADIUS + 1):
        if offset == 0:
            continue
        cand_row = row + offset * dr
        cand_col = col + offset * dc
        if not in_bounds(cand_row, cand_col):
            continue
        if probe[cand_row][cand_col] is not None:
            continue
        probe[cand_row][cand_col] = Player.BLACK
        lo, hi = _run_span(probe, cand_row, cand_col, Player.BLACK, dr, dc)
        is_open_four = False
        if hi - lo + 1 == WIN_LENGTH - 1 and lo <= -offset <= hi:
            before_row = cand_row + (lo - 1) * dr
            before_col = cand_col + (lo - 1) * dc
            after_row = cand_row + (hi + 1) * dr
            after_col = cand_col + (hi + 1) * dc
            if (
                in_bounds(before_row, before_col)
                and probe[before_row][before_col] is None
                and in_bounds(after_row, after_col)
                and probe[after_row][after_col] is None
            ):
                is_open_four = True
        probe[cand_row][cand_col] = None
        if is_open_four:
            return True
    return False


def is_forbidden_move(
    board: BoardLike,
    row: int,
    col: int,
    player: Optional[Player],
    mode: RuleMode | str = DEFAULT_RULE_MODE,
) -> bool:
    """判断黑棋在 ``(row, col)`` 落子是否构成连珠禁手。

    参数：
        board: 二维棋盘，空位为 None。
        row: 待落子的行号。
        col: 待落子的列号。
        player: 待落子的棋手。
        mode: 规则模式；默认为自由规则。

    返回：
        自由规则、白棋、越界、已占用或 ``player`` 不是黑棋时返回 ``False``
        （这些合法性判断属于 :class:`GameModel`）；连珠规则下，若该手
        造成长连（任一方向连续 6 颗及以上）、四四（两个不同方向可以
        在下一手形成恰好五连）或三三（两个不同方向可以在下一手形成
        活四），返回 ``True``。

    实现只在落点周围 ±4 格的有限窗口内模拟候选落子，不做全盘扫描；
    不识别跳三、跳四等带空隙的棋形，限制见模块文档。
    """
    if parse_rule_mode(mode) is RuleMode.FREESTYLE:
        return False
    if player is not Player.BLACK:
        return False
    if not in_bounds(row, col):
        return False
    if board[row][col] is not None:
        return False

    # 在副本上模拟本手，避免调用方棋盘是只读的 tuple。
    probe: list[list[Cell]] = [list(board_row) for board_row in board]
    probe[row][col] = Player.BLACK

    # 长连：通过落点的任一方向连续 6 颗及以上。
    for dr, dc in WIN_DIRECTIONS:
        lo, hi = _run_span(probe, row, col, Player.BLACK, dr, dc)
        if hi - lo + 1 > WIN_LENGTH:
            return True

    four_directions = 0
    three_directions = 0
    for dr, dc in WIN_DIRECTIONS:
        if _creates_exact_five(probe, row, col, dr, dc):
            four_directions += 1
        if _creates_open_four(probe, row, col, dr, dc):
            three_directions += 1
        if four_directions >= 2 or three_directions >= 2:
            return True
    return False


def parse_player(
    value: object,
    error_type: type[Exception] = ValueError,
    field: str = "player",
) -> Player:
    """把 ``"black"`` / ``"white"``（或 :class:`Player`）解析成棋手。

    协议层、棋谱层和题库层都各有一份“解析颜色”的私有实现，错误类型却
    不同（``ProtocolError`` / ``RecordError`` / ``PuzzleError``）。这里提供
    唯一实现，由调用方通过 ``error_type`` 指定失败时抛出的异常类型，
    ``field`` 只用于拼装中文错误消息。
    """
    if isinstance(value, Player):
        return value
    if value == "black":
        return Player.BLACK
    if value == "white":
        return Player.WHITE
    raise error_type(f"{field} 必须是 'black' 或 'white'")


__all__ = [
    "BOARD_SIZE",
    "WIN_LENGTH",
    "BOARD_CELL_COUNT",
    "DEFAULT_RULE_MODE",
    "NON_BOARD_ENDINGS",
    "RESULT_REASONS",
    "RESULT_REASON_DRAW",
    "RESULT_REASON_ESCAPE",
    "RESULT_REASON_FIVE",
    "RESULT_REASON_RESIGN",
    "RESULT_REASON_TIMEOUT",
    "WIN_DIRECTIONS",
    "Player",
    "RuleMode",
    "Move",
    "Cell",
    "BoardLike",
    "find_winning_line",
    "in_bounds",
    "is_forbidden_move",
    "parse_player",
    "parse_rule_mode",
    "would_win",
]
