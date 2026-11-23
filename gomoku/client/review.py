"""棋谱 AI 复盘：逐手评估损失、推荐着法与整体准确率。

评估方法（为了让普通玩家的棋谱也能在可接受时间内分析完）：

1. 在每一手之前，用轻量搜索分析局面，得到推荐着法与最佳评分；
2. 如果实战着法就是推荐着法，损失为 0；
3. 否则把实战着法落到棋盘上，再让对手搜索一次，取对手最佳评分的
   相反数作为实战着法评分；
4. 评分差映射为胜率差，并按阈值标注“好棋/一般/失误/败着”。

这是通用复盘工具的近似做法，不宣称与职业引擎完全一致，但足以稳定地
标出明显漏着，且完全在客户端本地运行。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Callable, Optional, Sequence

from gomoku.client.ai_engine import (
    DIFFICULTY_CONFIGS,
    AIDifficulty,
    GomokuAI,
    SearchConfig,
    SearchAnalysis,
    WIN_SCORE,
)
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import (
    BOARD_SIZE,
    Cell,
    Move,
    Player,
    find_winning_line,
)

#: 复盘用的短预算搜索：比大师档快，避免一局分析几分钟。
REVIEW_CONFIG: SearchConfig = replace(
    DIFFICULTY_CONFIGS[AIDifficulty.MASTER],
    time_limit=0.18,
    node_limit=8_000,
    root_candidates=10,
    inner_candidates=8,
    threat_time_limit=0.08,
    threat_node_limit=8_000,
    tt_max_entries=20_000,
)

#: 评分差 → 标注阈值（从低到高）。
INACCURACY_LOSS = 1_200
MISTAKE_LOSS = 3_500
BLUNDER_LOSS = 8_000

LABEL_BEST = "最佳"
LABEL_GOOD = "好棋"
LABEL_INACCURATE = "一般"
LABEL_MISTAKE = "失误"
LABEL_BLUNDER = "败着"
LABEL_UNKNOWN = "—"


def score_to_win_rate(score: int) -> float:
    """把引擎评分粗略映射为胜率（0.01～0.99）。"""
    bounded = max(-100_000.0, min(100_000.0, float(score)))
    value = 1.0 / (1.0 + math.exp(-bounded / 2_500.0))
    return max(0.01, min(0.99, value))


def classify_loss(
    loss: int, *, is_best: bool = False, winning_move: bool = False
) -> str:
    """按评分损失给出中文标注。"""
    if winning_move and not is_best:
        return LABEL_BEST
    if is_best or loss <= 150:
        return LABEL_BEST if is_best else LABEL_GOOD
    if loss <= INACCURACY_LOSS:
        return LABEL_INACCURATE
    if loss <= MISTAKE_LOSS:
        return LABEL_MISTAKE
    return LABEL_BLUNDER


def accuracy_percent(average_loss: float) -> int:
    """由平均评分损失估算整局准确率，供界面展示。"""
    value = 100.0 - average_loss / 60.0
    return max(0, min(100, int(round(value))))


@dataclass(frozen=True)
class MoveReview:
    """一手的复盘结果。"""

    move_no: int
    player: Player
    move: tuple[int, int]
    best_move: Optional[tuple[int, int]]
    played_score: int
    best_score: int
    loss: int
    label: str
    win_rate_before: float
    win_rate_after: float


@dataclass(frozen=True)
class ReviewResult:
    """整局复盘结果。"""

    record_id: str
    reviews: tuple[MoveReview, ...]
    average_loss: float
    accuracy: int
    counts: dict[str, int]

    @property
    def summary_text(self) -> str:
        parts = [
            f"准确率 {self.accuracy}%",
            f"平均损失 {int(round(self.average_loss))}",
        ]
        for label in (
            LABEL_BLUNDER,
            LABEL_MISTAKE,
            LABEL_INACCURATE,
            LABEL_BEST,
        ):
            count = self.counts.get(label, 0)
            if count:
                parts.append(f"{label} {count}")
        return " · ".join(parts)


def _copy_board(
    board: Sequence[Sequence[Cell]],
) -> list[list[Cell]]:
    return [list(row) for row in board]


def _evaluate_played_move(
    ai: GomokuAI,
    board_before: Sequence[Sequence[Cell]],
    move: Move,
    difficulty: AIDifficulty,
) -> tuple[int, bool]:
    """返回（实战着法评分, 是否立即取胜）。"""
    board_after = _copy_board(board_before)
    board_after[move.row][move.col] = move.player
    if find_winning_line(
        tuple(tuple(row) for row in board_after),
        move.row,
        move.col,
        move.player,
    ):
        return WIN_SCORE, True
    analysis = ai.analyze(
        tuple(tuple(row) for row in board_after),
        move.player.opponent,
        difficulty,
        top_n=1,
        config_override=REVIEW_CONFIG,
    )
    if analysis is None or analysis.best_move is None:
        return 0, False
    return -int(analysis.best_score), False


def review_record(
    record: GameRecord,
    ai: Optional[GomokuAI] = None,
    *,
    difficulty: AIDifficulty = AIDifficulty.MASTER,
    progress: Optional[Callable[[int, int], None]] = None,
    on_move: Optional[Callable[[MoveReview], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
) -> Optional[ReviewResult]:
    """分析整条棋谱；被取消时返回 None。

    ``progress``/``on_move`` 会被后台线程逐手调用，调用方负责把结果
    通过 Qt 信号送回主线程。
    """
    engine = ai or GomokuAI(seed=0)
    board: list[list[Cell]] = [
        [None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)
    ]
    total = len(record.moves)
    reviews: list[MoveReview] = []
    counts: dict[str, int] = {}

    for index, move in enumerate(record.moves, start=1):
        if cancel is not None and cancel():
            return None
        board_before = tuple(tuple(row) for row in board)
        analysis: Optional[SearchAnalysis] = engine.analyze(
            board_before,
            move.player,
            difficulty,
            top_n=1,
            config_override=REVIEW_CONFIG,
        )
        if analysis is None or analysis.best_move is None:
            review = MoveReview(
                move_no=index,
                player=move.player,
                move=(move.row, move.col),
                best_move=None,
                played_score=0,
                best_score=0,
                loss=0,
                label=LABEL_UNKNOWN,
                win_rate_before=0.5,
                win_rate_after=0.5,
            )
        else:
            best_move = analysis.best_move
            best_score = int(analysis.best_score)
            is_best = (move.row, move.col) == best_move
            if is_best:
                played_score = best_score
                winning_move = False
            else:
                played_score, winning_move = _evaluate_played_move(
                    engine, board_before, move, difficulty
                )
            loss = max(0, best_score - played_score)
            label = classify_loss(
                loss, is_best=is_best, winning_move=winning_move
            )
            review = MoveReview(
                move_no=index,
                player=move.player,
                move=(move.row, move.col),
                best_move=best_move,
                played_score=played_score,
                best_score=best_score,
                loss=loss,
                label=label,
                win_rate_before=score_to_win_rate(best_score),
                win_rate_after=score_to_win_rate(played_score),
            )
            counts[label] = counts.get(label, 0) + 1
        reviews.append(review)
        if on_move is not None:
            on_move(review)
        if progress is not None:
            progress(index, total)
        board[move.row][move.col] = move.player
        if find_winning_line(
            tuple(tuple(row) for row in board),
            move.row,
            move.col,
            move.player,
        ):
            break

    if not reviews:
        return ReviewResult(
            record_id=record.record_id,
            reviews=(),
            average_loss=0.0,
            accuracy=100,
            counts={},
        )
    average = sum(item.loss for item in reviews) / len(reviews)
    return ReviewResult(
        record_id=record.record_id,
        reviews=tuple(reviews),
        average_loss=average,
        accuracy=accuracy_percent(average),
        counts=counts,
    )


def review_for_move(
    result: Optional[ReviewResult], move_no: int
) -> Optional[MoveReview]:
    if result is None:
        return None
    for item in result.reviews:
        if item.move_no == move_no:
            return item
    return None


__all__ = [
    "BLUNDER_LOSS",
    "INACCURACY_LOSS",
    "LABEL_BEST",
    "LABEL_BLUNDER",
    "LABEL_GOOD",
    "LABEL_INACCURATE",
    "LABEL_MISTAKE",
    "LABEL_UNKNOWN",
    "MISTAKE_LOSS",
    "MoveReview",
    "REVIEW_CONFIG",
    "ReviewResult",
    "accuracy_percent",
    "classify_loss",
    "review_for_move",
    "review_record",
    "score_to_win_rate",
]
