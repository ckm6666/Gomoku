"""每日残局选择与连续通关进度。

选择算法只依赖日期和题库顺序，因此同一天多次打开页面会得到同一题；
进度保存在 QSettings，跨重启保留。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional, Sequence

from PySide6.QtCore import QSettings

from gomoku.shared.puzzle import Puzzle

SETTINGS_LAST_DATE = "daily_puzzle/last_date"
SETTINGS_STREAK = "daily_puzzle/streak"


def daily_puzzle(
    puzzles: Sequence[Puzzle],
    today: Optional[date] = None,
) -> Optional[Puzzle]:
    """按日期确定性选择当天的残局。"""
    if not puzzles:
        return None
    day = today or date.today()
    return puzzles[day.toordinal() % len(puzzles)]


def advance_streak(
    last_date: str,
    streak: int,
    today: date,
    success: bool,
) -> tuple[str, int]:
    """返回新的 (最后通关日期, 连续天数)。

    失败、重复通关同一天都不会改变进度；跳过一天后再次通关会从 1 重新开始。
    """
    streak = max(0, int(streak))
    if not success:
        return last_date, streak
    today_text = today.isoformat()
    if last_date == today_text:
        return last_date, streak
    previous: Optional[date]
    try:
        previous = date.fromisoformat(last_date) if last_date else None
    except ValueError:
        previous = None
    if previous == today - timedelta(days=1):
        return today_text, streak + 1
    return today_text, 1


def load_progress(settings: QSettings) -> tuple[str, int]:
    last_date = str(settings.value(SETTINGS_LAST_DATE, "") or "")
    try:
        streak = int(settings.value(SETTINGS_STREAK, 0) or 0)
    except (TypeError, ValueError):
        streak = 0
    return last_date, max(0, streak)


def save_progress(
    settings: QSettings, last_date: str, streak: int
) -> None:
    settings.setValue(SETTINGS_LAST_DATE, last_date)
    settings.setValue(SETTINGS_STREAK, max(0, int(streak)))
    settings.sync()


def record_result(
    settings: QSettings,
    today: date,
    success: bool,
) -> tuple[str, int]:
    """记录一次每日残局结果并持久化。"""
    last_date, streak = load_progress(settings)
    new_date, new_streak = advance_streak(
        last_date, streak, today, success
    )
    if (new_date, new_streak) != (last_date, streak):
        save_progress(settings, new_date, new_streak)
    return new_date, new_streak


__all__ = [
    "SETTINGS_LAST_DATE",
    "SETTINGS_STREAK",
    "advance_streak",
    "daily_puzzle",
    "load_progress",
    "record_result",
    "save_progress",
]
