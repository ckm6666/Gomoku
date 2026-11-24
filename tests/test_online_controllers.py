"""联机面板控制器拆分的聚焦测试。

覆盖三件事：

* ``OnlinePanel`` 保留门面方法，并委托给对应的领域控制器；
* 登录流程走认证控制器，且模块级凭据函数的替换依然生效；
* 对局页的时钟/错误提示等行为由对局控制器驱动，模块级常量替换可见。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

import gomoku.client.online_panel as online_panel_module  # noqa: E402
from gomoku.client.config import AppConfig  # noqa: E402
from gomoku.client.online_controllers.auth_flow import (  # noqa: E402
    AuthFlowController,
)
from gomoku.client.online_controllers.connection import (  # noqa: E402
    ConnectionController,
)
from gomoku.client.online_controllers.game import (  # noqa: E402
    GameController,
)
from gomoku.client.online_controllers.game_actions import (  # noqa: E402
    GameActionsController,
)
from gomoku.client.online_controllers.game_chat import (  # noqa: E402
    GameChatController,
)
from gomoku.client.online_controllers.game_clock import (  # noqa: E402
    GameClockController,
)
from gomoku.client.online_controllers.game_notices import (  # noqa: E402
    GameNoticesController,
)
from gomoku.client.online_controllers.game_sandbox import (  # noqa: E402
    GameSandboxController,
)
from gomoku.client.online_controllers.game_sync import (  # noqa: E402
    GameSyncController,
)
from gomoku.client.online_controllers.match import (  # noqa: E402
    MatchController,
)
from gomoku.client.online_controllers.profile import (  # noqa: E402
    ProfileController,
)
from gomoku.client.online_controllers.social import (  # noqa: E402
    SocialController,
)
from gomoku.client.online_panel import OnlinePanel  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])

CONFIG = AppConfig(
    environment="production",
    api_base_url="https://api.example.test",
    ws_base_url="wss://api.example.test",
)


class ControllerCompositionTests(unittest.TestCase):
    """面板在控件就绪后组装六个领域控制器。"""

    def test_panel_owns_all_domain_controllers(self) -> None:
        panel = OnlinePanel()
        try:
            expected = {
                "_auth_flow": AuthFlowController,
                "_connection_ctrl": ConnectionController,
                "_profile_ctrl": ProfileController,
                "_social_ctrl": SocialController,
                "_match_ctrl": MatchController,
                "_game_ctrl": GameController,
            }
            for attr, controller_type in expected.items():
                controller = getattr(panel, attr)
                self.assertIsInstance(controller, controller_type)
                # 控制器通过弱引用持有面板，不形成引用环。
                self.assertIs(controller.panel, panel)
        finally:
            panel.close()

    def test_module_level_exports_are_kept(self) -> None:
        self.assertTrue(callable(online_panel_module.online_victory_notice))
        self.assertEqual(
            online_panel_module.SETTINGS_CHAT_LOG_VISIBLE,
            "chat_log_visible",
        )


class FacadeDelegationTests(unittest.TestCase):
    """门面方法只转发给控制器，自身不再承载领域逻辑。"""

    def setUp(self) -> None:
        self.panel = OnlinePanel(config=CONFIG)
        self.addCleanup(self.panel.shutdown)

    def test_auth_actions_delegate_to_auth_controller(self) -> None:
        calls: list[str] = []
        self.panel._auth_flow.on_login_clicked = (
            lambda: calls.append("login")
        )
        self.panel._auth_flow.logout = lambda: calls.append("logout")
        self.panel._on_login_clicked()
        self.panel._logout()
        self.assertEqual(calls, ["login", "logout"])

    def test_game_actions_delegate_to_game_controller(self) -> None:
        calls: list[str] = []
        self.panel._game_ctrl.update_clock_label = (
            lambda: calls.append("clock")
        )
        self.panel._game_ctrl.sync_game_page = (
            lambda: calls.append("sync")
        )
        self.panel._update_clock_label()
        self.panel._sync_game_page()
        self.assertEqual(calls, ["clock", "sync"])

    def test_session_actions_delegate_to_their_controllers(self) -> None:
        calls: list[str] = []
        self.panel._profile_ctrl.on_live_ready = (
            lambda data: calls.append("live")
        )
        self.panel._social_ctrl.on_social_ready = (
            lambda kind, data: calls.append("social")
        )
        self.panel._match_ctrl.on_room_created = (
            lambda code: calls.append("room")
        )
        self.panel._on_live_ready({"entries": []})
        self.panel._on_social_ready("friends", {})
        self.panel._on_room_created("ABCD1234")
        self.assertEqual(calls, ["live", "social", "room"])


class AuthFlowControllerTests(unittest.TestCase):
    """登录流程与凭据处理由认证控制器承担。"""

    def setUp(self) -> None:
        self.panel = OnlinePanel(config=CONFIG)
        self.addCleanup(self.panel.shutdown)
        self.panel.stack.setCurrentWidget(self.panel.auth_page)

    def test_login_uses_auth_client_and_locks_the_form(self) -> None:
        self.panel.username_edit.setText("alice")
        self.panel.password_edit.setText("s3cret-pass")
        login = mock.Mock(return_value=True)
        self.panel._auth_client.login = login

        self.panel._on_login_clicked()

        login.assert_called_once_with("alice", "s3cret-pass")
        self.assertEqual(
            self.panel.auth_status_label.text(), "正在登录…"
        )
        self.assertFalse(self.panel.username_edit.isEnabled())
        self.assertFalse(self.panel.login_button.isEnabled())

    def test_login_requires_username_and_password(self) -> None:
        self.panel.username_edit.setText("")
        self.panel.password_edit.setText("")
        login = mock.Mock(return_value=True)
        self.panel._auth_client.login = login

        self.panel._on_login_clicked()

        login.assert_not_called()
        self.assertIn("请输入用户名和密码", self.panel.auth_status_label.text())

    def test_saved_login_uses_module_patch_target(self) -> None:
        self.panel.remember_password_checkbox.setChecked(True)
        with mock.patch.object(
            online_panel_module, "save_login", return_value=False
        ):
            warning = self.panel._save_saved_login("alice")
        self.assertTrue(warning)
        self.assertFalse(self.panel.remember_password_checkbox.isChecked())


class GameSubControllerDelegationTests(unittest.TestCase):
    """对局门面把调用转发给六个聚焦的子控制器。

    每个模块至少有一条用例：替换子控制器上的方法后，面板门面方法
    必须原样转发到新的实现，证明拆分没有改变调用入口。
    """

    def setUp(self) -> None:
        self.panel = OnlinePanel()
        self.addCleanup(self.panel.close)
        self.game = self.panel._game_ctrl

    def test_facade_owns_focused_sub_controllers(self) -> None:
        expected = {
            "clock": GameClockController,
            "sync": GameSyncController,
            "chat": GameChatController,
            "sandbox": GameSandboxController,
            "actions": GameActionsController,
            "notices": GameNoticesController,
        }
        for attr, controller_type in expected.items():
            controller = getattr(self.game, attr)
            self.assertIsInstance(controller, controller_type)
            # 子控制器同样只弱引用面板，不形成引用环。
            self.assertIs(controller.panel, self.panel)

    def test_clock_delegation(self) -> None:
        calls: list[str] = []
        self.game.clock.update_clock_label = (
            lambda: calls.append("clock")
        )
        self.panel._update_clock_label()
        self.assertEqual(calls, ["clock"])

    def test_sync_delegation(self) -> None:
        calls: list[str] = []
        self.game.sync.sync_game_page = lambda: calls.append("sync")
        self.panel._sync_game_page()
        self.assertEqual(calls, ["sync"])

    def test_chat_delegation(self) -> None:
        calls: list[str] = []
        self.game.chat.refresh_chat_controls = (
            lambda: calls.append("chat")
        )
        self.panel._refresh_chat_controls()
        self.assertEqual(calls, ["chat"])

    def test_sandbox_delegation(self) -> None:
        opened: list[str] = []

        def _open() -> bool:
            opened.append("sandbox")
            return True

        self.game.sandbox.open_sandbox = _open
        self.assertTrue(self.panel._open_sandbox())
        self.assertEqual(opened, ["sandbox"])

    def test_actions_delegation(self) -> None:
        calls: list[str] = []
        self.game.actions.on_undo_clicked = (
            lambda: calls.append("undo")
        )
        self.panel._on_undo_clicked()
        self.assertEqual(calls, ["undo"])

    def test_notices_delegation(self) -> None:
        calls: list[str] = []
        self.game.notices.play_online_result_sound = (
            lambda: calls.append("sound")
        )
        self.panel._play_online_result_sound()
        self.assertEqual(calls, ["sound"])


class GameControllerTests(unittest.TestCase):
    """对局页行为由对局控制器驱动。"""

    def setUp(self) -> None:
        self.panel = OnlinePanel()
        # 测试替身没有完整会话接口，关闭面板即可（不触发 shutdown 的
        # 会话拆除路径），与既有 UI 测试的做法一致。
        self.addCleanup(self.panel.close)

    def test_board_click_only_forwards_when_input_is_accepted(self) -> None:
        class _Session:
            accepts_human_input = True
            is_authenticated = False
            in_room = False
            queued = False
            room_code = None

            def __init__(self) -> None:
                self.moves: list[tuple[int, int]] = []

            def request_move(self, row: int, col: int) -> bool:
                self.moves.append((row, col))
                return True

        session = _Session()
        self.panel._session = session  # type: ignore[assignment]
        self.panel._on_board_clicked(7, 8)
        self.assertEqual(session.moves, [(7, 8)])

        session.accepts_human_input = False
        self.panel._on_board_clicked(8, 8)
        self.assertEqual(session.moves, [(7, 8)])

    def test_game_error_hold_reads_module_constant(self) -> None:
        with mock.patch.object(
            online_panel_module, "GAME_ERROR_HOLD_SECONDS", 0.2
        ):
            self.panel._set_game_error("该位置已有棋子")
        self.assertEqual(
            self.panel.game_error_label.text(), "该位置已有棋子"
        )
        self.assertEqual(self.panel._game_error_timer.interval(), 200)
        self.assertTrue(self.panel._game_error_timer.isActive())

        self.panel._clear_game_error()
        self.assertEqual(self.panel.game_error_label.text(), "")
        self.assertFalse(self.panel._game_error_timer.isActive())


if __name__ == "__main__":
    unittest.main(verbosity=2)
