"""VCF / VCT 强制威胁搜索。

本模块只依赖规则层与棋盘几何，不导入 Qt，也不访问文件/网络，可在
``AIWorker`` 的后台线程中安全运行。

实现范围：

* **VCF**（连续冲四胜）：攻方每一步都必须形成四或直接成五；守方若只有
  一个成五点则被迫封堵，形成严格的强制序列。双四/活四视为已证明胜。
* **VCT-lite**（双威胁/活三延展）：在 VCF 未命中时，尝试寻找一手形成
  “下一步可造活四”的双威胁，并对所有本地守方应手逐一验证。它是 VCT
  的保守子集：只接受能被穷举验证的胜法，验证失败或超时一律返回 None。

所有搜索都受 :class:`SearchBudget` 的截止时间、节点上限和取消事件
约束；超时或取消不会返回未经证明的线路。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import IntEnum
from threading import Event
from typing import Optional, Sequence

from gomoku.client.board_geometry import neighbor_empty_cells
from gomoku.shared.rules import (
    BOARD_SIZE,
    Player,
    find_winning_line,
)

Move = tuple[int, int]
DIRECTIONS: tuple[tuple[int, int], ...] = ((0, 1), (1, 0), (1, 1), (1, -1))
_CHECK_INTERVAL = 128


class ThreatSearchCancelled(Exception):
    """搜索被取消事件中断。"""


class ThreatSearchTimeout(TimeoutError):
    """搜索达到截止时间或节点上限。"""


class SearchBudget:
    """威胁搜索共享的时间/节点/取消预算。"""

    def __init__(
        self,
        deadline: float,
        node_limit: int,
        cancel_event: Optional[Event] = None,
    ) -> None:
        self.deadline = deadline
        self.node_limit = max(1, int(node_limit))
        self.cancel_event = cancel_event
        self.nodes = 0

    def tick(self) -> None:
        """记录一个搜索节点；必要时抛出取消/超时异常。"""
        self.nodes += 1
        if (
            self.cancel_event is not None
            and self.cancel_event.is_set()
        ):
            raise ThreatSearchCancelled
        if self.nodes >= self.node_limit or time.monotonic() >= self.deadline:
            raise ThreatSearchTimeout
        if self.nodes % _CHECK_INTERVAL == 0:
            # 纯 Python 搜索会持有 GIL；周期性让出，保证撤销/关窗及时响应。
            time.sleep(0)


class ThreatKind(IntEnum):
    """一个候选点落子后可形成的最高威胁等级。"""

    NONE = 0
    THREE = 1
    OPEN_THREE = 2
    FOUR = 3
    OPEN_FOUR = 4
    FIVE = 5


@dataclass(frozen=True)
class Threat:
    """某个空点的威胁分析结果。"""

    kind: ThreatKind
    move: Move
    winning_points: tuple[Move, ...]
    line: tuple[Move, ...]


@dataclass(frozen=True)
class ThreatLine:
    """一条被证明的强制攻击线路。"""

    attacker: Player
    moves: tuple[Move, ...]
    nodes: int
    elapsed: float
    cancelled: bool = False
    timed_out: bool = False


# ---------------------------------------------------------------------------
# 棋盘工具
# ---------------------------------------------------------------------------


def _in_bounds(row: int, col: int) -> bool:
    return 0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE


def _copy_board(board: Sequence[Sequence[Player | None]]) -> list[list[Player | None]]:
    return [list(row) for row in board]


def _would_win_at(
    board: Sequence[Sequence[Player | None]],
    row: int,
    col: int,
    player: Player,
) -> bool:
    """在空点落子是否立即五连；不修改棋盘。"""
    if not _in_bounds(row, col) or board[row][col] is not None:
        return False

    for dr, dc in DIRECTIONS:
        count = 1
        r, c = row + dr, col + dc
        while _in_bounds(r, c) and board[r][c] is player:
            count += 1
            r += dr
            c += dc
        r, c = row - dr, col - dc
        while _in_bounds(r, c) and board[r][c] is player:
            count += 1
            r -= dr
            c -= dc
        if count >= 5:
            return True
    return False


def _winning_points_through(
    board: Sequence[Sequence[Player | None]],
    row: int,
    col: int,
    player: Player,
) -> list[Move]:
    """找出经过最近落点、能让该方立即成五的空点。

    调用前 ``(row, col)`` 已是 ``player`` 的棋子。一个五连若包含该点，
    成五点必在其四条线的 ±4 范围内，因此无需扫描全盘。
    """
    points: list[Move] = []
    seen: set[Move] = set()
    for dr, dc in DIRECTIONS:
        for step in range(-4, 5):
            if step == 0:
                continue
            r, c = row + dr * step, col + dc * step
            if not _in_bounds(r, c) or board[r][c] is not None:
                continue
            point = (r, c)
            if point in seen:
                continue
            if _would_win_at(board, r, c, player):
                seen.add(point)
                points.append(point)
    return points


def _winning_points(
    board: Sequence[Sequence[Player | None]],
    player: Player,
) -> list[Move]:
    """找出该方全盘所有立即成五点。"""
    return [
        (row, col)
        for row, col in neighbor_empty_cells(board)
        if _would_win_at(board, row, col, player)
    ]


def _open_four_points_through(
    board: Sequence[Sequence[Player | None]],
    row: int,
    col: int,
    player: Player,
) -> list[Move]:
    """找出经过最近落点、下一步可形成活四的空点。

    活四指落子后存在两个及以上成五点。只检查经过 ``(row, col)`` 的
    四条线：不经过它的活四点是落子前就存在的威胁，会由候选点本身覆盖。
    """
    points: list[Move] = []
    seen: set[Move] = set()
    for dr, dc in DIRECTIONS:
        for step in range(-4, 5):
            if step == 0:
                continue
            r, c = row + dr * step, col + dc * step
            if not _in_bounds(r, c) or board[r][c] is not None:
                continue
            point = (r, c)
            if point in seen:
                continue
            board[r][c] = player
            try:
                if len(_winning_points_through(board, r, c, player)) >= 2:
                    seen.add(point)
                    points.append(point)
            finally:
                board[r][c] = None
    return points


def _is_open_four_point(
    board: Sequence[Sequence[Player | None]],
    row: int,
    col: int,
    player: Player,
) -> bool:
    """该空点落子后是否形成活四（两个及以上成五点）。"""
    if not _in_bounds(row, col) or board[row][col] is not None:
        return False
    board[row][col] = player
    try:
        return len(_winning_points_through(board, row, col, player)) >= 2
    finally:
        board[row][col] = None


def _center_bonus(row: int, col: int) -> int:
    center = BOARD_SIZE // 2
    return center - max(abs(row - center), abs(col - center))


# ---------------------------------------------------------------------------
# VCF：连续冲四
# ---------------------------------------------------------------------------


def _vcf_candidates(
    board: list[list[Player | None]],
    attacker: Player,
    budget: SearchBudget,
) -> list[Move]:
    """按“造四”价值排序攻方候选点。"""
    scored: list[tuple[int, int, int, int]] = []
    for row, col in neighbor_empty_cells(board):
        budget.tick()
        board[row][col] = attacker
        try:
            if find_winning_line(board, row, col, attacker):
                scored.append((10_000, -_center_bonus(row, col), row, col))
                continue
            wins = len(_winning_points_through(board, row, col, attacker))
        finally:
            board[row][col] = None
        if wins:
            scored.append((wins, -_center_bonus(row, col), row, col))

    scored.sort(reverse=True)
    return [(row, col) for _, _, row, col in scored]


def _vcf_search(
    board: list[list[Player | None]],
    attacker: Player,
    defender: Player,
    budget: SearchBudget,
    depth: int,
    line: list[Move],
) -> bool:
    """递归证明一个 VCF 强制序列；成功时 ``line`` 含完整线路。"""
    budget.tick()
    if depth <= 0:
        return False

    own_wins = _winning_points(board, attacker)
    if own_wins:
        line.append(own_wins[0])
        return True

    opponent_wins = _winning_points(board, defender)
    if len(opponent_wins) >= 2:
        # 对方有两个成五点，无法同时封堵，且我方没有立即胜。
        return False

    if len(opponent_wins) == 1:
        # 唯一合法选择：封堵，并且封堵手必须同时形成四，才能继续强制。
        block = opponent_wins[0]
        row, col = block
        board[row][col] = attacker
        line.append(block)
        success = False
        try:
            after = _winning_points_through(board, row, col, attacker)
            if len(after) >= 2:
                success = True
                return True
            if len(after) == 1:
                reply = after[0]
                board[reply[0]][reply[1]] = defender
                line.append(reply)
                inner_success = False
                try:
                    if _vcf_search(
                        board,
                        attacker,
                        defender,
                        budget,
                        depth - 1,
                        line,
                    ):
                        inner_success = True
                        success = True
                        return True
                finally:
                    if not inner_success:
                        line.pop()
                        board[reply[0]][reply[1]] = None
            return False
        finally:
            if not success:
                line.pop()
                board[row][col] = None

    for move in _vcf_candidates(board, attacker, budget):
        row, col = move
        board[row][col] = attacker
        line.append(move)
        success = False
        try:
            if find_winning_line(board, row, col, attacker):
                success = True
                return True

            wins = _winning_points_through(board, row, col, attacker)
            if len(wins) >= 2:
                # 双四/活四：守方只能封堵一个，下一手必胜。
                success = True
                return True
            if len(wins) != 1:
                continue

            reply = wins[0]
            board[reply[0]][reply[1]] = defender
            line.append(reply)
            inner_success = False
            try:
                if _vcf_search(
                    board,
                    attacker,
                    defender,
                    budget,
                    depth - 1,
                    line,
                ):
                    inner_success = True
                    success = True
                    return True
            finally:
                if not inner_success:
                    line.pop()
                    board[reply[0]][reply[1]] = None
            return False
        finally:
            if not success:
                line.pop()
                board[row][col] = None

    return False


def verify_forced_line(
    board_snapshot: Sequence[Sequence[Player | None]],
    line: Sequence[Move],
    attacker: Player,
) -> bool:
    """独立复盘并验证一条 VCF 线路。

    规则：攻方每手必须成五或形成恰好一个成五点；守方每手必须恰好封堵
    该成五点。线路可以以攻方成五结束，或以攻方双四/活四（对手无法同时
    封堵）结束。验证失败返回 False。
    """
    if not line:
        return False

    board = _copy_board(board_snapshot)
    defender = attacker.opponent
    turn = attacker

    for index, (row, col) in enumerate(line):
        if not _in_bounds(row, col) or board[row][col] is not None:
            return False

        if turn is defender:
            winning = _winning_points(board, attacker)
            if len(winning) != 1 or winning[0] != (row, col):
                return False
            board[row][col] = defender
            turn = attacker
            continue

        board[row][col] = attacker
        if find_winning_line(board, row, col, attacker):
            return index == len(line) - 1

        winning = _winning_points_through(board, row, col, attacker)
        is_last = index == len(line) - 1
        if is_last:
            if len(winning) < 2:
                return False
            # 攻方留下双威胁；守方此时若已有成五点，可以先一步获胜。
            return not _winning_points(board, defender)
        if len(winning) != 1:
            return False
        if index + 1 >= len(line) or line[index + 1] != winning[0]:
            return False
        turn = defender

    return False


def find_vcf_win(
    board_snapshot: Sequence[Sequence[Player | None]],
    player: Player,
    *,
    budget: SearchBudget,
    max_depth: int = 12,
) -> Optional[ThreatLine]:
    """尝试为 ``player`` 找到一条 VCF 强制胜线路。"""
    started = time.monotonic()
    board = _copy_board(board_snapshot)
    line: list[Move] = []
    try:
        found = _vcf_search(
            board,
            player,
            player.opponent,
            budget,
            max_depth,
            line,
        )
    except ThreatSearchTimeout:
        return None
    except ThreatSearchCancelled:
        raise

    if not found:
        return None
    if not verify_forced_line(board_snapshot, line, player):
        return None
    return ThreatLine(
        attacker=player,
        moves=tuple(line),
        nodes=budget.nodes,
        elapsed=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# VCT-lite：双威胁 / 活三延展
# ---------------------------------------------------------------------------


def _defender_refutes_double_threat(
    board: list[list[Player | None]],
    attacker: Player,
    defender: Player,
    reply: Move,
    open_four_points: Sequence[Move],
    budget: SearchBudget,
) -> bool:
    """验证守方一手是否能化解攻方的双威胁。

    返回 True 表示该应手可化解（攻方此路不通），False 表示攻方仍胜。
    """
    row, col = reply
    board[row][col] = defender
    try:
        budget.tick()
        # 守方应手若形成立即成五威胁，攻方必须先应对，双威胁不再成立。
        if _winning_points(board, defender):
            return True

        for point in open_four_points:
            pr, pc = point
            if board[pr][pc] is not None:
                continue
            if _is_open_four_point(board, pr, pc, attacker):
                return False
        return True
    finally:
        board[row][col] = None


def _vct_candidate_moves(
    board: list[list[Player | None]],
    attacker: Player,
    budget: SearchBudget,
    limit: int,
) -> list[tuple[Move, tuple[Move, ...]]]:
    """找出能制造“下一步活四”双威胁的候选手。"""
    scored: list[tuple[int, int, int, Move, tuple[Move, ...]]] = []
    for row, col in neighbor_empty_cells(board):
        budget.tick()
        board[row][col] = attacker
        try:
            if find_winning_line(board, row, col, attacker):
                scored.append(
                    (
                        10_000,
                        -_center_bonus(row, col),
                        row,
                        (row, col),
                        (),
                    )
                )
                continue
            points = _open_four_points_through(board, row, col, attacker)
        finally:
            board[row][col] = None
        if points:
            scored.append(
                (
                    len(points),
                    -_center_bonus(row, col),
                    row,
                    (row, col),
                    tuple(points),
                )
            )

    scored.sort(reverse=True, key=lambda item: item[:3])
    return [
        (move, points)
        for _, _, _, move, points in scored[:limit]
    ]


def _vct_search(
    board: list[list[Player | None]],
    attacker: Player,
    budget: SearchBudget,
    candidate_limit: int,
    open_four_cap: int,
) -> Optional[tuple[Move, ...]]:
    """寻找一手可被穷举验证的双威胁胜着。"""
    budget.tick()
    own_wins = _winning_points(board, attacker)
    if own_wins:
        return (own_wins[0],)
    # 对方已有成五点时，本保守版本不处理，交给 VCF/常规搜索。
    if _winning_points(board, attacker.opponent):
        return None

    for move, points in _vct_candidate_moves(
        board, attacker, budget, candidate_limit
    ):
        row, col = move
        board[row][col] = attacker
        try:
            if find_winning_line(board, row, col, attacker):
                return (move,)
            if not points:
                continue

            capped_points = points[:open_four_cap]
            defended = False
            for reply in neighbor_empty_cells(board):
                budget.tick()
                if _defender_refutes_double_threat(
                    board,
                    attacker,
                    attacker.opponent,
                    reply,
                    capped_points,
                    budget,
                ):
                    defended = True
                    break
            if not defended:
                return (move,)
        finally:
            board[row][col] = None
    return None


def verify_double_threat(
    board_snapshot: Sequence[Sequence[Player | None]],
    move: Move,
    attacker: Player,
    *,
    open_four_cap: int = 8,
) -> bool:
    """独立验证“落子后所有守方应手都无法化解双威胁”。"""
    board = _copy_board(board_snapshot)
    row, col = move
    if not _in_bounds(row, col) or board[row][col] is not None:
        return False

    board[row][col] = attacker
    if find_winning_line(board, row, col, attacker):
        return True

    points = _open_four_points_through(board, row, col, attacker)
    if not points:
        return False

    defender = attacker.opponent
    # 出现守方成五点则该“双威胁”不成立。
    if _winning_points(board, defender):
        return False

    for reply in neighbor_empty_cells(board):
        board[reply[0]][reply[1]] = defender
        try:
            if _winning_points(board, defender):
                return False
            survives = False
            for point in points[:open_four_cap]:
                pr, pc = point
                if board[pr][pc] is not None:
                    continue
                if _is_open_four_point(board, pr, pc, attacker):
                    survives = True
                    break
            if not survives:
                return False
        finally:
            board[reply[0]][reply[1]] = None
    return True


def find_vct_win(
    board_snapshot: Sequence[Sequence[Player | None]],
    player: Player,
    *,
    budget: SearchBudget,
    candidate_limit: int = 10,
    open_four_cap: int = 8,
) -> Optional[ThreatLine]:
    """尝试找到一条可验证的 VCT-lite 双威胁胜线路。"""
    started = time.monotonic()
    board = _copy_board(board_snapshot)
    try:
        line = _vct_search(
            board,
            player,
            budget,
            candidate_limit,
            open_four_cap,
        )
    except ThreatSearchTimeout:
        return None
    except ThreatSearchCancelled:
        raise

    if line is None:
        return None
    if not verify_double_threat(
        board_snapshot,
        line[0],
        player,
        open_four_cap=open_four_cap,
    ):
        return None
    return ThreatLine(
        attacker=player,
        moves=line,
        nodes=budget.nodes,
        elapsed=time.monotonic() - started,
    )


# ---------------------------------------------------------------------------
# 单点威胁分析
# ---------------------------------------------------------------------------


def analyze_point(
    board_snapshot: Sequence[Sequence[Player | None]],
    row: int,
    col: int,
    player: Player,
) -> Threat:
    """分析在空点落子后形成的最高威胁等级。"""
    if not _in_bounds(row, col) or board_snapshot[row][col] is not None:
        return Threat(ThreatKind.NONE, (row, col), (), ())

    board = _copy_board(board_snapshot)
    board[row][col] = player
    line = find_winning_line(board, row, col, player)
    if line:
        return Threat(
            ThreatKind.FIVE,
            (row, col),
            ((row, col),),
            tuple(line),
        )

    winning = _winning_points_through(board, row, col, player)
    if len(winning) >= 2:
        kind = ThreatKind.OPEN_FOUR
    elif len(winning) == 1:
        kind = ThreatKind.FOUR
    else:
        open_three = False
        three = False
        for r, c in neighbor_empty_cells(board):
            board[r][c] = player
            try:
                count = len(_winning_points_through(board, r, c, player))
            finally:
                board[r][c] = None
            if count >= 2:
                open_three = True
                break
            if count == 1:
                three = True
        kind = (
            ThreatKind.OPEN_THREE
            if open_three
            else (ThreatKind.THREE if three else ThreatKind.NONE)
        )

    return Threat(kind, (row, col), tuple(winning), ())


__all__ = [
    "DIRECTIONS",
    "SearchBudget",
    "ThreatSearchCancelled",
    "ThreatSearchTimeout",
    "ThreatKind",
    "Threat",
    "ThreatLine",
    "analyze_point",
    "find_vcf_win",
    "find_vct_win",
    "verify_forced_line",
    "verify_double_threat",
]
