"""Elo 积分与段位纯函数测试。"""

from __future__ import annotations

import unittest

from server.app.ratings import (
    MIN_RATING,
    START_RATING,
    calculate_pair,
    expected_score,
    k_factor,
    rank_for_rating,
    rank_progress_percent,
)


class RankTierTests(unittest.TestCase):
    def test_default_rating_is_gold(self) -> None:
        self.assertEqual(rank_for_rating(START_RATING).name, "黄金")

    def test_tier_boundaries(self) -> None:
        self.assertEqual(rank_for_rating(0).name, "青铜")
        self.assertEqual(rank_for_rating(1099).name, "青铜")
        self.assertEqual(rank_for_rating(1100).name, "白银")
        self.assertEqual(rank_for_rating(1199).name, "白银")
        self.assertEqual(rank_for_rating(1200).name, "黄金")
        self.assertEqual(rank_for_rating(1650).name, "大师")
        self.assertEqual(rank_for_rating(1800).name, "王者")
        self.assertIsNone(rank_for_rating(2500).next_rating)

    def test_progress(self) -> None:
        self.assertEqual(rank_progress_percent(1200), 0)
        self.assertEqual(rank_progress_percent(2500), 100)
        self.assertGreater(rank_progress_percent(1275), 0)
        self.assertLess(rank_progress_percent(1275), 100)


class EloTests(unittest.TestCase):
    def test_expected_score_symmetry(self) -> None:
        self.assertAlmostEqual(expected_score(1200, 1200), 0.5)
        self.assertAlmostEqual(
            expected_score(1400, 1200),
            1.0 - expected_score(1200, 1400),
        )
        self.assertGreater(expected_score(1400, 1200), 0.5)

    def test_k_factor(self) -> None:
        self.assertEqual(k_factor(1200, 0), 40)
        self.assertEqual(k_factor(1200, 9), 40)
        self.assertEqual(k_factor(1200, 10), 32)
        self.assertEqual(k_factor(1700, 50), 24)
        self.assertEqual(k_factor(2000, 50), 16)

    def test_equal_rating_decisive_game(self) -> None:
        black, white = calculate_pair(1200, 0, 1200, 0, 1.0)
        self.assertEqual(black.after, 1220)
        self.assertEqual(white.after, 1180)
        self.assertEqual(black.delta, 20)
        self.assertEqual(white.delta, -20)

    def test_equal_rating_draw_stays_even(self) -> None:
        black, white = calculate_pair(1200, 0, 1200, 0, 0.5)
        self.assertEqual(black.after, 1200)
        self.assertEqual(white.after, 1200)

    def test_upset_gains_more_than_favorite(self) -> None:
        underdog, favorite = calculate_pair(1200, 20, 1600, 20, 1.0)
        self.assertGreater(underdog.delta, 0)
        self.assertLess(favorite.delta, 0)
        self.assertGreater(abs(underdog.delta), 10)

    def test_minimum_rating_clamp(self) -> None:
        _, loser = calculate_pair(1200, 0, MIN_RATING, 0, 1.0)
        self.assertGreaterEqual(loser.after, MIN_RATING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
