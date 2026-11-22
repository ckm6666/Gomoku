"""成就目录与进度推导的单元测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.achievements import (
    ACHIEVEMENTS,
    ACHIEVEMENT_KEYS,
    ACHIEVEMENT_TOTAL,
    TIER_COLORS,
    AchievementStats,
    achievement_progress,
    achievement_unlocked,
    new_achievement_keys,
    normalize_achievement_keys,
    stats_from_mapping,
    top_achievement_keys,
    unlocked_achievement_keys,
)


class AchievementCatalogTests(unittest.TestCase):
    def test_catalog_keys_are_unique_and_colored(self) -> None:
        keys = [spec.key for spec in ACHIEVEMENTS]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(len(keys), ACHIEVEMENT_TOTAL)
        for spec in ACHIEVEMENTS:
            self.assertIn(spec.key, ACHIEVEMENT_KEYS)
            self.assertIn(spec.tier, TIER_COLORS)
            self.assertGreater(spec.target, 0)
            self.assertIn(spec.metric, ("wins", "streak", "peak_rating"))

    def test_new_account_only_has_starting_rank_medal(self) -> None:
        stats = AchievementStats()
        unlocked = unlocked_achievement_keys(stats)
        self.assertEqual(unlocked, ("rank_gold",))
        self.assertEqual(AchievementStats().peak_rating, 1200)

    def test_win_streak_and_rank_progress(self) -> None:
        stats = AchievementStats(
            wins=25,
            rated_games=40,
            current_streak=5,
            best_streak=6,
            rating=1500,
            peak_rating=1520,
        )
        unlocked = unlocked_achievement_keys(stats)
        self.assertIn("first_win", unlocked)
        self.assertIn("wins_10", unlocked)
        self.assertIn("wins_25", unlocked)
        self.assertNotIn("wins_50", unlocked)
        self.assertIn("streak_3", unlocked)
        self.assertIn("streak_5", unlocked)
        self.assertNotIn("streak_10", unlocked)
        self.assertIn("rank_diamond", unlocked)
        self.assertNotIn("rank_master", unlocked)

        wins_50 = next(spec for spec in ACHIEVEMENTS if spec.key == "wins_50")
        current, target, percent = achievement_progress(wins_50, stats)
        self.assertEqual((current, target, percent), (25, 50, 50))
        self.assertFalse(achievement_unlocked(wins_50, stats))

    def test_stats_from_mapping_tolerates_missing_or_bad_fields(self) -> None:
        stats = stats_from_mapping(
            {
                "wins": 7,
                "rated_games": "bad",
                "current_win_streak": True,
                "best_win_streak": 4,
                "rating": 1300,
                "peak_rating": None,
            }
        )
        self.assertEqual(stats.wins, 7)
        self.assertEqual(stats.rated_games, 0)
        self.assertEqual(stats.current_streak, 0)
        self.assertEqual(stats.best_streak, 4)
        self.assertEqual(stats.rating, 1300)
        self.assertEqual(stats.peak_rating, 1300)

    def test_normalize_and_diff(self) -> None:
        normalized = normalize_achievement_keys(
            ["wins_100", "nope", "first_win", "first_win", 3]
        )
        self.assertEqual(normalized, ("first_win", "wins_100"))
        self.assertEqual(normalize_achievement_keys("first_win"), ())

        new = new_achievement_keys(
            ("rank_gold", "first_win"),
            ("rank_gold", "first_win", "streak_3"),
        )
        self.assertEqual(new, ("streak_3",))

    def test_top_medals_prefer_rarity(self) -> None:
        keys = ("first_win", "rank_gold", "rank_diamond", "wins_10")
        top = top_achievement_keys(keys, 3)
        self.assertEqual(top, ("rank_diamond", "rank_gold", "wins_10"))
        self.assertEqual(top_achievement_keys(keys, 0), ())


if __name__ == "__main__":
    unittest.main(verbosity=2)
