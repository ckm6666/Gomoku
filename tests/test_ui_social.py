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

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QPushButton,
    QWidget,
)

from gomoku.client.friends_panel import FriendsPanel  # noqa: E402
from gomoku.client.online_panel import OnlinePanel  # noqa: E402
from gomoku.client.private_chat_panel import PrivateChatPanel  # noqa: E402
from gomoku.client.profile_panel import (  # noqa: E402
    AvatarPickerPanel,
    ProfilePanel,
)
from gomoku.client.auth import AuthSession, AuthUser  # noqa: E402
from gomoku.shared.avatar import AVATARS  # noqa: E402
from gomoku.shared.social import DirectMessage  # noqa: E402

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


class ProfilePanelTests(unittest.TestCase):
    def test_profile_fields_and_add_friend(self) -> None:
        window = ProfilePanel()
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
        window = ProfilePanel()
        window.set_profile(profile_dict(friendship="friends"))
        texts = {
            button.text()
            for button in window.findChildren(QPushButton)
        }
        self.assertIn("发消息", texts)
        self.assertIn("删除好友", texts)
        window.close()

        own = ProfilePanel()
        own.set_profile(profile_dict(friendship="self"))
        texts = {
            button.text()
            for button in own.findChildren(QPushButton)
        }
        self.assertIn("更换头像", texts)
        own.close()

    def test_avatar_picker(self) -> None:
        picker = AvatarPickerPanel("mei")
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


class FriendsPanelTests(unittest.TestCase):
    def test_rows_and_actions(self) -> None:
        window = FriendsPanel()
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
        window = FriendsPanel()
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


class PrivateChatPanelTests(unittest.TestCase):
    def test_history_and_send(self) -> None:
        window = PrivateChatPanel("u2", "好友", "lan")
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

    def test_close_button_hides_the_chat_window(self) -> None:
        """私聊浮层没有系统标题栏，必须自己提供关闭入口。"""
        window = PrivateChatPanel("u2", "好友", "lan")
        window.show()
        self.assertTrue(window.isVisible())
        closed: list[bool] = []
        window.closed.connect(lambda: closed.append(True))
        window.close_button.click()
        self.assertFalse(window.isVisible())
        self.assertEqual(closed, [True])

    def test_escape_closes_the_chat_window(self) -> None:
        """焦点在输入框里时，Esc 也要能关掉聊天窗。"""
        window = PrivateChatPanel("u2", "好友", "lan")
        window.show()
        window.input.setFocus()
        QTest.keyClick(window.input, Qt.Key.Key_Escape)
        self.assertFalse(window.isVisible())

    def test_closed_chat_window_can_be_reopened(self) -> None:
        """关掉之后重新点“聊天”，窗口要能再回来。"""
        panel = OnlinePanel()
        panel._open_private_chat("u2", "好友")
        window = panel._chat_panels["u2"]
        # 宿主联机页没有显示，isVisible() 恒为假，只能看显式隐藏标记。
        self.assertFalse(window.isHidden())
        window.close()
        self.assertTrue(window.isHidden())
        panel._open_private_chat("u2", "好友")
        self.assertFalse(window.isHidden())
        self.assertIs(panel._chat_panels["u2"], window)
        panel.close()


class OnlinePanelSocialTests(unittest.TestCase):
    def test_leaderboard_avatar_and_friends_badge(self) -> None:
        window = OnlinePanel()
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

        window._show_profile_panel(profile_dict())
        self.assertIsNotNone(window._profile_panel)
        self.assertEqual(
            window._profile_panel.name_label.text(), "好友"
        )
        window.close()


class FakeSocialClient:
    """社交 REST 客户端的替身：只记账，不发网络请求。"""

    def __init__(self) -> None:
        self.read_calls: list[tuple[str, list[str]]] = []
        self.sent: list[tuple[str, str]] = []

    def mark_messages_read(
        self, access_token: str, user_id: str, message_ids
    ) -> bool:
        self.read_calls.append((user_id, list(message_ids)))
        return True

    def fetch_messages(self, *args, **kwargs) -> bool:
        return True

    def fetch_friends(self, *args, **kwargs) -> bool:
        return True

    def send_message(self, access_token: str, user_id: str, content: str):
        self.sent.append((user_id, content))
        return True


class UnreadBadgeTests(unittest.TestCase):
    """消息读完以后，好友列表上的未读标记必须跟着消失。"""

    def _session(self) -> AuthSession:
        return AuthSession(
            access_token="token",
            refresh_token="refresh",
            user=AuthUser(id="me", username="我"),
            expires_in=3600,
        )

    def _panel(self) -> OnlinePanel:
        """造一个不发网络请求的联机面板。

        登录态一旦挂上，真实的社交客户端会朝本机 8000 端口发请求；
        请求还在飞的时候测试结束、面板被回收，回调就会打到已经销毁的
        对象上，Qt 直接 abort。所以这里把客户端换成只记账的替身。
        """
        panel = OnlinePanel()
        panel._auth_session = self._session()
        panel._social_client = FakeSocialClient()
        return panel

    def _friends_data(self, unread: int = 3) -> dict:
        return {
            "friends": [
                {
                    "user_id": "u2",
                    "username": "好友",
                    "avatar": "lan",
                    "online": True,
                    "unread": unread,
                }
            ],
            "incoming": [],
            "outgoing": [],
        }

    def _incoming(self, message_id: str = "m9") -> DirectMessage:
        return DirectMessage(
            id=message_id,
            sender_id="u2",
            sender_name="好友",
            sender_avatar="lan",
            recipient_id="me",
            content="在吗",
            sent_at_ms=2,
        )

    def test_reading_history_clears_the_unread_count(self) -> None:
        """读完历史，服务端已标为已读，本地未读数要归零。"""
        panel = self._panel()
        panel._friends_data = self._friends_data()
        panel._open_private_chat("u2", "好友")
        window = panel._chat_panels["u2"]

        panel._on_social_ready(
            "messages:u2",
            {
                "messages": [
                    {
                        "id": "m1",
                        "sender_id": "u2",
                        "sender_name": "好友",
                        "content": "你好",
                        "sent_at_ms": 1,
                    }
                ]
            },
        )
        self.assertEqual(
            panel._friends_data["friends"][0]["unread"], 0
        )
        # 历史接口已经销账，不用再报一次。
        self.assertEqual(window.pending_read_ids(), [])
        panel.close()

    def test_friends_panel_badge_disappears_after_reading(self) -> None:
        """好友列表里的“N 条未读”标记要在读完消息后消失。"""
        panel = self._panel()
        friends_panel = FriendsPanel(panel)
        panel._friends_panel = friends_panel
        panel._friends_data = self._friends_data()
        friends_panel.set_data(panel._friends_data)

        def badge_texts() -> list[str]:
            return [
                label.text()
                for label in friends_panel.findChildren(QLabel)
            ]

        self.assertIn("3 条未读", badge_texts())
        panel._open_private_chat("u2", "好友")
        panel._on_social_ready("messages:u2", {"messages": []})
        self.assertNotIn("3 条未读", badge_texts())
        panel.close()

    def test_live_message_in_open_window_is_marked_read(self) -> None:
        """聊天窗开着时来的消息，当场就读完了，未读数要销账。"""
        panel = self._panel()
        panel._friends_data = self._friends_data()
        panel.show()
        panel._open_private_chat("u2", "好友")
        window = panel._chat_panels["u2"]
        window.show()
        QApplication.instance().processEvents()
        self.assertTrue(window.isVisible())

        panel._on_direct_message(self._incoming())
        self.assertEqual(
            panel._social_client.read_calls, [("u2", ["m9"])]
        )
        self.assertEqual(window.pending_read_ids(), [])
        self.assertEqual(
            panel._friends_data["friends"][0]["unread"], 0
        )
        panel.close()

    def test_live_message_in_closed_window_stays_unread(self) -> None:
        """聊天窗被关着时来的消息，仍然算未读，不能悄悄销账。"""
        panel = self._panel()
        panel._friends_data = self._friends_data()
        panel.show()
        panel._open_private_chat("u2", "好友")
        window = panel._chat_panels["u2"]
        window.show()
        window.close()

        panel._on_direct_message(self._incoming())
        self.assertEqual(panel._social_client.read_calls, [])
        self.assertEqual(window.pending_read_ids(), ["m9"])
        panel.close()


class SocialRegressionTests(unittest.TestCase):
    """社交界面上几个真实踩过的坑。"""
    def test_history_merge_keeps_live_messages(self) -> None:
        """历史与实时推送乱序到达时，消息既不能被抹掉也不能重复。"""
        window = PrivateChatPanel("u2", "好友", "lan")
        # 实时推送先到。
        window.append_message(
            {
                "id": "m2",
                "sender_id": "u2",
                "sender_name": "好友",
                "content": "刚到的消息",
                "sent_at_ms": 2,
            },
            "me",
        )
        # 历史随后才回来，里面也有同一条。
        window.set_history(
            [
                {
                    "id": "m1",
                    "sender_id": "me",
                    "sender_name": "我",
                    "content": "旧消息",
                    "sent_at_ms": 1,
                },
                {
                    "id": "m2",
                    "sender_id": "u2",
                    "sender_name": "好友",
                    "content": "刚到的消息",
                    "sent_at_ms": 2,
                },
            ],
            "me",
        )
        text = window.messages_view.toPlainText()
        self.assertIn("刚到的消息", text)
        self.assertIn("旧消息", text)
        self.assertEqual(text.count("刚到的消息"), 1)
        window.close()

    def test_failed_send_restores_the_draft(self) -> None:
        """发送失败要把文本放回输入框。"""
        window = PrivateChatPanel("u2", "好友", "lan")
        sent: list[str] = []
        window.send_requested.connect(sent.append)
        window.input.setText("在吗")
        window._on_send()
        self.assertEqual(window.input.text(), "")
        self.assertEqual(window.last_draft, "在吗")

        window.restore_draft()
        self.assertEqual(window.input.text(), "在吗")
        self.assertEqual(window.last_draft, "")
        window.close()

    def test_successful_send_clears_the_draft(self) -> None:
        window = PrivateChatPanel("u2", "好友", "lan")
        window.input.setText("在吗")
        window._on_send()
        window.clear_draft()
        self.assertEqual(window.last_draft, "")
        window.close()

    def test_stale_profile_response_is_ignored(self) -> None:
        """过期的资料响应不能把当前显示的用户换掉。"""
        panel = OnlinePanel()
        panel._pending_profile_user_id = "u2"
        panel._on_social_ready(
            "user:u1",
            {
                "user": {"id": "u1", "username": "别人", "avatar": "mei"},
                "friendship": "none",
                "online": True,
            },
        )
        self.assertIsNone(panel._profile_panel)

        panel._on_social_ready(
            "user:u2",
            {
                "user": {"id": "u2", "username": "好友", "avatar": "lan"},
                "friendship": "none",
                "online": True,
            },
        )
        self.assertIsNotNone(panel._profile_panel)
        self.assertEqual(panel._profile_panel.user_id, "u2")
        panel.close()

    def test_friends_panel_switches_invite_mode(self) -> None:
        """复用同一个好友浮层时，标题要跟着“邀请对局”切换。"""
        panel = FriendsPanel()
        self.assertEqual(panel._title_label.text(), "我的好友")
        panel.set_invite_mode(True)
        self.assertEqual(panel._title_label.text(), "邀请好友对局")
        panel.set_invite_mode(False)
        self.assertEqual(panel._title_label.text(), "我的好友")
        panel.close()

    def test_accept_button_is_locked_after_click(self) -> None:
        """点过“接受”之后这一行不能再点，避免重复提交。"""
        panel = FriendsPanel()
        accepted: list[str] = []
        panel.accept_requested.connect(accepted.append)
        panel.set_data(
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
            }
        )
        buttons = [
            b
            for b in panel.findChildren(QPushButton)
            if b.text() == "接受"
        ]
        self.assertEqual(len(buttons), 1)
        buttons[0].click()
        self.assertEqual(accepted, ["r1"])
        self.assertFalse(buttons[0].isEnabled())
        panel.close()


class SocialPanelIsInWindowTests(unittest.TestCase):
    """社交界面必须是主窗口内的浮层，而不是第二个原生窗口。

    应用全局只保留一个原生窗口（见 ``gomoku.client.modal_overlay``）：
    好友、资料、私聊和头像选择都挂在联机面板上，任务栏始终只有一个条目。
    """

    def _panels(self, host: QWidget) -> list:
        return [
            FriendsPanel(host),
            ProfilePanel(host),
            PrivateChatPanel("u2", "好友", "lan", host),
            AvatarPickerPanel("mei", host),
        ]

    def test_panels_are_child_widgets_not_windows(self) -> None:
        host = QWidget()
        host.resize(1000, 700)
        for panel in self._panels(host):
            panel.show()
            self.assertFalse(
                panel.isWindow(),
                f"{type(panel).__name__} 不应该成为原生窗口",
            )
            self.assertIs(panel.parentWidget(), host)

    def test_panels_stay_inside_the_host(self) -> None:
        host = QWidget()
        host.resize(900, 600)
        for panel in self._panels(host):
            panel.show()
            self.assertTrue(host.rect().contains(panel.geometry()))

    def test_modal_picker_reports_modal_and_result(self) -> None:
        host = QWidget()
        host.resize(900, 600)
        picker = AvatarPickerPanel("mei", host)
        self.assertTrue(picker.isModal())
        self.assertEqual(picker.result(), 0)
        picker.reject()
        self.assertEqual(picker.result(), 0)

    def test_closing_a_panel_emits_closed(self) -> None:
        host = QWidget()
        host.resize(900, 600)
        panel = FriendsPanel(host)
        seen: list[bool] = []
        panel.closed.connect(lambda: seen.append(True))
        panel.show()
        panel.close()
        self.assertEqual(seen, [True])
        self.assertFalse(panel.isVisible())


if __name__ == "__main__":
    unittest.main(verbosity=2)
