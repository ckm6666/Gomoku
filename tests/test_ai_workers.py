"""后台 AI/复盘线程的异常上报测试。

异常原文可能带有内部路径、SQL 片段等实现细节，不能直接进入界面信号：
界面只应收到固定的通用文案，细节由日志记录（带 exc_info）。

这些用例都是 unittest 风格，CI 的 ``unittest discover`` 也能收集到。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# 必须在导入 PySide6 之前设置，保证 CI/无桌面环境也能构造线程。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.ai_engine import AIDifficulty  # noqa: E402
from gomoku.client.ai_worker import (  # noqa: E402
    AIHintWorker,
    AIWorker,
    ANALYSIS_FAILED_MESSAGE,
    SEARCH_FAILED_MESSAGE,
)
from gomoku.client.review_worker import AIReviewWorker  # noqa: E402
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import BOARD_SIZE, Move, Player  # noqa: E402

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])

# 故意写得像内部实现细节，用来验证它不会泄漏到界面。
SECRET_DETAIL = "内部细节 /srv/app/engine.py 第 42 行"


def blank_board() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def sample_record() -> GameRecord:
    return GameRecord(
        record_id="r1",
        created_at="2026-01-01T00:00:00+00:00",
        mode="local",
        moves=(Move(7, 7, Player.BLACK),),
        winner=None,
        result_reason="",
        black_name="黑棋",
        white_name="白棋",
    )


def _raising_find_move(self, *args, **kwargs):
    """测试注入：模拟引擎内部抛异常。"""
    raise RuntimeError(SECRET_DETAIL)


def _raising_analyze(self, *args, **kwargs):
    """测试注入：模拟引擎内部抛异常。"""
    raise RuntimeError(SECRET_DETAIL)


def _raising_review(self, *args, **kwargs):
    """测试注入：模拟复盘过程抛异常。"""
    raise RuntimeError(SECRET_DETAIL)


def _no_move(self, *args, **kwargs):
    """测试注入：引擎没有找到任何落点。"""
    return None


def _no_analysis(self, *args, **kwargs):
    """测试注入：分析没有返回结果。"""
    return None


class AIWorkerErrorTests(unittest.TestCase):
    """AI 搜索线程的失败上报必须是通用文案。"""

    def test_search_failure_message_is_generic(self) -> None:
        """引擎抛异常时，界面只收到通用文案。"""
        messages: list[tuple[int, str]] = []
        worker = AIWorker(
            request_id=7,
            board_snapshot=blank_board(),
            player=Player.BLACK,
            difficulty=AIDifficulty.EASY,
        )
        worker.search_failed.connect(
            lambda rid, msg: messages.append((rid, msg)),
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_raising_find_move,
        ):
            with self.assertLogs(
                "gomoku.client.ai_worker", level="ERROR"
            ) as logs:
                worker.start()
                self.assertTrue(worker.wait(5000))

        self.assertEqual(len(messages), 1)
        request_id, message = messages[0]
        self.assertEqual(request_id, 7)
        self.assertNotIn(SECRET_DETAIL, message)
        self.assertNotIn("RuntimeError", message)
        self.assertIn("请重试", message)

        # 细节必须留在日志里，并且带上异常堆栈。
        self.assertTrue(logs.records)
        record = logs.records[0]
        self.assertIsNotNone(record.exc_info)
        self.assertIn(SECRET_DETAIL, str(record.exc_info[1]))

    def test_analysis_failure_message_is_generic(self) -> None:
        """提示分析线程抛异常时，界面只收到通用文案。"""
        messages: list[tuple[int, str]] = []
        worker = AIHintWorker(
            request_id=3,
            board_snapshot=blank_board(),
            player=Player.BLACK,
            difficulty=AIDifficulty.EASY,
        )
        worker.search_failed.connect(
            lambda rid, msg: messages.append((rid, msg)),
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.analyze",
            new=_raising_analyze,
        ):
            with self.assertLogs(
                "gomoku.client.ai_worker", level="ERROR"
            ) as logs:
                worker.start()
                self.assertTrue(worker.wait(5000))

        self.assertEqual(len(messages), 1)
        request_id, message = messages[0]
        self.assertEqual(request_id, 3)
        self.assertNotIn(SECRET_DETAIL, message)
        self.assertNotIn("RuntimeError", message)
        self.assertIn("请重试", message)

        self.assertTrue(logs.records)
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_cancelled_search_stays_silent(self) -> None:
        """已取消的搜索即便遇到异常也不打扰界面。"""
        messages: list[tuple[int, str]] = []
        worker = AIWorker(
            request_id=1,
            board_snapshot=blank_board(),
            player=Player.BLACK,
            difficulty=AIDifficulty.EASY,
        )
        worker.search_failed.connect(
            lambda rid, msg: messages.append((rid, msg)),
            Qt.ConnectionType.DirectConnection,
        )

        def raising_after_cancel(self, *, cancel_event=None, **kwargs):
            """取消之后才抛异常：界面应当保持安静。"""
            if cancel_event is not None:
                cancel_event.set()
            raise RuntimeError(SECRET_DETAIL)

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=raising_after_cancel,
        ):
            worker.start()
            self.assertTrue(worker.wait(5000))

        self.assertEqual(messages, [])

    def test_no_move_result_uses_generic_message(self) -> None:
        """引擎没有返回落点时，界面同样只收到通用文案。"""
        messages: list[tuple[int, str]] = []
        worker = AIWorker(
            request_id=9,
            board_snapshot=blank_board(),
            player=Player.BLACK,
            difficulty=AIDifficulty.EASY,
        )
        worker.search_failed.connect(
            lambda rid, msg: messages.append((rid, msg)),
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_no_move,
        ):
            with self.assertLogs(
                "gomoku.client.ai_worker", level="WARNING"
            ) as logs:
                worker.start()
                self.assertTrue(worker.wait(5000))

        self.assertEqual(messages, [(9, SEARCH_FAILED_MESSAGE)])
        self.assertTrue(logs.records)

    def test_no_analysis_result_uses_generic_message(self) -> None:
        """分析没有返回结果时，界面同样只收到通用文案。"""
        messages: list[tuple[int, str]] = []
        worker = AIHintWorker(
            request_id=4,
            board_snapshot=blank_board(),
            player=Player.BLACK,
            difficulty=AIDifficulty.EASY,
        )
        worker.search_failed.connect(
            lambda rid, msg: messages.append((rid, msg)),
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.analyze",
            new=_no_analysis,
        ):
            with self.assertLogs(
                "gomoku.client.ai_worker", level="WARNING"
            ) as logs:
                worker.start()
                self.assertTrue(worker.wait(5000))

        self.assertEqual(messages, [(4, ANALYSIS_FAILED_MESSAGE)])
        self.assertTrue(logs.records)


class AIReviewWorkerErrorTests(unittest.TestCase):
    """复盘线程的失败上报必须是通用文案。"""

    def test_review_failure_message_is_generic(self) -> None:
        messages: list[str] = []
        worker = AIReviewWorker(sample_record())
        worker.review_failed.connect(
            messages.append,
            Qt.ConnectionType.DirectConnection,
        )

        with mock.patch(
            "gomoku.client.review_worker.review_record",
            new=_raising_review,
        ):
            with self.assertLogs(
                "gomoku.client.review_worker", level="ERROR"
            ) as logs:
                worker.start()
                self.assertTrue(worker.wait(5000))

        self.assertEqual(len(messages), 1)
        message = messages[0]
        self.assertNotIn(SECRET_DETAIL, message)
        self.assertNotIn("RuntimeError", message)
        self.assertIn("请重试", message)

        self.assertTrue(logs.records)
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIn(SECRET_DETAIL, str(logs.records[0].exc_info[1]))

    def test_cancelled_review_stays_silent(self) -> None:
        """已取消的复盘遇到异常时不打扰界面。"""
        messages: list[str] = []
        worker = AIReviewWorker(sample_record())
        worker.review_failed.connect(
            messages.append,
            Qt.ConnectionType.DirectConnection,
        )

        def raising_after_cancel(self, *args, **kwargs):
            worker.cancel()
            raise RuntimeError(SECRET_DETAIL)

        with mock.patch(
            "gomoku.client.review_worker.review_record",
            new=raising_after_cancel,
        ):
            worker.start()
            self.assertTrue(worker.wait(5000))

        self.assertEqual(messages, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
