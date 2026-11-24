"""联机对战面板：登录、自建房大厅和在线棋盘。

联机不再是独立窗口，而是主窗口页面栈中的一页：应用全局只保留一个
原生窗口。登录成功后使用 HTTPS 认证 + WebSocket 自建房/排位；在线落子只由
服务器权威快照驱动，不在本地抢先更新。

本模块中的 :class:`OnlinePanel` 是薄门面：页面切换、控件状态与信号接线
留在面板上，具体领域行为由 :mod:`gomoku.client.online_controllers` 下的
控制器承担。
"""

from __future__ import annotations

import logging
from typing import Mapping, Optional

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.achievement_widgets import AchievementPanel
from gomoku.client.auth import AuthSession
from gomoku.client.avatar_images import (
    avatar_image_cache,
    configure_avatar_images,
)
from gomoku.client.config import AppConfig, resolve_endpoints
from gomoku.client.font_control import FontSizeButton
from gomoku.client.online_controllers.common import (  # noqa: F401
    entry_avatar_version as _entry_avatar_version,
    format_clock_ms as _format_clock_ms,
    format_game_time as _format_game_time,
    result_label as _result_label,
)
from gomoku.client.friends_panel import FriendsPanel
from gomoku.client.net.game_history_api import GameHistoryHTTPClient
from gomoku.client.net.http_api import AuthHTTPClient
from gomoku.client.net.profile_api import ProfileHTTPClient
from gomoku.client.net.report_api import ReportHTTPClient
from gomoku.client.net.social_api import SocialHTTPClient
from gomoku.client.online_controllers.auth_flow import AuthFlowController
from gomoku.client.online_controllers.connection import ConnectionController
from gomoku.client.online_controllers.game import GameController
from gomoku.client.online_controllers.match import MatchController
from gomoku.client.online_controllers.profile import ProfileController
from gomoku.client.online_controllers.social import SocialController
from gomoku.client.online_pages import (
    PlazaPage,
    build_auth_page,
    build_game_page,
    build_lobby_page,
)
from gomoku.client.online_pages.common import (  # noqa: F401
    online_timeout_notice,
    online_victory_notice,
)
from gomoku.client.online_player_card import OnlinePlayerCard
from gomoku.client.private_chat_panel import PrivateChatPanel
from gomoku.client.profile_panel import ProfilePanel
from gomoku.client.report_panel import ReportPanel
from gomoku.client.sound_control import SoundButton
from gomoku.client.ui_theme import ONLINE_BACKDROP_SPEC, InkBackdrop
from gomoku.shared.capabilities import CAP_ROOM_OPTIONS, CAP_TIME_CONTROL
from gomoku.shared.chat import ChatMessage
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import BOARD_SIZE, Cell

# 兼容既有测试/调用方：控制器通过 online_panel 模块解析这些名字，
# 因此 mock.patch 本模块中的名字依然能影响控制器行为。
from gomoku.client.audio import get_audio_manager  # noqa: F401
from gomoku.client.confirm_dialog import ConfirmDialog  # noqa: F401
from gomoku.client.credential_store import (  # noqa: F401
    clear_login,
    clear_refresh_token,
    load_login,
    load_refresh_token,
    save_login,
    save_refresh_token,
)
from gomoku.client.modal_overlay import NoticeOverlay  # noqa: F401
from gomoku.client.net.websocket_transport import (  # noqa: F401
    QtWebSocketTransport,
)
from gomoku.client.sessions.online import OnlineGameSession  # noqa: F401

logger = logging.getLogger(__name__)

#: 对话日志展开状态在 QSettings 里的键名。
SETTINGS_CHAT_LOG_VISIBLE = "chat_log_visible"

#: 倒计时进入最后这么多秒时数字转红，并开始逐秒播放“咚”提示音。
COUNTDOWN_URGENT_SECONDS = 10

#: 对局页错误提示在标签上停留的秒数。
#:
#: 服务端的落子拒绝会先发 error、紧接着发 state_changed，状态同步会把
#: 错误标签清空。提示必须停留几秒，玩家才看得见“该位置已有棋子”这类
#: 原因。
GAME_ERROR_HOLD_SECONDS = 4.0

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


class OnlinePanel(InkBackdrop):
    """登录、自建房和联机对局面板。"""

    #: 用户请求返回主菜单时发出。
    back_requested = Signal()
    #: 请求复盘某局棋谱；参数为棋谱列表。
    replay_requested = Signal(object)

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, spec=ONLINE_BACKDROP_SPEC)
        self.setObjectName("onlinePanel")
        self.config = config or AppConfig()
        self._closing = False
        self._logging_out = False
        self._restoring_session = False
        self._auth_session: Optional[AuthSession] = None
        self._transport: Optional[QtWebSocketTransport] = None
        self._session: Optional[OnlineGameSession] = None
        self._ws_connected = False
        self._room_action_pending = False
        self._reconnect_attempts = 0
        self._reconnect_after_refresh = False
        self._last_snapshot: Optional[GameSnapshot] = None
        self._audio_game_id: Optional[str] = None
        self._deadline_monotonic: Optional[float] = None
        # 当前这一手的标识（服务器截止时间 + 手数）：同一手内它是固定的，
        # 变了就说明进入了新的一手，提示音的节拍要重新开始。
        self._countdown_turn_key: Optional[tuple[int, int]] = None
        # 已经响过“咚”提示音的秒数；同一秒只响一次。
        self._countdown_beep_second: Optional[int] = None
        # 快照接收时刻，用于 Fischer 双方余量的本地平滑倒数。
        self._snapshot_monotonic: Optional[float] = None
        self._profile_user: Optional[dict] = None
        self._season_info: Optional[dict] = None
        self._season_rank: Optional[int] = None
        self._achievements_panel: Optional[AchievementPanel] = None
        self._leaderboard_entries: list = []
        self._achievement_notice_game_id = ""
        self._victory_notice_game_id = ""
        self._timeout_notice_game_id = ""
        self._auto_requeue = False
        self._refresh_after_auth_refresh = False
        # 对话日志的展开状态（逻辑值，不受页面是否在前台影响）。
        self._chat_log_expanded = False
        # 沙盘推演是否处于打开状态：打开时沙盘面板覆盖真实棋盘。
        self._sandbox_open: bool = False
        # 用户偏好（对话日志展开状态）通过 QSettings 持久化。
        self._settings = QSettings()

        self._api_base_url, self._ws_url = self._resolve_urls()
        # 自定义头像按需下载；离线模式没有地址时缓存器保持静默。
        configure_avatar_images(self._api_base_url or "")
        avatar_image_cache().image_loaded.connect(
            self._on_avatar_image_loaded
        )
        self._auth_client = AuthHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._auth_client.session_ready.connect(self._on_auth_session_ready)
        self._auth_client.session_refreshed.connect(
            self._on_auth_session_refreshed
        )
        self._auth_client.failed.connect(self._on_http_failed)
        self._auth_client.logged_out.connect(self._on_logged_out)
        self._auth_client.busy_changed.connect(self._on_auth_busy_changed)

        self._profile_client = ProfileHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._profile_client.profile_ready.connect(self._on_profile_ready)
        self._profile_client.leaderboard_ready.connect(
            self._on_leaderboard_ready
        )
        self._profile_client.failed.connect(self._on_profile_failed)
        self._profile_client.unauthorized.connect(
            self._on_profile_unauthorized
        )
        self._profile_client.busy_changed.connect(
            self._on_profile_busy_changed
        )

        self._history_client = GameHistoryHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._history_client.recent_ready.connect(
            self._on_history_recent_ready
        )
        self._history_client.game_ready.connect(self._on_history_game_ready)
        self._history_client.failed.connect(self._on_history_failed)
        self._history_client.unauthorized.connect(
            self._on_history_unauthorized
        )
        self._history_client.busy_changed.connect(
            self._on_history_busy_changed
        )
        self._recent_games_entries: list = []

        # 举报与回放审计：报局面板打开时先拉一次审计摘要。
        self._report_client = ReportHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._report_client.submitted.connect(self._on_report_submitted)
        self._report_client.audit_ready.connect(self._on_report_audit_ready)
        self._report_client.failed.connect(self._on_report_failed)
        self._report_client.unauthorized.connect(
            self._on_history_unauthorized
        )
        self._report_panel: Optional[ReportPanel] = None
        self._report_game_id = ""
        self._report_awaiting_response = False

        # 观战列表单独用一个 HTTP 客户端：它的失败提示不应该混进
        # “最近对局”的状态栏里。
        self._live_client = GameHistoryHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._live_client.live_ready.connect(self._on_live_ready)
        self._live_client.failed.connect(self._on_live_failed)
        self._live_client.unauthorized.connect(
            self._on_history_unauthorized
        )
        self._live_client.busy_changed.connect(
            self._on_history_busy_changed
        )
        self._live_games_entries: list = []

        self._social_client = SocialHTTPClient(
            self._api_base_url or None,
            timeout_seconds=self.config.request_timeout_seconds,
            parent=self,
        )
        self._social_client.response_ready.connect(self._on_social_ready)
        self._social_client.failed.connect(self._on_social_failed)
        self._social_client.unauthorized.connect(
            self._on_social_unauthorized
        )
        self._friends_data: dict = {}
        self._pending_removal_user_id = ""
        self._friends_panel: Optional[FriendsPanel] = None
        self._profile_panel: Optional[ProfilePanel] = None
        self._chat_panels: dict[str, PrivateChatPanel] = {}
        self._pending_profile_user_id = ""
        self._pending_chat_user: Optional[dict] = None
        # 观战结束后的去处：从对战广场进去的，回到广场而不是大厅。
        self._watching_from_plaza = False

        self._heartbeat_timer = QTimer(self)
        self._heartbeat_timer.setInterval(20_000)
        self._heartbeat_timer.timeout.connect(self._send_heartbeat)

        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(250)
        self._clock_timer.timeout.connect(self._update_clock_label)
        self._clock_timer.start()

        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._attempt_reconnect)

        self._undo_notice_timer = QTimer(self)
        self._undo_notice_timer.setSingleShot(True)
        self._undo_notice_timer.timeout.connect(self._clear_undo_notice)

        # 错误提示的停留计时器：状态同步不得把它立刻清掉。
        self._game_error_timer = QTimer(self)
        self._game_error_timer.setSingleShot(True)
        self._game_error_timer.timeout.connect(self._clear_game_error)
        # 已经回应过对手悔棋请求：等服务器确认前不再重复发送。
        self._undo_responded = False
        # 面板是否被切到后台（隐藏）：隐藏时停界面定时器与终局通知。
        self._visible_paused = False
        # 隐藏期间对局结束：回到前台时补一次终局音与终局提示。
        self._pending_end_notice = False

        # 对战广场的兜底刷新：服务器会推送“对局列表变了”，但推送可能
        # 在断线期间丢失，定时兜底保证列表不会一直停在前一次的结果上。
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(10_000)
        self._live_refresh_timer.timeout.connect(self._on_live_timer)

        self._build_ui()
        # 领域控制器：控件就绪后构造，面板只保留页面与信号编排。
        self._auth_flow = AuthFlowController(self)
        self._connection_ctrl = ConnectionController(self)
        self._profile_ctrl = ProfileController(self)
        self._social_ctrl = SocialController(self)
        self._match_ctrl = MatchController(self)
        self._game_ctrl = GameController(self)
        # 页面构建器只搭控件：对话日志默认收起，初始化放在控制器
        # 就绪之后执行。
        self._apply_chat_log_visibility(
            self._load_chat_log_visible()
        )
        self._refresh_chat_controls()
        self._load_saved_login()
        self._show_auth_page()
        self._refresh_auth_availability()
        # 等界面构建完成后再尝试静默恢复登录，避免阻塞构造过程。
        QTimer.singleShot(0, self._try_restore_session)

    def _resolve_urls(self) -> tuple[str, str]:
        """解析 REST/WS 地址；开发环境默认本机 8000 端口。"""
        return resolve_endpoints(self.config)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 声音设置与返回入口在登录、大厅和对局页都保持可见。
        utility_row = QHBoxLayout()
        utility_row.setContentsMargins(0, 0, 2, 0)
        utility_row.addStretch(1)
        self.back_button = QPushButton("返回主菜单", self)
        self.back_button.setObjectName("menuBackButton")
        self.back_button.setToolTip("回到主菜单，联机状态保持不变")
        self.back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.back_button.setMinimumWidth(104)
        self.back_button.setMinimumHeight(32)
        self.back_button.setAutoDefault(False)
        self.back_button.clicked.connect(self._on_back_clicked)
        utility_row.addWidget(
            self.back_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.sound_button = SoundButton(self)
        utility_row.addWidget(
            self.sound_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.font_button = FontSizeButton(self)
        utility_row.addWidget(
            self.font_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        root.addLayout(utility_row)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("onlineStack")
        self.auth_page = build_auth_page(self)
        self.lobby_page = build_lobby_page(self)
        self.plaza_page = PlazaPage(self)
        self.game_page = build_game_page(self)
        self.stack.addWidget(self.auth_page)
        self.stack.addWidget(self.lobby_page)
        self.stack.addWidget(self.plaza_page)
        self.stack.addWidget(self.game_page)
        root.addWidget(self.stack)

        # 兼容既有测试/调用方：广场页内部控件仍可从面板访问。
        self.refresh_live_button = self.plaza_page.refresh_live_button
        self.live_games_container = self.plaza_page.live_games_container
        self.live_games_layout = self.plaza_page.live_games_layout
        self.live_status_label = self.plaza_page.live_status_label
        self.plaza_status_label = self.plaza_page.plaza_status_label
        self.plaza_back_button = self.plaza_page.plaza_back_button
        self.plaza_page.refresh_requested.connect(self._on_refresh_live)
        self.plaza_page.back_requested.connect(self._on_plaza_back)
        self.plaza_page.watch_requested.connect(self._on_watch_game)

    def _on_back_clicked(self) -> None:
        """“返回主菜单”按钮：交由主窗口切页。"""
        self.back_requested.emit()

    def in_room(self) -> bool:
        """当前是否在房间或匹配队列中。"""
        session = self._session
        if session is None:
            return False
        return bool(session.room_code) or session.queued

    def pause_visible_state(self) -> None:
        """面板被切到后台时暂停界面层的活动。

        决策：**只暂停界面层**——250 毫秒倒计时、10 秒广场刷新，以及
        终局提示与终局音效。心跳与重连**继续运行**：连接保持在线，
        对局不会因为玩家切走界面而被服务器判超时掉线；服务器推来的
        权威快照照常接收，只是界面不刷新、也不打扰用户。
        """
        self._visible_paused = True
        self._clock_timer.stop()
        self._live_refresh_timer.stop()

    def resume_visible_state(self) -> None:
        """面板回到前台：恢复界面定时器并立刻刷新一次显示。"""
        was_paused = self._visible_paused
        self._visible_paused = False
        self._clock_timer.start()
        if self.stack.currentWidget() is self.plaza_page:
            self._live_refresh_timer.start()
        # 立刻刷新一次：否则用户会看到暂停前那一拍的过期倒计时。
        self._update_clock_label()
        self.board_widget.update()
        if was_paused and self._pending_end_notice:
            # 隐藏期间对局已经结束：这时才补终局音与终局提示。
            self._pending_end_notice = False
            self._play_online_result_sound()
            QTimer.singleShot(0, self._show_online_victory_notice)
            QTimer.singleShot(0, self._show_online_timeout_notice)

    def _show_auth_page(self) -> None:
        self.stack.setCurrentWidget(self.auth_page)
        # 回到登录页就不该再刷新对战广场了。
        self._live_refresh_timer.stop()
        self.auth_status_label.setText("")
        # 退出登录或被强制重新登录后，把已保存的密码重新填回去。
        self._restore_saved_login()
        self._refresh_auth_availability()

    def _show_lobby_page(self, message: str = "") -> None:
        self.stack.setCurrentWidget(self.lobby_page)
        # 对战广场不在大厅上，离开页面时停掉它的定时刷新。
        self._live_refresh_timer.stop()
        if self._auth_session is not None:
            user = self._auth_session.user.username
        elif self._session is not None:
            user = self._session.username
        else:
            user = ""
        self.lobby_welcome_label.setText(f"已登录：{user}" if user else "已登录")
        self.lobby_status_label.setText(message)
        self._update_rating_summary()
        self._refresh_lobby_controls()

    def _show_plaza_page(self, message: str = "") -> None:
        """打开对战广场：进行中的对局列表在这里集中管理。"""
        self.stack.setCurrentWidget(self.plaza_page)
        self.plaza_status_label.setText(message)
        self._refresh_plaza_controls()
        self._fetch_live_games()
        if not self._live_refresh_timer.isActive():
            self._live_refresh_timer.start()

    def _on_plaza_clicked(self) -> None:
        """大厅里的“对战广场”入口。"""
        if self._session is not None and self._session.in_room:
            self._set_lobby_status(
                "你已经在对局中，无法进入对战广场", error=True
            )
            return
        self._show_plaza_page()

    def _on_plaza_back(self) -> None:
        """从对战广场返回大厅。"""
        self._show_lobby_page()

    def _show_game_page(self) -> None:
        if self.stack.currentWidget() is not self.game_page:
            self.stack.setCurrentWidget(self.game_page)
        # 进到对局页就不再需要刷新广场列表了。
        self._live_refresh_timer.stop()

    def _leave_game_page(self, message: str = "") -> None:
        """离开对局页：从对战广场进去观战的，回广场；其余回大厅。"""
        # 沙盘不能跟着离开对局页：先收起来。
        self._close_sandbox()
        if self._watching_from_plaza:
            self._watching_from_plaza = False
            self._show_plaza_page(message)
        else:
            self._show_lobby_page(message)

    def _refresh_auth_availability(self) -> None:
        available = bool(self._api_base_url and self._ws_url)
        enabled = (
            available
            and not self._auth_client.busy
            and self.stack.currentWidget() is self.auth_page
        )
        for widget in (
            self.username_edit,
            self.password_edit,
            self.login_button,
            self.register_button,
        ):
            widget.setEnabled(enabled)
        if not available:
            self._set_auth_status(
                "未配置联机服务器，当前只有离线模式可用", error=True
            )

    def _refresh_lobby_controls(self) -> None:
        connected = (
            self._session is not None
            and self._session.is_authenticated
            and self._ws_connected
            and not self._closing
        )
        pending = self._room_action_pending
        queued = bool(self._session is not None and self._session.queued)
        self.quick_match_button.setEnabled(connected and not pending)
        self.quick_match_button.setText(
            "取消排位匹配" if queued else "开始排位匹配"
        )
        friend_enabled = connected and not pending and not queued
        self.create_room_button.setEnabled(friend_enabled)
        supports_time_control = (
            self._session is not None
            and self._session.supports(CAP_TIME_CONTROL)
        )
        # 旧服务端不读取客户端的时限设置，会静默退回每手 30 秒；
        # 这里直接禁用选择并提示，避免用户以为选择没有生效。
        self.turn_seconds_combo.setEnabled(
            friend_enabled and supports_time_control
        )
        # 规则模式随建房请求一起提交，未连接或操作在途时同样禁用。
        self.rule_mode_combo.setEnabled(friend_enabled)
        self.time_control_hint.setVisible(
            connected and not supports_time_control
        )
        if connected and not supports_time_control:
            self.time_control_hint.setText(
                "服务端版本过旧，不支持自定义时限；请升级服务端，"
                "否则自建房会按每手 30 秒进行。"
            )
        self.allow_spectators_checkbox.setVisible(
            self._session is not None
            and self._session.supports(CAP_ROOM_OPTIONS)
        )
        self.refresh_leaderboard_button.setEnabled(
            connected and not self._profile_client.busy
        )
        if hasattr(self, "refresh_history_button"):
            self.refresh_history_button.setEnabled(
                connected and not self._history_client.busy
            )
            has_selection = self.recent_games_table.currentRow() >= 0
            self.replay_history_button.setEnabled(
                connected
                and has_selection
                and bool(self._recent_games_entries)
            )
        if hasattr(self, "refresh_live_button"):
            self.refresh_live_button.setEnabled(
                connected and not self._live_client.busy
            )
        # 对战广场入口：已经在对局里时不能进去看别的棋局。
        self.plaza_button.setEnabled(connected and not pending)
        self.logout_button.setEnabled(
            not self._auth_client.busy and not pending
        )

    def _refresh_plaza_controls(self) -> None:
        """对战广场页的控件状态。"""
        connected = (
            self._session is not None
            and self._session.is_authenticated
            and self._ws_connected
            and not self._closing
        )
        self.refresh_live_button.setEnabled(
            connected and not self._live_client.busy
        )
        self.plaza_back_button.setEnabled(not self._closing)

    @staticmethod
    def _set_status_label(label: QLabel, text: str, error: bool) -> None:
        label.setText(text)
        value = "true" if error else "false"
        if label.property("error") != value:
            label.setProperty("error", value)
            label.style().unpolish(label)
            label.style().polish(label)

    def _set_auth_status(self, text: str, error: bool = False) -> None:
        self._set_status_label(self.auth_status_label, text, error)

    def _set_lobby_status(self, text: str, error: bool = False) -> None:
        self._set_status_label(self.lobby_status_label, text, error)

    def _set_match_status(self, text: str, error: bool = False) -> None:
        self._set_status_label(self.match_status_label, text, error)

    # ------------------------------------------------------------------
    # 登录/注册（委托到控制器）
    # ------------------------------------------------------------------

    def _on_login_clicked(self) -> None:
        self._auth_flow.on_login_clicked()

    def _on_register_clicked(self) -> None:
        self._auth_flow.on_register_clicked()

    def _load_saved_login(self) -> None:
        self._auth_flow.load_saved_login()

    def _try_restore_session(self) -> None:
        self._auth_flow.try_restore_session()

    def _save_refresh_token(self, token: str) -> None:
        self._auth_flow.save_refresh_token(token)

    def _restore_saved_login(self) -> None:
        self._auth_flow.restore_saved_login()

    def _save_saved_login(self, username: str) -> str:
        return self._auth_flow.save_saved_login(username)

    def _on_remember_password_toggled(self, checked: bool) -> None:
        self._auth_flow.on_remember_password_toggled(checked)

    def _set_auth_form_busy(self, busy: bool) -> None:
        self._auth_flow.set_auth_form_busy(busy)

    def _on_auth_busy_changed(self, busy: bool) -> None:
        self._auth_flow.on_auth_busy_changed(busy)

    def _on_auth_session_ready(
        self, session: AuthSession, *, restored: bool = False
    ) -> None:
        self._auth_flow.on_auth_session_ready(session, restored=restored)

    def _on_auth_session_refreshed(self, session: AuthSession) -> None:
        self._auth_flow.on_auth_session_refreshed(session)

    def _on_http_failed(self, message: str) -> None:
        self._auth_flow.on_http_failed(message)

    def _logout(self) -> None:
        self._auth_flow.logout()

    def _on_logged_out(self) -> None:
        self._auth_flow.on_logged_out()

    # ------------------------------------------------------------------
    # WebSocket 生命周期（委托到控制器）
    # ------------------------------------------------------------------

    def _start_ws_session(self) -> None:
        self._connection_ctrl.start_ws_session()

    def _open_transport(self, reset_status: bool = True) -> None:
        self._connection_ctrl.open_transport(reset_status)

    def _on_ws_connected(self) -> None:
        self._connection_ctrl.on_ws_connected()

    def _on_ws_authenticated(self, _user: object) -> None:
        self._connection_ctrl.on_ws_authenticated(_user)

    def _on_ws_disconnected(self, reason: str) -> None:
        self._connection_ctrl.on_ws_disconnected(reason)

    def _on_ws_error(self, message: str) -> None:
        self._connection_ctrl.on_ws_error(message)

    def _schedule_reconnect(self, reason: str) -> None:
        self._connection_ctrl.schedule_reconnect(reason)

    def _attempt_reconnect(self) -> None:
        self._connection_ctrl.attempt_reconnect()

    def _force_relogin(self, message: str) -> None:
        self._connection_ctrl.force_relogin(message)

    def _teardown_ws(self) -> None:
        self._connection_ctrl.teardown_ws()

    def _send_heartbeat(self) -> None:
        self._connection_ctrl.send_heartbeat()

    # ------------------------------------------------------------------
    # 资料/排行榜/历史/广场（委托到控制器）
    # ------------------------------------------------------------------

    def _rating_source(self) -> Optional[dict]:
        return self._profile_ctrl.rating_source()

    def _update_rating_summary(self) -> None:
        self._profile_ctrl.update_rating_summary()

    def _refresh_profile_data(self) -> None:
        self._profile_ctrl.refresh_profile_data()

    def _on_refresh_leaderboard(self) -> None:
        self._profile_ctrl.on_refresh_leaderboard()

    def _on_profile_ready(self, data: object) -> None:
        self._profile_ctrl.on_profile_ready(data)

    def _sync_auth_user_from_profile(self, raw_user: Mapping) -> None:
        self._profile_ctrl.sync_auth_user_from_profile(raw_user)

    def _on_leaderboard_ready(self, data: object) -> None:
        self._profile_ctrl.on_leaderboard_ready(data)

    def _fill_leaderboard(self, entries: list, total: int) -> None:
        self._profile_ctrl.fill_leaderboard(entries, total)

    def _on_avatar_image_loaded(self, user_id: str, version: int) -> None:
        self._profile_ctrl.on_avatar_image_loaded(user_id, version)

    def _on_profile_failed(self, message: str) -> None:
        self._profile_ctrl.on_profile_failed(message)

    def _on_profile_unauthorized(self) -> None:
        self._profile_ctrl.on_profile_unauthorized()

    def _on_profile_busy_changed(self, _busy: bool) -> None:
        self._profile_ctrl.on_profile_busy_changed(_busy)

    def _on_leaderboard_cell_clicked(self, row: int, column: int) -> None:
        self._profile_ctrl.on_leaderboard_cell_clicked(row, column)

    def _update_self_avatar(self) -> None:
        self._profile_ctrl.update_self_avatar()

    def _self_profile(self) -> dict:
        return self._profile_ctrl.self_profile()

    def _open_user_profile(self, user_id: str) -> None:
        self._profile_ctrl.open_user_profile(user_id)

    def _show_profile_panel(self, profile: object) -> None:
        self._profile_ctrl.show_profile_panel(profile)

    def _open_achievements_panel(self) -> None:
        self._profile_ctrl.open_achievements_panel()

    def _open_avatar_picker(self) -> None:
        self._profile_ctrl.open_avatar_picker()

    def _on_avatar_selected(self, avatar: str) -> None:
        self._profile_ctrl.on_avatar_selected(avatar)

    def _on_avatar_image_selected(self, data_url: str) -> None:
        self._profile_ctrl.on_avatar_image_selected(data_url)

    def _apply_avatar_update(self, data: object) -> None:
        self._profile_ctrl.apply_avatar_update(data)

    def _refresh_open_profile(self) -> None:
        self._profile_ctrl.refresh_open_profile()

    def _on_refresh_history(self) -> None:
        self._profile_ctrl.on_refresh_history()

    def _on_history_recent_ready(self, data: object) -> None:
        self._profile_ctrl.on_history_recent_ready(data)

    def _fill_recent_games(self, entries: list, total: int) -> None:
        self._profile_ctrl.fill_recent_games(entries, total)

    def _on_history_game_ready(self, data: object) -> None:
        self._profile_ctrl.on_history_game_ready(data)

    def _open_replay(self, record: GameRecord) -> None:
        self._profile_ctrl.open_replay(record)

    def _on_history_failed(self, message: str) -> None:
        self._profile_ctrl.on_history_failed(message)

    def _on_history_unauthorized(self) -> None:
        self._profile_ctrl.on_history_unauthorized()

    def _on_history_busy_changed(self, _busy: bool) -> None:
        self._profile_ctrl.on_history_busy_changed(_busy)

    def _on_recent_selection_changed(self) -> None:
        self._profile_ctrl.on_recent_selection_changed()

    def _on_recent_double_clicked(self, row: int, _column: int) -> None:
        self._profile_ctrl.on_recent_double_clicked(row, _column)

    def _on_replay_history(self) -> None:
        self._profile_ctrl.on_replay_history()

    def _request_replay_row(self, row: int) -> None:
        self._profile_ctrl.request_replay_row(row)

    def _fetch_live_games(self) -> None:
        self._profile_ctrl.fetch_live_games()

    def _on_refresh_live(self) -> None:
        self._profile_ctrl.on_refresh_live()

    def _on_live_timer(self) -> None:
        self._profile_ctrl.on_live_timer()

    def _on_live_games_changed(self) -> None:
        self._profile_ctrl.on_live_games_changed()

    def _on_live_ready(self, data: object) -> None:
        self._profile_ctrl.on_live_ready(data)

    def _on_live_failed(self, message: str) -> None:
        self._profile_ctrl.on_live_failed(message)

    def _fill_live_games(self, entries: list, total: int) -> None:
        self._profile_ctrl.fill_live_games(entries, total)

    def _on_watch_game(self, game_id: str) -> None:
        self._profile_ctrl.on_watch_game(game_id)

    def _clear_profile_ui(self) -> None:
        self._profile_ctrl.clear_profile_ui()

    # ------------------------------------------------------------------
    # 好友/私聊/邀请/举报（委托到控制器）
    # ------------------------------------------------------------------

    def _refresh_friends(self) -> None:
        self._social_ctrl.refresh_friends()

    def _update_friends_badge(self) -> None:
        self._social_ctrl.update_friends_badge()

    def _open_friends_panel(self, invite_mode: bool = False) -> None:
        self._social_ctrl.open_friends_panel(invite_mode)

    def _send_friend_request(self, user_id: str) -> None:
        self._social_ctrl.send_friend_request(user_id)

    def _accept_friend_request(self, request_id: str) -> None:
        self._social_ctrl.accept_friend_request(request_id)

    def _decline_friend_request(self, request_id: str) -> None:
        self._social_ctrl.decline_friend_request(request_id)

    def _remove_friend(self, user_id: str) -> None:
        self._social_ctrl.remove_friend(user_id)

    def _search_users(self, query: str) -> None:
        self._social_ctrl.search_users(query)

    def _username_avatar(
        self, user_id: str, fallback: str = ""
    ) -> tuple[str, str, int]:
        return self._social_ctrl.username_avatar(user_id, fallback)

    def _open_private_chat(
        self, user_id: str, username: str = ""
    ) -> None:
        self._social_ctrl.open_private_chat(user_id, username)

    def _sync_chat_self_avatar(self, window: PrivateChatPanel) -> None:
        self._social_ctrl.sync_chat_self_avatar(window)

    def _send_private_message(self, user_id: str, text: str) -> None:
        self._social_ctrl.send_private_message(user_id, text)

    def _on_direct_message(self, message: object) -> None:
        self._social_ctrl.on_direct_message(message)

    def _report_messages_read(
        self, user_id: str, window: PrivateChatPanel
    ) -> None:
        self._social_ctrl.report_messages_read(user_id, window)

    def _clear_friend_unread(self, user_id: str) -> None:
        self._social_ctrl.clear_friend_unread(user_id)

    def _on_friend_event(self, event: object) -> None:
        self._social_ctrl.on_friend_event(event)

    def _on_room_invited(self, payload: object) -> None:
        self._social_ctrl.on_room_invited(payload)

    def _on_room_invite_result(self, payload: object) -> None:
        self._social_ctrl.on_room_invite_result(payload)

    def _on_social_ready(self, kind: str, data: object) -> None:
        self._social_ctrl.on_social_ready(kind, data)

    def _on_social_failed(self, kind: str, message: str) -> None:
        self._social_ctrl.on_social_failed(kind, message)

    def _on_social_unauthorized(self, _kind: str) -> None:
        self._social_ctrl.on_social_unauthorized(_kind)

    def _close_social_windows(self) -> None:
        self._social_ctrl.close_social_windows()

    def _on_report_clicked(self) -> None:
        self._social_ctrl.on_report_clicked()

    def _on_report_submit(self, category: str, description: str) -> None:
        self._social_ctrl.on_report_submit(category, description)

    def _on_report_submitted(self, _data: object) -> None:
        self._social_ctrl.on_report_submitted(_data)

    def _on_report_audit_ready(self, data: object) -> None:
        self._social_ctrl.on_report_audit_ready(data)

    def _on_report_failed(self, message: str) -> None:
        self._social_ctrl.on_report_failed(message)

    def _on_report_panel_closed(self) -> None:
        self._social_ctrl.on_report_panel_closed()

    # ------------------------------------------------------------------
    # 建房/匹配（委托到控制器）
    # ------------------------------------------------------------------

    def _on_create_room(self) -> None:
        self._match_ctrl.on_create_room()

    def _on_quick_match(self) -> None:
        self._match_ctrl.on_quick_match()

    def _on_queue_status_changed(self, queued: bool, size: int) -> None:
        self._match_ctrl.on_queue_status_changed(queued, size)

    def _on_match_found(self, _game_id: str, opponent_name: str) -> None:
        self._match_ctrl.on_match_found(_game_id, opponent_name)

    def _on_room_created(self, code: str) -> None:
        self._match_ctrl.on_room_created(code)

    def _on_room_closed(self, reason: str) -> None:
        self._match_ctrl.on_room_closed(reason)

    def _on_leave_room(self) -> None:
        self._match_ctrl.on_leave_room()

    def _on_invite_friend_clicked(self) -> None:
        self._match_ctrl.on_invite_friend_clicked()

    def _invite_friend(self, user_id: str, username: str) -> None:
        self._match_ctrl.invite_friend(user_id, username)

    # ------------------------------------------------------------------
    # 对局页/倒计时/悔棋/沙盘（委托到控制器）
    # ------------------------------------------------------------------

    def _show_status(self, text: str, error: bool = False) -> None:
        self._game_ctrl.show_status(text, error)

    def _set_game_error(self, text: str) -> None:
        self._game_ctrl.set_game_error(text)

    def _clear_game_error(self) -> None:
        self._game_ctrl.clear_game_error()

    def _clear_stale_game_error(self) -> None:
        self._game_ctrl.clear_stale_game_error()

    def _log_game_notice(self, text: str) -> None:
        self._game_ctrl.log_game_notice(text)

    def _on_chat_received(self, message: ChatMessage) -> None:
        self._game_ctrl.on_chat_received(message)

    def _on_emoji_picked(self, emoji: str) -> None:
        self._game_ctrl.on_emoji_picked(emoji)

    def _on_chat_failed(self, reason: str) -> None:
        self._game_ctrl.on_chat_failed(reason)

    def _flash_chat_hint(self, text: str) -> None:
        self._game_ctrl.flash_chat_hint(text)

    def _restore_chat_hint(self) -> None:
        self._game_ctrl.restore_chat_hint()

    def _on_chat_log_toggled(self, checked: bool) -> None:
        self._game_ctrl.on_chat_log_toggled(checked)

    def _apply_chat_log_visibility(self, visible: bool) -> None:
        self._game_ctrl.apply_chat_log_visibility(visible)

    def _load_chat_log_visible(self) -> bool:
        return self._game_ctrl.load_chat_log_visible()

    def _save_chat_log_visible(self, visible: bool) -> None:
        self._game_ctrl.save_chat_log_visible(visible)

    def _on_preset_comment_picked(self, text: str) -> None:
        self._game_ctrl.on_preset_comment_picked(text)

    def _on_send_comment(self) -> None:
        self._game_ctrl.on_send_comment()

    def _refresh_chat_controls(self) -> None:
        self._game_ctrl.refresh_chat_controls()

    def _clear_chat_area(self) -> None:
        self._game_ctrl.clear_chat_area()

    def _on_board_clicked(self, row: int, col: int) -> None:
        self._game_ctrl.on_board_clicked(row, col)

    def _on_sandbox_toggled(self) -> None:
        self._game_ctrl.on_sandbox_toggled()

    def _open_sandbox(self) -> bool:
        return self._game_ctrl.open_sandbox()

    def _close_sandbox(self) -> None:
        self._game_ctrl.close_sandbox()

    def _hide_sandbox(self) -> None:
        self._game_ctrl.hide_sandbox()

    def _sync_sandbox_button(
        self, session: Optional[OnlineGameSession]
    ) -> None:
        self._game_ctrl.sync_sandbox_button(session)

    def _on_undo_clicked(self) -> None:
        self._game_ctrl.on_undo_clicked()

    def _on_undo_declined(self) -> None:
        self._game_ctrl.on_undo_declined()

    def _on_undo_request_received(self, name: str) -> None:
        self._game_ctrl.on_undo_request_received(name)

    def _on_undo_status_changed(self, state: str, reason: str) -> None:
        self._game_ctrl.on_undo_status_changed(state, reason)

    def _show_undo_notice(self, text: str) -> None:
        self._game_ctrl.show_undo_notice(text)

    def _clear_undo_notice(self) -> None:
        self._game_ctrl.clear_undo_notice()

    def _set_undo_text(self, text: str) -> None:
        self._game_ctrl.set_undo_text(text)

    def _refresh_undo_controls(self) -> None:
        self._game_ctrl.refresh_undo_controls()

    def _on_resign(self) -> None:
        self._game_ctrl.on_resign()

    def _on_rematch(self) -> None:
        self._game_ctrl.on_rematch()

    def _on_online_state_changed(self) -> None:
        self._game_ctrl.on_online_state_changed()

    def _on_online_stone_placed(self) -> None:
        self._game_ctrl.on_online_stone_placed()

    def _on_online_game_over(self) -> None:
        self._game_ctrl.on_online_game_over()

    def _show_online_victory_notice(self) -> None:
        self._game_ctrl.show_online_victory_notice()

    def _show_online_timeout_notice(self) -> None:
        self._game_ctrl.show_online_timeout_notice()

    def _play_online_result_sound(self) -> None:
        self._game_ctrl.play_online_result_sound()

    def _maybe_play_online_start(self, snapshot: GameSnapshot) -> None:
        self._game_ctrl.maybe_play_online_start(snapshot)

    def _on_online_error(self, message: str) -> None:
        self._game_ctrl.on_online_error(message)

    def _sync_online_ui(self) -> None:
        self._game_ctrl.sync_online_ui()

    def _sync_game_page(self) -> None:
        self._game_ctrl.sync_game_page()

    def _apply_snapshot_deadline(self, snapshot: GameSnapshot) -> None:
        self._game_ctrl.apply_snapshot_deadline(snapshot)

    def _set_chat_visibility(self, visible: bool) -> None:
        self._game_ctrl.set_chat_visibility(visible)

    def _sync_spectator_page(self, snapshot: GameSnapshot) -> None:
        self._game_ctrl.sync_spectator_page(snapshot)

    def _update_achievement_notice(
        self, snapshot: Optional[GameSnapshot]
    ) -> None:
        self._game_ctrl.update_achievement_notice(snapshot)

    def _set_game_pill(self, text: str, result: bool) -> None:
        self._game_ctrl.set_game_pill(text, result)

    def _set_action_visibility(
        self,
        resign: bool,
        rematch: bool,
        leave: bool,
        *,
        undo: bool,
        reject_undo: bool,
        report: bool = False,
    ) -> None:
        self._game_ctrl.set_action_visibility(
            resign,
            rematch,
            leave,
            undo=undo,
            reject_undo=reject_undo,
            report=report,
        )

    def _update_clock_label(self) -> None:
        self._game_ctrl.update_clock_label()

    def _hide_countdown(self) -> None:
        self._game_ctrl.hide_countdown()

    def _set_countdown_urgent(self, urgent: bool) -> None:
        self._game_ctrl.set_countdown_urgent(urgent)

    def _show_countdown(self, seconds: int, snapshot: GameSnapshot) -> None:
        self._game_ctrl.show_countdown(seconds, snapshot)

    def _maybe_beep_countdown(
        self, seconds: int, snapshot: GameSnapshot
    ) -> None:
        self._game_ctrl.maybe_beep_countdown(seconds, snapshot)

    def _countdown_is_mine(self, snapshot: GameSnapshot) -> bool:
        return self._game_ctrl.countdown_is_mine(snapshot)

    def _fischer_clock_text(self, snapshot: GameSnapshot) -> str:
        return self._game_ctrl.fischer_clock_text(snapshot)

    def shutdown(self) -> None:
        """停止定时器与网络连接；面板本身由主窗口的页面栈管理。"""
        if self._closing:
            return
        self._closing = True
        self._heartbeat_timer.stop()
        self._clock_timer.stop()
        self._reconnect_timer.stop()
        self._undo_notice_timer.stop()
        self._live_refresh_timer.stop()
        self._auth_client.abort()
        self._profile_client.abort_all()
        self._history_client.abort_all()
        self._live_client.abort_all()
        self._social_client.abort_all()
        self._report_client.abort_all()
        self._close_social_windows()
        if self._report_panel is not None:
            self._report_panel.close()
        self._teardown_ws()
        if hasattr(self, "danmaku_overlay"):
            self.danmaku_overlay.clear()
        # 沙盘面板是子控件，关闭时顺手收起，避免残留可见状态。
        self._hide_sandbox()


__all__ = ["OnlinePanel", "OnlinePlayerCard"]
