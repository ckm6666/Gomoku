"""棋谱时间线与复盘面板测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.replay import ReplayError, ReplayTimeline  # noqa: E402
from gomoku.client.replay_panel import ReplayPanel  # noqa: E402
from gomoku.client.review import MoveReview, ReviewResult  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def _record(
    winner: Player | None = Player.BLACK,
    *,
    rule_mode: str = "freestyle",
) -> GameRecord:
    moves = (
        Move(7, 3, Player.BLACK),
        Move(9, 3, Player.WHITE),
        Move(7, 4, Player.BLACK),
        Move(9, 4, Player.WHITE),
        Move(7, 5, Player.BLACK),
        Move(9, 5, Player.WHITE),
        Move(7, 6, Player.BLACK),
        Move(9, 6, Player.WHITE),
        Move(7, 7, Player.BLACK),
    )
    return GameRecord(
        record_id="replay-1",
        created_at="2026-01-01T00:00:00+00:00",
        mode="local",
        moves=moves,
        winner=winner,
        result_reason="five",
        black_name="黑棋",
        white_name="白棋",
        rule_mode=rule_mode,
    )


class ReplayTimelineTests(unittest.TestCase):
    def test_frames_and_final_winning_line(self) -> None:
        timeline = ReplayTimeline.from_record(_record())
        self.assertEqual(len(timeline.frames), 10)
        self.assertEqual(timeline.frames[0].move_count, 0)
        self.assertEqual(timeline.frames[-1].move_count, 9)
        self.assertIs(timeline.frames[-1].winner, Player.BLACK)
        self.assertEqual(len(timeline.frames[-1].winning_line), 5)
        self.assertEqual(timeline.frame_at(99).move_count, 9)
        self.assertEqual(timeline.frame_at(-1).move_count, 0)
        self.assertIn("第 1 手", timeline.move_description(1))
        self.assertIn("黑棋", timeline.move_description(1))

    def test_winner_mismatch_is_rejected(self) -> None:
        with self.assertRaises(ReplayError):
            ReplayTimeline.from_record(_record(winner=Player.WHITE))


class ReplayPanelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        apply_app_theme(cls.app)

    def setUp(self) -> None:
        # 复盘是主窗口页面栈中的一页，不是独立窗口；测试里用一个
        # 宿主窗口承载它，可见性与布局断言才有意义。
        self.host = QWidget()
        self.host.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.host.setLayout(QVBoxLayout(self.host))
        self.window = ReplayPanel([_record()], parent=self.host)
        self.host.layout().addWidget(self.window)
        self.host.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.shutdown()
        self.host.close()
        self.app.processEvents()

    def test_navigation_buttons_update_board(self) -> None:
        self.assertEqual(self.window.index_label.text(), "第 0 / 9 手")
        self.assertTrue(self.window.next_button.isEnabled())

        self.window.next_button.click()
        self.app.processEvents()
        self.assertEqual(self.window.index_label.text(), "第 1 / 9 手")
        self.assertEqual(self.window.board_widget._view.move_count, 1)

        self.window.last_button.click()
        self.app.processEvents()
        self.assertEqual(self.window.index_label.text(), "第 9 / 9 手")
        self.assertIs(
            self.window.board_widget._view.winner, Player.BLACK
        )
        self.assertFalse(self.window.next_button.isEnabled())
        self.assertTrue(self.window.prev_button.isEnabled())

        self.window.first_button.click()
        self.app.processEvents()
        self.assertEqual(self.window.board_widget._view.move_count, 0)

    def test_empty_records_show_placeholder(self) -> None:
        panel = ReplayPanel([])
        try:
            self.assertIn("暂无", panel.move_info_label.text())
            self.assertFalse(panel.next_button.isEnabled())
        finally:
            panel.shutdown()

    def test_renju_record_shows_rule_in_metadata(self) -> None:
        """连珠棋谱在复盘元信息里标明规则；自由规则不追加。"""
        panel = ReplayPanel([_record(rule_mode="renju")])
        try:
            self.assertIn("连珠", panel.meta_label.text())
        finally:
            panel.shutdown()

        panel = ReplayPanel([_record()])
        try:
            self.assertNotIn("连珠", panel.meta_label.text())
        finally:
            panel.shutdown()


class ReplayReviewTests(unittest.TestCase):
    """AI 复盘标注要完整写进右侧落子列表，不能被省略号截断。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        apply_app_theme(cls.app)

    def setUp(self) -> None:
        self.host = QWidget()
        self.host.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.host.setLayout(QVBoxLayout(self.host))
        self.window = ReplayPanel([_record()], parent=self.host)
        self.host.layout().addWidget(self.window)
        self.host.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.shutdown()
        self.host.close()
        self.app.processEvents()

    def _apply_review(self) -> tuple[MoveReview, ...]:
        record = _record()
        reviews = tuple(
            MoveReview(
                move_no=index,
                player=move.player,
                move=(move.row, move.col),
                best_move=(7, 3 + index),
                played_score=100,
                best_score=200,
                loss=100,
                label="最佳" if index % 2 else "失误",
                win_rate_before=0.6,
                win_rate_after=0.5,
            )
            for index, move in enumerate(record.moves, start=1)
        )
        self.window._on_review_finished(
            ReviewResult(
                record_id=record.record_id,
                reviews=reviews,
                average_loss=100.0,
                accuracy=95,
                counts={"最佳": 5, "失误": 4},
            )
        )
        self.app.processEvents()
        return reviews

    def test_move_list_never_elides_text(self) -> None:
        """落子列表不省略文字：窄面板下改用换行。"""
        self.assertEqual(
            self.window.moves_list.textElideMode(),
            Qt.TextElideMode.ElideNone,
        )
        self.assertTrue(self.window.moves_list.wordWrap())

    def test_every_move_shows_its_own_verdict(self) -> None:
        """每一手都要在右侧落子列表里写出评定与推荐着法。"""
        reviews = self._apply_review()
        for index, review in enumerate(reviews, start=1):
            text = self.window.moves_list.item(index).text()
            self.assertIn(review.label, text)
            self.assertIn("推荐", text)
            self.assertNotIn("…", text)
            self.assertNotIn("...", text)

    def test_move_list_keeps_room_for_the_verdict(self) -> None:
        """落子列表要留得下评定，不能被棋谱列表挤成省略号。"""
        self.assertGreaterEqual(
            self.window.moves_panel.minimumWidth(), 160
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
