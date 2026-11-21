"""五子棋核心规则：棋手、坐标、胜负判定与棋盘约束。

本模块完全不依赖 Qt，可被 Windows 客户端、AI 和未来 FastAPI 服务端复用。
坐标约定：``row`` 从上到下为 0～14，``col`` 从左到右为 0～14。
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


def find_winning_line(
    board: BoardLike,
    row: int,
    col: int,
    player: Player,
) -> list[tuple[int, int]]:
    """检查指定落子是否形成五连，并返回完整连续线。

    该函数只依赖棋盘与最后一手，不修改状态，供客户端、AI 和未来服务端
    共用。规则采用休闲自由五子棋：连续 5 颗及以上同色棋子都算获胜。

    参数：
        board: 二维棋盘，空位为 None。
        row: 最后落子的行号。
        col: 最后落子的列号。
        player: 最后落子的棋手。

    返回：
        若存在五连，返回从一端到另一端的所有连续同色棋子坐标；
        否则返回空列表。
    """
    if not 0 <= row < BOARD_SIZE or not 0 <= col < BOARD_SIZE:
        return []
    # 该函数既用于真实落子，也可能被 AI 用于试探；目标点必须确属该棋手。
    if board[row][col] is not player:
        return []

    def is_same_stone(r: int, c: int) -> bool:
        """判断棋盘内某一点是否属于当前检查的棋手。"""
        return (
            0 <= r < BOARD_SIZE
            and 0 <= c < BOARD_SIZE
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


__all__ = [
    "BOARD_SIZE",
    "WIN_LENGTH",
    "BOARD_CELL_COUNT",
    "WIN_DIRECTIONS",
    "Player",
    "Move",
    "Cell",
    "BoardLike",
    "find_winning_line",
]
