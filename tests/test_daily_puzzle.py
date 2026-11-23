"""每日残局选择、连续通关进度与页面卡片测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.daily_puzzle import (  # noqa: E402
    advance_streak,
    daily_puzzle,
    load_progress,
    record_result,
)
from gomoku.client.puzzle_page import PuzzlePage  # noqa: E402
from gomoku.shared.puzzle import (  # noqa: E402
    Puzzle,
    PuzzleDifficulty,
)
from gomoku.shared.rules import Player  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def _puzzle(index: int) -> Puzzle:
    return Puzzle(
        id=f"p{index}",
        title=f"残局{index}",
        difficulty=PuzzleDifficulty.EASY,
        moves=(),
        to_move=Player.BLACK,
        max_player_moves=5,
    )


class DailySelectionTests(unittest.TestCase):
    def test_same_day_is_stable_and_next_day_changes(self) -> None:
        puzzles = [_puzzle(i) for i in range(5)]
        today = date(2026, 11, 23)
        first = daily_puzzle(puzzles, today)
        self.assertIsNotNone(first)
        self.assertIs(daily_puzzle(puzzles, today), first)
        self.assertIsNot(
            daily_puzzle(puzzles, today + timedelta(days=1)), first
        )

    def test_empty_bank_returns_none(self) -> None:
        self.assertIsNone(daily_puzzle((), date(2026, 11, 23)))


class StreakTests(unittest.TestCase):
    def test_first_completion(self) -> None:
        self.assertEqual(
            advance_streak("", 0, date(2026, 11, 23), True),
            ("2026-11-23", 1),
        )

    def test_same_day_does_not_double_count(self) -> None:
        self.assertEqual(
            advance_streak("2026-11-23", 3, date(2026, 11, 23), True),
            ("2026-11-23", 3),
        )

    def test_consecutive_day_increments(self) -> None:
        self.assertEqual(
            advance_streak("2026-11-22", 3, date(2026, 11, 23), True),
            ("2026-11-23", 4),
        )

    def test_skipped_day_resets(self) -> None:
        self.assertEqual(
            advance_streak("2026-11-20", 7, date(2026, 11, 23), True),
            ("2026-11-23", 1),
        )

    def test_failure_keeps_progress(self) -> None:
        self.assertEqual(
            advance_streak("2026-11-22", 3, date(2026, 11, 23), False),
            ("2026-11-22", 3),
        )

    def test_settings_round_trip(self) -> None:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            settings = QSettings(
                str(Path(tmp) / "daily.ini"),
                QSettings.Format.IniFormat,
            )
            self.assertEqual(load_progress(settings), ("", 0))
            record_result(settings, date(2026, 11, 23), True)
            self.assertEqual(
                load_progress(settings), ("2026-11-23", 1)
            )
            record_result(settings, date(2026, 11, 24), True)
            self.assertEqual(
                load_progress(settings), ("2026-11-24", 2)
            )


class DailyPuzzlePageTests(unittest.TestCase):
    def test_daily_card_records_completion(self) -> None:
        puzzles = [_puzzle(i) for i in range(3)]
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            settings = QSettings(
                str(Path(tmp) / "daily.ini"),
                QSettings.Format.IniFormat,
            )
            page = PuzzlePage(puzzles, settings=settings)
            daily = page._daily_puzzle
            self.assertIsNotNone(daily)
            assert daily is not None

            selected = []
            page.daily_selected.connect(selected.append)
            page.daily_button.click()
            self.assertEqual(selected, [daily])

            page.record_daily_result(daily.id, True)
            self.assertTrue(page._daily_completed)
            self.assertEqual(page._daily_streak, 1)
            self.assertIn("今日已通关", page.daily_status_label.text())
            self.assertEqual(page.daily_button.text(), "再次挑战")

            # 同一天再次通关不会增加连续天数。
            page.record_daily_result(daily.id, True)
            self.assertEqual(page._daily_streak, 1)
            page.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
