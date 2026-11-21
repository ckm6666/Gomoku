"""自研、可离线运行的五子棋 AI 引擎。

算法概览
========
1. **候选点生成**：只考虑已有棋子周围两格以内的空点，并按“自身进攻潜力 +
   对手若下在此处的威胁”排序；这能显著缩小 15×15 棋盘的搜索分支。
2. **强制战术先行**：任何难度都会先检查“我方一步成五”和“对手一步成五”，
   前者直接获胜，后者必须封堵。该检查发生在候选截断之前，避免浅层 AI
   因启发式漏掉致命一手。
3. **局面评估**：不在每个叶子全盘重算。初始化时把全部长度至少为 5 的直线
   线段建好；每次落子/撤销只更新经过该点的四条线，并增量维护双方累计分。
   中等难度使用连续的 5 格窗口评分；困难难度额外识别开放四、冲四、开放三
   等威胁，并给防守更高权重。
4. **搜索**：alpha-beta 极小极大。中等/困难采用迭代加深，保留最后一层完整
   搜索得到的最佳着法；每层用已得最佳着法优先排序，提高剪枝效率。
5. **保护**：截止时间 + 节点上限 + 取消事件三层限制；搜索循环定期检查并
   短暂让出 GIL。取消返回 None，超时返回上一完整迭代的合法着法。

本模块只依赖 Python 标准库和规则层，不导入 Qt，也不访问文件/网络，因此可以
在不启动界面的情况下单元测试，也可安全运行在后台 QThread 中。
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import Optional, Sequence

from gomoku.shared.rules import (
    BOARD_CELL_COUNT,
    BOARD_SIZE,
    WIN_DIRECTIONS,
    WIN_LENGTH,
    BoardLike,
    Cell,
    Player,
    find_winning_line,
)

Move = tuple[int, int]

# 搜索分数远高于普通局面评估；depth 偏移用于偏向更快取胜。
WIN_SCORE = 10_000_000
INFINITY = 10**18

# 让出 GIL / 检查取消的节点间隔。间隔太大会让撤销/关闭窗口响应变慢，
# 太小则增加搜索开销。
_CHECK_INTERVAL = 128


class AIDifficulty(Enum):
    """AI 难度。"""

    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"

    @property
    def label(self) -> str:
        """返回界面展示用中文名称。"""
        return {
            AIDifficulty.EASY: "简单",
            AIDifficulty.MEDIUM: "中等",
            AIDifficulty.HARD: "困难",
        }[self]


@dataclass(frozen=True)
class SearchConfig:
    """某一难度的搜索参数。

    参数不是越大越好：五子棋分支多，搜索过深会显著增加耗时。这里用时间
    与节点双重上限保证界面响应，同时用不同深度、候选数和评估方式形成差异。
    """

    max_depth: int
    time_limit: float
    node_limit: int
    root_candidates: int
    inner_candidates: int
    defense_weight: float
    random_top_k: int = 0
    rich_evaluation: bool = False
    iterative_deepening: bool = False


# 三档难度参数。实现后通过本机中盘局基准测试调优；时间单位是秒。
DIFFICULTY_CONFIGS: dict[AIDifficulty, SearchConfig] = {
    AIDifficulty.EASY: SearchConfig(
        max_depth=1,
        time_limit=0.25,
        node_limit=2_000,
        root_candidates=20,
        inner_candidates=4,
        defense_weight=0.85,
        random_top_k=4,
        rich_evaluation=False,
        iterative_deepening=False,
    ),
    AIDifficulty.MEDIUM: SearchConfig(
        max_depth=3,
        time_limit=0.6,
        node_limit=15_000,
        root_candidates=12,
        inner_candidates=8,
        defense_weight=1.03,
        rich_evaluation=False,
        iterative_deepening=True,
    ),
    AIDifficulty.HARD: SearchConfig(
        max_depth=5,
        time_limit=1.2,
        node_limit=40_000,
        root_candidates=16,
        inner_candidates=10,
        defense_weight=1.12,
        rich_evaluation=True,
        iterative_deepening=True,
    ),
}


@dataclass(frozen=True)
class SearchStats:
    """一次 AI 搜索的可观测统计，用于测试和性能诊断。"""

    difficulty: AIDifficulty
    completed_depth: int
    nodes: int
    elapsed_seconds: float
    cancelled: bool = False
    timed_out: bool = False


# ---------------------------------------------------------------------------
# 预计算棋盘上的所有直线线段
# ---------------------------------------------------------------------------


def _build_line_geometry() -> tuple[
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


_LINES, _CELL_LINE_IDS = _build_line_geometry()

# 中等/困难局面评估权重。索引表示一个无对手棋子的 5 格窗口中己方棋子数。
_MEDIUM_WINDOW_WEIGHTS = (0, 1, 8, 110, 1_800, 80_000)
_HARD_WINDOW_WEIGHTS = (0, 1, 14, 240, 4_200, 150_000)

# 困难模式额外奖励连续开放棋形：(开放端数量 0/1/2)。
# 开放四几乎等于胜利，开放三也极具威胁；这些额外项让困难档比中等档更善于
# 识别“下一步形成不可阻挡威胁”的棋形。
_HARD_RUN_BONUSES: dict[int, tuple[int, int, int]] = {
    5: (WIN_SCORE, WIN_SCORE, WIN_SCORE),
    4: (0, 6_000, 55_000),
    3: (0, 220, 2_200),
    2: (0, 10, 90),
}

# 候选排序时使用的“落子后局部棋形”分值。比叶子评估略粗，但足够便宜。
_POINT_RUN_WEIGHTS: dict[int, tuple[int, int, int]] = {
    4: (0, 5_000, 50_000),
    3: (0, 200, 2_000),
    2: (0, 10, 90),
    1: (0, 0, 5),
}


def _run_bonus(length: int, open_ends: int) -> int:
    """返回连续棋形的额外价值。"""
    table = _HARD_RUN_BONUSES.get(min(length, 5))
    if table is None:
        return 0
    return table[min(open_ends, 2)]


def _score_line_cells(cells: Sequence[Cell], rich: bool) -> tuple[int, int]:
    """评估一条线对黑、白双方分别产生的价值。

    使用滑动的 5 格窗口，可以识别带空隙的棋形；困难模式额外扫描连续棋段
    的开放端。返回值分别为 (黑棋分值, 白棋分值)，由增量累计器做差更新。
    """
    black_score = 0
    white_score = 0
    length = len(cells)
    weights = _HARD_WINDOW_WEIGHTS if rich else _MEDIUM_WINDOW_WEIGHTS

    for start in range(length - WIN_LENGTH + 1):
        black_count = 0
        white_count = 0
        for index in range(start, start + WIN_LENGTH):
            cell = cells[index]
            if cell is Player.BLACK:
                black_count += 1
            elif cell is Player.WHITE:
                white_count += 1

        # 同时包含双方棋子的五格窗口没有形成五连的可能。
        if black_count and white_count:
            continue
        if black_count:
            black_score += weights[black_count]
        elif white_count:
            white_score += weights[white_count]

    if rich:
        # 扫描连续的纯色棋段，根据长度和开放端数量额外加分。
        index = 0
        while index < length:
            player = cells[index]
            if player is None:
                index += 1
                continue

            end = index + 1
            while end < length and cells[end] is player:
                end += 1

            open_ends = 0
            if index > 0 and cells[index - 1] is None:
                open_ends += 1
            if end < length and cells[end] is None:
                open_ends += 1

            bonus = _run_bonus(end - index, open_ends)
            if player is Player.BLACK:
                black_score += bonus
            else:
                white_score += bonus
            index = end

    return black_score, white_score


def _point_shape_value(length: int, open_ends: int) -> int:
    """估计棋段长度/开放端对候选点排序的贡献。"""
    table = _POINT_RUN_WEIGHTS.get(length)
    if table is None:
        return 0
    return table[min(open_ends, 2)]


class _SearchCancelled(Exception):
    """用户撤销/重开/关窗时用于快速中断搜索的内部信号。"""


class _SearchTimeout(TimeoutError):
    """搜索达到时间或节点上限时用于中断当前迭代的内部信号。"""


class _SearchContext:
    """一次独立搜索的棋盘副本、增量评估和 alpha-beta 状态。

    每次 ``find_move`` 都创建新实例，避免不同请求共享可变搜索状态，也方便
    QThread 中单次调用后安全丢弃。
    """

    def __init__(
        self,
        board_snapshot: BoardLike,
        ai_player: Player,
        difficulty: AIDifficulty,
        config: SearchConfig,
        cancel_event: Optional[Event],
        rng: random.Random,
    ) -> None:
        self.board: list[list[Cell]] = [list(row) for row in board_snapshot]
        self.ai_player = ai_player
        self.opponent = ai_player.opponent
        self.difficulty = difficulty
        self.config = config
        self.cancel_event = cancel_event
        self.rng = rng

        self.deadline = time.monotonic() + config.time_limit
        self.nodes = 0
        self.completed_depth = 0
        self.timed_out = False

        # 每个空位周围两格内的已有棋子数，用于增量生成候选点。
        self._neighbor_counts: list[list[int]] = [
            [0 for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
        ]
        self._move_count = 0

        # 每条线段的双方评分缓存，以及双方累计分。
        self._line_scores: list[tuple[int, int]] = [(0, 0)] * len(_LINES)
        self._total_scores: list[int] = [0, 0]  # [黑棋累计, 白棋累计]
        self._history: list[tuple[int, int, list[tuple[int, tuple[int, int]]]]] = []

        # 初始化线段评分与邻域计数。初始化复杂度只与空盘 + 已有棋子有关。
        for line_id, coords in enumerate(_LINES):
            cells = [self.board[row][col] for row, col in coords]
            pair = _score_line_cells(cells, config.rich_evaluation)
            self._line_scores[line_id] = pair
            self._total_scores[0] += pair[0]
            self._total_scores[1] += pair[1]

        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if self.board[row][col] is not None:
                    self._move_count += 1
                    self._update_neighbor_counts(row, col, 1)

    # ------------------------------------------------------------------
    # 棋盘增量更新
    # ------------------------------------------------------------------
    def _update_neighbor_counts(self, row: int, col: int, delta: int) -> None:
        """更新落点周围两格内的邻域计数。"""
        for dr in range(-2, 3):
            for dc in range(-2, 3):
                if dr == 0 and dc == 0:
                    continue
                nr, nc = row + dr, col + dc
                if 0 <= nr < BOARD_SIZE and 0 <= nc < BOARD_SIZE:
                    self._neighbor_counts[nr][nc] += delta

    def _apply_move(self, row: int, col: int, player: Player) -> None:
        """在搜索棋盘上落子，并增量更新经过该点的四条线评分。"""
        self.board[row][col] = player
        self._move_count += 1
        self._update_neighbor_counts(row, col, 1)

        changed: list[tuple[int, tuple[int, int]]] = []
        for line_id in _CELL_LINE_IDS[row][col]:
            old_pair = self._line_scores[line_id]
            coords = _LINES[line_id]
            cells = [self.board[r][c] for r, c in coords]
            new_pair = _score_line_cells(cells, self.config.rich_evaluation)
            self._total_scores[0] += new_pair[0] - old_pair[0]
            self._total_scores[1] += new_pair[1] - old_pair[1]
            self._line_scores[line_id] = new_pair
            changed.append((line_id, old_pair))

        self._history.append((row, col, changed))

    def _undo_move(self) -> None:
        """撤销最近一次搜索落子，恢复棋盘、邻域与线评分。"""
        row, col, changed = self._history.pop()
        self.board[row][col] = None
        self._move_count -= 1
        self._update_neighbor_counts(row, col, -1)

        for line_id, old_pair in changed:
            current_pair = self._line_scores[line_id]
            self._total_scores[0] -= current_pair[0] - old_pair[0]
            self._total_scores[1] -= current_pair[1] - old_pair[1]
            self._line_scores[line_id] = old_pair

    # ------------------------------------------------------------------
    # 局面工具
    # ------------------------------------------------------------------
    def _evaluate(self) -> int:
        """从 AI 视角评估当前局面。"""
        if self.ai_player is Player.BLACK:
            own, opponent = self._total_scores
        else:
            opponent, own = self._total_scores
        # 防守权重略高于 1 表示宁可少一点进攻，也不让对手形成大棋形。
        return int(own - opponent * self.config.defense_weight)

    def _all_empty_cells(self) -> list[Move]:
        """返回全盘空点；仅在邻域候选异常为空时兜底。"""
        return [
            (row, col)
            for row in range(BOARD_SIZE)
            for col in range(BOARD_SIZE)
            if self.board[row][col] is None
        ]

    def _neighbor_empty_cells(self) -> list[Move]:
        """返回已有棋子周围两格内的空点。"""
        cells: list[Move] = []
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if (
                    self.board[row][col] is None
                    and self._neighbor_counts[row][col] > 0
                ):
                    cells.append((row, col))
        return cells if cells else self._all_empty_cells()

    def _would_win_at(self, row: int, col: int, player: Player) -> bool:
        """判断在空点落子是否会立即形成五连（不修改棋盘）。"""
        if self.board[row][col] is not None:
            return False

        for dr, dc in WIN_DIRECTIONS:
            count = 1

            r, c = row + dr, col + dc
            while (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is player
            ):
                count += 1
                r += dr
                c += dc

            r, c = row - dr, col - dc
            while (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is player
            ):
                count += 1
                r -= dr
                c -= dc

            if count >= WIN_LENGTH:
                return True

        return False

    def _immediate_winning_cells(
        self, cells: Sequence[Move], player: Player
    ) -> list[Move]:
        """从候选点中筛出立即获胜点。"""
        return [
            (row, col)
            for row, col in cells
            if self._would_win_at(row, col, player)
        ]

    def _shape_score_at(self, row: int, col: int, player: Player) -> int:
        """估算某一方若在空点落子形成的局部连续棋形价值。

        这里只统计连续棋段和开放端，不修改棋盘；它主要用于候选排序，
        真正的局面评估由增量线段分数完成。
        """
        total = 0
        for dr, dc in WIN_DIRECTIONS:
            forward = 0
            r, c = row + dr, col + dc
            while (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is player
            ):
                forward += 1
                r += dr
                c += dc
            forward_open = (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is None
            )

            backward = 0
            r, c = row - dr, col - dc
            while (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is player
            ):
                backward += 1
                r -= dr
                c -= dc
            backward_open = (
                0 <= r < BOARD_SIZE
                and 0 <= c < BOARD_SIZE
                and self.board[r][c] is None
            )

            run_length = 1 + forward + backward
            open_ends = int(forward_open) + int(backward_open)
            total += _point_shape_value(run_length, open_ends)

        return total

    def _ordering_score(self, row: int, col: int, player: Player) -> int:
        """候选排序：进攻潜力 + 加权防守潜力 + 轻微中心偏好。"""
        attack = self._shape_score_at(row, col, player)
        defense = self._shape_score_at(row, col, player.opponent)
        center_bonus = _BOARD_CENTER - max(
            abs(row - _BOARD_CENTER), abs(col - _BOARD_CENTER)
        )
        return attack + int(defense * self.config.defense_weight) + center_bonus

    def _ranked_candidates(
        self, cells: Sequence[Move], player: Player, limit: int
    ) -> list[Move]:
        """按局部攻防潜力排序并截断候选点。"""
        scored = [
            (self._ordering_score(row, col, player), row, col)
            for row, col in cells
        ]
        # 排序键包含坐标，保证同分时结果稳定；真正的同分选择留给根节点。
        scored.sort(key=lambda item: (-item[0], item[1], item[2]))
        return [(row, col) for _, row, col in scored[:limit]]

    def _ordered_candidates(self, player: Player, limit: int) -> list[Move]:
        """搜索节点内的候选点：强制战术优先，其次局部评分截断。"""
        empty_cells = self._neighbor_empty_cells()
        if not empty_cells:
            return []

        own_wins = self._immediate_winning_cells(empty_cells, player)
        if own_wins:
            return own_wins

        opponent_wins = self._immediate_winning_cells(
            empty_cells, player.opponent
        )
        if opponent_wins:
            # 对手有立刻成五的点，其他走法都会直接失败，只需考虑封堵。
            return opponent_wins

        return self._ranked_candidates(empty_cells, player, limit)

    # ------------------------------------------------------------------
    # 时间/取消保护与递归搜索
    # ------------------------------------------------------------------
    def _raise_if_stopped(self) -> None:
        """检查取消事件、截止时间/节点上限；必要时抛出控制流异常。"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise _SearchCancelled
        if self.nodes >= self.config.node_limit or time.monotonic() >= self.deadline:
            raise _SearchTimeout

    def _visit_node(self) -> None:
        """计数一个搜索节点，并按固定间隔检查保护条件。"""
        self.nodes += 1
        if self.nodes % _CHECK_INTERVAL == 0:
            self._raise_if_stopped()
            # 纯 Python CPU 循环会持有 GIL；偶尔让出给 GUI 线程，减少卡顿感。
            time.sleep(0)

    def _search(
        self, depth: int, alpha: int, beta: int, current_player: Player
    ) -> int:
        """alpha-beta 极小极大搜索剩余 depth 层。"""
        self._visit_node()

        if depth <= 0:
            return self._evaluate()
        if self._move_count >= BOARD_CELL_COUNT:
            return 0

        moves = self._ordered_candidates(
            current_player, self.config.inner_candidates
        )
        if not moves:
            return self._evaluate()

        if current_player is self.ai_player:
            value = -INFINITY
            for row, col in moves:
                self._apply_move(row, col, current_player)
                try:
                    if find_winning_line(self.board, row, col, current_player):
                        score = WIN_SCORE + depth
                    else:
                        score = self._search(
                            depth - 1, alpha, beta, current_player.opponent
                        )
                finally:
                    self._undo_move()

                value = max(value, score)
                alpha = max(alpha, value)
                if alpha >= beta:
                    break
            return value

        value = INFINITY
        for row, col in moves:
            self._apply_move(row, col, current_player)
            try:
                if find_winning_line(self.board, row, col, current_player):
                    score = -WIN_SCORE - depth
                else:
                    score = self._search(
                        depth - 1, alpha, beta, current_player.opponent
                    )
            finally:
                self._undo_move()

            value = min(value, score)
            beta = min(beta, value)
            if alpha >= beta:
                break
        return value

    def _root_search(
        self, depth: int, root_moves: Sequence[Move]
    ) -> tuple[int, list[Move]]:
        """在根节点遍历全部候选，返回最高分及同分着法。"""
        alpha = -INFINITY
        best_score = -INFINITY
        best_moves: list[Move] = []

        for row, col in root_moves:
            self._visit_node()  # 根节点自身也要参与时间/取消检查。
            self._apply_move(row, col, self.ai_player)
            try:
                if find_winning_line(self.board, row, col, self.ai_player):
                    score = WIN_SCORE + depth
                elif depth <= 1:
                    score = self._evaluate()
                else:
                    score = self._search(
                        depth - 1, alpha, INFINITY, self.opponent
                    )
            finally:
                self._undo_move()

            if score > best_score:
                best_score = score
                best_moves = [(row, col)]
            elif score == best_score:
                best_moves.append((row, col))

            alpha = max(alpha, best_score)
            self._raise_if_stopped_tail()

        return best_score, best_moves

    def _raise_if_stopped_tail(self) -> None:
        """根节点每处理完一个分支就检查一次取消/时间。"""
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise _SearchCancelled
        if self.nodes >= self.config.node_limit or time.monotonic() >= self.deadline:
            raise _SearchTimeout

    def _choose_easy_move(self, candidates: Sequence[Move]) -> Move:
        """简单难度从前几名候选中加权随机，保留可被击败的娱乐性。"""
        if not candidates:
            raise RuntimeError("AI 没有可落子的候选点")
        top_count = min(self.config.random_top_k, len(candidates))
        top = list(candidates[:top_count])
        if len(top) == 1:
            return top[0]
        weights = list(range(len(top), 0, -1))
        return self.rng.choices(top, weights=weights, k=1)[0]

    def find_best_move(self) -> Optional[Move]:
        """执行本局搜索并返回合法落点。"""
        self._raise_if_stopped()

        # 空盘首手固定天元，既符合常见开局，也避免候选集为空。
        if self._move_count == 0:
            return (_BOARD_CENTER, _BOARD_CENTER)

        local_cells = self._neighbor_empty_cells()
        if not local_cells:
            return None

        # 所有难度共用同一层强制战术，避免难度越低越像“随机送子”。
        own_wins = self._immediate_winning_cells(local_cells, self.ai_player)
        if own_wins:
            return own_wins[0]

        opponent_wins = self._immediate_winning_cells(local_cells, self.opponent)
        if opponent_wins:
            # 多个立即胜点时实际上无法全堵；选择局部价值最高的一个。
            return max(
                opponent_wins,
                key=lambda cell: self._ordering_score(
                    cell[0], cell[1], self.ai_player
                ),
            )

        root_candidates = self._ranked_candidates(
            local_cells, self.ai_player, self.config.root_candidates
        )
        if not root_candidates:
            return None

        if not self.config.iterative_deepening:
            self.completed_depth = 1
            return self._choose_easy_move(root_candidates)

        best_move = root_candidates[0]
        ordered_root = list(root_candidates)

        for depth in range(1, self.config.max_depth + 1):
            try:
                # 放在 try 内，保证“上一轮已有结果、本轮开始前正好超时”
                # 的情况仍能返回上一层完整结果，而不是退回兜底点。
                self._raise_if_stopped()
                score, tied_moves = self._root_search(depth, ordered_root)
            except _SearchTimeout:
                self.timed_out = True
                break

            self.completed_depth = depth
            best_move = self.rng.choice(tied_moves)

            # 如果已经找到必胜/必败的确定结果，不必继续加深。
            if abs(score) >= WIN_SCORE // 2:
                break

            # 下一层优先搜索本层最佳的根着法，提升 alpha-beta 剪枝效率。
            ordered_root = [best_move] + [
                move for move in root_candidates if move != best_move
            ]

        return best_move


# 天元坐标。
_BOARD_CENTER = BOARD_SIZE // 2


class GomokuAI:
    """五子棋 AI 的对外入口。

    该类不依赖 Qt；``find_move`` 每次调用都创建独立搜索上下文，
    因此可以从后台线程安全调用，并且不会污染调用方棋盘快照。
    """

    def __init__(self, seed: Optional[int] = None) -> None:
        self._rng = random.Random(seed)
        self.last_stats: Optional[SearchStats] = None

    def find_move(
        self,
        board_snapshot: BoardLike,
        player: Player,
        difficulty: AIDifficulty,
        cancel_event: Optional[Event] = None,
    ) -> Optional[Move]:
        """计算 AI 的下一手。

        参数：
            board_snapshot: 主线程传出的只读棋盘快照。
            player: AI 执子颜色。
            difficulty: 难度配置。
            cancel_event: 可选的线程安全取消事件；设置后搜索尽快返回 None。

        返回：
            合法空点 ``(row, col)``；没有空位、输入非法或用户取消时为 None。
        """
        started = time.monotonic()
        config = DIFFICULTY_CONFIGS[difficulty]
        stats_nodes = 0
        stats_depth = 0
        cancelled = False
        timed_out = False

        try:
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                return None

            if (
                not isinstance(player, Player)
                or len(board_snapshot) != BOARD_SIZE
                or any(len(row) != BOARD_SIZE for row in board_snapshot)
            ):
                return None

            if all(
                cell is not None
                for row in board_snapshot
                for cell in row
            ):
                return None

            empty_count = sum(
                1 for row in board_snapshot for cell in row if cell is None
            )
            if empty_count == 0:
                return None

            # 空盘首手在天元，直接返回；这也是 UI 中“AI 先手”的常见表现。
            if empty_count == BOARD_CELL_COUNT:
                return (_BOARD_CENTER, _BOARD_CENTER)

            context = _SearchContext(
                board_snapshot=board_snapshot,
                ai_player=player,
                difficulty=difficulty,
                config=config,
                cancel_event=cancel_event,
                rng=self._rng,
            )
            stats_nodes = context.nodes
            try:
                move = context.find_best_move()
            except _SearchCancelled:
                cancelled = True
                return None
            except _SearchTimeout:
                timed_out = True
                move = None
            finally:
                stats_nodes = context.nodes
                stats_depth = context.completed_depth
                timed_out = timed_out or context.timed_out

            # 结果必须在原始快照中仍为空位，双重防御线程取消/棋盘变更竞态。
            if move is not None:
                row, col = move
                if (
                    0 <= row < BOARD_SIZE
                    and 0 <= col < BOARD_SIZE
                    and board_snapshot[row][col] is None
                ):
                    return move

            # 极端情况下搜索被截断但没有得到结果：给 UI 一个合法兜底点。
            for row in range(BOARD_SIZE):
                for col in range(BOARD_SIZE):
                    if board_snapshot[row][col] is None:
                        return (row, col)
            return None

        finally:
            elapsed = time.monotonic() - started
            self.last_stats = SearchStats(
                difficulty=difficulty,
                completed_depth=stats_depth,
                nodes=stats_nodes,
                elapsed_seconds=elapsed,
                cancelled=cancelled,
                timed_out=timed_out,
            )
