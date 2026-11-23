"""后台 AI 复盘线程：逐手分析棋谱，不阻塞界面。"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QThread, Signal

from gomoku.client.ai_engine import AIDifficulty, GomokuAI
from gomoku.client.review import MoveReview, ReviewResult, review_record
from gomoku.shared.record import GameRecord


class AIReviewWorker(QThread):
    """一次性复盘线程；只接触纯 Python 引擎与棋谱快照。"""

    progress = Signal(int, int)  # done, total
    move_ready = Signal(int, object)  # move_no, MoveReview
    review_finished = Signal(object)  # ReviewResult
    review_failed = Signal(str)
    review_cancelled = Signal()

    def __init__(
        self,
        record: GameRecord,
        parent=None,
        *,
        difficulty: AIDifficulty = AIDifficulty.MASTER,
    ) -> None:
        super().__init__(parent)
        self._record = record
        self._difficulty = difficulty
        self._cancel_event = threading.Event()

    @property
    def record_id(self) -> str:
        return self._record.record_id

    def cancel(self) -> None:
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    def run(self) -> None:
        try:
            if self._cancel_event.is_set():
                self.review_cancelled.emit()
                return
            engine = GomokuAI(seed=0)

            def on_move(review: MoveReview) -> None:
                if not self._cancel_event.is_set():
                    self.move_ready.emit(review.move_no, review)

            result: Optional[ReviewResult] = review_record(
                self._record,
                engine,
                difficulty=self._difficulty,
                progress=lambda done, total: (
                    None
                    if self._cancel_event.is_set()
                    else self.progress.emit(done, total)
                ),
                on_move=on_move,
                cancel=self._cancel_event.is_set,
            )
            if result is None or self._cancel_event.is_set():
                self.review_cancelled.emit()
                return
            self.review_finished.emit(result)
        except Exception as exc:  # noqa: BLE001 - 线程必须自带最终兜底
            if not self._cancel_event.is_set():
                self.review_failed.emit(
                    f"AI 复盘异常：{type(exc).__name__}: {exc}"
                )


__all__ = ["AIReviewWorker"]
