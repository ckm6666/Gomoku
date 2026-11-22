"""公开匹配、排位结算、资料与排行榜的端到端测试。"""

from __future__ import annotations

import asyncio
import unittest

from sqlalchemy import func, select

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_QUICK_MATCH,
    CLIENT_REMATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_QUEUE_STATUS,
    SERVER_ROOM_CREATED,
    make_message,
    parse_game_state,
)
from gomoku.shared.rules import Player
from server.app.db import create_engine, create_session_factory
from server.app.models import Game, RatingEvent
from server.tests.helpers import ServerTestBase


class RankedMatchTests(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _receive_type(self, ws, expected: str, attempts: int = 8) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到期望消息 {expected}")
        return {}

    def test_match_resign_rating_profile_and_leaderboard(self) -> None:
        host_token = self._register_access("rank_host")
        guest_token = self._register_access("rank_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                host_auth = self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self.assertEqual(host_auth["payload"]["user"]["rating"], 1200)
                self.assertEqual(
                    host_auth["payload"]["user"]["rank_name"], "黄金"
                )

                host.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                queued = host.receive_json()
                self.assertEqual(queued["type"], SERVER_QUEUE_STATUS)
                self.assertEqual(queued["payload"]["state"], "queued")
                self.assertEqual(queued["payload"]["queue_size"], 1)

                guest.send_json(make_message(CLIENT_QUICK_MATCH, {}))

                host_idle = self._receive_type(host, SERVER_QUEUE_STATUS)
                host_found = self._receive_type(host, SERVER_MATCH_FOUND)
                host_state_message = self._receive_type(
                    host, SERVER_GAME_STATE
                )

                guest_idle = self._receive_type(guest, SERVER_QUEUE_STATUS)
                guest_found = self._receive_type(guest, SERVER_MATCH_FOUND)
                guest_state_message = self._receive_type(
                    guest, SERVER_GAME_STATE
                )

                self.assertEqual(host_idle["payload"]["state"], "idle")
                self.assertEqual(guest_idle["payload"]["state"], "idle")
                self.assertTrue(host_found["payload"]["rated"])
                self.assertTrue(guest_found["payload"]["rated"])
                self.assertNotEqual(
                    host_found["payload"]["your_color"],
                    guest_found["payload"]["your_color"],
                )

                host_snapshot = parse_game_state(
                    host_state_message["payload"]
                )
                guest_snapshot = parse_game_state(
                    guest_state_message["payload"]
                )
                self.assertTrue(host_snapshot.rated)
                self.assertTrue(guest_snapshot.rated)
                self.assertEqual(host_snapshot.your_rating_before, 1200)
                self.assertEqual(guest_snapshot.opponent_rating_before, 1200)
                self.assertEqual(host_snapshot.room_code, "")

                if host_snapshot.your_color is Player.BLACK:
                    black_ws, white_ws = host, guest
                    black_token, white_token = host_token, guest_token
                    black_name, white_name = "rank_host", "rank_guest"
                else:
                    black_ws, white_ws = guest, host
                    black_token, white_token = guest_token, host_token
                    black_name, white_name = "rank_guest", "rank_host"

                black_ws.send_json(make_message(CLIENT_RESIGN, {}))
                black_end_message = self._receive_type(
                    black_ws, SERVER_GAME_STATE
                )
                white_end_message = self._receive_type(
                    white_ws, SERVER_GAME_STATE
                )
                black_end = parse_game_state(black_end_message["payload"])
                white_end = parse_game_state(white_end_message["payload"])

                self.assertTrue(black_end.is_game_over)
                self.assertTrue(white_end.is_game_over)
                self.assertIs(white_end.winner, white_end.your_color)
                self.assertGreater(white_end.rating_delta or 0, 0)
                self.assertLess(black_end.rating_delta or 0, 0)
                # 胜者本局解锁“首胜”并同步到双方卡片；负者没有新成就。
                self.assertIn(
                    "first_win", white_end.your_new_achievements
                )
                self.assertIn("first_win", white_end.white_achievements)
                self.assertNotIn(
                    "first_win", black_end.black_achievements
                )
                self.assertEqual(black_end.your_new_achievements, ())
                self.assertEqual(black_end.black_avatar_version, 0)
                self.assertEqual(white_end.white_avatar_version, 0)
                self.assertGreater(
                    white_end.your_rating_after or 0,
                    white_end.your_rating_before or 0,
                )
                self.assertEqual(
                    white_end.your_rating_after,
                    (white_end.your_rating_before or 0)
                    + (white_end.rating_delta or 0),
                )

                # 终局后不能再战，客户端应回大厅重新匹配。
                white_ws.send_json(make_message(CLIENT_REMATCH, {}))
                rematch_error = white_ws.receive_json()
                self.assertEqual(rematch_error["type"], SERVER_ERROR)
                self.assertIn("排位", rematch_error["payload"]["reason"])

                # 排行榜：胜者第一、负者第二，段位与积分同步。
                leaderboard = self.client.get(
                    "/api/v1/leaderboard",
                    headers=self.auth_headers(white_token),
                )
                self.assertEqual(leaderboard.status_code, 200)
                data = leaderboard.json()
                self.assertEqual(data["total"], 2)
                self.assertEqual(data["entries"][0]["username"], white_name)
                self.assertEqual(data["entries"][1]["username"], black_name)
                self.assertEqual(
                    data["entries"][0]["rating"],
                    white_end.your_rating_after,
                )
                self.assertEqual(data["entries"][0]["position"], 1)

                # 自己的资料包含战绩与最近一局积分变化。
                profile = self.client.get(
                    "/api/v1/profile/me",
                    headers=self.auth_headers(white_token),
                )
                self.assertEqual(profile.status_code, 200)
                profile_data = profile.json()
                self.assertEqual(profile_data["user"]["wins"], 1)
                self.assertEqual(profile_data["user"]["rated_games"], 1)
                self.assertEqual(
                    profile_data["user"]["current_win_streak"], 1
                )
                self.assertEqual(
                    profile_data["user"]["best_win_streak"], 1
                )
                self.assertIn(
                    "first_win",
                    profile_data["user"]["achievements"],
                )
                self.assertEqual(
                    profile_data["recent_games"][0]["opponent_username"],
                    black_name,
                )
                self.assertEqual(
                    profile_data["recent_games"][0]["score"], "win"
                )
                self.assertEqual(
                    profile_data["recent_games"][0]["rating_before"], 1200
                )

                loser_me = self.client.get(
                    "/api/v1/auth/me",
                    headers=self.auth_headers(black_token),
                )
                self.assertEqual(loser_me.status_code, 200)
                self.assertEqual(loser_me.json()["losses"], 1)
                self.assertLess(loser_me.json()["rating"], 1200)

                # 数据库审计：同一局只结算一次，双方各一条 rating_events。
                game_id = host_state_message["payload"]["game_id"]

                async def inspect_game() -> tuple[bool, bool, int]:
                    engine = create_engine(self.settings)
                    factory = create_session_factory(engine)
                    try:
                        async with factory() as session:
                            game_row = await session.get(Game, game_id)
                            event_count = await session.scalar(
                                select(func.count())
                                .select_from(RatingEvent)
                                .where(RatingEvent.game_id == game_id)
                            )
                            return (
                                bool(game_row.rated),
                                bool(game_row.rating_applied),
                                int(event_count or 0),
                            )
                    finally:
                        await engine.dispose()

                rated, rating_applied, event_count = asyncio.run(
                    inspect_game()
                )
                self.assertTrue(rated)
                self.assertTrue(rating_applied)
                self.assertEqual(event_count, 2)

    def test_cancel_queue_then_create_friend_room(self) -> None:
        token = self._register_access("queue_canceller")
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)
            ws.send_json(make_message(CLIENT_QUICK_MATCH, {}))
            queued = ws.receive_json()
            self.assertEqual(queued["type"], SERVER_QUEUE_STATUS)
            self.assertEqual(queued["payload"]["state"], "queued")

            ws.send_json(make_message(CLIENT_CANCEL_MATCH, {}))
            idle = ws.receive_json()
            self.assertEqual(idle["type"], SERVER_QUEUE_STATUS)
            self.assertEqual(idle["payload"]["state"], "idle")
            self.assertEqual(idle["payload"]["queue_size"], 0)

            ws.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            created = ws.receive_json()
            self.assertEqual(created["type"], SERVER_ROOM_CREATED)
            self.assertEqual(len(created["payload"]["room_code"]), 8)

    def test_friend_room_does_not_change_rating(self) -> None:
        host_token = self._register_access("casual_host")
        guest_token = self._register_access("casual_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
                code = host.receive_json()["payload"]["room_code"]
                guest.send_json(
                    make_message(CLIENT_JOIN_ROOM, {"room_code": code})
                )
                host.receive_json()
                guest.receive_json()

                host.send_json(make_message(CLIENT_RESIGN, {}))
                host_end = self._receive_type(host, SERVER_GAME_STATE)
                guest_end = self._receive_type(guest, SERVER_GAME_STATE)
                self.assertFalse(host_end["payload"]["rated"])
                self.assertFalse(guest_end["payload"]["rated"])
                self.assertIsNone(host_end["payload"]["rating_delta"])
                self.assertIsNone(host_end["payload"]["your_rating_before"])

        me = self.client.get(
            "/api/v1/auth/me", headers=self.auth_headers(guest_token)
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["rating"], 1200)
        self.assertEqual(me.json()["rated_games"], 0)

    def _play_ranked_and_resign(
        self,
        first_token: str,
        second_token: str,
        loser_is_first: bool,
    ) -> None:
        """匹配一局后让指定玩家认输。

        使用临时的两个 WebSocket；函数退出时连接断开，终局房间随之清理，
        下一局可以重新匹配。
        """
        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as first_ws, self.client.websocket_connect(
            "/api/v1/ws"
        ) as second_ws:
            self._authenticate(first_ws, first_token)
            self._authenticate(second_ws, second_token)
            first_ws.send_json(make_message(CLIENT_QUICK_MATCH, {}))
            first_ws.receive_json()  # queue_status: queued
            second_ws.send_json(make_message(CLIENT_QUICK_MATCH, {}))

            for ws in (first_ws, second_ws):
                self._receive_type(ws, SERVER_QUEUE_STATUS)
                self._receive_type(ws, SERVER_MATCH_FOUND)
                self._receive_type(ws, SERVER_GAME_STATE)

            loser_ws = first_ws if loser_is_first else second_ws
            loser_ws.send_json(make_message(CLIENT_RESIGN, {}))
            for ws in (first_ws, second_ws):
                end = self._receive_type(ws, SERVER_GAME_STATE)
                self.assertNotEqual(end["payload"]["status"], "playing")

    def test_ranked_win_streak_unlocks_and_resets(self) -> None:
        winner_token = self._register_access("streak_winner")
        loser_token = self._register_access("streak_loser")

        for expected_streak in (1, 2, 3):
            self._play_ranked_and_resign(
                winner_token, loser_token, loser_is_first=False
            )
            profile = self.client.get(
                "/api/v1/profile/me",
                headers=self.auth_headers(winner_token),
            ).json()["user"]
            self.assertEqual(
                profile["current_win_streak"], expected_streak
            )
            self.assertEqual(profile["best_win_streak"], expected_streak)
            if expected_streak == 3:
                self.assertIn("streak_3", profile["achievements"])
            else:
                self.assertNotIn("streak_3", profile["achievements"])

        # 赢家输掉一局：当前连胜清零，历史最高连胜与已获勋章保留。
        self._play_ranked_and_resign(
            winner_token, loser_token, loser_is_first=True
        )
        profile = self.client.get(
            "/api/v1/profile/me",
            headers=self.auth_headers(winner_token),
        ).json()["user"]
        self.assertEqual(profile["current_win_streak"], 0)
        self.assertEqual(profile["best_win_streak"], 3)
        self.assertIn("streak_3", profile["achievements"])

        leaderboard = self.client.get(
            "/api/v1/leaderboard",
            headers=self.auth_headers(winner_token),
        ).json()
        for entry in leaderboard["entries"]:
            self.assertIn("avatar_version", entry)

    def test_leaderboard_requires_auth_and_is_empty_initially(self) -> None:
        unauth = self.client.get("/api/v1/leaderboard")
        self.assertEqual(unauth.status_code, 401)

        token = self._register_access("leaderboard_user")
        response = self.client.get(
            "/api/v1/leaderboard", headers=self.auth_headers(token)
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], 0)
        self.assertEqual(response.json()["entries"], [])

    def test_auth_response_exposes_rating_fields(self) -> None:
        response = self.register("rating_shape")
        self.assertEqual(response.status_code, 201)
        user = response.json()["user"]
        self.assertEqual(user["rating"], 1200)
        self.assertEqual(user["peak_rating"], 1200)
        self.assertEqual(user["rank_name"], "黄金")
        self.assertEqual(user["rank_key"], "gold")
        self.assertEqual(user["rated_games"], 0)
        self.assertEqual(user["win_rate"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
