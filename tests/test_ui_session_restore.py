"""启动时用保存的 refresh token 静默恢复登录的离屏 UI 测试。"""

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

from gomoku.client.auth import AuthSession, AuthUser  # noqa: E402
from gomoku.client.config import AppConfig  # noqa: E402
from gomoku.client.online_panel import OnlinePanel  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])

CONFIG = AppConfig(
    environment="production",
    api_base_url="https://api.example.test",
    ws_base_url="wss://api.example.test",
)


def make_session(refresh_token: str = "new-refresh") -> AuthSession:
    return AuthSession(
        access_token="access",
        refresh_token=refresh_token,
        user=AuthUser(id="u1", username="alice"),
        expires_in=900,
    )


class RestoreSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.panel = OnlinePanel(config=CONFIG)
        self.patch_attr(self.panel._auth_client, "refresh", return_value=True)
        self.patch_attr(self.panel, "_start_ws_session")
        self.patch_attr(self.panel, "_save_saved_login", return_value="")
        self.panel.remember_password_checkbox.setChecked(True)

    def tearDown(self) -> None:
        self.panel.deleteLater()

    def patch_attr(self, target: object, name: str, **kwargs) -> mock.Mock:
        patcher = mock.patch.object(target, name, **kwargs)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def test_restore_uses_saved_token_and_saves_rotated_token(self) -> None:
        with (
            mock.patch(
                "gomoku.client.online_panel.load_refresh_token",
                return_value="saved-refresh",
            ),
            mock.patch(
                "gomoku.client.online_panel.save_refresh_token",
                return_value=True,
            ) as save,
        ):
            self.panel._try_restore_session()
            self.assertTrue(self.panel._restoring_session)
            self.panel._auth_client.refresh.assert_called_once_with(
                "saved-refresh"
            )

            self.panel._on_auth_session_refreshed(make_session("rotated"))

        self.assertFalse(self.panel._restoring_session)
        assert self.panel._auth_session is not None
        self.assertEqual(
            self.panel._auth_session.refresh_token, "rotated"
        )
        save.assert_called_once_with(self.panel._settings, "rotated")
        self.panel._start_ws_session.assert_called_once()

    def test_rejected_token_is_cleared(self) -> None:
        with (
            mock.patch(
                "gomoku.client.online_panel.load_refresh_token",
                return_value="saved-refresh",
            ),
            mock.patch(
                "gomoku.client.online_panel.clear_refresh_token"
            ) as clear,
        ):
            self.panel._try_restore_session()
            self.panel._auth_client._last_error_status = 401
            self.panel._on_http_failed("刷新令牌无效")

        self.assertFalse(self.panel._restoring_session)
        clear.assert_called_once_with(self.panel._settings)

    def test_network_failure_keeps_token(self) -> None:
        with (
            mock.patch(
                "gomoku.client.online_panel.load_refresh_token",
                return_value="saved-refresh",
            ),
            mock.patch(
                "gomoku.client.online_panel.clear_refresh_token"
            ) as clear,
        ):
            self.panel._try_restore_session()
            self.panel._auth_client._last_error_status = None
            self.panel._on_http_failed("网络请求失败")

        clear.assert_not_called()

    def test_restore_requires_remember_checked(self) -> None:
        self.panel.remember_password_checkbox.setChecked(False)
        with mock.patch(
            "gomoku.client.online_panel.load_refresh_token",
            return_value="saved-refresh",
        ) as load:
            self.panel._try_restore_session()
        load.assert_not_called()
        self.panel._auth_client.refresh.assert_not_called()

    def test_logout_clears_saved_token(self) -> None:
        self.patch_attr(self.panel._auth_client, "logout", return_value=True)
        with mock.patch(
            "gomoku.client.online_panel.clear_refresh_token"
        ) as clear:
            self.panel._logout()
        clear.assert_called_once_with(self.panel._settings)
