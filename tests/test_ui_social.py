"""个人信息、好友、私聊与排行榜头像的离屏 UI 测试。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from gomoku.client.friends_window import FriendsWindow  # noqa: E402
from gomoku.client.online_window import OnlineWindow  # noqa: E402
from gomoku.client.private_chat_window import PrivateChatWindow  # noqa: E402
from gomoku.client.profile_window import (  # noqa: E402
    AvatarPickerDialog,
    ProfileWindow,
)
from gomoku.shared.avatar import AVATARS  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


def profile_dict(
    *,
    friendship: str = "none",
    online: bool = True,
) -> dict:
    return {
        "user": {
            "id": "u2",
            "username": "好友",
            "avatar": "lan",
            "rating": 1300,
            "peak_rating": 1400,
            "rated_games": 10,
            "wins": 6,
            "losses": 3,
            "draws": 1,
            "win_rate": 60.0,
            "rank_name": "铂金",
            "created_at": "2026-01-02T03:04:05",
        },
        "online": online,
        "friendship": friendship,
    }


class ProfileWindowTests(unittest.TestCase):
    def test_profile_fields_and_add_friend(self) -> None:
        window = ProfileWindow()
        window.set_profile(profile_dict())
        self.assertEqual(window.name_label.text(), "好友")
        self.assertIn("在线", window.online_label.text())
        self.assertEqual(
            window._stat_values["rating"].text(), "1300"
        )
        self.assertIn("6 胜", window._stat_values["record"].text())

        buttons = {
            button.text(): button
            for button in window.findChildren(QPushButton)
        }
        self.assertIn("加为好友", buttons)
        emitted: list[str] = []
        window.add_friend_requested.connect(emitted.append)
        buttons["加为好友"].click()
        self.assertEqual(emitted, ["u2"])
        window.close()

    def test_friend_actions_and_self_avatar(self) -> None:
        window = ProfileWindow()
        window.set_profile(profile_dict(friendship="friends"))
        texts = {
            button.text()
            for button in window.findChildren(QPushButton)
        }
        self.assertIn("发消息", texts)
        self.assertIn("删除好友", texts)
        window.close()

        own = ProfileWindow()
        own.set_profile(profile_dict(friendship="self"))
        texts = {
            button.text()
            for button in own.findChildren(QPushButton)
        }
        self.assertIn("更换头像", texts)
        own.close()

    def test_avatar_picker(self) -> None:
        picker = AvatarPickerDialog("mei")
        buttons = [
            button
            for button in picker.findChildren(QPushButton)
            if button.objectName() == "avatarChoice"
        ]
        self.assertEqual(len(buttons), len(AVATARS))
        selected: list[str] = []
        picker.avatar_selected.connect(selected.append)
        buttons[1].click()
        self.assertEqual(selected, [AVATARS[1].key])


class FriendsWindowTests(unittest.TestCase):
    def test_rows_and_actions(self) -> None:
        window = FriendsWindow()
        window.set_data(
            {
                "friends": [
                    {
                        "user_id": "u2",
                        "username": "好友",
                        "avatar": "lan",
                        "online": True,
                        "unread": 2,
                    }
                ],
                "incoming": [
                    {
                        "request_id": "r1",
                        "user_id": "u3",
                        "username": "小明",
                        "avatar": "mei",
                    }
                ],
                "outgoing": [],
            }
        )
        buttons = {
            button.text(): button
            for button in window.findChildren(QPushButton)
        }
        self.assertIn("接受", buttons)
        self.assertIn("聊天", buttons)
        self.assertIn("邀请对局", buttons)

        accepted: list[str] = []
        chats: list[tuple[str, str]] = []
        window.accept_requested.connect(accepted.append)
        window.chat_requested.connect(
            lambda uid, name: chats.append((uid, name))
        )
        buttons["接受"].click()
        buttons["聊天"].click()
        self.assertEqual(accepted, ["r1"])
        self.assertEqual(chats, [("u2", "好友")])
        window.close()

    def test_search_results_add_friend(self) -> None:
        window = FriendsWindow()
        window.set_search_results(
            [
                {
                    "id": "u9",
                    "username": "新朋友",
                    "avatar": "zhu",
                    "online": False,
                }
            ]
        )
        buttons = {
            button.text(): button
            for button in window.findChildren(QPushButton)
        }
        added: list[str] = []
        window.add_requested.connect(added.append)
        buttons["加为好友"].click()
        self.assertEqual(added, ["u9"])
        window.close()


class PrivateChatWindowTests(unittest.TestCase):
    def test_history_and_send(self) -> None:
        window = PrivateChatWindow("u2", "好友", "lan")
        window.set_history(
            [
                {
                    "sender_id": "me",
                    "sender_name": "我",
                    "content": "你好",
                    "sent_at_ms": 1_700_000_000_000,
                },
                {
                    "sender_id": "u2",
                    "sender_name": "好友",
                    "content": "<b>hi</b>",
                    "sent_at_ms": 1_700_000_060_000,
                },
            ],
            "me",
        )
        text = window.messages_view.toPlainText()
        self.assertIn("你好", text)
        self.assertIn("<b>hi</b>", text)

        sent: list[str] = []
        window.send_requested.connect(sent.append)
        window.input.setText("晚上来一局？")
        window._on_send()
        self.assertEqual(sent, ["晚上来一局？"])
        self.assertEqual(window.input.text(), "")
        window.close()


class OnlineWindowSocialTests(unittest.TestCase):
    def test_leaderboard_avatar_and_friends_badge(self) -> None:
        window = OnlineWindow()
        window._fill_leaderboard(
            [
                {
                    "position": 1,
                    "user_id": "u1",
                    "username": "冠军",
                    "avatar": "mei",
                    "rank_name": "大师",
                    "rating": 1500,
                    "wins": 1,
                    "losses": 0,
                    "draws": 0,
                }
            ],
            1,
        )
        item = window.leaderboard_table.item(0, 1)
        self.assertIsNotNone(item)
        self.assertFalse(item.icon().isNull())

        window._on_social_ready(
            "friends",
            {
                "friends": [],
                "incoming": [
                    {
                        "request_id": "r1",
                        "user_id": "u3",
                        "username": "小明",
                        "avatar": "mei",
                    }
                ],
                "outgoing": [],
            },
        )
        self.assertEqual(window.friends_button.text(), "好友 (1)")

        window._show_profile_window(profile_dict())
        self.assertIsNotNone(window._profile_window)
        self.assertEqual(
            window._profile_window.name_label.text(), "好友"
        )
        window.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
