"""自建房邀请对局测试：无需房间码，直接邀请在线好友。"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_INVITE_TO_ROOM,
    CLIENT_RESPOND_ROOM_INVITE,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_ROOM_CREATED,
    SERVER_ROOM_INVITATION,
    SERVER_ROOM_INVITE_RESULT,
    make_message,
)
from server.tests.helpers import ServerTestBase


class InviteRoomTests(ServerTestBase):
    def _register_user(self, username: str) -> tuple[str, dict]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return body["access_token"], body["user"]

    def _authenticate(self, ws, token: str) -> None:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)

    def _receive_type(self, ws, expected: str, attempts: int = 20) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到 {expected}")

    def _make_friends(
        self,
        alice_token: str,
        alice_id: str,
        bob_token: str,
        bob_id: str,
    ) -> None:
        response = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": bob_id},
            headers=self.auth_headers(alice_token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        request_id = response.json()["request_id"]
        accepted = self.client.post(
            f"/api/v1/friends/requests/{request_id}/accept",
            headers=self.auth_headers(bob_token),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)

    def _create_room(self, host_ws) -> dict:
        host_ws.send_json(make_message(CLIENT_CREATE_ROOM, {}))
        return self._receive_type(host_ws, SERVER_ROOM_CREATED)

    def _invite(self, host_ws, guest_id: str) -> dict:
        host_ws.send_json(
            make_message(
                CLIENT_INVITE_TO_ROOM,
                {"user_id": guest_id},
                request_id="invite-1",
            )
        )
        return self._receive_type(host_ws, SERVER_ROOM_INVITE_RESULT)

    def test_invite_accept_starts_game_without_room_code(self) -> None:
        host_token, host = self._register_user("invite_host")
        guest_token, guest = self._register_user("invite_guest")
        self._make_friends(
            host_token, host["id"], guest_token, guest["id"]
        )

        with self.client.websocket_connect("/api/v1/ws") as host_ws:
            self._authenticate(host_ws, host_token)
            self._create_room(host_ws)

            with self.client.websocket_connect("/api/v1/ws") as guest_ws:
                self._authenticate(guest_ws, guest_token)
                sent = self._invite(host_ws, guest["id"])
                self.assertEqual(sent["payload"]["status"], "sent")

                invitation = self._receive_type(
                    guest_ws, SERVER_ROOM_INVITATION
                )
                self.assertEqual(
                    invitation["payload"]["inviter"]["id"], host["id"]
                )
                invite_id = invitation["payload"]["invite_id"]

                guest_ws.send_json(
                    make_message(
                        CLIENT_RESPOND_ROOM_INVITE,
                        {"invite_id": invite_id, "accept": True},
                        request_id="accept-1",
                    )
                )
                result = self._receive_type(
                    host_ws, SERVER_ROOM_INVITE_RESULT
                )
                self.assertEqual(result["payload"]["status"], "accepted")
                self.assertTrue(result["payload"]["accepted"])

                host_state = self._receive_type(
                    host_ws, SERVER_GAME_STATE
                )
                guest_state = self._receive_type(
                    guest_ws, SERVER_GAME_STATE
                )

        for state, color in (
            (host_state, "black"),
            (guest_state, "white"),
        ):
            payload = state["payload"]
            self.assertEqual(payload["black_user_id"], host["id"])
            self.assertEqual(payload["white_user_id"], guest["id"])
            self.assertEqual(payload["your_color"], color)
            self.assertEqual(payload["black_avatar"], host["avatar"])
            self.assertEqual(payload["white_avatar"], guest["avatar"])
            self.assertEqual(payload["move_no"], 0)

    def test_invite_decline_notifies_host(self) -> None:
        host_token, host = self._register_user("decline_host")
        guest_token, guest = self._register_user("decline_guest")
        self._make_friends(
            host_token, host["id"], guest_token, guest["id"]
        )

        with self.client.websocket_connect("/api/v1/ws") as host_ws:
            self._authenticate(host_ws, host_token)
            self._create_room(host_ws)
            with self.client.websocket_connect("/api/v1/ws") as guest_ws:
                self._authenticate(guest_ws, guest_token)
                self._invite(host_ws, guest["id"])
                invitation = self._receive_type(
                    guest_ws, SERVER_ROOM_INVITATION
                )
                invite_id = invitation["payload"]["invite_id"]

                guest_ws.send_json(
                    make_message(
                        CLIENT_RESPOND_ROOM_INVITE,
                        {"invite_id": invite_id, "accept": False},
                        request_id="decline-1",
                    )
                )
                result = self._receive_type(
                    host_ws, SERVER_ROOM_INVITE_RESULT
                )
                self.assertEqual(result["payload"]["status"], "declined")
                self.assertFalse(result["payload"]["accepted"])

    def test_invite_requires_friend(self) -> None:
        host_token, host = self._register_user("friendreq_host")
        outsider_token, outsider = self._register_user("friendreq_guest")

        with self.client.websocket_connect("/api/v1/ws") as host_ws:
            self._authenticate(host_ws, host_token)
            self._create_room(host_ws)
            with self.client.websocket_connect("/api/v1/ws") as guest_ws:
                self._authenticate(guest_ws, outsider_token)
                host_ws.send_json(
                    make_message(
                        CLIENT_INVITE_TO_ROOM,
                        {"user_id": outsider["id"]},
                        request_id="invite-nonfriend",
                    )
                )
                error = self._receive_type(host_ws, SERVER_ERROR)
                self.assertEqual(error["payload"]["reason"], "只能邀请好友")

    def test_invite_requires_online_friend(self) -> None:
        host_token, host = self._register_user("offline_host")
        guest_token, guest = self._register_user("offline_guest")
        self._make_friends(
            host_token, host["id"], guest_token, guest["id"]
        )

        with self.client.websocket_connect("/api/v1/ws") as host_ws:
            self._authenticate(host_ws, host_token)
            self._create_room(host_ws)
            host_ws.send_json(
                make_message(
                    CLIENT_INVITE_TO_ROOM,
                    {"user_id": guest["id"]},
                    request_id="invite-offline",
                )
            )
            error = self._receive_type(host_ws, SERVER_ERROR)
            self.assertEqual(error["payload"]["reason"], "好友不在线")


if __name__ == "__main__":
    unittest.main(verbosity=2)
