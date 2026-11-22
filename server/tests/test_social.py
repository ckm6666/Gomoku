"""头像、公开资料、好友与私聊接口测试。"""

from __future__ import annotations

import base64
import struct
import unittest
import zlib

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    SERVER_AUTHENTICATED,
    SERVER_DIRECT_MESSAGE,
    SERVER_FRIEND_EVENT,
    SERVER_GAME_STATE,
    SERVER_ROOM_CREATED,
    make_message,
)
from server.tests.helpers import ServerTestBase


class SocialAPITests(ServerTestBase):
    def _register_user(
        self, username: str
    ) -> tuple[str, dict]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        return body["access_token"], body["user"]

    def _register_access(self, username: str) -> str:
        token, _ = self._register_user(username)
        return token

    def _make_friends(
        self,
        alice_token: str,
        alice_id: str,
        bob_token: str,
        bob_id: str,
    ) -> str:
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
        return request_id

    def _authenticate_ws(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def test_avatar_update_and_public_profile(self) -> None:
        token, user = self._register_user("avatar_alice")
        headers = self.auth_headers(token)

        updated = self.client.patch(
            "/api/v1/users/me/avatar",
            json={"avatar": "mei"},
            headers=headers,
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["avatar"], "mei")

        me = self.client.get("/api/v1/auth/me", headers=headers)
        self.assertEqual(me.json()["avatar"], "mei")

        invalid = self.client.patch(
            "/api/v1/users/me/avatar",
            json={"avatar": "not-an-avatar"},
            headers=headers,
        )
        self.assertEqual(invalid.status_code, 400)

        profile = self.client.get(
            f"/api/v1/users/{user['id']}", headers=headers
        )
        self.assertEqual(profile.status_code, 200, profile.text)
        self.assertEqual(profile.json()["user"]["avatar"], "mei")
        self.assertEqual(profile.json()["friendship"], "self")

    @staticmethod
    def _png_data_url(width: int = 16, height: int = 16) -> str:
        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data))
                + kind
                + data
                + struct.pack(
                    ">I", zlib.crc32(kind + data) & 0xFFFFFFFF
                )
            )

        header = struct.pack(
            ">IIBBBBB", width, height, 8, 2, 0, 0, 0
        )
        raw = b"".join(
            b"\x00" + b"\xff\x00\x00" * width for _ in range(height)
        )
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b"")
        )
        return "data:image/png;base64," + base64.b64encode(
            png
        ).decode("ascii")

    def test_avatar_image_upload_fetch_and_remove(self) -> None:
        token, user = self._register_user("upload_alice")
        headers = self.auth_headers(token)

        uploaded = self.client.put(
            "/api/v1/users/me/avatar-image",
            json={"image": self._png_data_url()},
            headers=headers,
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        body = uploaded.json()
        self.assertEqual(body["avatar_version"], 1)
        self.assertIn("rank_gold", body["achievements"])
        public = self.client.get(
            f"/api/v1/users/{user['id']}", headers=headers
        )
        self.assertEqual(public.status_code, 200)
        self.assertEqual(
            public.json()["user"]["avatar_version"], 1
        )

        image = self.client.get(
            f"/api/v1/users/{user['id']}/avatar?v=1"
        )
        self.assertEqual(image.status_code, 200, image.text)
        self.assertEqual(image.headers["content-type"], "image/png")
        self.assertTrue(image.content.startswith(b"\x89PNG"))

        # 选择预置印章会移除自定义图片并把版本归零。
        preset = self.client.patch(
            "/api/v1/users/me/avatar",
            json={"avatar": "zhu"},
            headers=headers,
        )
        self.assertEqual(preset.status_code, 200, preset.text)
        self.assertEqual(preset.json()["avatar_version"], 0)
        missing = self.client.get(
            f"/api/v1/users/{user['id']}/avatar"
        )
        self.assertEqual(missing.status_code, 404)

        # 版本号只增不减：清空后再次上传不会复用旧缓存 URL。
        reuploaded = self.client.put(
            "/api/v1/users/me/avatar-image",
            json={"image": self._png_data_url()},
            headers=headers,
        )
        self.assertEqual(reuploaded.status_code, 200, reuploaded.text)
        self.assertEqual(reuploaded.json()["avatar_version"], 3)
        image_again = self.client.get(
            f"/api/v1/users/{user['id']}/avatar?v=3"
        )
        self.assertEqual(image_again.status_code, 200)

    def test_avatar_image_rejects_invalid_payload(self) -> None:
        token, _ = self._register_user("upload_bob")
        headers = self.auth_headers(token)

        bad_type = self.client.put(
            "/api/v1/users/me/avatar-image",
            json={"image": "data:image/svg+xml;base64,PHN2Zy8+"},
            headers=headers,
        )
        self.assertEqual(bad_type.status_code, 400)

        bad_magic = self.client.put(
            "/api/v1/users/me/avatar-image",
            json={
                "image": "data:image/png;base64,"
                + base64.b64encode(b"not a real png").decode("ascii")
            },
            headers=headers,
        )
        self.assertEqual(bad_magic.status_code, 400)

        too_big = self.client.put(
            "/api/v1/users/me/avatar-image",
            json={
                "image": self._png_data_url(width=2048, height=16)
            },
            headers=headers,
        )
        self.assertEqual(too_big.status_code, 400)

    def test_avatar_update_reaches_live_room(self) -> None:
        host_token, host = self._register_user("live_avatar_host")
        guest_token, _guest = self._register_user("live_avatar_guest")
        with self.client.websocket_connect(
            "/api/v1/ws"
        ) as host_ws, self.client.websocket_connect(
            "/api/v1/ws"
        ) as guest_ws:
            self._authenticate_ws(host_ws, host_token)
            self._authenticate_ws(guest_ws, guest_token)

            host_ws.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            created = host_ws.receive_json()
            self.assertEqual(created["type"], SERVER_ROOM_CREATED)
            code = created["payload"]["room_code"]

            uploaded = self.client.put(
                "/api/v1/users/me/avatar-image",
                json={"image": self._png_data_url()},
                headers=self.auth_headers(host_token),
            )
            self.assertEqual(uploaded.status_code, 200)
            self.assertEqual(uploaded.json()["avatar_version"], 1)

            guest_ws.send_json(
                make_message(CLIENT_JOIN_ROOM, {"room_code": code})
            )
            payload = None
            for _ in range(4):
                message = host_ws.receive_json()
                if message["type"] == SERVER_GAME_STATE:
                    payload = message["payload"]
                    break
            self.assertIsNotNone(payload)
            # 房间在头像上传前创建，版本仍然同步到了等待中的房间。
            self.assertEqual(payload["black_avatar_version"], 1)
            self.assertEqual(payload["black_avatar"], host["avatar"])

    def test_user_search(self) -> None:
        token, _ = self._register_user("search_alice")
        _, bob = self._register_user("search_bobby")
        headers = self.auth_headers(token)

        response = self.client.get(
            "/api/v1/users/search",
            params={"q": "bobby"},
            headers=headers,
        )
        self.assertEqual(response.status_code, 200, response.text)
        ids = {entry["id"] for entry in response.json()["entries"]}
        self.assertIn(bob["id"], ids)

        other = self.client.get(
            f"/api/v1/users/{bob['id']}", headers=headers
        )
        self.assertEqual(other.status_code, 200, other.text)
        self.assertEqual(other.json()["friendship"], "none")
        self.assertFalse(other.json()["online"])

        unauthorized = self.client.get(
            "/api/v1/users/search", params={"q": "bobby"}
        )
        self.assertEqual(unauthorized.status_code, 401)

    def test_friend_request_accept_remove_and_push(self) -> None:
        alice_token, alice = self._register_user("friend_alice")
        bob_token, bob = self._register_user("friend_bob")

        with self.client.websocket_connect("/api/v1/ws") as bob_ws:
            self._authenticate_ws(bob_ws, bob_token)

            response = self.client.post(
                "/api/v1/friends/requests",
                json={"user_id": bob["id"]},
                headers=self.auth_headers(alice_token),
            )
            self.assertEqual(response.status_code, 200, response.text)
            request_id = response.json()["request_id"]

            event = bob_ws.receive_json()
            self.assertEqual(event["type"], SERVER_FRIEND_EVENT)
            self.assertEqual(event["payload"]["event"], "request")
            self.assertEqual(event["payload"]["request_id"], request_id)
            self.assertEqual(event["payload"]["user"]["id"], alice["id"])

            outgoing = self.client.get(
                "/api/v1/friends",
                headers=self.auth_headers(alice_token),
            ).json()
            self.assertEqual(outgoing["friends"], [])
            self.assertEqual(len(outgoing["outgoing"]), 1)
            self.assertEqual(
                outgoing["outgoing"][0]["user_id"], bob["id"]
            )

            incoming = self.client.get(
                "/api/v1/friends",
                headers=self.auth_headers(bob_token),
            ).json()
            self.assertEqual(len(incoming["incoming"]), 1)
            self.assertEqual(
                incoming["incoming"][0]["user_id"], alice["id"]
            )

            accepted = self.client.post(
                f"/api/v1/friends/requests/{request_id}/accept",
                headers=self.auth_headers(bob_token),
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)
            self.assertEqual(
                accepted.json()["user_id"], alice["id"]
            )

            alice_friends = self.client.get(
                "/api/v1/friends",
                headers=self.auth_headers(alice_token),
            ).json()
            self.assertEqual(len(alice_friends["friends"]), 1)
            self.assertEqual(
                alice_friends["friends"][0]["user_id"], bob["id"]
            )

            duplicate = self.client.post(
                "/api/v1/friends/requests",
                json={"user_id": bob["id"]},
                headers=self.auth_headers(alice_token),
            )
            self.assertEqual(duplicate.status_code, 400)

            removed = self.client.delete(
                f"/api/v1/friends/{alice['id']}",
                headers=self.auth_headers(bob_token),
            )
            self.assertEqual(removed.status_code, 204)
            self.assertEqual(
                self.client.get(
                    "/api/v1/friends",
                    headers=self.auth_headers(alice_token),
                ).json()["friends"],
                [],
            )

    def test_accept_then_decline_keeps_the_friendship(self) -> None:
        """接受之后再拒绝，不能把已经建立的好友关系悄悄删掉。"""
        alice_token, alice = self._register_user("race_alice")
        bob_token, bob = self._register_user("race_bob")

        response = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": bob["id"]},
            headers=self.auth_headers(alice_token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        request_id = response.json()["request_id"]

        accepted = self.client.post(
            f"/api/v1/friends/requests/{request_id}/accept",
            headers=self.auth_headers(bob_token),
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)

        # 申请已经处理完，再次拒绝必须干净地失败，而不是删掉好友关系。
        declined = self.client.post(
            f"/api/v1/friends/requests/{request_id}/decline",
            headers=self.auth_headers(bob_token),
        )
        self.assertEqual(declined.status_code, 404, declined.text)

        friends = self.client.get(
            "/api/v1/friends", headers=self.auth_headers(bob_token)
        )
        self.assertEqual(friends.status_code, 200, friends.text)
        friend_ids = {f["user_id"] for f in friends.json()["friends"]}
        self.assertIn(alice["id"], friend_ids)

    def test_duplicate_friend_request_returns_conflict(self) -> None:
        """重复的“加好友”必须是 4xx，不能变成 500。"""
        alice_token, alice = self._register_user("dup_alice")
        bob_token, bob = self._register_user("dup_bob")

        first = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": bob["id"]},
            headers=self.auth_headers(alice_token),
        )
        self.assertEqual(first.status_code, 200, first.text)

        # 同一方向再发一次：提示已发送。
        again = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": bob["id"]},
            headers=self.auth_headers(alice_token),
        )
        self.assertIn(again.status_code, (400, 409), again.text)

        # 对方也发一次：冲突，而不是服务器错误。
        reverse = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": alice["id"]},
            headers=self.auth_headers(bob_token),
        )
        self.assertIn(reverse.status_code, (400, 409), reverse.text)

    def test_history_only_marks_returned_messages_read(self) -> None:
        """只取回 limit 条时，没取回的消息不能被标成已读。"""
        alice_token, alice = self._register_user("dm_alice")
        bob_token, bob = self._register_user("dm_bob")
        self._make_friends(
            alice_token, alice["id"], bob_token, bob["id"]
        )

        for index in range(5):
            sent = self.client.post(
                f"/api/v1/messages/{bob['id']}",
                json={"content": f"第 {index} 条"},
                headers=self.auth_headers(alice_token),
            )
            self.assertEqual(sent.status_code, 200, sent.text)

        history = self.client.get(
            f"/api/v1/messages/{alice['id']}",
            params={"limit": 2},
            headers=self.auth_headers(bob_token),
        )
        self.assertEqual(history.status_code, 200, history.text)
        self.assertEqual(len(history.json()["messages"]), 2)

        # 5 条只取回 2 条：未读数必须是 3，而不是被清零。
        friends = self.client.get(
            "/api/v1/friends", headers=self.auth_headers(bob_token)
        ).json()
        entries = [
            f for f in friends["friends"] if f["user_id"] == alice["id"]
        ]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["unread"], 3)

    def test_friend_request_rejects_self_and_missing(self) -> None:
        token, user = self._register_user("friend_solo")
        headers = self.auth_headers(token)

        self_request = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": user["id"]},
            headers=headers,
        )
        self.assertEqual(self_request.status_code, 400)

        missing = self.client.post(
            "/api/v1/friends/requests",
            json={"user_id": "missing-user"},
            headers=headers,
        )
        self.assertEqual(missing.status_code, 404)

    def test_direct_message_flow_and_push(self) -> None:
        alice_token, alice = self._register_user("dm_alice")
        bob_token, bob = self._register_user("dm_bob")
        self._make_friends(
            alice_token, alice["id"], bob_token, bob["id"]
        )

        with self.client.websocket_connect("/api/v1/ws") as bob_ws:
            self._authenticate_ws(bob_ws, bob_token)

            sent = self.client.post(
                f"/api/v1/messages/{bob['id']}",
                json={"content": "你好，晚上来一局？"},
                headers=self.auth_headers(alice_token),
            )
            self.assertEqual(sent.status_code, 200, sent.text)
            self.assertEqual(
                sent.json()["content"], "你好，晚上来一局？"
            )

            push = bob_ws.receive_json()
            self.assertEqual(push["type"], SERVER_DIRECT_MESSAGE)
            self.assertEqual(
                push["payload"]["content"], "你好，晚上来一局？"
            )
            self.assertEqual(push["payload"]["sender_id"], alice["id"])

            history = self.client.get(
                f"/api/v1/messages/{alice['id']}",
                headers=self.auth_headers(bob_token),
            )
            self.assertEqual(history.status_code, 200, history.text)
            self.assertEqual(len(history.json()["messages"]), 1)
            self.assertEqual(
                history.json()["messages"][0]["content"],
                "你好，晚上来一局？",
            )

            friends = self.client.get(
                "/api/v1/friends",
                headers=self.auth_headers(bob_token),
            ).json()
            self.assertEqual(friends["friends"][0]["unread"], 0)

        charlie_token, _ = self._register_user("dm_charlie")
        not_friend = self.client.post(
            f"/api/v1/messages/{bob['id']}",
            json={"content": "hi"},
            headers=self.auth_headers(charlie_token),
        )
        self.assertEqual(not_friend.status_code, 403)

        empty = self.client.post(
            f"/api/v1/messages/{bob['id']}",
            json={"content": "​"},
            headers=self.auth_headers(alice_token),
        )
        self.assertEqual(empty.status_code, 400)

    def test_friend_presence_events(self) -> None:
        alice_token, alice = self._register_user("presence_alice")
        bob_token, bob = self._register_user("presence_bob")
        self._make_friends(
            alice_token, alice["id"], bob_token, bob["id"]
        )

        with self.client.websocket_connect("/api/v1/ws") as alice_ws:
            self._authenticate_ws(alice_ws, alice_token)
            with self.client.websocket_connect("/api/v1/ws") as bob_ws:
                self._authenticate_ws(bob_ws, bob_token)
                online = alice_ws.receive_json()
                self.assertEqual(online["type"], SERVER_FRIEND_EVENT)
                self.assertEqual(online["payload"]["event"], "online")
                self.assertEqual(online["payload"]["user"]["id"], bob["id"])

            offline = alice_ws.receive_json()
            self.assertEqual(offline["type"], SERVER_FRIEND_EVENT)
            self.assertEqual(offline["payload"]["event"], "offline")
            self.assertEqual(
                offline["payload"]["user"]["id"], bob["id"]
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
