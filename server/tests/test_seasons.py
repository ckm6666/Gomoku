"""赛季滚动、软重置与赛季资料接口测试。"""

from __future__ import annotations

import asyncio
import unittest
from datetime import timedelta

from sqlalchemy import select

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_QUICK_MATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    make_message,
    parse_game_state,
)
from server.app.db import create_engine, create_session_factory, utcnow_naive
from server.app.models import Season, SeasonRating, User
from server.app.seasons import ensure_current_season
from server.tests.helpers import ServerTestBase


class SeasonEndpointTests(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def test_current_season_endpoint_and_profile(self) -> None:
        first_token = self._register_access("season_a")
        second_token = self._register_access("season_b")
        with self.client.websocket_connect("/api/v1/ws") as first:
            with self.client.websocket_connect("/api/v1/ws") as second:
                for ws, token in ((first, first_token), (second, second_token)):
                    ws.send_json(
                        make_message(CLIENT_AUTH, {"access_token": token})
                    )
                    self.assertEqual(
                        ws.receive_json()["type"], SERVER_AUTHENTICATED
                    )
                first.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                first.receive_json()
                second.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                snapshots = []
                for ws in (first, second):
                    for _ in range(8):
                        message = ws.receive_json()
                        if message["type"] == SERVER_GAME_STATE:
                            snapshots.append(
                                parse_game_state(message["payload"])
                            )
                            break
                    else:
                        self.fail("未收到开局快照")
                first.send_json(make_message(CLIENT_RESIGN, {}))
                for ws in (first, second):
                    for _ in range(8):
                        message = ws.receive_json()
                        if message["type"] == SERVER_GAME_STATE:
                            snapshot = parse_game_state(message["payload"])
                            if snapshot.is_game_over:
                                break
                    else:
                        self.fail("未收到终局快照")

        season = self.client.get(
            "/api/v1/seasons/current",
            headers=self.auth_headers(first_token),
        )
        self.assertEqual(season.status_code, 200, season.text)
        payload = season.json()
        self.assertEqual(payload["season"]["number"], 1)
        self.assertGreaterEqual(payload["season"]["days_remaining"], 89)
        self.assertIn(payload["your_rank"], (1, 2))
        self.assertIsNotNone(payload["your_rating"])
        self.assertIsNotNone(payload["your_rd"])

        profile = self.client.get(
            "/api/v1/profile/me", headers=self.auth_headers(first_token)
        )
        self.assertEqual(profile.status_code, 200)
        profile_data = profile.json()
        self.assertEqual(profile_data["season"]["number"], 1)
        self.assertIn(profile_data["season_rank"], (1, 2))

        archive = self.client.get(
            "/api/v1/seasons/1/leaderboard",
            headers=self.auth_headers(first_token),
        )
        self.assertEqual(archive.status_code, 200)
        # 赛季尚未结束时归档表为空，但接口可用。
        self.assertEqual(archive.json()["total"], 0)

    def test_unknown_season_leaderboard_returns_404(self) -> None:
        token = self._register_access("season_missing")
        response = self.client.get(
            "/api/v1/seasons/99/leaderboard",
            headers=self.auth_headers(token),
        )
        self.assertEqual(response.status_code, 404)


class SeasonRolloverTests(ServerTestBase):
    def test_rollover_archives_stats_and_soft_resets_rating(self) -> None:
        response = self.register("season_roller")
        self.assertEqual(response.status_code, 201)
        user_id = response.json()["user"]["id"]
        now = utcnow_naive()

        async def scenario() -> tuple[int, int, float, int, int]:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    season = await session.scalar(
                        select(Season).order_by(Season.number.desc())
                    )
                    assert season is not None
                    season.ends_at = now - timedelta(days=1)
                    user = await session.get(User, user_id)
                    assert user is not None
                    user.rated_games = 12
                    user.wins = 9
                    user.losses = 3
                    user.rating = 1600
                    user.peak_rating = 1700
                    user.rating_rd = 50.0
                    user.rating_volatility = 0.06
                    await session.commit()
                info = await ensure_current_season(
                    factory, self.settings, now=now
                )
                async with factory() as session:
                    archived = await session.scalar(
                        select(SeasonRating).where(
                            SeasonRating.user_id == user_id
                        )
                    )
                    user = await session.get(User, user_id)
                    assert user is not None and archived is not None
                    return (
                        info.number,
                        archived.rating,
                        user.rating_rd,
                        user.rating,
                        user.rated_games,
                    )
            finally:
                await engine.dispose()

        number, archived_rating, rd, rating, rated_games = asyncio.run(
            scenario()
        )
        self.assertEqual(number, 2)
        self.assertEqual(archived_rating, 1600)
        # 1600 → 1200 + (1600-1200)*0.5 = 1400
        self.assertEqual(rating, 1400)
        self.assertGreaterEqual(rd, 120.0)
        # 生涯战绩不随赛季重置，成就不会被清空。
        self.assertEqual(rated_games, 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
