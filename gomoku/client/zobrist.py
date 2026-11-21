"""确定性 Zobrist 哈希。

置换表需要把「棋盘 + 行棋方」映射为 64 位整数。这里使用内联的
splitmix64 生成固定随机表，不依赖 ``random.Random`` 的内部实现，保证
不同 Python 版本、进程和测试中哈希一致。
"""

from __future__ import annotations

from gomoku.shared.rules import BOARD_SIZE, BoardLike, Player

_MASK64 = (1 << 64) - 1


def _splitmix64(seed: int) -> int:
    """一个确定性的 64 位混合函数。"""
    value = (seed + 0x9E3779B97F4A7C15) & _MASK64
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


def _build_stone_keys() -> tuple[tuple[tuple[int, ...], ...], ...]:
    """生成 [黑/白][行][列] 的棋子随机键。"""
    tables: list[tuple[tuple[int, ...], ...]] = []
    seed = 0
    for _ in range(2):
        rows: list[tuple[int, ...]] = []
        for _ in range(BOARD_SIZE):
            row: list[int] = []
            for _ in range(BOARD_SIZE):
                seed += 1
                row.append(_splitmix64(seed))
            rows.append(tuple(row))
        tables.append(tuple(rows))
    return tuple(tables)


STONE_KEYS = _build_stone_keys()
# 行棋方切换键：黑方走子时哈希不含该键，白方走子时包含。
SIDE_TO_MOVE_KEY = _splitmix64(0xC0FFEE)
# 规则集键；当前只有自由规则，保留给未来禁手/连珠规则。
RULE_SET_KEY = _splitmix64(0x5EED)


def move_key(player: Player, row: int, col: int) -> int:
    """返回某方在指定交叉点的随机键。"""
    index = 0 if player is Player.BLACK else 1
    return STONE_KEYS[index][row][col]


def compute_hash(board: BoardLike, side_to_move: Player) -> int:
    """计算完整棋盘哈希。

    ``side_to_move`` 为白方时异或行棋方键；``apply_move`` 每次落子都会
    切换该键，因此连续应用落子与直接重算的结果一致。
    """
    value = 0
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            cell = board[row][col]
            if cell is not None:
                value ^= move_key(cell, row, col)
    if side_to_move is Player.WHITE:
        value ^= SIDE_TO_MOVE_KEY
    return value


def apply_move(hash_value: int, player: Player, row: int, col: int) -> int:
    """返回落子后的哈希；同一调用也用于撤销（异或自反）。"""
    return (
        hash_value
        ^ move_key(player, row, col)
        ^ SIDE_TO_MOVE_KEY
    )


__all__ = [
    "STONE_KEYS",
    "SIDE_TO_MOVE_KEY",
    "RULE_SET_KEY",
    "move_key",
    "compute_hash",
    "apply_move",
]
