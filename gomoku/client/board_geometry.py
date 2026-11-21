"""棋盘直线几何与邻域候选工具。

本模块从 :mod:`gomoku.client.ai_engine` 中抽出，供 AI 评估、威胁搜索与
置换表共享。只依赖规则层，不导入 Qt，因此可以安全地在后台线程或纯
Python 单测中使用。
"""

from __future__ import annotations

from gomoku.shared.rules import BOARD_SIZE, WIN_LENGTH, BoardLike

Move = tuple[int, int]


def build_line_geometry() -> tuple[
    tuple[tuple[Move, ...], ...],
    tuple[tuple[tuple[int, ...], ...], ...],
]:
    """预计算所有长度 >= 5 的行、列、斜线及其反向索引。

    增量评估只需要更新经过最后落点的四条线；预先存下线段避免了搜索时
    为每个叶子重新生成整盘几何。
    """
    lines: list[tuple[Move, ...]] = []
    cell_line_ids: list[list[list[int]]] = [
        [[] for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]

    def add_line(coords: list[Move]) -> None:
        if len(coords) < WIN_LENGTH:
            return
        line_id = len(lines)
        lines.append(tuple(coords))
        for row, col in coords:
            cell_line_ids[row][col].append(line_id)

    # 15 条横线、15 条竖线。
    for row in range(BOARD_SIZE):
        add_line([(row, col) for col in range(BOARD_SIZE)])
    for col in range(BOARD_SIZE):
        add_line([(row, col) for row in range(BOARD_SIZE)])

    # 主对角线（向右下）。
    for start_col in range(BOARD_SIZE):
        coords: list[Move] = []
        row, col = 0, start_col
        while row < BOARD_SIZE and col < BOARD_SIZE:
            coords.append((row, col))
            row += 1
            col += 1
        add_line(coords)
    for start_row in range(1, BOARD_SIZE):
        coords = []
        row, col = start_row, 0
        while row < BOARD_SIZE and col < BOARD_SIZE:
            coords.append((row, col))
            row += 1
            col += 1
        add_line(coords)

    # 副对角线（向右上/向左下）。
    for start_col in range(BOARD_SIZE):
        coords = []
        row, col = 0, start_col
        while row < BOARD_SIZE and col >= 0:
            coords.append((row, col))
            row += 1
            col -= 1
        add_line(coords)
    for start_row in range(1, BOARD_SIZE):
        coords = []
        row, col = start_row, BOARD_SIZE - 1
        while row < BOARD_SIZE and col >= 0:
            coords.append((row, col))
            row += 1
            col -= 1
        add_line(coords)

    frozen_cell_lines = tuple(
        tuple(tuple(ids) for ids in row) for row in cell_line_ids
    )
    return tuple(lines), frozen_cell_lines


LINES, CELL_LINE_IDS = build_line_geometry()


def neighbor_empty_cells(board: BoardLike, radius: int = 2) -> list[Move]:
    """返回已有棋子周围 ``radius`` 格内的空点。

    若棋盘为空则返回全部空点；返回顺序按行、列稳定排序，便于测试与
    确定性搜索。该函数不修改棋盘。
    """
    occupied: list[Move] = []
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if board[row][col] is not None:
                occupied.append((row, col))

    if not occupied:
        return [
            (row, col)
            for row in range(BOARD_SIZE)
            for col in range(BOARD_SIZE)
            if board[row][col] is None
        ]

    result: set[Move] = set()
    span = max(1, int(radius))
    for row, col in occupied:
        for dr in range(-span, span + 1):
            nr = row + dr
            if not 0 <= nr < BOARD_SIZE:
                continue
            for dc in range(-span, span + 1):
                if dr == 0 and dc == 0:
                    continue
                nc = col + dc
                if (
                    0 <= nc < BOARD_SIZE
                    and board[nr][nc] is None
                ):
                    result.add((nr, nc))

    return sorted(result)


__all__ = [
    "LINES",
    "CELL_LINE_IDS",
    "build_line_geometry",
    "neighbor_empty_cells",
]
