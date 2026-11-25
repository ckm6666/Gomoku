"""对局历史与棋谱接口测试。"""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    make_message,
)
from server.app.db import create_engine, create_session_factory
from server.app.models import Game
from server.tests.helpers import ServerTestBase


class GameHistoryTests(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _play_friend_game(self, *, resign: bool = False):
        host_token = self._register_access("history_host")
        guest_token = self._register_access("history_guest")
        game_id = ""
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

                if resign:
                    host.send_json(
                        make_message(
                            CLIENT_MOVE_INTENT,
                            {"row": 7, "col": 7},
                            request_id="history-move-1",
                        )
                    )
                    host.receive_json()
                    guest.receive_json()
                    host.send_json(make_message(CLIENT_RESIGN, {}))
                    final = host.receive_json()
                    guest.receive_json()
                else:
                    black_cols = [3, 4, 5, 6, 7]
                    white_cols = [3, 4, 5, 6]
                    final = None
                    for index, col in enumerate(black_cols):
                        host.send_json(
                            make_message(
                                CLIENT_MOVE_INTENT,
                                {"row": 7, "col": col},
                                request_id=f"history-b-{index}",
                            )
                        )
                        host.receive_json()
                        final = guest.receive_json()
                        if index < len(white_cols):
                            guest.send_json(
                                make_message(
                                    CLIENT_MOVE_INTENT,
                                    {"row": 9, "col": white_cols[index]},
                                    request_id=f"history-w-{index}",
                                )
                            )
                            host.receive_json()
                            guest.receive_json()
                self.assertIsNotNone(final)
                game_id = final["payload"]["game_id"]
        return host_token, guest_token, game_id

    def test_recent_and_detail_visible_to_participants_only(self) -> None:
        host_token, guest_token, game_id = self._play_friend_game()

        recent = self.client.get(
            "/api/v1/games/recent",
            headers=self.auth_headers(host_token),
        )
        self.assertEqual(recent.status_code, 200, recent.text)
        payload = recent.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(len(payload["entries"]), 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["game_id"], game_id)
        self.assertEqual(entry["result"], "win")
        self.assertEqual(entry["your_color"], "black")
        self.assertFalse(entry["rated"])
        self.assertIsNone(entry["rating_delta"])
        self.assertEqual(entry["move_no"], 9)

        guest_recent = self.client.get(
            "/api/v1/games/recent",
            headers=self.auth_headers(guest_token),
        )
        guest_entry = guest_recent.json()["entries"][0]
        self.assertEqual(guest_entry["result"], "loss")
        self.assertEqual(guest_entry["your_color"], "white")

        detail = self.client.get(
            f"/api/v1/games/{game_id}",
            headers=self.auth_headers(host_token),
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        detail_payload = detail.json()
        self.assertEqual(detail_payload["status"], "black_won")
        self.assertEqual(detail_payload["black_username"], "history_host")
        self.assertEqual(detail_payload["white_username"], "history_guest")
        self.assertEqual(detail_payload["your_color"], "black")
        self.assertEqual(len(detail_payload["moves"]), 9)
        self.assertEqual(
            [move["move_no"] for move in detail_payload["moves"]],
            list(range(1, 10)),
        )
        self.assertEqual(detail_payload["moves"][0]["color"], "black")
        self.assertEqual(detail_payload["moves"][1]["color"], "white")

        guest_detail = self.client.get(
            f"/api/v1/games/{game_id}",
            headers=self.auth_headers(guest_token),
        )
        self.assertEqual(guest_detail.status_code, 200)
        self.assertEqual(guest_detail.json()["your_color"], "white")

        outsider_token = self._register_access("history_outsider")
        outsider = self.client.get(
            f"/api/v1/games/{game_id}",
            headers=self.auth_headers(outsider_token),
        )
        self.assertEqual(outsider.status_code, 404)

        anonymous = self.client.get(f"/api/v1/games/{game_id}")
        self.assertEqual(anonymous.status_code, 401)

    def test_resign_game_is_visible_without_board_win(self) -> None:
        host_token, _guest_token, game_id = self._play_friend_game(
            resign=True
        )
        detail = self.client.get(
            f"/api/v1/games/{game_id}",
            headers=self.auth_headers(host_token),
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        payload = detail.json()
        self.assertEqual(payload["status"], "white_won")
        self.assertEqual(payload["result_reason"], "resign")
        self.assertEqual(len(payload["moves"]), 1)
        self.assertIsNotNone(payload["winner_user_id"])

    def test_recent_pagination_and_auth(self) -> None:
        host_token, _guest_token, _game_id = self._play_friend_game()
        page = self.client.get(
            "/api/v1/games/recent?limit=1&offset=0",
            headers=self.auth_headers(host_token),
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(page.json()["entries"]), 1)
        self.assertEqual(page.json()["total"], 1)

        anonymous = self.client.get("/api/v1/games/recent")
        self.assertEqual(anonymous.status_code, 401)


class GameRuleModeTests(ServerTestBase):
    """详情与最近对局必须回传数据库中记录的规则模式。"""

    def _register_user(self, username: str) -> tuple[str, str]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        payload = response.json()
        return payload["user"]["id"], payload["access_token"]

    def _seed_game(
        self,
        game_id: str,
        black_id: str,
        white_id: str,
        rule_mode: str,
    ) -> None:
        async def seed() -> None:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    session.add(
                        Game(
                            id=game_id,
                            room_code=game_id[:12],
                            black_user_id=black_id,
                            white_user_id=white_id,
                            status="black_won",
                            winner_user_id=black_id,
                            result_reason="resign",
                            move_no=0,
                            rated=False,
                            rule_mode=rule_mode,
                        )
                    )
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(seed())

    def test_detail_and_recent_expose_rule_mode(self) -> None:
        black_id, black_token = self._register_user("rule_black")
        white_id, _white_token = self._register_user("rule_white")
        self._seed_game("renju-game", black_id, white_id, "renju")
        self._seed_game("freestyle-game", black_id, white_id, "freestyle")

        recent = self.client.get(
            "/api/v1/games/recent",
            headers=self.auth_headers(black_token),
        )
        self.assertEqual(recent.status_code, 200, recent.text)
        entries = {
            entry["game_id"]: entry for entry in recent.json()["entries"]
        }
        self.assertEqual(entries["renju-game"]["rule_mode"], "renju")
        self.assertEqual(
            entries["freestyle-game"]["rule_mode"], "freestyle"
        )

        detail = self.client.get(
            "/api/v1/games/renju-game",
            headers=self.auth_headers(black_token),
        )
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["rule_mode"], "renju")


class LiveGamesEndpointTests(ServerTestBase):
    """/games/live 要能返回观战人数，旧房间数据缺字段时回落到 0。"""

    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _live_entry(self, **extra) -> dict:
        return {
            "game_id": "live-game",
            "rated": False,
            "black_username": "live_black",
            "white_username": "live_white",
            "black_user_id": "u1",
            "white_user_id": "u2",
            "move_no": 12,
            "status": "playing",
            **extra,
        }

    def _fetch_live(self, token: str, entries: list[dict]):
        manager = self.app.state.room_manager
        with patch.object(
            manager, "list_live_games", return_value=entries
        ):
            return self.client.get(
                "/api/v1/games/live",
                headers=self.auth_headers(token),
            )

    def test_live_games_returns_entries(self) -> None:
        """列表接口照常返回对局摘要。"""
        token = self._register_access("live_viewer")
        response = self._fetch_live(token, [self._live_entry()])
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["total"], 1)
        entry = payload["entries"][0]
        self.assertEqual(entry["game_id"], "live-game")
        self.assertEqual(entry["move_no"], 12)
        # 旧房间数据没有观战人数时，字段回落到 0。
        self.assertEqual(entry["spectator_count"], 0)

    def test_live_games_exposes_spectator_count(self) -> None:
        """有人观战时，列表条目要带上观战人数。"""
        token = self._register_access("live_viewer_two")
        response = self._fetch_live(
            token, [self._live_entry(spectator_count=3)]
        )
        self.assertEqual(response.status_code, 200, response.text)
        entry = response.json()["entries"][0]
        self.assertEqual(entry["spectator_count"], 3)

    def test_live_games_requires_auth(self) -> None:
        """没登录不能拉取对局列表。"""
        response = self.client.get("/api/v1/games/live")
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
