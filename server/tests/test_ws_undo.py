"""联机悔棋请求与掉线逃跑判负的端到端测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_UNDO_REQUEST,
    CLIENT_UNDO_RESPONSE,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_OPPONENT_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_UNDO_STATUS,
    make_message,
    parse_game_state,
)
from server.tests.helpers import ServerTestBase


class _WsTestBase(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> None:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)

    def _open_game(self, host, guest) -> str:
        host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
        created = host.receive_json()
        self.assertEqual(created["type"], SERVER_ROOM_CREATED)
        code = created["payload"]["room_code"]
        guest.send_json(
            make_message(CLIENT_JOIN_ROOM, {"room_code": code})
        )
        host_state = host.receive_json()
        guest_state = guest.receive_json()
        self.assertEqual(host_state["type"], SERVER_GAME_STATE)
        self.assertEqual(guest_state["type"], SERVER_GAME_STATE)
        return host_state["payload"]["game_id"]

    def _move(
        self, sender, other, row: int, col: int, request_id: str
    ) -> None:
        sender.send_json(
            make_message(
                CLIENT_MOVE_INTENT,
                {"row": row, "col": col},
                request_id=request_id,
            )
        )
        sender.receive_json()
        other.receive_json()

    def _started_game(self, host, guest) -> None:
        """黑、白各一手，轮到黑棋（房主）请求悔棋。"""
        self._open_game(host, guest)
        self._move(host, guest, 7, 7, "setup-m1")
        self._move(guest, host, 7, 8, "setup-m2")

    def _request_and_decline(self, host, guest) -> None:
        host.send_json(
            make_message(
                CLIENT_UNDO_REQUEST,
                {},
                request_id="quota-request",
            )
        )
        host.receive_json()  # status requested
        host.receive_json()  # game state
        guest.receive_json()  # status requested
        guest.receive_json()  # game state
        guest.send_json(
            make_message(
                CLIENT_UNDO_RESPONSE,
                {"accept": False},
                request_id="quota-decline",
            )
        )
        host.receive_json()  # status declined
        host.receive_json()  # game state
        guest.receive_json()
        guest.receive_json()


class UndoRequestTests(_WsTestBase):
    def test_undo_requires_opponent_acceptance(self) -> None:
        host_token = self._register_access("undo_host")
        guest_token = self._register_access("undo_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                game_id = self._open_game(host, guest)

                # 黑、白、黑、白各一手；现在轮到黑棋。
                self._move(host, guest, 7, 7, "m1")
                self._move(guest, host, 7, 8, "m2")
                self._move(host, guest, 8, 8, "m3")
                self._move(guest, host, 8, 9, "m4")

                # 黑棋请求悔棋：双方都收到待回应通知和带请求者的快照。
                host.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="undo-1",
                    )
                )
                host_status = host.receive_json()
                host_state = host.receive_json()
                guest_status = guest.receive_json()
                guest.receive_json()
                self.assertEqual(host_status["type"], SERVER_UNDO_STATUS)
                self.assertEqual(
                    host_status["payload"]["state"], "requested"
                )
                self.assertEqual(guest_status["type"], SERVER_UNDO_STATUS)
                requested = parse_game_state(host_state["payload"])
                self.assertTrue(requested.undo_requester_id)

                # 拒绝：棋盘保持不变。
                guest.send_json(
                    make_message(
                        CLIENT_UNDO_RESPONSE,
                        {"accept": False},
                        request_id="undo-decline",
                    )
                )
                self.assertEqual(
                    host.receive_json()["payload"]["state"], "declined"
                )
                declined = parse_game_state(host.receive_json()["payload"])
                self.assertEqual(
                    guest.receive_json()["payload"]["state"], "declined"
                )
                guest.receive_json()
                self.assertEqual(declined.move_no, 4)
                self.assertEqual(declined.undo_requester_id, "")

                # 再次请求并同意：回滚到黑棋最后一手之前（含白棋应手）。
                host.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="undo-2",
                    )
                )
                host.receive_json()  # status requested
                host.receive_json()  # game state
                guest.receive_json()  # status requested
                guest.receive_json()  # game state
                guest.send_json(
                    make_message(
                        CLIENT_UNDO_RESPONSE,
                        {"accept": True},
                        request_id="undo-accept",
                    )
                )
                self.assertEqual(
                    host.receive_json()["payload"]["state"], "accepted"
                )
                rolled_back = parse_game_state(
                    host.receive_json()["payload"]
                )
                self.assertEqual(
                    guest.receive_json()["payload"]["state"], "accepted"
                )
                guest_snapshot = parse_game_state(
                    guest.receive_json()["payload"]
                )
                self.assertEqual(rolled_back.move_no, 2)
                self.assertEqual(guest_snapshot.move_no, 2)
                self.assertEqual(
                    rolled_back.board[7][7].value, "black"
                )
                self.assertEqual(
                    rolled_back.board[7][8].value, "white"
                )
                self.assertIsNone(rolled_back.board[8][8])
                self.assertIsNone(rolled_back.board[8][9])

                # 数据库同步删除被撤销的落子。
                detail = self.client.get(
                    f"/api/v1/games/{game_id}",
                    headers=self.auth_headers(host_token),
                )
                self.assertEqual(detail.status_code, 200, detail.text)
                self.assertEqual(
                    [move["move_no"] for move in detail.json()["moves"]],
                    [1, 2],
                )

    def test_undo_request_out_of_turn_is_rejected(self) -> None:
        host_token = self._register_access("undo_turn_host")
        guest_token = self._register_access("undo_turn_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._open_game(host, guest)
                self._move(host, guest, 7, 7, "turn-m1")

                # 白棋还没有落子，且现在轮到黑棋：请求必须被拒绝。
                guest.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="undo-out-of-turn",
                    )
                )
                error = guest.receive_json()
                self.assertEqual(error["type"], "server_error")
                self.assertEqual(
                    error["request_id"], "undo-out-of-turn"
                )


class UndoQuotaTests(_WsTestBase):
    undo_limit_per_game = 1
    undo_cooldown_seconds = 0.0

    def test_undo_limit_is_enforced(self) -> None:
        host_token = self._register_access("quota_host")
        guest_token = self._register_access("quota_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._started_game(host, guest)

                self._request_and_decline(host, guest)
                host.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="quota-exhausted",
                    )
                )
                error = host.receive_json()
                self.assertEqual(error["type"], SERVER_ERROR)
                self.assertIn("次数已用完", error["payload"]["reason"])


class UndoCooldownTests(_WsTestBase):
    undo_cooldown_seconds = 10.0

    def test_undo_cooldown_is_enforced(self) -> None:
        host_token = self._register_access("cooldown_host")
        guest_token = self._register_access("cooldown_guest")
        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._started_game(host, guest)

                self._request_and_decline(host, guest)
                host.send_json(
                    make_message(
                        CLIENT_UNDO_REQUEST,
                        {},
                        request_id="cooldown-too-fast",
                    )
                )
                error = host.receive_json()
                self.assertEqual(error["type"], SERVER_ERROR)
                self.assertIn("频繁", error["payload"]["reason"])


class DisconnectForfeitTests(_WsTestBase):
    """掉线宽限结束后按“逃跑”判负，并带上 result_reason。"""

    disconnect_forfeit_seconds = 0.1

    def test_disconnect_ends_game_as_escape(self) -> None:
        host_token = self._register_access("escape_host")
        guest_token = self._register_access("escape_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            self._authenticate(host, host_token)
            host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            code = host.receive_json()["payload"]["room_code"]

            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(guest, guest_token)
                guest.send_json(
                    make_message(CLIENT_JOIN_ROOM, {"room_code": code})
                )
                host.receive_json()
                guest.receive_json()

                host.send_json(
                    make_message(
                        CLIENT_MOVE_INTENT,
                        {"row": 7, "col": 7},
                        request_id="escape-move",
                    )
                )
                host.receive_json()
                guest.receive_json()

            # 白棋断线：房主先收到离线通知，宽限期后收到逃跑判负快照。
            offline = host.receive_json()
            self.assertEqual(offline["type"], SERVER_OPPONENT_STATUS)
            self.assertFalse(offline["payload"]["online"])
            ended = host.receive_json()
            self.assertEqual(ended["type"], SERVER_GAME_STATE)
            snapshot = parse_game_state(ended["payload"])
            self.assertTrue(snapshot.is_game_over)
            self.assertEqual(snapshot.winner.value, "black")
            self.assertEqual(snapshot.result_reason, "escape")


if __name__ == "__main__":
    unittest.main(verbosity=2)
