"""共享头像/好友/私聊校验与会话消息测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.avatar import (  # noqa: E402
    AVATARS,
    DEFAULT_AVATAR,
    default_avatar_for,
    is_valid_avatar,
    normalize_avatar,
)
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_INVITE_TO_ROOM,
    CLIENT_RESPOND_ROOM_INVITE,
    SERVER_AUTHENTICATED,
    SERVER_DIRECT_MESSAGE,
    SERVER_FRIEND_EVENT,
    SERVER_ROOM_INVITATION,
    SERVER_ROOM_INVITE_RESULT,
    make_message,
)
from gomoku.shared.social import (  # noqa: E402
    MAX_DIRECT_MESSAGE_LENGTH,
    FriendEvent,
    SocialError,
    normalize_direct_message,
    parse_direct_message,
    parse_friend_event,
)
from tests.fakes import FakeTransport  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


class AvatarTests(unittest.TestCase):
    def test_presets_and_fallback(self) -> None:
        self.assertGreaterEqual(len(AVATARS), 8)
        self.assertTrue(is_valid_avatar("mei"))
        self.assertFalse(is_valid_avatar("nope"))
        self.assertEqual(normalize_avatar("mei", "u1"), "mei")
        self.assertTrue(is_valid_avatar(normalize_avatar("nope", "u1")))
        self.assertEqual(
            default_avatar_for("same-user"),
            default_avatar_for("same-user"),
        )
        self.assertEqual(default_avatar_for(""), DEFAULT_AVATAR)


class DirectMessageValidationTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(
            normalize_direct_message("  你好\n 晚上好  "),
            "你好 晚上好",
        )
        with self.assertRaises(SocialError):
            normalize_direct_message(" ​ ")
        with self.assertRaises(SocialError):
            normalize_direct_message("x" * (MAX_DIRECT_MESSAGE_LENGTH + 1))
        with self.assertRaises(SocialError):
            normalize_direct_message(123)

    def test_parse_direct_message(self) -> None:
        message = parse_direct_message(
            {
                "id": "m1",
                "sender_id": "u2",
                "sender_name": "好友",
                "sender_avatar": "lan",
                "recipient_id": "u1",
                "content": "hi",
                "sent_at_ms": 1234,
            }
        )
        self.assertEqual(message.sender_id, "u2")
        self.assertEqual(message.sender_avatar, "lan")
        with self.assertRaises(SocialError):
            parse_direct_message({"id": "m1"})

    def test_parse_friend_event(self) -> None:
        event = parse_friend_event(
            {
                "event": "request",
                "request_id": "r1",
                "user": {
                    "id": "u2",
                    "username": "好友",
                    "avatar": "lan",
                    "rating": 1300,
                    "rank_name": "铂金",
                },
            }
        )
        self.assertIsInstance(event, FriendEvent)
        self.assertEqual(event.event, "request")
        self.assertEqual(event.request_id, "r1")
        with self.assertRaises(SocialError):
            parse_friend_event({"event": "unknown", "user": {}})


class OnlineSessionSocialTests(unittest.TestCase):
    def _session(self) -> tuple[FakeTransport, OnlineGameSession]:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        transport.receive(
            make_message(
                SERVER_AUTHENTICATED,
                {
                    "user": {
                        "id": "me",
                        "username": "我",
                        "avatar": "mei",
                        "rating": 1200,
                        "rank_name": "黄金",
                    }
                },
            )
        )
        return transport, session

    def test_authenticated_payload_includes_avatar(self) -> None:
        transport, session = self._session()
        received: list[dict] = []
        session.authenticated.connect(received.append)
        transport.receive(
            make_message(
                SERVER_AUTHENTICATED,
                {
                    "user": {
                        "id": "me2",
                        "username": "我2",
                        "avatar": "lan",
                        "rating": 1250,
                        "rank_name": "铂金",
                    }
                },
            )
        )
        self.assertEqual(received[-1]["avatar"], "lan")
        session.shutdown()

    def test_invitation_signals_and_commands(self) -> None:
        transport, session = self._session()
        invites: list[dict] = []
        results: list[dict] = []
        session.room_invited.connect(invites.append)
        session.room_invite_result.connect(results.append)

        transport.receive(
            make_message(
                SERVER_ROOM_INVITATION,
                {
                    "invite_id": "i1",
                    "inviter": {
                        "id": "u2",
                        "username": "好友",
                        "avatar": "lan",
                    },
                    "rated": False,
                },
            )
        )
        self.assertEqual(invites[0]["invite_id"], "i1")
        self.assertEqual(invites[0]["inviter"]["username"], "好友")

        transport.receive(
            make_message(
                SERVER_ROOM_INVITE_RESULT,
                {"invite_id": "i1", "status": "accepted", "accepted": True},
            )
        )
        self.assertEqual(results[0]["status"], "accepted")

        self.assertTrue(session.invite_to_room("u2"))
        sent = transport.sent_messages[-1]
        self.assertEqual(sent["type"], CLIENT_INVITE_TO_ROOM)
        self.assertEqual(sent["payload"]["user_id"], "u2")

        self.assertTrue(session.respond_room_invite("i1", True))
        sent = transport.sent_messages[-1]
        self.assertEqual(sent["type"], CLIENT_RESPOND_ROOM_INVITE)
        self.assertTrue(sent["payload"]["accept"])
        session.shutdown()

    def test_friend_and_direct_message_signals(self) -> None:
        transport, session = self._session()
        events: list = []
        messages: list = []
        session.friend_event.connect(events.append)
        session.direct_message.connect(messages.append)

        transport.receive(
            make_message(
                SERVER_FRIEND_EVENT,
                {
                    "event": "accepted",
                    "user": {
                        "id": "u2",
                        "username": "好友",
                        "avatar": "lan",
                    },
                },
            )
        )
        self.assertEqual(events[0].event, "accepted")

        transport.receive(
            make_message(
                SERVER_DIRECT_MESSAGE,
                {
                    "id": "m1",
                    "sender_id": "u2",
                    "sender_name": "好友",
                    "sender_avatar": "lan",
                    "recipient_id": "me",
                    "content": "晚上来一局？",
                    "sent_at_ms": 1000,
                },
            )
        )
        self.assertEqual(messages[0].content, "晚上来一局？")
        session.shutdown()


if __name__ == "__main__":
    unittest.main(verbosity=2)
