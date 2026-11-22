"""从账号统计推导成就的辅助函数。"""

from __future__ import annotations

from gomoku.shared.achievements import (
    AchievementStats,
    unlocked_achievement_keys,
)
from server.app.models import User


def stats_for_user(user: User) -> AchievementStats:
    """抓取用户当前统计快照；返回的是不可变副本，可安全地用于前后对比。"""
    return AchievementStats(
        wins=user.wins,
        rated_games=user.rated_games,
        current_streak=user.current_win_streak,
        best_streak=user.best_win_streak,
        rating=user.rating,
        peak_rating=user.peak_rating,
    )


def achievement_keys_for_user(user: User) -> tuple[str, ...]:
    """按用户当前统计返回已解锁成就 key（目录顺序）。"""
    return unlocked_achievement_keys(stats_for_user(user))


__all__ = ["stats_for_user", "achievement_keys_for_user"]
