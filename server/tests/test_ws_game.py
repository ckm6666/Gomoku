"""好友房 WebSocket 端到端测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_REMATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    SERVER_MOVE_REJECTED,
    SERVER_OPPONENT_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CREATED,
    make_message,
    parse_game_state,
)
from server.tests.helpers import ServerTestBase


class WebSocketGameTests(ServerTestBase):
    def _register_access(self, username: str, invite_index: int) -> str:
        response = self.register(username, invite_index=invite_index)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(
            make_message(CLIENT_AUTH, {"access_token": token})
        )
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def test_full_friend_game_flow(self) -> None:
        host_token = self._register_access("host_user", 0)
        guest_token = self._register_access("guest_user", 1)

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)

                host.send_json(
                    make_message(CLIENT_CREATE_ROOM, {}, request_id="create-1")
                )
                created = host.receive_json()
                self.assertEqual(created["type"], SERVER_ROOM_CREATED)
                code = created["payload"]["room_code"]
                self.assertEqual(len(code), 8)

                guest.send_json(
                    make_message(
                        CLIENT_JOIN_ROOM,
                        {"room_code": code},
                        request_id="join-1",
                    )
                )
                host_state = host.receive_json()
                guest_state = guest.receive_json()
                self.assertEqual(host_state["type"], SERVER_GAME_STATE)
                self.assertEqual(guest_state["type"], SERVER_GAME_STATE)
                host_snapshot = parse_game_state(host_state["payload"])
                guest_snapshot = parse_game_state(guest_state["payload"])
                self.assertEqual(host_snapshot.your_color.value, "black")
                self.assertEqual(guest_snapshot.your_color.value, "white")
                self.assertFalse(host_snapshot.is_game_over)

                # 房主黑棋先落子；服务器广播权威状态。
                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 7},
                        request_id="move-1",
                    )
                )
                host_after = host.receive_json()
                guest_after = guest.receive_json()
                self.assertEqual(host_after["type"], SERVER_GAME_STATE)
                self.assertEqual(host_after["request_id"], "move-1")
                snap = parse_game_state(host_after["payload"])
                self.assertEqual(snap.move_no, 1)
                self.assertEqual(snap.last_move.player.value, "black")

                # 没轮到房主：只给房主发拒绝，不广播给对手。
                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 8, "col": 8},
                        request_id="move-out-of-turn",
                    )
                )
                rejected = host.receive_json()
                self.assertEqual(rejected["type"], SERVER_MOVE_REJECTED)
                self.assertEqual(rejected["request_id"], "move-out-of-turn")

                # 白棋落子
                guest.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 8},
                        request_id="move-2",
                    )
                )
                host_after2 = host.receive_json()
                guest_after2 = guest.receive_json()
                self.assertEqual(
                    parse_game_state(host_after2["payload"]).move_no, 2
                )

                # 重发旧 move_id 只返回当前状态，不重复落子。
                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 0, "col": 0},
                        request_id="move-1",
                    )
                )
                duplicate = host.receive_json()
                self.assertEqual(duplicate["type"], SERVER_GAME_STATE)
                self.assertEqual(
                    parse_game_state(duplicate["payload"]).move_no, 2
                )

                # 房主认输，白方获胜；双方收到终局状态。
                host.send_json(make_message(CLIENT_RESIGN, {}))
                host_end = host.receive_json()
                guest_end = guest.receive_json()
                self.assertEqual(
                    host_end["payload"]["status"], "white_won"
                )
                self.assertEqual(
                    guest_end["payload"]["status"], "white_won"
                )

                # 双方都申请再战：交换黑白，生成新局。
                host.send_json(make_message(CLIENT_REMATCH, {}))
                host_rematch = host.receive_json()
                guest_rematch = guest.receive_json()
                self.assertEqual(host_rematch["type"], SERVER_REMATCH_STATUS)
                self.assertEqual(guest_rematch["type"], SERVER_REMATCH_STATUS)

                guest.send_json(make_message(CLIENT_REMATCH, {}))
                host_new = host.receive_json()
                guest_new = guest.receive_json()
                new_host = parse_game_state(host_new["payload"])
                new_guest = parse_game_state(guest_new["payload"])
                self.assertEqual(new_host.move_no, 0)
                self.assertEqual(new_host.your_color.value, "white")
                self.assertEqual(new_guest.your_color.value, "black")
                self.assertFalse(new_host.is_game_over)

    def test_server_authoritative_five_in_a_row(self) -> None:
        host_token = self._register_access("win_host", 0)
        guest_token = self._register_access("win_guest", 1)
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

                black_cols = [3, 4, 5, 6, 7]
                white_cols = [3, 4, 5, 6]
                final_host_message = None
                for index, col in enumerate(black_cols):
                    host.send_json(
                        make_message(
                            CLIENT_MOVE_INTENT,
                            {"row": 7, "col": col},
                            request_id=f"win-b-{index}",
                        )
                    )
                    host.receive_json()
                    guest_after_black = guest.receive_json()
                    if index == len(black_cols) - 1:
                        final_host_message = guest_after_black
                    if index < len(white_cols):
                        guest.send_json(
                            make_message(
                                CLIENT_MOVE_INTENT,
                                {"row": 9, "col": white_cols[index]},
                                request_id=f"win-w-{index}",
                            )
                        )
                        host.receive_json()
                        guest.receive_json()
                self.assertIsNotNone(final_host_message)
                self.assertEqual(
                    final_host_message["payload"]["status"], "black_won"
                )

    def test_reconnect_restores_state(self) -> None:
        host_token = self._register_access("reconnect_host", 0)
        guest_token = self._register_access("reconnect_guest", 1)

        with self.client.websocket_connect("/api/v1/ws") as host:
            self._authenticate(host, host_token)
            host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            code = host.receive_json()["payload"]["room_code"]

            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(guest, guest_token)
                guest.send_json(
                    make_message(CLIENT_JOIN_ROOM, {"room_code": code})
                )
                host.receive_json()  # game state
                guest.receive_json()  # game state

            # 客人断开后，房主收到离线通知；房间仍保留等待重连。
            offline = host.receive_json()
            self.assertEqual(offline["type"], SERVER_OPPONENT_STATUS)
            self.assertFalse(offline["payload"]["online"])

            with self.client.websocket_connect("/api/v1/ws") as guest2:
                self._authenticate(guest2, guest_token)
                restored = guest2.receive_json()
                self.assertEqual(restored["type"], SERVER_GAME_STATE)
                self.assertEqual(restored["payload"]["move_no"], 0)
                online = host.receive_json()
                self.assertEqual(online["type"], SERVER_OPPONENT_STATUS)
                self.assertTrue(online["payload"]["online"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
