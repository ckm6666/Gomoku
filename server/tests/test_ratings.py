"""Glicko-2 积分、段位与赛季软重置纯函数测试。"""

from __future__ import annotations

import unittest

from server.app.glicko2 import (
    MAX_RD,
    RATING_CENTER,
    START_RD,
    START_VOLATILITY,
    GlickoState,
    calculate_pair as calculate_glicko_pair,
    season_soft_reset,
)
from server.app.ratings import (
    MIN_RATING,
    START_RATING,
    calculate_pair,
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


class Glicko2Tests(unittest.TestCase):
    def test_equal_rating_decisive_game_is_symmetric(self) -> None:
        black, white = calculate_pair(
            START_RATING,
            START_RD,
            START_VOLATILITY,
            START_RATING,
            START_RD,
            START_VOLATILITY,
            1.0,
        )
        self.assertGreater(black.after, black.before)
        self.assertLess(white.after, white.before)
        self.assertEqual(black.delta, -white.delta)
        self.assertLess(black.rd_after, black.rd_before)
        self.assertLess(white.rd_after, white.rd_before)

    def test_draw_keeps_rating_but_reduces_rd(self) -> None:
        black, white = calculate_pair(
            1200,
            START_RD,
            START_VOLATILITY,
            1200,
            START_RD,
            START_VOLATILITY,
            0.5,
        )
        self.assertEqual(black.after, 1200)
        self.assertEqual(white.after, 1200)
        self.assertLess(black.rd_after, START_RD)

    def test_underdog_gains_more_than_favorite(self) -> None:
        underdog, favorite = calculate_pair(
            1200,
            START_RD,
            START_VOLATILITY,
            1600,
            START_RD,
            START_VOLATILITY,
            1.0,
        )
        self.assertGreater(underdog.delta, 0)
        self.assertLess(favorite.delta, 0)
        self.assertGreater(underdog.delta, favorite.delta)

    def test_settled_player_moves_less(self) -> None:
        newcomer, _ = calculate_pair(
            1200, START_RD, START_VOLATILITY, 1200, START_RD,
            START_VOLATILITY, 1.0,
        )
        settled, _ = calculate_pair(
            1200, 40.0, START_VOLATILITY, 1200, 40.0,
            START_VOLATILITY, 1.0,
        )
        self.assertGreater(newcomer.delta, settled.delta)
        self.assertGreater(settled.delta, 0)

    def test_validation_and_clamp(self) -> None:
        with self.assertRaises(ValueError):
            calculate_pair(
                1200, START_RD, START_VOLATILITY,
                1200, START_RD, START_VOLATILITY, 0.7,
            )
        _, loser = calculate_pair(
            1200,
            START_RD,
            START_VOLATILITY,
            MIN_RATING,
            START_RD,
            START_VOLATILITY,
            1.0,
        )
        # 权威算法允许极端下克上，但展示积分不得低于下限。
        self.assertGreaterEqual(loser.after, MIN_RATING)

    def test_rd_clamped_to_supported_range(self) -> None:
        low = GlickoState.from_stored(1200, 1.0, 0.06)
        high = GlickoState.from_stored(1200, 9999.0, 0.06)
        self.assertEqual(low.rd, 30.0)
        self.assertEqual(high.rd, MAX_RD)

    def test_glicko_pair_does_not_drift_volatility(self) -> None:
        black, _ = calculate_glicko_pair(
            GlickoState.from_stored(1200, 80.0, 0.06),
            GlickoState.from_stored(1200, 80.0, 0.06),
            1.0,
        )
        self.assertLess(black.after.volatility, 0.5)
        self.assertGreater(black.after.volatility, 0.0)


class SeasonResetTests(unittest.TestCase):
    def test_soft_reset_compresses_rating_and_raises_rd(self) -> None:
        rating, rd = season_soft_reset(1600, 50.0)
        self.assertEqual(rating, int(round(RATING_CENTER + 200.0)))
        self.assertGreaterEqual(rd, 120.0)
        self.assertLessEqual(rd, MAX_RD)

    def test_soft_reset_never_below_floor(self) -> None:
        rating, _ = season_soft_reset(100, 300.0)
        self.assertGreaterEqual(rating, MIN_RATING)


if __name__ == "__main__":
    unittest.main(verbosity=2)
