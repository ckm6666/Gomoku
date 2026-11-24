"""WebSocket 连接生命周期控制器。

负责建连、认证、断线重连、心跳、拆除以及在登录过期时强制重新登录。
"""

from __future__ import annotations

import logging
from gomoku.client.online_controllers.common import (
    PanelController,
    host_module,
)
from gomoku.shared.errors import TransportError

logger = logging.getLogger(__name__)


class ConnectionController(PanelController):
    """联机面板的 WebSocket 生命周期。"""

    def start_ws_session(self) -> None:
        if not self.panel._auth_session:
            return
        self.panel._teardown_ws()
        transport = host_module().QtWebSocketTransport()
        session = host_module().OnlineGameSession(transport)
        self.panel._transport = transport
        self.panel._session = session
        # 新的会话：上一局的悔棋回应状态不再有效。
        self.panel._undo_responded = False

        transport.connected.connect(self.panel._on_ws_connected)
        transport.disconnected.connect(self.panel._on_ws_disconnected)
        transport.error.connect(self.panel._on_ws_error)
        session.authenticated.connect(self.panel._on_ws_authenticated)
        session.state_changed.connect(self.panel._on_online_state_changed)
        session.stone_placed.connect(self.panel._on_online_stone_placed)
        session.game_over.connect(self.panel._on_online_game_over)
        session.error.connect(self.panel._on_online_error)
        session.undo_request_received.connect(
            self.panel._on_undo_request_received
        )
        session.undo_status_changed.connect(self.panel._on_undo_status_changed)
        session.room_created.connect(self.panel._on_room_created)
        session.room_closed.connect(self.panel._on_room_closed)
        session.queue_status_changed.connect(
            self.panel._on_queue_status_changed
        )
        session.matched.connect(self.panel._on_match_found)
        session.chat_received.connect(self.panel._on_chat_received)
        session.chat_failed.connect(self.panel._on_chat_failed)
        session.room_invited.connect(self.panel._on_room_invited)
        session.room_invite_result.connect(self.panel._on_room_invite_result)
        session.friend_event.connect(self.panel._on_friend_event)
        session.direct_message.connect(self.panel._on_direct_message)
        session.live_games_changed.connect(self.panel._on_live_games_changed)

        self.panel._open_transport(reset_status=True)

    def open_transport(self, reset_status: bool = True) -> None:
        if (
            self.panel._closing
            or self.panel._transport is None
            or self.panel._auth_session is None
        ):
            return
        if reset_status:
            self.panel._set_auth_status("正在连接游戏服务器…")
        try:
            self.panel._transport.open(self.panel._ws_url)
        except TransportError as exc:
            logger.warning("打开 WebSocket 失败：%s", exc)
            self.panel._set_auth_status("无法连接游戏服务器", error=True)
            self.panel._set_auth_form_busy(False)

    def on_ws_connected(self) -> None:
        if (
            self.panel._closing
            or self.panel._auth_session is None
            or self.panel._session is None
        ):
            return
        self.panel._ws_connected = True
        self.panel.connection_label.setText("正在认证…")
        if not self.panel._session.authenticate(self.panel._auth_session.access_token):
            self.panel._set_auth_status("发送认证信息失败", error=True)

    def on_ws_authenticated(self, _user: object) -> None:
        if self.panel._closing or self.panel._session is None:
            return
        self.panel._reconnect_attempts = 0
        self.panel._reconnect_after_refresh = False
        self.panel._ws_connected = True
        if not self.panel._heartbeat_timer.isActive():
            self.panel._heartbeat_timer.start()
        username = self.panel._session.username or (
            self.panel._auth_session.user.username if self.panel._auth_session else ""
        )
        self.panel.lobby_welcome_label.setText(f"已登录：{username}")
        self.panel._update_self_avatar()
        self.panel._show_lobby_page("已连接联机服务器")
        self.panel._set_match_status("")
        self.panel._refresh_profile_data()
        self.panel._refresh_friends()
        self.panel._refresh_lobby_controls()

    def on_ws_disconnected(self, reason: str) -> None:
        if self.panel.sender() is not self.panel._transport:
            return
        self.panel._ws_connected = False
        self.panel._heartbeat_timer.stop()
        self.panel.connection_label.setText("连接中断")
        self.panel._refresh_lobby_controls()
        if self.panel.stack.currentWidget() is self.panel.lobby_page:
            self.panel._set_match_status("连接中断，正在尝试重连…", error=True)
        if self.panel._closing or self.panel._logging_out:
            return

        if self.panel._session is not None and self.panel._session.is_authenticated:
            self.panel._schedule_reconnect(reason)
        else:
            # 初次连接/认证失败：回到登录页，允许再次尝试。
            self.panel._show_auth_page()
            self.panel._set_auth_status(
                reason or "无法连接游戏服务器，请检查服务端是否启动",
                error=True,
            )
            self.panel._set_auth_form_busy(False)

    def on_ws_error(self, message: str) -> None:
        if self.panel.sender() is not self.panel._transport:
            return
        if self.panel.stack.currentWidget() is self.panel.auth_page:
            self.panel._set_auth_status(message, error=True)
            if self.panel._session is None or not self.panel._session.is_authenticated:
                self.panel._set_auth_form_busy(False)
        else:
            self.panel._set_game_error(message)

    def schedule_reconnect(self, reason: str) -> None:
        if self.panel._reconnect_timer.isActive():
            return
        self.panel._reconnect_attempts += 1
        if self.panel._reconnect_attempts > 5:
            self.panel._force_relogin("连接中断且多次重连失败，请重新登录")
            return
        self.panel.connection_label.setText(
            f"连接中断，{self.panel._reconnect_attempts}/5 次重连中…"
        )
        if self.panel._session:
            self.panel._set_game_error(reason)
        self.panel._reconnect_timer.start(2000)

    def attempt_reconnect(self) -> None:
        if (
            self.panel._closing
            or self.panel._logging_out
            or self.panel._auth_session is None
            or self.panel._transport is None
            or self.panel._session is None
        ):
            return
        if self.panel._transport.is_connected:
            return
        if self.panel._auth_session.is_access_expired():
            self.panel._reconnect_after_refresh = True
            if not self.panel._auth_client.busy:
                if not self.panel._auth_client.refresh():
                    self.panel._force_relogin("登录已过期，请重新登录")
            return
        try:
            self.panel._transport.open(self.panel._ws_url)
        except TransportError:
            self.panel._schedule_reconnect("重连失败")

    def force_relogin(self, message: str) -> None:
        self.panel._reconnect_timer.stop()
        self.panel._heartbeat_timer.stop()
        self.panel._teardown_ws()
        self.panel._profile_client.abort_all()
        self.panel._history_client.abort_all()
        self.panel._live_client.abort_all()
        self.panel._social_client.abort_all()
        self.panel._auth_session = None
        host_module().clear_refresh_token(self.panel._settings)
        self.panel._close_social_windows()
        self.panel._clear_profile_ui()
        self.panel._show_auth_page()
        self.panel._set_auth_status(message, error=True)
        self.panel._set_auth_form_busy(False)

    def teardown_ws(self) -> None:
        self.panel._ws_connected = False
        self.panel._audio_game_id = None
        session = self.panel._session
        self.panel._session = None
        self.panel._transport = None
        if session is not None:
            # OnlineGameSession.shutdown 会关闭 transport；其信号回调检查
            # 当前 sender/引用，不会误触发重连。
            session.shutdown()

    def send_heartbeat(self) -> None:
        if self.panel._session is not None and self.panel._session.is_authenticated:
            self.panel._session.send_ping()
