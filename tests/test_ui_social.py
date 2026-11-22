"""个人信息、好友、私聊与排行榜头像的离屏 UI 测试。"""

from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QFrame,
    QPushButton,
    QWidget,
)

from gomoku.client.achievement_widgets import (  # noqa: E402
    AchievementPanel,
    achievement_pixmap,
)
from gomoku.client.avatar_images import (  # noqa: E402
    prepare_avatar_image,
)
from gomoku.client.auth import AuthSession, AuthUser  # noqa: E402
from gomoku.client.friends_panel import FriendsPanel  # noqa: E402
from gomoku.client.online_panel import (  # noqa: E402
    OnlinePanel,
    OnlinePlayerCard,
)
from gomoku.client.private_chat_panel import PrivateChatPanel  # noqa: E402
from gomoku.shared.rules import Player  # noqa: E402
from gomoku.client.profile_panel import (  # noqa: E402
    AvatarPickerPanel,
    ProfilePanel,
)
from gomoku.shared.achievements import (  # noqa: E402
    ACHIEVEMENTS,
    ACHIEVEMENT_TOTAL,
    stats_from_mapping,
    unlocked_achievement_keys,
)
from gomoku.shared.avatar import AVATARS  # noqa: E402
from gomoku.shared.protocol import GameSnapshot  # noqa: E402

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

    def test_avatar_picker_and_upload_button(self) -> None:
        picker = AvatarPickerPanel("mei")
        buttons = [
            button
            for button in picker.findChildren(QPushButton)
            if button.objectName() == "avatarChoice"
        ]
        self.assertEqual(len(buttons), len(AVATARS))
        texts = {
            button.text()
            for button in picker.findChildren(QPushButton)
        }
        self.assertIn("上传本地图片", texts)
        selected: list[str] = []
        picker.avatar_selected.connect(selected.append)
        buttons[1].click()
        self.assertEqual(selected, [AVATARS[1].key])

    def test_profile_achievement_section(self) -> None:
        window = ProfilePanel()
        profile = profile_dict()
        window.set_profile(profile)
        stats = stats_from_mapping(profile["user"])
        expected = unlocked_achievement_keys(stats)
        self.assertEqual(
            window.achievement_count_label.text(),
            f"{len(expected)} / {ACHIEVEMENT_TOTAL}",
        )
        medal_widgets = [
            label
            for label in window.medal_container.findChildren(QWidget)
            if label.pixmap() is not None
            and not label.pixmap().isNull()
        ]
        self.assertEqual(len(medal_widgets), min(len(expected), 6))

        requested: list[bool] = []
        window.achievements_requested.connect(
            lambda: requested.append(True)
        )
        buttons = {
            button.text(): button
            for button in window.findChildren(QPushButton)
        }
        buttons["全部成就"].click()
        self.assertEqual(requested, [True])
        window.close()

    def test_achievement_wall_and_badge(self) -> None:
        user = dict(profile_dict()["user"])
        user["achievements"] = ["first_win", "rank_gold"]
        panel = AchievementPanel(user)
        tiles = [
            tile
            for tile in panel.findChildren(QFrame)
            if tile.objectName() == "achievementTile"
        ]
        self.assertEqual(len(tiles), ACHIEVEMENT_TOTAL)
        unlocked = [
            tile for tile in tiles if tile.property("unlocked") == "true"
        ]
        self.assertEqual(len(unlocked), 2)
        panel.close()

        spec = next(
            item for item in ACHIEVEMENTS if item.key == "rank_diamond"
        )
        self.assertFalse(achievement_pixmap(spec).isNull())
        self.assertFalse(
            achievement_pixmap(spec, unlocked=False).isNull()
        )


class AvatarImageTests(unittest.TestCase):
    def test_prepare_avatar_image_crops_and_encodes(self) -> None:
        image = QImage(320, 200, QImage.Format.Format_RGB32)
        image.fill(QColor("#336699"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "avatar.png"
            self.assertTrue(image.save(str(path), "PNG"))
            data_url = prepare_avatar_image(str(path))

        self.assertTrue(data_url.startswith("data:image/jpeg;base64,"))
        payload = base64.b64decode(data_url.split(",", 1)[1])
        self.assertGreater(len(payload), 0)
        self.assertLessEqual(len(payload), 256 * 1024)
        decoded = QImage.fromData(payload)
        self.assertFalse(decoded.isNull())
        self.assertEqual(decoded.width(), 192)
        self.assertEqual(decoded.height(), 192)

    def test_prepare_avatar_image_rejects_missing_file(self) -> None:
        from gomoku.client.avatar_images import AvatarImageError

        with self.assertRaises(AvatarImageError):
            prepare_avatar_image("definitely-missing-avatar.png")


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

    def test_match_medals_and_avatar_version_update(self) -> None:
        card = OnlinePlayerCard()
        card.set_content(
            "棋手",
            Player.BLACK,
            True,
            "落子中",
            1500,
            "mei",
            "u1",
            0,
            (
                "first_win",
                "rank_diamond",
                "rank_gold",
                "wins_10",
            ),
        )
        self.assertEqual(
            card._achievement_keys,
            ("rank_diamond", "rank_gold", "wins_10"),
        )

        window = OnlinePanel()
        window._auth_session = AuthSession(
            access_token="access",
            refresh_token="refresh",
            user=AuthUser(id="u1", username="自己"),
            expires_in=900,
        )
        window._apply_avatar_update(
            {"avatar": "mei", "avatar_version": 4, "achievements": []}
        )
        self.assertEqual(
            window._auth_session.user.avatar_version, 4
        )
        self.assertEqual(window._auth_session.user.avatar, "mei")

        snapshot = GameSnapshot(
            game_id="g1",
            board=tuple(
                tuple(None for _ in range(15)) for _ in range(15)
            ),
            current_player=Player.BLACK,
            last_move=None,
            winner=Player.BLACK,
            winning_line=(),
            is_draw=False,
            is_game_over=True,
            move_no=9,
            your_color=Player.BLACK,
            your_new_achievements=("first_win", "streak_3"),
        )
        window._update_achievement_notice(snapshot)
        self.assertFalse(window.game_achievement_label.isHidden())
        self.assertIn("初捷", window.game_achievement_label.text())
        self.assertIn("三连捷", window.game_achievement_label.text())
        window.close()


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
