"""同观频道（spectator_chat）的服务端端到端测试。

同观是观战者之间的一条私密频道，设计上有三条硬约束：

* 频道内容**只**在观战席内部流动，棋手既收不到也看不到；
* 段位赛关闭这个口子——观战者能实时看到棋盘，场外传话会绕过
  现有的举报与回放审计；
* 频道不落库，房间关闭就没了，因此这里不涉及任何数据库改动。
"""

from __future__ import annotations

import unittest

from gomoku.shared.chat import CHAT_BURST
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_QUICK_MATCH,
    CLIENT_SPECTATOR_CHAT,
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_QUEUE_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_SPECTATOR_CHAT,
    make_message,
)
from server.tests.helpers import ServerTestBase


class _SpectatorChatTestBase(ServerTestBase):
    """两名玩家 + 若干观战者的联机夹具。"""

    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> dict:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)
        return message

    def _receive_type(self, ws, expected: str, attempts: int = 20) -> dict:
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到 {expected}")

    def _collect_until(
        self, ws, expected: str, attempts: int = 20
    ) -> tuple[dict, list[dict]]:
        """读取到期望类型为止，并返回期间收到的所有消息。

        返回中间消息是为了断言"本该被隔离的广播没有泄漏出去"。
        """
        seen: list[dict] = []
        for _ in range(attempts):
            message = ws.receive_json()
            seen.append(message)
            if message["type"] == expected:
                return message, seen
        self.fail(f"未收到 {expected}")

    def _watch(self, viewer, game_id: str) -> dict:
        viewer.send_json(
            make_message(CLIENT_WATCH_GAME, {"game_id": game_id})
        )
        return self._receive_type(viewer, SERVER_GAME_STATE)

    def _move(self, sender, row: int, col: int, request_id: str) -> None:
        sender.send_json(
            make_message(
                CLIENT_MOVE_INTENT,
                {"row": row, "col": col},
                request_id=request_id,
            )
        )


class SpectatorChatDeliveryTests(_SpectatorChatTestBase):
    """同观消息的投递范围。"""

    def _room_game_id(self, host, guest) -> str:
        """开一局好友房并返回对局编号。"""
        host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
        created = host.receive_json()
        self.assertEqual(created["type"], SERVER_ROOM_CREATED)
        code = created["payload"]["room_code"]
        guest.send_json(make_message(CLIENT_JOIN_ROOM, {"room_code": code}))
        host_state = host.receive_json()
        guest_state = guest.receive_json()
        self.assertEqual(host_state["type"], SERVER_GAME_STATE)
        self.assertEqual(guest_state["type"], SERVER_GAME_STATE)
        return host_state["payload"]["game_id"]

    def test_spectator_message_does_not_reach_players(self) -> None:
        """核心约束：观战者发言只到观战席，棋手一条都收不到。"""
        host_token = self._register_access("sch_host")
        guest_token = self._register_access("sch_guest")
        viewer_token = self._register_access("sch_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._room_game_id(host, guest)
                    self._watch(viewer, game_id)

                    viewer.send_json(
                        make_message(
                            CLIENT_SPECTATOR_CHAT,
                            {"kind": "text", "content": "这步该走那里"},
                            request_id="sch-1",
                        )
                    )
                    # 观战者自己收到广播（含发送者本人）。
                    received = self._receive_type(viewer, SERVER_SPECTATOR_CHAT)
                    self.assertEqual(
                        received["payload"]["content"], "这步该走那里"
                    )
                    self.assertEqual(received["payload"]["kind"], "text")
                    self.assertTrue(received["payload"]["sender_name"])

                    # 玩家继续落子，收集期间收到的所有消息：
                    # 中间不能出现任何同观广播。
                    self._move(host, 7, 7, "sch-m1")
                    _, host_seen = self._collect_until(host, SERVER_GAME_STATE)
                    _, guest_seen = self._collect_until(guest, SERVER_GAME_STATE)
                    for seen in (host_seen, guest_seen):
                        self.assertFalse(
                            any(
                                m["type"] == SERVER_SPECTATOR_CHAT
                                for m in seen
                            ),
                            seen,
                        )
                        self.assertFalse(
                            any(m["type"] == SERVER_ERROR for m in seen),
                            seen,
                        )

    def test_two_spectators_see_each_other(self) -> None:
        """同席的两位观战者互相看得见。"""
        host_token = self._register_access("sch2_host")
        guest_token = self._register_access("sch2_guest")
        first_token = self._register_access("sch2_viewer1")
        second_token = self._register_access("sch2_viewer2")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as first:
                    with self.client.websocket_connect(
                        "/api/v1/ws"
                    ) as second:
                        self._authenticate(host, host_token)
                        self._authenticate(guest, guest_token)
                        self._authenticate(first, first_token)
                        self._authenticate(second, second_token)
                        game_id = self._room_game_id(host, guest)
                        self._watch(first, game_id)
                        self._watch(second, game_id)

                        first.send_json(
                            make_message(
                                CLIENT_SPECTATOR_CHAT,
                                {"kind": "text", "content": "我押左边"},
                            )
                        )
                        # 发送者自己也收到。
                        self._receive_type(first, SERVER_SPECTATOR_CHAT)
                        # 同席的另一位观战者也收到。
                        other = self._receive_type(
                            second, SERVER_SPECTATOR_CHAT
                        )
                        self.assertEqual(
                            other["payload"]["content"], "我押左边"
                        )

    def test_player_cannot_use_spectator_channel(self) -> None:
        """棋手不能往同观频道里塞消息。"""
        host_token = self._register_access("sch3_host")
        guest_token = self._register_access("sch3_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                self._room_game_id(host, guest)

                host.send_json(
                    make_message(
                        CLIENT_SPECTATOR_CHAT,
                        {"kind": "text", "content": "我其实是观战者"},
                    )
                )
                error = self._receive_type(host, SERVER_ERROR)
                self.assertEqual(
                    error["payload"]["reason"], "你不在观战席中"
                )


class SpectatorChatRestrictionTests(_SpectatorChatTestBase):
    """同观频道的边界：段位赛关闭、内容校验、限流。"""

    def _rated_game_id(self, host, guest) -> str:
        """用快速匹配开一局段位赛，返回对局编号。"""
        host.send_json(make_message(CLIENT_QUICK_MATCH, {}))
        self._receive_type(host, SERVER_QUEUE_STATUS)
        guest.send_json(make_message(CLIENT_QUICK_MATCH, {}))
        self._receive_type(host, SERVER_QUEUE_STATUS)
        host_found = self._receive_type(host, SERVER_MATCH_FOUND)
        self._receive_type(guest, SERVER_MATCH_FOUND)
        self._receive_type(host, SERVER_GAME_STATE)
        self._receive_type(guest, SERVER_GAME_STATE)
        self.assertTrue(host_found["payload"]["rated"])
        return host_found["payload"]["game_id"]

    def test_rated_game_rejects_spectator_chat(self) -> None:
        """段位赛不开放观战席聊天。"""
        host_token = self._register_access("rated_host")
        guest_token = self._register_access("rated_guest")
        viewer_token = self._register_access("rated_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._rated_game_id(host, guest)
                    self._watch(viewer, game_id)

                    viewer.send_json(
                        make_message(
                            CLIENT_SPECTATOR_CHAT,
                            {"kind": "text", "content": "给他递个话"},
                        )
                    )
                    error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        error["payload"]["reason"],
                        "排位对局不支持观战席聊天",
                    )

    def test_invalid_content_is_rejected(self) -> None:
        """同观频道沿用对局聊天的内容校验。"""
        host_token = self._register_access("bad_host")
        guest_token = self._register_access("bad_guest")
        viewer_token = self._register_access("bad_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
                    created = host.receive_json()
                    code = created["payload"]["room_code"]
                    guest.send_json(
                        make_message(
                            CLIENT_JOIN_ROOM, {"room_code": code}
                        )
                    )
                    host_state = host.receive_json()
                    guest.receive_json()
                    self._watch(viewer, host_state["payload"]["game_id"])

                    # 不是预设的表情：拒绝。
                    viewer.send_json(
                        make_message(
                            CLIENT_SPECTATOR_CHAT,
                            {"kind": "emoji", "content": "不存在的表情"},
                        )
                    )
                    error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertIn(
                        "表情", error["payload"]["reason"]
                    )

                    # 空评论：拒绝。
                    viewer.send_json(
                        make_message(
                            CLIENT_SPECTATOR_CHAT,
                            {"kind": "text", "content": "   "},
                        )
                    )
                    empty_error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertIn("评论", empty_error["payload"]["reason"])

    def test_spectator_chat_is_rate_limited(self) -> None:
        """连续发言会被限流挡住。"""
        host_token = self._register_access("rate_host")
        guest_token = self._register_access("rate_guest")
        viewer_token = self._register_access("rate_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
                    created = host.receive_json()
                    code = created["payload"]["room_code"]
                    guest.send_json(
                        make_message(
                            CLIENT_JOIN_ROOM, {"room_code": code}
                        )
                    )
                    host_state = host.receive_json()
                    guest.receive_json()
                    self._watch(viewer, host_state["payload"]["game_id"])

                    # 连续发满突发额度后，多出来的一条会被拒绝。
                    for index in range(CHAT_BURST):
                        viewer.send_json(
                            make_message(
                                CLIENT_SPECTATOR_CHAT,
                                {
                                    "kind": "text",
                                    "content": f"第{index}条",
                                },
                            )
                        )
                        self._receive_type(viewer, SERVER_SPECTATOR_CHAT)

                    viewer.send_json(
                        make_message(
                            CLIENT_SPECTATOR_CHAT,
                            {"kind": "text", "content": "太多了"},
                        )
                    )
                    error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        error["payload"]["reason"], "发言太快，请稍候"
                    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
