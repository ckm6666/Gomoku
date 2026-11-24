"""登录/注册与凭据恢复流程控制器。

把 :class:`~gomoku.client.online_panel.OnlinePanel` 里的认证流程集中到这里：
登录、注册、启动时恢复 refresh token、登出以及认证请求的失败/忙碌处理。
"""

from __future__ import annotations

import logging
from gomoku.client.auth import AuthSession
from gomoku.client.online_controllers.common import (
    PanelController,
    host_module,
)

logger = logging.getLogger(__name__)


class AuthFlowController(PanelController):
    """联机面板的登录/注册与凭据流程。"""

    def on_login_clicked(self) -> None:
        if self.panel._auth_client.busy or not self.panel._api_base_url:
            return
        username = self.panel.username_edit.text().strip()
        password = self.panel.password_edit.text()
        if not username or not password:
            self.panel._set_auth_status("请输入用户名和密码", error=True)
            return
        self.panel._set_auth_status("正在登录…")
        self.panel._set_auth_form_busy(True)
        if not self.panel._auth_client.login(username, password):
            self.panel._set_auth_status("无法发起登录请求", error=True)
            self.panel._set_auth_form_busy(False)

    def on_register_clicked(self) -> None:
        if self.panel._auth_client.busy or not self.panel._api_base_url:
            return
        username = self.panel.username_edit.text().strip()
        password = self.panel.password_edit.text()
        if not username or not password:
            self.panel._set_auth_status("注册需要用户名和密码", error=True)
            return
        self.panel._set_auth_status("正在注册…")
        self.panel._set_auth_form_busy(True)
        if not self.panel._auth_client.register(username, password):
            self.panel._set_auth_status("无法发起注册请求", error=True)
            self.panel._set_auth_form_busy(False)

    def load_saved_login(self) -> None:
        """启动时恢复“记住密码”保存的账号与密码。"""
        remember, username, password = host_module().load_login(self.panel._settings)
        if username:
            self.panel.username_edit.setText(username)
        if password:
            self.panel.password_edit.setText(password)
        self.panel.remember_password_checkbox.setChecked(remember)

    def try_restore_session(self) -> None:
        """启动时用已保存的 refresh token 静默恢复登录。"""
        if (
            self.panel._closing
            or self.panel._restoring_session
            or self.panel._auth_session is not None
            or not self.panel.config.online_configured
            or not self.panel.remember_password_checkbox.isChecked()
        ):
            return
        token = host_module().load_refresh_token(self.panel._settings)
        if not token:
            return
        self.panel._restoring_session = True
        self.panel._set_auth_status("正在恢复登录…")
        self.panel._set_auth_form_busy(True)
        if not self.panel._auth_client.refresh(token):
            if self.panel._restoring_session:
                self.panel._restoring_session = False
                self.panel._set_auth_form_busy(False)
                self.panel._set_auth_status("")

    def save_refresh_token(self, token: str) -> None:
        """按“记住密码”状态保存或清除 refresh token。"""
        if not self.panel.remember_password_checkbox.isChecked():
            host_module().clear_refresh_token(self.panel._settings)
            return
        if not host_module().save_refresh_token(self.panel._settings, token):
            logger.warning(
                "refresh token 无法通过 DPAPI 安全保存，"
                "下次启动需要重新登录"
            )

    def restore_saved_login(self) -> None:
        """回到登录页时重新填入已保存的密码。

        登录成功后密码框会被清空；不重新填入的话，用户退出登录或被强制
        重新登录后还得再输一遍密码，尽管它已经安全地存在本机。
        """
        remember, _username, password = host_module().load_login(self.panel._settings)
        if not remember or not password:
            return
        if not self.panel.password_edit.text():
            self.panel.password_edit.setText(password)

    def save_saved_login(self, username: str) -> str:
        """按复选框状态保存或清除登录凭据。

        返回一条给用户看的提示（没有则返回空串）：密码没能安全加密时
        不能让界面继续显示“已记住”，否则用户会以为密码已经存好。
        """
        if not self.panel.remember_password_checkbox.isChecked():
            host_module().clear_login(self.panel._settings)
            return ""
        if not username:
            return ""
        if not host_module().save_login(
            self.panel._settings, username, self.panel.password_edit.text()
        ):
            logger.warning(
                "登录密码无法通过 DPAPI 安全保存，本次仅记住用户名"
            )
            # 复选框必须跟着真实状态走：密码没存下来就不能继续勾着。
            # 这里只改勾选状态，不触发 toggled → 不会顺手删掉刚记住
            # 的用户名。
            previous = self.panel.remember_password_checkbox.blockSignals(True)
            self.panel.remember_password_checkbox.setChecked(False)
            self.panel.remember_password_checkbox.blockSignals(previous)
            return "当前环境无法安全保存密码，本次仅记住用户名"
        return ""

    def on_remember_password_toggled(self, checked: bool) -> None:
        """取消勾选时立即删除已保存的凭据。"""
        if not checked:
            host_module().clear_login(self.panel._settings)

    def set_auth_form_busy(self, busy: bool) -> None:
        enabled = not busy and self.panel.stack.currentWidget() is self.panel.auth_page
        for widget in (
            self.panel.username_edit,
            self.panel.password_edit,
            self.panel.remember_password_checkbox,
            self.panel.login_button,
            self.panel.register_button,
        ):
            widget.setEnabled(enabled)

    def on_auth_busy_changed(self, busy: bool) -> None:
        if busy:
            self.panel._set_auth_form_busy(True)
        elif self.panel.stack.currentWidget() is self.panel.auth_page:
            self.panel._set_auth_form_busy(False)

    def on_auth_session_ready(
        self, session: AuthSession, *, restored: bool = False
    ) -> None:
        self.panel._auth_session = session
        self.panel._logging_out = False
        self.panel._reconnect_attempts = 0
        # 密码编辑框马上就要清空；先按用户的选择保存凭据。
        warning = self.panel._save_saved_login(session.user.username)
        self.panel._save_refresh_token(session.refresh_token)
        self.panel.password_edit.clear()
        self.panel._clear_profile_ui()
        status = (
            "已恢复登录，正在连接游戏服务器…"
            if restored
            else "登录成功，正在连接游戏服务器…"
        )
        if warning:
            status = f"{status}（{warning}）"
        self.panel._set_auth_status(status)
        self.panel._set_auth_form_busy(True)
        self.panel._start_ws_session()

    def on_auth_session_refreshed(self, session: AuthSession) -> None:
        self.panel._auth_session = session
        if self.panel._restoring_session:
            self.panel._restoring_session = False
            # _on_auth_session_ready 会保存凭据与轮换后的 token。
            self.panel._on_auth_session_ready(session, restored=True)
            return
        # 服务端每次刷新都会轮换 refresh token，必须同步保存新值。
        self.panel._save_refresh_token(session.refresh_token)
        if self.panel._reconnect_after_refresh:
            self.panel._reconnect_after_refresh = False
            self.panel._open_transport(reset_status=False)
        if self.panel._refresh_after_auth_refresh:
            self.panel._refresh_after_auth_refresh = False
            self.panel._refresh_profile_data()
        self.panel._update_rating_summary()

    def on_http_failed(self, message: str) -> None:
        if self.panel._restoring_session:
            self.panel._restoring_session = False
            status = self.panel._auth_client.last_error_status
            if status in (401, 403):
                host_module().clear_refresh_token(self.panel._settings)
                self.panel._set_auth_status(
                    "自动登录已失效，请重新登录", error=True
                )
            else:
                # 网络暂时不可用时保留 token，下次启动还能再试。
                self.panel._set_auth_status(
                    "无法自动恢复登录，请手动登录", error=True
                )
            self.panel._set_auth_form_busy(False)
            return
        if self.panel._reconnect_after_refresh:
            self.panel._reconnect_after_refresh = False
            self.panel._force_relogin(message)
            return
        if self.panel._refresh_after_auth_refresh:
            self.panel._refresh_after_auth_refresh = False
            self.panel._force_relogin(message)
            return
        if self.panel._logging_out:
            self.panel._logging_out = False
            self.panel._show_auth_page()
            self.panel._set_auth_status(message, error=True)
            self.panel._set_auth_form_busy(False)
            return
        if self.panel.stack.currentWidget() is self.panel.auth_page:
            self.panel._set_auth_status(message, error=True)
            self.panel._set_auth_form_busy(False)
        elif self.panel.stack.currentWidget() is self.panel.lobby_page:
            self.panel._set_lobby_status(message, error=True)
            self.panel._refresh_lobby_controls()
        else:
            self.panel._set_game_error(message)

    def logout(self) -> None:
        if self.panel._logging_out:
            return
        self.panel._logging_out = True
        self.panel._heartbeat_timer.stop()
        self.panel._reconnect_timer.stop()
        self.panel._teardown_ws()
        self.panel._profile_client.abort_all()
        self.panel._history_client.abort_all()
        self.panel._live_client.abort_all()
        self.panel._social_client.abort_all()
        self.panel._auth_client.logout()
        self.panel._auth_session = None
        host_module().clear_refresh_token(self.panel._settings)
        self.panel._close_social_windows()
        self.panel._clear_profile_ui()
        self.panel._show_auth_page()
        self.panel._set_auth_status("已退出登录")

    def on_logged_out(self) -> None:
        self.panel._logging_out = False
        self.panel._auth_session = None
        host_module().clear_refresh_token(self.panel._settings)
        self.panel._profile_client.abort_all()
        self.panel._history_client.abort_all()
        self.panel._live_client.abort_all()
        self.panel._social_client.abort_all()
        self.panel._close_social_windows()
        self.panel._clear_profile_ui()
        self.panel._show_auth_page()
        self.panel._set_auth_status("已退出登录")
