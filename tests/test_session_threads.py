"""人机会话的线程生命周期与提示分析生命周期测试。

覆盖两条审查意见：
* G10——关闭会话时对后台线程的等待必须有上限，不能用无参数的
  ``QThread.wait()`` 把 GUI 线程卡死；
* A13（会话侧）——提示分析必须可以被取消，新一轮分析必须清掉上一轮
  的结果，被取代或因棋盘变化而过期的结果必须被丢弃。

这些用例都是 unittest 风格，CI 的 ``unittest discover`` 也能收集到。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from typing import Optional
from unittest import mock

# 必须在导入 PySide6 之前设置，保证 CI/无桌面环境也能构造控件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, QThread, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.ai_engine import (  # noqa: E402
    AIDifficulty,
    CandidateScore,
    SearchAnalysis,
)
from gomoku.client.ai_worker import disconnect_signals  # noqa: E402
from gomoku.client.sessions import ai as ai_session_module  # noqa: E402
from gomoku.client.sessions.ai import (  # noqa: E402
    AIGameSession,
    shutdown_worker,
)
from gomoku.client.sessions.puzzle import PuzzleSession  # noqa: E402
from gomoku.shared.puzzle import (  # noqa: E402
    Puzzle,
    PuzzleDifficulty,
)
from gomoku.shared.rules import BOARD_SIZE, Player  # noqa: E402

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QT_APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QT_APP.processEvents()
    return bool(predicate())


def _fake_analyze(
    self,
    board_snapshot,
    player,
    difficulty,
    cancel_event=None,
    *,
    top_n=3,
    config_override=None,
):
    """测试注入：立即返回第一个空点作为推荐落点。"""
    del self, difficulty, cancel_event, top_n, config_override
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if board_snapshot[row][col] is None:
                move = (row, col)
                return SearchAnalysis(
                    best_move=move,
                    best_score=1,
                    candidates=(CandidateScore(move, 1),),
                    player=player,
                    difficulty=AIDifficulty.MASTER,
                    completed_depth=1,
                    nodes=1,
                    elapsed_seconds=0.0,
                    cancelled=False,
                    timed_out=False,
                    source="search",
                )
    return None


def _slow_analyze(
    self,
    board_snapshot,
    player,
    difficulty,
    cancel_event=None,
    *,
    top_n=3,
    config_override=None,
):
    """测试注入：阻塞到取消事件触发。"""
    del self, board_snapshot, player, difficulty, top_n, config_override
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.wait(0.01):
            return None
    return None


def _late_analyze(
    self,
    board_snapshot,
    player,
    difficulty,
    cancel_event=None,
    *,
    top_n=3,
    config_override=None,
):
    """测试注入：无视取消事件，模拟“迟到的分析结果”。"""
    del cancel_event
    time.sleep(0.3)
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if board_snapshot[row][col] is None:
                move = (row, col)
                return SearchAnalysis(
                    best_move=move,
                    best_score=1,
                    candidates=(CandidateScore(move, 1),),
                    player=player,
                    difficulty=AIDifficulty.MASTER,
                    completed_depth=1,
                    nodes=1,
                    elapsed_seconds=0.0,
                    cancelled=False,
                    timed_out=False,
                    source="search",
                )
    return None


def _fake_find_move(
    self,
    *,
    board_snapshot,
    player,
    difficulty,
    cancel_event=None,
):
    """测试注入：AI 立即应一手固定落点，让用例不依赖真实搜索速度。"""
    del self, player, difficulty, cancel_event
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            if board_snapshot[row][col] is None:
                return (row, col)
    return None


class _NeverEndingWorker(QObject):
    """模拟一个永远不结束的线程。

    ``wait()`` 永远返回 False，并且记录每次调用的超时参数：会话关闭时
    必须每次都带上明确的上限，任何一次无参等待都会让 GUI 线程卡死。
    """

    finished = Signal()
    move_ready = Signal(int, int, int)
    search_failed = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0
        self.wait_timeouts: list[Optional[int]] = []

    def cancel(self) -> None:
        self.cancel_calls += 1

    def wait(self, msecs: Optional[int] = None) -> bool:
        self.wait_timeouts.append(msecs)
        return False

    def detach(self) -> None:
        """与真实线程一致：断开所有出站信号。"""
        disconnect_signals((self.move_ready, self.search_failed, self.finished))


class _PlainWorker(_NeverEndingWorker):
    """没有 ``detach()`` 的线程：走按信号名断开的兜底逻辑。"""

    detach = None  # type: ignore[assignment]


class _FinishedWorker(_NeverEndingWorker):
    """模拟一个立刻结束的线程。"""

    def wait(self, msecs: Optional[int] = None) -> bool:
        self.wait_timeouts.append(msecs)
        return True


class ShutdownWorkerTests(unittest.TestCase):
    """G10：后台线程的等待必须始终有上限。"""

    def test_wait_is_always_bounded(self) -> None:
        """线程不结束时也不能出现无超时的等待。"""
        worker = _NeverEndingWorker()
        started = time.monotonic()
        with self.assertLogs(
            "gomoku.client.sessions.ai", level="WARNING"
        ) as logs:
            finished = shutdown_worker(worker, "测试线程")
        elapsed = time.monotonic() - started

        self.assertFalse(finished)
        self.assertGreaterEqual(worker.cancel_calls, 1)
        self.assertTrue(worker.wait_timeouts)
        for timeout in worker.wait_timeouts:
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0)
        self.assertLess(elapsed, 1.0)
        # 超时必须留下警告，便于事后排查。
        self.assertTrue(logs.records)
        self.assertIn("测试线程", logs.records[0].getMessage())

        # 收尾：让保活器释放这个假线程。
        worker.finished.emit()
        QT_APP.processEvents()

    def test_detached_worker_keeps_no_ui_connection(self) -> None:
        """放飞的线程必须与界面断开连接，避免退出后仍回调界面。"""
        worker = _NeverEndingWorker()
        received: list[int] = []
        worker.move_ready.connect(
            lambda row, col, _col: received.append(row),
        )

        with self.assertLogs(
            "gomoku.client.sessions.ai", level="WARNING"
        ):
            self.assertFalse(shutdown_worker(worker, "测试线程"))
        worker.move_ready.emit(0, 1, 2)
        self.assertEqual(received, [])

        # 收尾：让保活器释放这个假线程。
        worker.finished.emit()
        QT_APP.processEvents()

    def test_detached_worker_without_detach_api_also_disconnects(self) -> None:
        """线程没有 detach() 时也要断开已知的信号。"""
        worker = _PlainWorker()
        received: list[int] = []
        worker.move_ready.connect(
            lambda row, col, _col: received.append(row),
        )

        with self.assertLogs(
            "gomoku.client.sessions.ai", level="WARNING"
        ):
            self.assertFalse(shutdown_worker(worker, "测试线程"))
        worker.move_ready.emit(0, 1, 2)
        self.assertEqual(received, [])

        worker.finished.emit()
        QT_APP.processEvents()

    def test_finished_worker_returns_true(self) -> None:
        """线程及时结束时直接返回成功。"""
        worker = _FinishedWorker()
        self.assertTrue(shutdown_worker(worker, "测试线程"))
        self.assertEqual(len(worker.wait_timeouts), 1)
        self.assertGreater(worker.wait_timeouts[0], 0)


class DetachedThreadExitTests(unittest.TestCase):
    """进程退出兜底：被放飞的线程要有时间自行收尾。"""

    def test_exit_wait_lets_detached_thread_finish(self) -> None:
        """退出前的有界等待会让线程有机会自己结束。"""

        class _ShortThread(QThread):
            """很快就会结束的线程。"""

            def run(self) -> None:
                time.sleep(0.2)

        keeper = ai_session_module._DETACHED_THREADS
        worker = _ShortThread()
        worker.start()
        try:
            keeper.keep(worker)
            keeper.wait_all(2000)
            self.assertFalse(worker.isRunning())
        finally:
            if worker in keeper._threads:
                keeper._threads.remove(worker)


class SessionShutdownTests(unittest.TestCase):
    """G10：会话关闭不能被卡住的线程拖住。"""

    def test_ai_session_shutdown_does_not_block(self) -> None:
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        worker = _NeverEndingWorker()
        session._ai_thread = worker  # 注入一个不会结束的线程

        started = time.monotonic()
        with self.assertLogs(
            "gomoku.client.sessions.ai", level="WARNING"
        ):
            session.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(worker.cancel_calls, 1)
        self.assertTrue(worker.wait_timeouts)
        for timeout in worker.wait_timeouts:
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0)
        self.assertIsNone(session.ai_thread)

        worker.finished.emit()
        QT_APP.processEvents()

    def test_puzzle_session_shutdown_does_not_block(self) -> None:
        puzzle = Puzzle(
            id="p1",
            title="测试残局",
            difficulty=PuzzleDifficulty.EASY,
            moves=(),
            to_move=Player.BLACK,
            max_player_moves=3,
            defender="easy",
        )
        session = PuzzleSession(puzzle)
        worker = _NeverEndingWorker()
        session._ai_thread = worker  # 注入一个不会结束的线程

        started = time.monotonic()
        with self.assertLogs(
            "gomoku.client.sessions.ai", level="WARNING"
        ):
            session.shutdown()
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 1.0)
        self.assertGreaterEqual(worker.cancel_calls, 1)
        self.assertTrue(worker.wait_timeouts)
        for timeout in worker.wait_timeouts:
            self.assertIsNotNone(timeout)
            self.assertGreater(timeout, 0)
        self.assertIsNone(session.ai_thread)

        worker.finished.emit()
        QT_APP.processEvents()


class HintLifecycleTests(unittest.TestCase):
    """A13（会话侧）：提示分析可取消、结果不串场。

    所有用例都替换掉真实引擎：AI 应手与局面分析都用确定性的假实现，
    这样用例既不依赖搜索速度，也不会把真实线程留在后台。
    """

    def _session_ready_for_hint(self) -> AIGameSession:
        """构造一个轮到玩家、可以请求提示的会话。

        AI 的应手用假引擎固定为“第一个空点”，棋盘状态完全可预期。
        """
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_fake_find_move,
        ):
            self.assertTrue(session.request_move(7, 7))
            self.assertTrue(
                wait_until(
                    lambda: session.view.move_count == 2
                    and session.ai_thread is None
                )
            )
        return session

    def _shutdown(self, session: AIGameSession) -> None:
        """关闭会话并确认没有线程被留在后台。"""
        session.shutdown()
        self.assertIsNone(session.ai_thread)
        self.assertIsNone(session.hint_thread)

    def test_cancel_hint_stops_running_analysis(self) -> None:
        """取消后立刻恢复“可以再次请求提示”的状态。"""
        session = self._session_ready_for_hint()
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_slow_analyze,
            ):
                self.assertTrue(session.request_hint())
                self.assertTrue(session.hint_running)
                self.assertFalse(session.can_hint)

                self.assertTrue(session.cancel_hint())
                self.assertFalse(session.hint_running)
                self.assertTrue(session.can_hint)
                self.assertTrue(
                    wait_until(lambda: session.hint_thread is None)
                )
        finally:
            self._shutdown(session)

    def test_cancel_hint_without_analysis_is_safe(self) -> None:
        """没有在途分析时调用取消接口不会出错。"""
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        try:
            self.assertFalse(session.cancel_hint())
            self.assertFalse(session.hint_running)
            self.assertIsNone(session.hint_analysis)
        finally:
            self._shutdown(session)

    def test_cancel_hint_wait_is_bounded(self) -> None:
        """取消提示时，等待线程退出的时间也必须有上限。"""
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        worker = _NeverEndingWorker()
        session._hint_thread = worker  # 注入一个不会自己结束的线程
        session._hint_pending = True

        try:
            started = time.monotonic()
            self.assertTrue(session.cancel_hint())
            elapsed = time.monotonic() - started

            self.assertFalse(session.hint_running)
            self.assertGreaterEqual(worker.cancel_calls, 1)
            self.assertTrue(worker.wait_timeouts)
            for timeout in worker.wait_timeouts:
                self.assertIsNotNone(timeout)
                self.assertGreater(timeout, 0)
            # 假线程的 wait() 立刻返回 False：不能出现无参等待，也不能卡住。
            self.assertLess(elapsed, 1.0)
        finally:
            # 假线程从未真正运行；清掉引用后再正常关闭会话。
            session._hint_thread = None
            session._hint_pending = False
            session.shutdown()

    def test_cancel_hint_clears_cached_result(self) -> None:
        """取消会清掉已经算好的提示，并通知界面清除标记。"""
        session = self._session_ready_for_hint()
        cleared: list[bool] = []
        session.hint_cleared.connect(lambda: cleared.append(True))
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_fake_analyze,
            ):
                self.assertTrue(session.request_hint())
                self.assertTrue(
                    wait_until(
                        lambda: session.hint_analysis is not None
                        and session.hint_thread is None
                    )
                )
                self.assertFalse(session.hint_running)

                before = len(cleared)
                # 分析已经算完：没有在途分析可取消，但缓存结果照样作废。
                self.assertTrue(session.cancel_hint())
                self.assertIsNone(session.hint_analysis)
                self.assertGreater(len(cleared), before)
        finally:
            self._shutdown(session)

    def test_new_hint_request_clears_previous_result(self) -> None:
        """新一轮分析开始前必须清掉上一轮的结果。"""
        session = self._session_ready_for_hint()
        cleared: list[bool] = []
        session.hint_cleared.connect(lambda: cleared.append(True))
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_fake_analyze,
            ):
                self.assertTrue(session.request_hint())
                self.assertTrue(
                    wait_until(
                        lambda: session.hint_analysis is not None
                        and session.hint_thread is None
                    )
                )

                before = len(cleared)
                self.assertTrue(session.request_hint())
                # 上一轮的结果立刻作废，界面不会继续显示陈旧推荐。
                self.assertIsNone(session.hint_analysis)
                self.assertGreater(len(cleared), before)
                self.assertTrue(
                    wait_until(
                        lambda: session.hint_analysis is not None
                        and session.hint_thread is None
                    )
                )
        finally:
            self._shutdown(session)

    def test_superseded_hint_result_is_dropped(self) -> None:
        """被取消的那一轮迟到的结果必须被丢弃。"""
        session = self._session_ready_for_hint()
        hints: list[SearchAnalysis] = []
        session.hint_ready.connect(hints.append)
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_late_analyze,
            ):
                self.assertTrue(session.request_hint())
                # 在结果送达之前取消：这一轮的结果已经没有意义。
                self.assertTrue(session.cancel_hint())
                self.assertTrue(
                    wait_until(lambda: session.hint_thread is None)
                )
                self.assertEqual(hints, [])
                self.assertIsNone(session.hint_analysis)
        finally:
            self._shutdown(session)

    def test_hint_result_after_board_change_is_dropped(self) -> None:
        """棋盘已经变化时，迟到的提示结果必须被丢弃。"""
        session = self._session_ready_for_hint()
        hints: list[SearchAnalysis] = []
        session.hint_ready.connect(hints.append)
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.find_move",
                new=_fake_find_move,
            ), mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_late_analyze,
            ):
                self.assertTrue(session.request_hint())
                # 玩家落子：棋盘变化，这一轮提示立即失效。
                self.assertTrue(session.request_move(0, 1))
                self.assertTrue(
                    wait_until(lambda: session.hint_thread is None)
                )
                self.assertEqual(hints, [])
                self.assertIsNone(session.hint_analysis)
        finally:
            self._shutdown(session)

    def test_hint_result_delivered_for_current_request(self) -> None:
        """正常流程下提示结果仍然会送达界面。"""
        session = self._session_ready_for_hint()
        hints: list[SearchAnalysis] = []
        session.hint_ready.connect(hints.append)
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_fake_analyze,
            ):
                self.assertTrue(session.request_hint())
                self.assertTrue(
                    wait_until(
                        lambda: hints and session.hint_thread is None
                    )
                )
                self.assertEqual(len(hints), 1)
                self.assertIs(session.hint_analysis, hints[0])
                self.assertFalse(session.hint_running)
                self.assertTrue(session.can_hint)
        finally:
            self._shutdown(session)

    def test_hint_can_be_requested_again_right_after_cancel(self) -> None:
        """取消之后可以立刻发起新一轮分析，旧线程仍在收尾也不受影响。"""
        session = self._session_ready_for_hint()
        hints: list[SearchAnalysis] = []
        session.hint_ready.connect(hints.append)
        try:
            with mock.patch(
                "gomoku.client.ai_worker.GomokuAI.analyze",
                new=_late_analyze,
            ):
                self.assertTrue(session.request_hint())
                self.assertTrue(session.cancel_hint())
                self.assertTrue(session.can_hint)
                # 旧线程还在收尾，新的一轮照样可以发起。
                self.assertTrue(session.request_hint())
                self.assertTrue(session.hint_running)

                self.assertTrue(
                    wait_until(lambda: session.hint_thread is None)
                )
                # 只有新一轮的结果被采纳，上一轮的迟到结果被丢弃。
                self.assertEqual(len(hints), 1)
                self.assertIs(session.hint_analysis, hints[0])
        finally:
            self._shutdown(session)


if __name__ == "__main__":
    unittest.main(verbosity=2)
