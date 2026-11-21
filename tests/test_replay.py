"""棋谱时间线与复盘窗口测试。"""

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
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.replay import ReplayError, ReplayTimeline  # noqa: E402
from gomoku.client.replay_window import ReplayWindow  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.shared.record import GameRecord  # noqa: E402
from gomoku.shared.rules import Move, Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def _record(winner: Player | None = Player.BLACK) -> GameRecord:
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


class ReplayWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        apply_app_theme(cls.app)

    def setUp(self) -> None:
        self.window = ReplayWindow([_record()])
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
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
        window = ReplayWindow([])
        try:
            self.assertIn("暂无", window.move_info_label.text())
            self.assertFalse(window.next_button.isEnabled())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
