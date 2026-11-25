"""主窗口交互生命周期回归测试。

覆盖五件事：

* 切走对局页必须暂停会话（后台 AI 不再思考），回到对局页再恢复；
* 联机面板被切到后台时暂停界面活动，回到前台再恢复；
* 终局弹窗在模态层开着时挂起一次，而不是每 100ms 无限轮询；
* 提示按钮在分析进行中变成“取消提示”，新一轮分析清掉旧 tooltip；
* 本地棋谱写入失败时给用户一条非阻塞提示。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QTimer, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.ai_engine import (  # noqa: E402
    AIDifficulty,
    CandidateScore,
    SearchAnalysis,
)
from gomoku.client.main_window import GameMode, MainWindow  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.client.result_dialog import ResultDialog  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402


def wait_until(
    predicate,
    timeout: float = 6.0,
    app: QApplication | None = None,
) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    application = app or QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    application.processEvents()
    return bool(predicate())


def _fast_analyze(
    self,
    board_snapshot,
    player,
    difficulty,
    cancel_event=None,
    *,
    top_n=3,
    config_override=None,
):
    """测试注入：立即返回第一个空点作为提示落点。"""
    del self, difficulty, cancel_event, top_n, config_override
    for row in range(15):
        for col in range(15):
            if board_snapshot[row][col] is None:
                move = (row, col)
                return SearchAnalysis(
                    best_move=move,
                    best_score=42,
                    candidates=(
                        CandidateScore(move, 42),
                        CandidateScore((7, 8), 12),
                    ),
                    player=player,
                    difficulty=AIDifficulty.MASTER,
                    completed_depth=2,
                    nodes=10,
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
    """测试注入：可取消的慢速分析，制造“分析进行中”的状态。"""
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.wait(0.01):
            return None
    return _fast_analyze(
        self,
        board_snapshot,
        player,
        difficulty,
        cancel_event,
        top_n=top_n,
        config_override=config_override,
    )


class ClientLifecycleTests(unittest.TestCase):
    """主窗口的页面切换、弹窗与提示按钮。"""

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance() or QApplication([])
        cls.app = app
        app.setStyle("Fusion")
        apply_app_theme(app)

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.window = MainWindow(
            record_store=LocalRecordStore(
                Path(self._tmpdir.name) / "records"
            ),
            # 测试不联网：关掉启动时的自动更新检查。
            auto_update_check=False,
        )
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _click_cell(self, row: int, col: int) -> None:
        point = self.window.board_widget._point_for(row, col).toPoint()
        QTest.mouseClick(
            self.window.board_widget,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            point,
        )
        self.app.processEvents()

    def _start_local(self) -> None:
        self.window.local_button.click()
        self.app.processEvents()
        self.assertIs(self.window._game_mode, GameMode.LOCAL)

    def _start_ai(self, *, difficulty_index: int = 0) -> None:
        self.window.ai_button.click()
        self.app.processEvents()
        self.window.difficulty_combo.setCurrentIndex(difficulty_index)
        self.app.processEvents()
        self.assertIs(self.window._game_mode, GameMode.AI)

    def _play_local_win(self) -> None:
        """在双人模式落出黑棋五连（9 手，白棋只有四连）。"""
        pairs = (
            ((7, 3), (0, 0)),
            ((7, 4), (0, 1)),
            ((7, 5), (0, 2)),
            ((7, 6), (0, 3)),
        )
        for black, white in pairs:
            self._click_cell(*black)
            self._click_cell(*white)
        self._click_cell(7, 7)

    def _arm_result_dialog_button(
        self, text: str, timeout_ms: int = 5000
    ) -> dict:
        """轮询等待终局弹窗，并点击指定按钮。"""
        state = {"count": 0, "text": ""}
        deadline = time.monotonic() + timeout_ms / 1000.0

        def attempt() -> None:
            box = self.window.active_modal()
            if isinstance(box, ResultDialog):
                state["count"] += 1
                state["text"] = box.title_label.text()
                for button in (box.restart_button, box.view_button):
                    if button.text() == text:
                        button.click()
                        return
            if time.monotonic() < deadline:
                QTimer.singleShot(50, attempt)

        QTimer.singleShot(50, attempt)
        return state

    # ------------------------------------------------------------------
    # G8：离开对局页必须暂停会话
    # ------------------------------------------------------------------
    def test_leaving_game_page_pauses_and_reentering_resumes(self) -> None:
        """切到复盘页/联机页要暂停会话，回到对局页再恢复。"""
        self._start_ai()
        session = self.window.session
        self.assertTrue(session.accepts_human_input)

        # 切到复盘页：后台 AI 不能继续思考。
        self.window._open_replay_page()
        self.app.processEvents()
        self.assertIs(
            self.window.stack.currentWidget(), self.window._replay_panel
        )
        self.assertFalse(session.accepts_human_input)

        # 回到对局页：恢复会话。
        self.window._on_replay_panel_closed()
        self.app.processEvents()
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.assertTrue(session.accepts_human_input)

        # 切到联机页：同样必须暂停。
        self.window._open_online_page()
        self.app.processEvents()
        self.assertIs(
            self.window.stack.currentWidget(), self.window._online_panel
        )
        self.assertFalse(session.accepts_human_input)

    # ------------------------------------------------------------------
    # G9：联机面板的可见状态
    # ------------------------------------------------------------------
    def test_online_panel_visibility_follows_page_switch(self) -> None:
        """联机页隐藏时暂停界面定时器，回到前台再恢复。"""
        self.window._open_online_page()
        self.app.processEvents()
        panel = self.window._online_panel
        self.assertIsNotNone(panel)
        if panel is None:
            return
        self.assertFalse(panel._visible_paused)

        # 返回主菜单：面板隐藏，界面定时器与终局提示都暂停。
        self.window._return_to_menu_from_online()
        self.app.processEvents()
        self.assertTrue(panel._visible_paused)
        self.assertFalse(panel._clock_timer.isActive())

        # 再打开联机页：恢复。
        self.window._open_online_page()
        self.app.processEvents()
        self.assertFalse(panel._visible_paused)
        self.assertTrue(panel._clock_timer.isActive())

    # ------------------------------------------------------------------
    # A12：终局弹窗挂起而不是轮询
    # ------------------------------------------------------------------
    def test_result_dialog_is_deferred_while_a_modal_is_open(self) -> None:
        """模态层开着时终局弹窗只挂起一次，不再安排 100ms 轮询。"""
        self._start_local()
        with mock.patch.object(self.window, "_schedule_result_dialog"):
            self._play_local_win()
        self.app.processEvents()
        self.assertTrue(self.window.game.is_game_over)

        # 模拟“返回主菜单”的确认框正开着。
        self.window._modal_dialog_open = True
        generation = self.window._game_generation

        with mock.patch("gomoku.client.main_window.QTimer") as fake_timer:
            self.window._show_result_dialog(generation)

        fake_timer.singleShot.assert_not_called()
        self.assertEqual(
            self.window._pending_result_dialog, generation
        )
        self.assertFalse(self.window._result_dialog_open)

    def test_pending_result_dialog_appears_after_modal_closes(self) -> None:
        """模态层关闭后，被挂起的终局弹窗要补上。"""
        self._start_local()
        with mock.patch.object(self.window, "_schedule_result_dialog"):
            self._play_local_win()
        self.app.processEvents()

        self.window._modal_dialog_open = True
        generation = self.window._game_generation
        self.window._show_result_dialog(generation)
        self.assertIsNotNone(self.window._pending_result_dialog)

        state = self._arm_result_dialog_button("查看棋盘")
        self.window._modal_dialog_open = False
        self.window._flush_pending_result_dialog()
        self.assertTrue(wait_until(lambda: state["count"] == 1, 8.0, self.app))
        self.assertIsNone(self.window._pending_result_dialog)

    # ------------------------------------------------------------------
    # A13：提示按钮变成“取消提示”
    # ------------------------------------------------------------------
    def _wait_for_human_turn(self) -> None:
        self._click_cell(7, 7)
        self.assertTrue(
            wait_until(
                lambda: self.window.game.move_count == 2
                and self.window._ai_thread is None,
                6.0,
                self.app,
            )
        )
        self.assertIs(
            self.window.game.current_player, self.window._human_player
        )

    def test_hint_button_toggles_between_request_and_cancel(self) -> None:
        """分析进行中按钮变成“取消提示”，取消后变回“提示”。"""
        self._start_ai()
        self._wait_for_human_turn()

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.analyze", new=_slow_analyze
        ):
            self.window.hint_button.click()
            self.app.processEvents()
            # 分析进行中：文案变成“取消提示”，而且保持可点。
            self.assertEqual(self.window.hint_button.text(), "取消提示")
            self.assertTrue(self.window.hint_button.isEnabled())

            # 再点一次：取消这次分析。
            self.window.hint_button.click()
            self.app.processEvents()
            self.assertEqual(self.window.hint_button.text(), "提示")

    def test_new_hint_request_clears_stale_tooltip(self) -> None:
        """新一轮分析必须清掉上一轮的备选着法 tooltip。"""
        self._start_ai()
        self._wait_for_human_turn()

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.analyze", new=_fast_analyze
        ):
            self.window.hint_button.click()
            self.assertTrue(
                wait_until(
                    lambda: self.window.session.hint_thread is None
                    and self.window.board_widget._hint_cell is not None,
                    5.0,
                    self.app,
                )
            )
        self.assertIn("备选", self.window.hint_button.toolTip())

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.analyze", new=_slow_analyze
        ):
            self.window.hint_button.click()
            self.app.processEvents()
            # 新一轮分析开始：旧 tooltip 与旧标记都要作废。
            self.assertNotIn("备选", self.window.hint_button.toolTip())
            self.assertIsNone(self.window.board_widget._hint_cell)

    # ------------------------------------------------------------------
    # A21：棋谱写入失败要提示用户
    # ------------------------------------------------------------------
    def test_failed_record_write_shows_non_blocking_notice(self) -> None:
        """写盘失败时给一条非阻塞提示，而不是只记日志。"""
        self._start_local()
        with mock.patch.object(
            self.window._record_store, "append", return_value=False
        ), mock.patch.object(self.window, "_schedule_result_dialog"):
            self._play_local_win()
        self.app.processEvents()

        notice = self.window._transient_notice
        self.assertIsNotNone(notice)
        if notice is None:
            return
        self.assertFalse(notice.isHidden())
        self.assertIn("棋谱", notice.label.text())
        # 非阻塞：不能顺手把模态状态打开。
        self.assertFalse(self.window._modal_dialog_open)

    def test_successful_record_write_shows_no_notice(self) -> None:
        """写入成功时不弹提示。"""
        self._start_local()
        with mock.patch.object(
            self.window._record_store, "append", return_value=True
        ), mock.patch.object(self.window, "_schedule_result_dialog"):
            self._play_local_win()
        self.app.processEvents()

        notice = self.window._transient_notice
        self.assertIsNone(notice)


if __name__ == "__main__":
    unittest.main(verbosity=2)
