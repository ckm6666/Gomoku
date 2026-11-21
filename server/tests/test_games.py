"""对局历史与棋谱接口测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    make_message,
)
from server.tests.helpers import ServerTestBase


class GameHistoryTests(ServerTestBase):
    def _register_access(self, username: str, invite_index: int) -> str:
        response = self.register(username, invite_index=invite_index)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _play_friend_game(self, *, resign: bool = False):
        host_token = self._register_access("history_host", 0)
        guest_token = self._register_access("history_guest", 1)
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

        outsider_token = self._register_access("history_outsider", 2)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
