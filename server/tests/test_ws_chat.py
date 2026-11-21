"""好友房表情/评论的 WebSocket 端到端测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.chat import (
    CHAT_BURST,
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    EMOJI_PRESETS,
    MAX_COMMENT_LENGTH,
)
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CHAT,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_LEAVE_ROOM,
    SERVER_AUTHENTICATED,
    SERVER_CHAT,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_CREATED,
    make_message,
)
from server.tests.helpers import ServerTestBase


class WebSocketChatTests(ServerTestBase):
    """两个客户端在同一个好友房里互发表情/评论。"""

    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(
            make_message(CLIENT_AUTH, {"access_token": token})
        )
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _start_friend_room(self, host, guest, host_token, guest_token):
        """建房并让对手加入，返回房间码。"""
        self._authenticate(host, host_token)
        self._authenticate(guest, guest_token)
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
        return code

    def test_chat_comment_is_broadcast_to_both_players(self) -> None:
        host_token = self._register_access("chat_host")
        guest_token = self._register_access("chat_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                host_auth = self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                host_user_id = host_auth["payload"]["user"]["id"]
                host_name = host_auth["payload"]["user"]["username"]

                host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
                code = host.receive_json()["payload"]["room_code"]
                guest.send_json(
                    make_message(CLIENT_JOIN_ROOM, {"room_code": code})
                )
                host.receive_json()  # game_state
                guest.receive_json()  # game_state

                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_TEXT, "content": "好棋！"},
                        request_id="chat-1",
                    )
                )
                host_message = host.receive_json()
                guest_message = guest.receive_json()

                self.assertEqual(host_message["type"], SERVER_CHAT)
                self.assertEqual(guest_message["type"], SERVER_CHAT)
                for message in (host_message, guest_message):
                    payload = message["payload"]
                    self.assertEqual(payload["kind"], CHAT_KIND_TEXT)
                    self.assertEqual(payload["content"], "好棋！")
                    self.assertEqual(payload["sender_id"], host_user_id)
                    self.assertEqual(payload["sender_name"], host_name)

    def test_chat_emoji_is_broadcast_to_both_players(self) -> None:
        host_token = self._register_access("emoji_host")
        guest_token = self._register_access("emoji_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                emoji = EMOJI_PRESETS[0]
                guest.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_EMOJI, "content": emoji},
                        request_id="emoji-1",
                    )
                )
                host_message = host.receive_json()
                guest_message = guest.receive_json()
                self.assertEqual(host_message["type"], SERVER_CHAT)
                self.assertEqual(guest_message["type"], SERVER_CHAT)
                self.assertEqual(host_message["payload"]["content"], emoji)
                self.assertEqual(
                    host_message["payload"]["kind"], CHAT_KIND_EMOJI
                )

    def test_chat_comment_is_trimmed_before_broadcast(self) -> None:
        host_token = self._register_access("trim_host")
        guest_token = self._register_access("trim_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_TEXT, "content": "  好棋！  "},
                        request_id="trim-1",
                    )
                )
                host_message = host.receive_json()
                guest_message = guest.receive_json()
                self.assertEqual(host_message["type"], SERVER_CHAT)
                self.assertEqual(guest_message["type"], SERVER_CHAT)
                self.assertEqual(host_message["payload"]["content"], "好棋！")
                self.assertEqual(guest_message["payload"]["content"], "好棋！")

    def test_chat_content_is_sanitized_by_the_server(self) -> None:
        """服务器必须自己清洗内容，不能依赖客户端已经清洗过。

        换行、零宽空格这类字符会破坏对话日志排版，甚至伪装成别人
        说的话；恶意客户端可以直接构造原始帧绕开客户端校验。
        """
        host_token = self._register_access("dirty_host")
        guest_token = self._register_access("dirty_guest")

        # 用码位构造脏字符，避免源码里出现不可见字符。
        bidi = chr(0x202E)      # 方向覆写
        zero_width = chr(0x200B)  # 零宽空格
        newline = chr(0x0A)      # 换行
        dirty = "你好" + bidi + zero_width + newline + "好"

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_TEXT, "content": dirty},
                        request_id="dirty-1",
                    )
                )
                host_message = host.receive_json()
                guest_message = guest.receive_json()
                self.assertEqual(host_message["type"], SERVER_CHAT)
                self.assertEqual(guest_message["type"], SERVER_CHAT)
                for message in (host_message, guest_message):
                    content = message["payload"]["content"]
                    self.assertNotIn(newline, content)
                    self.assertNotIn(bidi, content)
                    self.assertNotIn(zero_width, content)
                    # 换行被换成空格，其余不可见字符被删除。
                    self.assertEqual(content, "你好 好")

    def test_unknown_chat_kind_is_rejected(self) -> None:
        host_token = self._register_access("bad_kind_host")
        guest_token = self._register_access("bad_kind_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": "sticker", "content": "😀"},
                        request_id="bad-kind",
                    )
                )
                error = host.receive_json()
                self.assertEqual(error["type"], SERVER_ERROR)
                self.assertEqual(error["request_id"], "bad-kind")
                self.assertTrue(error["payload"]["reason"])

    def test_overlong_comment_is_rejected(self) -> None:
        host_token = self._register_access("long_host")
        guest_token = self._register_access("long_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {
                            "kind": CHAT_KIND_TEXT,
                            "content": "评" * (MAX_COMMENT_LENGTH + 1),
                        },
                        request_id="too-long",
                    )
                )
                error = host.receive_json()
                self.assertEqual(error["type"], SERVER_ERROR)
                self.assertEqual(error["request_id"], "too-long")

    def test_non_preset_emoji_is_rejected(self) -> None:
        host_token = self._register_access("emoji_bad_host")
        guest_token = self._register_access("emoji_bad_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                host.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_EMOJI, "content": "😀"},
                        request_id="bad-emoji",
                    )
                )
                error = host.receive_json()
                self.assertEqual(error["type"], SERVER_ERROR)
                self.assertEqual(error["request_id"], "bad-emoji")

    def test_chat_rate_limit_is_per_user(self) -> None:
        """限流按玩家算，不是按房间算。

        一名玩家把额度用光，不应该连累同房间的另一名玩家。
        """
        host_token = self._register_access("peruser_host")
        guest_token = self._register_access("peruser_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                # 房主先把自己的额度用光。
                for index in range(CHAT_BURST):
                    host.send_json(
                        make_message(
                            CLIENT_CHAT,
                            {
                                "kind": CHAT_KIND_TEXT,
                                "content": f"快点{index}",
                            },
                            request_id=f"pu-host-{index}",
                        )
                    )
                # 双方都会收到房主这批广播，先各自收完。
                for _ in range(CHAT_BURST):
                    self.assertEqual(
                        host.receive_json()["type"], SERVER_CHAT
                    )
                for _ in range(CHAT_BURST):
                    self.assertEqual(
                        guest.receive_json()["type"], SERVER_CHAT
                    )

                # 同一时刻，对手仍然可以正常发言。
                guest.send_json(
                    make_message(
                        CLIENT_CHAT,
                        {"kind": CHAT_KIND_TEXT, "content": "我不急"},
                        request_id="pu-guest-1",
                    )
                )
                guest_message = guest.receive_json()
                self.assertEqual(guest_message["type"], SERVER_CHAT)
                self.assertEqual(
                    guest_message["payload"]["content"], "我不急"
                )

    def test_chat_burst_beyond_limit_is_rejected(self) -> None:
        host_token = self._register_access("burst_host")
        guest_token = self._register_access("burst_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._start_friend_room(
                    host, guest, host_token, guest_token
                )
                total = CHAT_BURST + 1
                for index in range(total):
                    host.send_json(
                        make_message(
                            CLIENT_CHAT,
                            {
                                "kind": CHAT_KIND_TEXT,
                                "content": f"快点{index}",
                            },
                            request_id=f"burst-{index}",
                        )
                    )

                received = [
                    host.receive_json() for _ in range(total)
                ]
                types = [message["type"] for message in received]
                self.assertEqual(types.count(SERVER_CHAT), CHAT_BURST)
                self.assertGreaterEqual(types.count(SERVER_ERROR), 1)

                errors = [
                    message
                    for message in received
                    if message["type"] == SERVER_ERROR
                ]
                self.assertEqual(
                    errors[0]["request_id"], f"burst-{CHAT_BURST}"
                )

    def test_chat_while_waiting_for_opponent_is_rejected(self) -> None:
        host_token = self._register_access("waiting_host")

        with self.client.websocket_connect("/api/v1/ws") as host:
            self._authenticate(host, host_token)
            host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
            created = host.receive_json()
            self.assertEqual(created["type"], SERVER_ROOM_CREATED)

            host.send_json(
                make_message(
                    CLIENT_CHAT,
                    {"kind": CHAT_KIND_TEXT, "content": "有人吗？"},
                    request_id="waiting-chat",
                )
            )
            error = host.receive_json()
            self.assertEqual(error["type"], SERVER_ERROR)
            self.assertEqual(error["request_id"], "waiting-chat")
            self.assertIn("对手", error["payload"]["reason"])

            # 主动离开房间：让等待房的作废写库在应用存活期间完成，
            # 避免断开连接时的后台写库与测试夹具清理临时数据库竞争。
            host.send_json(make_message(CLIENT_LEAVE_ROOM, {}))
            closed = host.receive_json()
            self.assertEqual(closed["type"], SERVER_ROOM_CLOSED)

    def test_chat_outside_room_is_rejected(self) -> None:
        token = self._register_access("no_room_user")

        with self.client.websocket_connect("/api/v1/ws") as solo:
            self._authenticate(solo, token)
            solo.send_json(
                make_message(
                    CLIENT_CHAT,
                    {"kind": CHAT_KIND_TEXT, "content": "有人吗？"},
                    request_id="no-room",
                )
            )
            error = solo.receive_json()
            self.assertEqual(error["type"], SERVER_ERROR)
            self.assertEqual(error["request_id"], "no-room")


if __name__ == "__main__":
    unittest.main(verbosity=2)
