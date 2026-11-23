"""联机对战面板：登录、自建房大厅和在线棋盘。

联机不再是独立窗口，而是主窗口页面栈中的一页：应用全局只保留一个
原生窗口。登录成功后使用 HTTPS 认证 + WebSocket 自建房/排位；在线落子只由
服务器权威快照驱动，不在本地抢先更新。
"""

from __future__ import annotations

import logging
import time
from dataclasses import replace
from datetime import datetime
from typing import Mapping, Optional

from PySide6.QtCore import QSettings, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.achievement_widgets import (
    AchievementPanel,
    achievement_names,
)
from gomoku.client.audio import get_audio_manager
from gomoku.client.auth import AuthSession
from gomoku.client.avatar_images import (
    avatar_image_cache,
    configure_avatar_images,
)
from gomoku.client.avatar_widgets import avatar_icon
from gomoku.client.confirm_dialog import ConfirmDialog
from gomoku.client.friends_panel import FriendsPanel
from gomoku.client.history_records import record_from_game_detail
from gomoku.client.net.social_api import SocialHTTPClient
from gomoku.client.private_chat_panel import PrivateChatPanel
from gomoku.client.profile_panel import AvatarPickerPanel, ProfilePanel
from gomoku.client.report_panel import ReportPanel
from gomoku.client.ui_theme import ONLINE_BACKDROP_SPEC, InkBackdrop
from gomoku.client.config import AppConfig, resolve_endpoints
from gomoku.client.modal_overlay import NoticeOverlay
from gomoku.client.net.game_history_api import GameHistoryHTTPClient
from gomoku.client.net.report_api import ReportHTTPClient
from gomoku.client.net.http_api import AuthHTTPClient
from gomoku.client.net.profile_api import ProfileHTTPClient
from gomoku.client.net.websocket_transport import QtWebSocketTransport
from gomoku.client.online_pages import (
    build_auth_page,
    build_game_page,
    build_lobby_page,
)
from gomoku.client.online_pages.common import (
    CHAT_INPUT_HINT,
    online_victory_notice,
)
from gomoku.client.credential_store import (
    clear_login,
    clear_refresh_token,
    load_login,
    load_refresh_token,
    save_login,
    save_refresh_token,
)
from gomoku.client.online_player_card import OnlinePlayerCard
from gomoku.client.online_pages.plaza_page import PlazaPage
from gomoku.client.sessions.online import OnlineGameSession
from gomoku.client.sessions.online_state import OnlineSessionPhase
from gomoku.client.font_control import FontSizeButton
from gomoku.client.sound_control import SoundButton
from gomoku.shared.achievements import normalize_achievement_keys
from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.capabilities import CAP_ROOM_OPTIONS, CAP_TIME_CONTROL
from gomoku.shared.chat import (
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    ChatMessage,
)
from gomoku.shared.errors import TransportError
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.record import GameRecord, RecordError
from gomoku.shared.rules import BOARD_SIZE, Cell, Player
from gomoku.shared.social import DirectMessage, FriendEvent
from gomoku.shared.time_control import TimeControl

logger = logging.getLogger(__name__)

#: 对话日志展开状态在 QSettings 里的键名。
SETTINGS_CHAT_LOG_VISIBLE = "chat_log_visible"

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


def _format_game_time(value: object) -> str:
    if not isinstance(value, str) or not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:16]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%m-%d %H:%M")


def _result_label(value: object) -> str:
    if not isinstance(value, str):
        return "—"
    return {
        "win": "胜",
        "loss": "负",
        "draw": "和",
    }.get(value, "—")


def _format_clock_ms(value: Optional[int]) -> str:
    """把 Fischer 余量格式化为 M:SS（小时以上显示 H:MM:SS）。"""
    if value is None:
        return "—"
    total = max(0, int(value) // 1000)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _entry_avatar_version(entry: Mapping) -> int:
    """从资料字典里安全读取头像版本号。"""
    value = entry.get("avatar_version", 0)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0


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
        # 快照接收时刻，用于 Fischer 双方余量的本地平滑倒数。
        self._snapshot_monotonic: Optional[float] = None
        self._profile_user: Optional[dict] = None
        self._season_info: Optional[dict] = None
        self._season_rank: Optional[int] = None
        self._achievements_panel: Optional[AchievementPanel] = None
        self._leaderboard_entries: list = []
        self._achievement_notice_game_id = ""
        self._victory_notice_game_id = ""
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

        # 对战广场的兜底刷新：服务器会推送“对局列表变了”，但推送可能
        # 在断线期间丢失，定时兜底保证列表不会一直停在前一次的结果上。
        self._live_refresh_timer = QTimer(self)
        self._live_refresh_timer.setInterval(10_000)
        self._live_refresh_timer.timeout.connect(self._on_live_timer)

        self._build_ui()
        self._load_saved_login()
        self._show_auth_page()
        self._refresh_auth_availability()
        # 等界面构建完成后再尝试静默恢复登录，避免阻塞构造过程。
        QTimer.singleShot(0, self._try_restore_session)

    # ------------------------------------------------------------------
    # 地址
    # ------------------------------------------------------------------
    def _resolve_urls(self) -> tuple[str, str]:
        """解析 REST/WS 地址；开发环境默认本机 8000 端口。"""
        return resolve_endpoints(self.config)

    # ------------------------------------------------------------------
    # 页面构建
    # ------------------------------------------------------------------
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
    # 排位资料/排行榜
    # ------------------------------------------------------------------
    def _rating_source(self) -> Optional[dict]:
        if isinstance(self._profile_user, Mapping):
            return dict(self._profile_user)
        user = self._auth_session.user if self._auth_session else None
        if user is None:
            return None
        return {
            "rating": user.rating,
            "peak_rating": user.peak_rating,
            "rated_games": user.rated_games,
            "wins": user.wins,
            "losses": user.losses,
            "draws": user.draws,
            "rank_name": user.rank_name,
        }

    def _update_rating_summary(self) -> None:
        data = self._rating_source()
        if not data:
            self.rating_badge_label.setText("未定级")
            self.rating_summary_label.setText("登录后展示段位与积分")
            return

        rating = data.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool):
            rating = 1200
        rank_name = data.get("rank_name")
        self.rating_badge_label.setText(
            rank_name if isinstance(rank_name, str) and rank_name else "已定级"
        )

        rated_games = data.get("rated_games", 0)
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        draws = data.get("draws", 0)
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (rated_games, wins, losses, draws)
        ):
            rated_games = wins = losses = draws = 0
        record = (
            f"{wins}胜 {losses}负 {draws}平"
            if rated_games
            else "尚未完成排位赛"
        )
        summary = (
            f"积分 {rating}，最高 {data.get('peak_rating', rating)}，{record}"
            if rated_games
            else f"积分 {rating}，{record}"
        )
        season_name = ""
        days_remaining = None
        if isinstance(self._season_info, Mapping):
            raw_name = self._season_info.get("name")
            if isinstance(raw_name, str) and raw_name:
                season_name = raw_name
            raw_days = self._season_info.get("days_remaining")
            if isinstance(raw_days, int) and not isinstance(raw_days, bool):
                days_remaining = max(0, raw_days)
        if season_name:
            season_text = season_name
            if days_remaining is not None:
                season_text += f"（剩余 {days_remaining} 天）"
            if isinstance(self._season_rank, int) and not isinstance(
                self._season_rank, bool
            ):
                season_text += f" · 赛季排名 #{self._season_rank}"
            summary = f"{season_text}\n{summary}"
        self.rating_summary_label.setText(summary)

    def _refresh_profile_data(self) -> None:
        if self._auth_session is None or not self._api_base_url:
            return
        token = self._auth_session.access_token
        self._profile_client.fetch_profile(token)
        self._profile_client.fetch_leaderboard(token, 50)
        self._history_client.fetch_recent(token, 20)
        self._social_client.fetch_friends(token)

    def _on_refresh_leaderboard(self) -> None:
        if self._auth_session is None:
            return
        self._profile_client.fetch_leaderboard(
            self._auth_session.access_token, 50
        )

    def _on_profile_ready(self, data: object) -> None:
        if not isinstance(data, Mapping):
            return
        raw_user = data.get("user")
        if isinstance(raw_user, Mapping):
            self._profile_user = dict(raw_user)
            self._sync_auth_user_from_profile(raw_user)
        raw_season = data.get("season")
        self._season_info = (
            dict(raw_season) if isinstance(raw_season, Mapping) else None
        )
        raw_rank = data.get("season_rank")
        self._season_rank = (
            raw_rank
            if isinstance(raw_rank, int) and not isinstance(raw_rank, bool)
            else None
        )
        self._update_self_avatar()
        self._update_rating_summary()

    def _sync_auth_user_from_profile(self, raw_user: Mapping) -> None:
        """把资料接口的最新头像/成就/战绩同步进登录会话。"""
        if self._auth_session is None:
            return
        current = self._auth_session.user
        avatar = raw_user.get("avatar")
        if not isinstance(avatar, str) or not avatar:
            avatar = current.avatar
        achievements = normalize_achievement_keys(
            raw_user.get("achievements")
        )
        if not achievements:
            achievements = current.achievement_keys

        def int_value(key: str, fallback: int) -> int:
            value = raw_user.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return fallback

        user = replace(
            current,
            avatar=normalize_avatar(avatar, current.id),
            avatar_version=_entry_avatar_version(raw_user),
            rating=int_value("rating", current.rating),
            peak_rating=int_value("peak_rating", current.peak_rating),
            rated_games=int_value(
                "rated_games", current.rated_games
            ),
            wins=int_value("wins", current.wins),
            losses=int_value("losses", current.losses),
            draws=int_value("draws", current.draws),
            current_win_streak=int_value(
                "current_win_streak", current.current_win_streak
            ),
            best_win_streak=int_value(
                "best_win_streak", current.best_win_streak
            ),
            achievements=achievements,
        )
        self._auth_session = replace(self._auth_session, user=user)

    def _on_leaderboard_ready(self, data: object) -> None:
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        total = data.get("total", len(entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(entries)
        self._fill_leaderboard(entries, total)

    def _fill_leaderboard(self, entries: list, total: int) -> None:
        table = self.leaderboard_table
        valid = [entry for entry in entries if isinstance(entry, Mapping)]
        # 行号必须和 _leaderboard_entries 一一对应，否则点击第 i 行打开的
        # 是另一个人。
        self._leaderboard_entries = valid
        table.setRowCount(len(valid))
        own_id = (
            self._auth_session.user.id if self._auth_session else ""
        )
        for row, entry in enumerate(valid):
            username = entry.get("username")
            username_text = (
                username if isinstance(username, str) else "未知玩家"
            )
            if entry.get("user_id") == own_id:
                username_text += "（你）"
            values = (
                str(entry.get("position", row + 1)),
                username_text,
                str(entry.get("rank_name", "")),
                str(entry.get("rating", "")),
                (
                    f"{entry.get('wins', 0)}-"
                    f"{entry.get('losses', 0)}-"
                    f"{entry.get('draws', 0)}"
                ),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(
                    item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if column == 1:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter
                        | Qt.AlignmentFlag.AlignLeft
                    )
                    item.setIcon(
                        avatar_icon(
                            entry.get("avatar"),
                            str(entry.get("user_id", "")),
                            20,
                            avatar_version=_entry_avatar_version(entry),
                        )
                    )
                    item.setToolTip("点击玩家查看个人信息")
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, item)

        if valid:
            self.leaderboard_status_label.setText(
                f"共 {total} 名玩家完成过排位赛"
            )
        else:
            self.leaderboard_status_label.setText(
                "还没有玩家完成排位赛，成为第一个上榜的人吧"
            )

    def _on_avatar_image_loaded(self, user_id: str, version: int) -> None:
        """头像图片异步到货后，刷新排行榜里对应的图标。"""
        for row, entry in enumerate(self._leaderboard_entries):
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("user_id", "")) != user_id:
                continue
            item = self.leaderboard_table.item(row, 1)
            if item is not None:
                item.setIcon(
                    avatar_icon(
                        entry.get("avatar"),
                        user_id,
                        20,
                        avatar_version=version,
                    )
                )

    def _on_profile_failed(self, message: str) -> None:
        logger.warning("读取排位资料失败：%s", message)
        if (
            self.leaderboard_table.rowCount() == 0
            and self.stack.currentWidget() is self.lobby_page
        ):
            self.leaderboard_status_label.setText(
                message or "无法加载排行榜"
            )

    def _on_profile_unauthorized(self) -> None:
        if self._auth_session is None or self._auth_client.busy:
            return
        self._refresh_after_auth_refresh = True
        if not self._auth_client.refresh():
            self._refresh_after_auth_refresh = False
            self._force_relogin("登录已过期，请重新登录")

    def _on_profile_busy_changed(self, _busy: bool) -> None:
        if hasattr(self, "refresh_leaderboard_button"):
            self._refresh_lobby_controls()

    def _on_leaderboard_cell_clicked(self, row: int, column: int) -> None:
        """点击排行榜玩家列打开个人信息。"""
        if column != 1:
            return
        if 0 <= row < len(self._leaderboard_entries):
            entry = self._leaderboard_entries[row]
            if isinstance(entry, Mapping):
                user_id = entry.get("user_id")
                if isinstance(user_id, str) and user_id:
                    self._open_user_profile(user_id)

    # ------------------------------------------------------------------
    # 个人信息/头像
    # ------------------------------------------------------------------
    def _update_self_avatar(self) -> None:
        if self._auth_session is None:
            return
        user = self._auth_session.user
        self.self_avatar.set_avatar(
            user.avatar, user.id, user.avatar_version
        )
        self.self_avatar.setVisible(True)

    def _self_profile(self) -> dict:
        user = dict(self._profile_user or {})
        if self._auth_session is not None:
            auth_user = self._auth_session.user
            user.setdefault("id", auth_user.id)
            user.setdefault("username", auth_user.username)
            user.setdefault("avatar", auth_user.avatar)
            user.setdefault("avatar_version", auth_user.avatar_version)
            user.setdefault("rating", auth_user.rating)
            user.setdefault("peak_rating", auth_user.peak_rating)
            user.setdefault("rated_games", auth_user.rated_games)
            user.setdefault("wins", auth_user.wins)
            user.setdefault("losses", auth_user.losses)
            user.setdefault("draws", auth_user.draws)
            user.setdefault("rank_name", auth_user.rank_name)
            user.setdefault("win_rate", auth_user.win_rate)
            user.setdefault(
                "current_win_streak", auth_user.current_win_streak
            )
            user.setdefault(
                "best_win_streak", auth_user.best_win_streak
            )
            user.setdefault(
                "achievements", list(auth_user.achievement_keys)
            )
        return {"user": user, "online": True, "friendship": "self"}

    def _open_user_profile(self, user_id: str) -> None:
        if not user_id or self._auth_session is None:
            return
        self._pending_profile_user_id = user_id
        if self._auth_session.user.id == user_id:
            self._show_profile_panel(self._self_profile())
            return
        self._social_client.fetch_user(
            self._auth_session.access_token, user_id
        )

    def _show_profile_panel(self, profile: object) -> None:
        if not isinstance(profile, Mapping):
            return
        self._current_profile = dict(profile)
        if self._profile_panel is None:
            window = ProfilePanel(self)
            window.add_friend_requested.connect(
                self._send_friend_request
            )
            window.message_requested.connect(
                self._open_private_chat
            )
            window.remove_friend_requested.connect(self._remove_friend)
            window.avatar_requested.connect(self._open_avatar_picker)
            window.open_friends_requested.connect(
                self._open_friends_panel
            )
            window.achievements_requested.connect(
                self._open_achievements_panel
            )
            window.destroyed.connect(
                lambda *_: setattr(self, "_profile_panel", None)
            )
            self._profile_panel = window
        self._profile_panel.set_profile(profile)
        self._profile_panel.show()

    def _open_achievements_panel(self) -> None:
        """打开当前资料对应玩家的完整成就墙。"""
        profile = getattr(self, "_current_profile", None)
        if not isinstance(profile, Mapping):
            return
        user = profile.get("user")
        if not isinstance(user, Mapping):
            return
        if self._achievements_panel is not None:
            self._achievements_panel.close()
            self._achievements_panel.deleteLater()
            self._achievements_panel = None
        panel = AchievementPanel(user, self)
        panel.destroyed.connect(
            lambda *_: setattr(self, "_achievements_panel", None)
        )
        self._achievements_panel = panel
        panel.show()

    def _open_avatar_picker(self) -> None:
        if self._auth_session is None:
            return
        picker = AvatarPickerPanel(
            self._auth_session.user.avatar,
            self,
            current_version=self._auth_session.user.avatar_version,
        )
        picker.avatar_selected.connect(self._on_avatar_selected)
        picker.image_selected.connect(self._on_avatar_image_selected)
        picker.exec()

    def _on_avatar_selected(self, avatar: str) -> None:
        if self._auth_session is None:
            return
        self._social_client.update_avatar(
            self._auth_session.access_token, avatar
        )

    def _on_avatar_image_selected(self, data_url: str) -> None:
        if self._auth_session is None or not data_url:
            return
        self._show_status("正在上传头像…")
        self._social_client.update_avatar_image(
            self._auth_session.access_token, data_url
        )

    def _apply_avatar_update(self, data: object) -> None:
        if not isinstance(data, Mapping) or self._auth_session is None:
            return
        avatar = data.get("avatar")
        if not isinstance(avatar, str):
            return
        avatar_version = _entry_avatar_version(data)
        user = replace(
            self._auth_session.user,
            avatar=normalize_avatar(
                avatar, self._auth_session.user.id
            ),
            avatar_version=avatar_version,
        )
        self._auth_session = replace(self._auth_session, user=user)
        avatar_image_cache().invalidate(user.id)
        if self._profile_user is not None:
            self._profile_user["avatar"] = user.avatar
            self._profile_user["avatar_version"] = user.avatar_version
        self._update_self_avatar()
        if (
            self._profile_panel is not None
            and self._profile_panel.user_id == user.id
        ):
            # 资料浮层此刻显示的不是自己时，不能把本人的头像盖上去。
            self._profile_panel.set_avatar(
                user.avatar, user.avatar_version
            )
        self._show_status("头像已更新")

    # ------------------------------------------------------------------
    # 好友
    # ------------------------------------------------------------------
    def _refresh_friends(self) -> None:
        if self._auth_session is None:
            return
        self._social_client.fetch_friends(
            self._auth_session.access_token
        )

    def _update_friends_badge(self) -> None:
        incoming = self._friends_data.get("incoming", [])
        count = len(incoming) if isinstance(incoming, list) else 0
        self.friends_button.setText(
            f"好友 ({count})" if count else "好友"
        )

    def _open_friends_panel(self, invite_mode: bool = False) -> None:
        if self._auth_session is None:
            return
        if self._friends_panel is not None:
            self._friends_panel.set_invite_mode(invite_mode)
            self._friends_panel.show()
            return
        window = FriendsPanel(self, invite_mode=invite_mode)
        window.profile_requested.connect(self._open_user_profile)
        window.chat_requested.connect(self._open_private_chat)
        window.invite_requested.connect(self._invite_friend)
        window.add_requested.connect(self._send_friend_request)
        window.accept_requested.connect(self._accept_friend_request)
        window.decline_requested.connect(self._decline_friend_request)
        window.remove_requested.connect(self._remove_friend)
        window.search_requested.connect(self._search_users)
        window.refresh_requested.connect(self._refresh_friends)
        window.destroyed.connect(
            lambda *_: setattr(self, "_friends_panel", None)
        )
        self._friends_panel = window
        if self._friends_data:
            window.set_data(self._friends_data)
        window.show()
        self._refresh_friends()

    def _send_friend_request(self, user_id: str) -> None:
        if self._auth_session is None or not user_id:
            return
        self._social_client.send_friend_request(
            self._auth_session.access_token, user_id
        )
        self._show_status("正在发送好友申请…")

    def _accept_friend_request(self, request_id: str) -> None:
        if self._auth_session is None or not request_id:
            return
        self._social_client.accept_friend_request(
            self._auth_session.access_token, request_id
        )

    def _decline_friend_request(self, request_id: str) -> None:
        if self._auth_session is None or not request_id:
            return
        self._social_client.decline_friend_request(
            self._auth_session.access_token, request_id
        )

    def _remove_friend(self, user_id: str) -> None:
        if self._auth_session is None or not user_id:
            return
        username, _avatar, _version = self._username_avatar(user_id)
        dialog = ConfirmDialog(
            "删除好友",
            f"确定删除好友「{username}」吗？",
            confirm_text="删除",
            cancel_text="取消",
            seal_text="友",
            parent=self,
        )
        dialog.exec()
        if not dialog.confirm_requested:
            # 取消删除：这一行不能锁，聊天和邀请对局还得能点。
            return
        # 确认之后再锁行。锁早了的话，用户一取消，这一行就再也点不动。
        self._pending_removal_user_id = user_id
        if self._friends_panel is not None:
            self._friends_panel.lock_friend_row(user_id)
        self._social_client.remove_friend(
            self._auth_session.access_token, user_id
        )

    def _search_users(self, query: str) -> None:
        if self._auth_session is None:
            return
        if self._friends_panel is not None:
            self._friends_panel.set_status("正在搜索…")
        self._social_client.search_users(
            self._auth_session.access_token, query
        )

    def _refresh_open_profile(self) -> None:
        if (
            self._profile_panel is not None
            and self._pending_profile_user_id
            and self._auth_session is not None
        ):
            self._social_client.fetch_user(
                self._auth_session.access_token,
                self._pending_profile_user_id,
            )

    def _username_avatar(
        self, user_id: str, fallback: str = ""
    ) -> tuple[str, str, int]:
        current = getattr(self, "_current_profile", None)
        if isinstance(current, Mapping):
            user = current.get("user")
            if isinstance(user, Mapping) and user.get("id") == user_id:
                return (
                    str(user.get("username", fallback or "好友")),
                    str(user.get("avatar", "")),
                    _entry_avatar_version(user),
                )
        for section in ("friends", "incoming", "outgoing"):
            entries = self._friends_data.get(section, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                entry_id = entry.get("user_id") or entry.get("id")
                if entry_id == user_id:
                    return (
                        str(entry.get("username", fallback or "好友")),
                        str(entry.get("avatar", "")),
                        _entry_avatar_version(entry),
                    )
        return fallback or "好友", "", 0

    # ------------------------------------------------------------------
    # 私聊
    # ------------------------------------------------------------------
    def _open_private_chat(
        self, user_id: str, username: str = ""
    ) -> None:
        if not user_id:
            return
        resolved_name, avatar, avatar_version = self._username_avatar(
            user_id, username
        )
        existing = self._chat_panels.get(user_id)
        if existing is not None:
            # 聊天窗可以被关闭，重新点“聊天”时把旧窗拉回来：刷新一下
            # 好友资料，再补拉一次历史（关闭期间对方可能发过消息）。
            existing.set_friend(
                resolved_name, avatar, avatar_version=avatar_version
            )
            existing.show()
            if self._auth_session is not None:
                self._social_client.fetch_messages(
                    self._auth_session.access_token, user_id, limit=200
                )
            return
        window = PrivateChatPanel(
            user_id, resolved_name, avatar, self, avatar_version
        )
        window.send_requested.connect(
            lambda text, uid=user_id: self._send_private_message(
                uid, text
            )
        )
        window.destroyed.connect(
            lambda *_: self._chat_panels.pop(user_id, None)
        )
        self._chat_panels[user_id] = window
        window.set_send_enabled(True)
        window.show()
        if self._auth_session is not None:
            self._social_client.fetch_messages(
                self._auth_session.access_token, user_id, limit=200
            )

    def _send_private_message(self, user_id: str, text: str) -> None:
        if self._auth_session is None or not text.strip():
            return
        window = self._chat_panels.get(user_id)
        if window is not None:
            window.set_status("发送中…")
        self._social_client.send_message(
            self._auth_session.access_token, user_id, text
        )

    def _on_direct_message(self, message: object) -> None:
        if not isinstance(message, DirectMessage):
            return
        payload = {
            "id": message.id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "sender_avatar": message.sender_avatar,
            "sender_avatar_version": message.sender_avatar_version,
            "content": message.content,
            "sent_at_ms": message.sent_at_ms,
        }
        self_id = (
            self._auth_session.user.id if self._auth_session else ""
        )
        window = self._chat_panels.get(message.sender_id)
        if window is not None:
            window.append_message(payload, self_id)
            window.set_status("")
        if window is not None and window.isVisible():
            # 聊天窗正开着，这条消息当场就读完了：让服务端销账，
            # 免得好友列表里的未读数一直挂着。
            self._report_messages_read(message.sender_id, window)
        else:
            # 没有聊天窗，或者窗被关掉了：这条消息算未读，好友列表
            # 上要冒出未读标记来。
            self._show_status(f"收到 {message.sender_name} 的新消息")
            self._refresh_friends()

    def _report_messages_read(
        self, user_id: str, window: PrivateChatPanel
    ) -> None:
        """把窗口里已读的消息报给服务端，并清掉本地未读数。"""
        message_ids = window.pending_read_ids()
        if message_ids:
            window.mark_reported(message_ids)
            if self._auth_session is not None:
                self._social_client.mark_messages_read(
                    self._auth_session.access_token, user_id, message_ids
                )
        self._clear_friend_unread(user_id)

    def _clear_friend_unread(self, user_id: str) -> None:
        """把某位好友的未读数清零。

        先在本地改一次，好友列表立刻不再显示未读；再向服务端要一次
        好友列表校准——本地只剩“已经显示过”的消息才算读完，服务端
        才知道最终数字。
        """
        friends = self._friends_data.get("friends", [])
        changed = False
        if isinstance(friends, list):
            for entry in friends:
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("user_id", "")) != user_id:
                    continue
                if entry.get("unread"):
                    entry["unread"] = 0
                    changed = True
        if changed and self._friends_panel is not None:
            self._friends_panel.set_data(self._friends_data)
        self._refresh_friends()

    # ------------------------------------------------------------------
    # 好友事件/自建房邀请
    # ------------------------------------------------------------------
    def _on_friend_event(self, event: object) -> None:
        if not isinstance(event, FriendEvent):
            return
        if event.event == "request":
            self._show_status(f"{event.username} 想加你为好友")
            if (
                self.stack.currentWidget() is self.lobby_page
                and event.request_id
            ):
                dialog = ConfirmDialog(
                    "好友申请",
                    f"{event.username} 想加你为好友，是否接受？",
                    confirm_text="接受",
                    cancel_text="稍后处理",
                    seal_text="友",
                    parent=self,
                )
                dialog.exec()
                if dialog.confirm_requested:
                    self._accept_friend_request(event.request_id)
        elif event.event == "accepted":
            self._show_status(f"你和 {event.username} 已成为好友")
        elif event.event == "removed":
            self._show_status(f"{event.username} 已从好友列表中移除")
        elif event.event == "declined":
            self._show_status(f"{event.username} 拒绝了你的好友申请")
        self._refresh_friends()

    def _on_room_invited(self, payload: object) -> None:
        if (
            self._session is None
            or not isinstance(payload, Mapping)
        ):
            return
        invite_id = payload.get("invite_id")
        inviter = payload.get("inviter")
        if not isinstance(invite_id, str) or not isinstance(
            inviter, Mapping
        ):
            return
        name = str(inviter.get("username", "好友"))
        if self._session.in_room:
            self._session.respond_room_invite(invite_id, False)
            self._show_status(
                "你正在对局中，已拒绝好友邀请", error=True
            )
            return
        dialog = ConfirmDialog(
            "对局邀请",
            f"{name} 邀请你进行一局自建房对局，是否接受？",
            confirm_text="接受",
            cancel_text="拒绝",
            seal_text="邀",
            parent=self,
        )
        dialog.exec()
        accept = dialog.confirm_requested
        self._session.respond_room_invite(invite_id, accept)
        self._show_status(
            "已接受邀请，正在进入房间…"
            if accept
            else "已拒绝好友邀请"
        )

    def _on_room_invite_result(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        status = payload.get("status")
        user = payload.get("user")
        name = (
            str(user.get("username", "好友"))
            if isinstance(user, Mapping)
            else "好友"
        )
        reason = str(payload.get("reason", ""))
        if status == "sent":
            self._show_status(f"已向 {name} 发送对局邀请")
        elif status == "accepted":
            self._show_status(f"{name} 已接受邀请，正在开始对局")
        elif status == "declined":
            self._show_status(f"{name} 拒绝了邀请", error=True)
        elif status == "cancelled":
            self._show_status(
                f"对局邀请已失效{('：' + reason) if reason else ''}",
                error=True,
            )

    def _on_invite_friend_clicked(self) -> None:
        self._open_friends_panel(invite_mode=True)

    def _invite_friend(self, user_id: str, username: str) -> None:
        if self._session is None or not user_id:
            return
        if not self._session.waiting_for_opponent:
            self._show_status("请先创建自建房", error=True)
            return
        if self._session.invite_to_room(user_id):
            self._show_status(f"已向 {username} 发送对局邀请")

    # ------------------------------------------------------------------
    # 社交响应
    # ------------------------------------------------------------------
    def _on_social_ready(self, kind: str, data: object) -> None:
        if not isinstance(kind, str):
            return
        if kind.startswith("user:"):
            requested_id = kind.split(":", 1)[1]
            if requested_id != self._pending_profile_user_id:
                # 已经切到别的用户了，过期响应直接丢掉。
                return
            if isinstance(data, Mapping):
                user = data.get("user")
                if isinstance(user, Mapping):
                    returned_id = str(user.get("id", ""))
                    if returned_id and returned_id != requested_id:
                        return
            self._show_profile_panel(data)
            return
        if kind == "avatar":
            self._apply_avatar_update(data)
            return
        if kind == "search":
            entries = (
                data.get("entries", [])
                if isinstance(data, Mapping)
                else []
            )
            if self._friends_panel is not None:
                self._friends_panel.set_search_results(entries)
                self._friends_panel.set_status(
                    f"找到 {len(entries)} 位玩家"
                )
            return
        if kind == "friends":
            if isinstance(data, Mapping):
                self._friends_data = dict(data)
            self._update_friends_badge()
            if self._friends_panel is not None:
                self._friends_panel.set_data(self._friends_data)
            return
        if kind == "friend_request":
            self._show_status("好友申请已发送")
            self._refresh_friends()
            self._refresh_open_profile()
            return
        if kind == "friend_accept":
            self._show_status("已添加好友")
            self._refresh_friends()
            self._refresh_open_profile()
            return
        if kind in ("friend_decline", "friend_remove"):
            self._pending_removal_user_id = ""
            self._refresh_friends()
            self._refresh_open_profile()
            return
        if kind.startswith("messages:"):
            user_id = kind.split(":", 1)[1]
            window = self._chat_panels.get(user_id)
            if window is None:
                return
            messages = (
                data.get("messages", [])
                if isinstance(data, Mapping)
                else []
            )
            self_id = (
                self._auth_session.user.id
                if self._auth_session
                else ""
            )
            window.set_history(messages, self_id)
            window.set_status("")
            window.set_send_enabled(True)
            # 历史接口已经把返回的消息标成已读，好友列表上的未读数要
            # 跟着归零，否则会出现"消息读完了还显示未读"。
            if window.isVisible():
                # 实时推送先到、还没被历史覆盖的那几条，仍然要销账。
                self._report_messages_read(user_id, window)
            else:
                self._clear_friend_unread(user_id)
            return
        if kind.startswith("messages_read:"):
            # 销账成功：好友列表的未读数已经本地清零，这里等下一次
            # 好友列表刷新校准即可，不必再发一次请求。
            return
        if kind.startswith("message_send:"):
            user_id = kind.split(":", 1)[1]
            window = self._chat_panels.get(user_id)
            if window is not None and isinstance(data, Mapping):
                self_id = (
                    self._auth_session.user.id
                    if self._auth_session
                    else ""
                )
                window.append_message(data, self_id)
                window.clear_draft()
                window.set_status("")

    def _on_social_failed(self, kind: str, message: str) -> None:
        logger.warning("社交请求失败 kind=%s：%s", kind, message)
        if kind.startswith("messages_read:"):
            # 销账请求是后台动作，失败了下次刷新好友列表自然会把
            # 未读数补回来，不用拿红色报错打扰用户。
            return
        if kind == "friend_remove":
            # 删除失败：对方仍是好友，把锁住的那一行解开。
            pending = self._pending_removal_user_id
            self._pending_removal_user_id = ""
            if self._friends_panel is not None and pending:
                self._friends_panel.unlock_friend_row(pending)
        elif kind in ("friend_accept", "friend_decline"):
            # 接受/拒绝失败：申请还在列表里，重新拉一次把行按钮解锁。
            self._refresh_friends()
        if kind.startswith("messages:") or kind.startswith(
            "message_send:"
        ):
            user_id = kind.split(":", 1)[1]
            window = self._chat_panels.get(user_id)
            if window is not None:
                window.set_status(message or "发送失败")
                if kind.startswith("message_send:"):
                    # 发送失败：把文本放回输入框，别让用户白打一遍。
                    window.restore_draft()
                return
        if self._friends_panel is not None and kind in (
            "friends",
            "friend_request",
            "friend_accept",
            "friend_decline",
            "friend_remove",
            "search",
        ):
            self._friends_panel.set_status(message)
        self._show_status(message or "操作失败", error=True)

    def _on_social_unauthorized(self, _kind: str) -> None:
        if self._auth_session is None or self._auth_client.busy:
            return
        self._refresh_after_auth_refresh = True
        if not self._auth_client.refresh():
            self._refresh_after_auth_refresh = False
            self._force_relogin("登录已过期，请重新登录")

    def _show_status(self, text: str, error: bool = False) -> None:
        if self.stack.currentWidget() is self.game_page:
            if error:
                self.game_error_label.setText(text)
            else:
                # 普通提示别塞进红色的错误标签里。
                self.game_error_label.setText("")
                self._set_lobby_status(text)
        else:
            self._set_lobby_status(text, error=error)

    def _close_social_windows(self) -> None:
        for panel in (
            self._friends_panel,
            self._profile_panel,
            self._achievements_panel,
        ):
            if panel is not None:
                try:
                    panel.close()
                    panel.deleteLater()
                except RuntimeError:
                    pass
        for panel in list(self._chat_panels.values()):
            try:
                panel.close()
                panel.deleteLater()
            except RuntimeError:
                pass
        self._friends_panel = None
        self._profile_panel = None
        self._achievements_panel = None
        self._chat_panels.clear()
        self._friends_data = {}
        self._pending_profile_user_id = ""

    # ------------------------------------------------------------------
    # 最近对局/复盘
    # ------------------------------------------------------------------
    def _on_refresh_history(self) -> None:
        if self._auth_session is None:
            return
        self._history_client.fetch_recent(
            self._auth_session.access_token, 20
        )

    def _on_history_recent_ready(self, data: object) -> None:
        if self._auth_session is None or self._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        self._recent_games_entries = [
            entry for entry in entries if isinstance(entry, Mapping)
        ]
        total = data.get("total", len(self._recent_games_entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(self._recent_games_entries)
        self._fill_recent_games(self._recent_games_entries, total)
        self._refresh_lobby_controls()

    def _fill_recent_games(self, entries: list, total: int) -> None:
        table = self.recent_games_table
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                _format_game_time(entry.get("ended_at")),
                str(entry.get("opponent_username", "对手")),
                _result_label(entry.get("result")),
                "排位" if entry.get("rated") else "自建房",
                str(entry.get("move_no", 0)),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(
                    item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if column == 1:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter
                        | Qt.AlignmentFlag.AlignLeft
                    )
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, item)

        if entries:
            self.history_status_label.setText(
                f"共 {total} 局已完成对局，双击可复盘"
            )
        else:
            self.history_status_label.setText(
                "暂无已完成对局，完成一局排位或自建房后会显示在这里"
            )
        self.replay_history_button.setEnabled(False)

    def _on_history_game_ready(self, data: object) -> None:
        if self._auth_session is None or self._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        try:
            record = record_from_game_detail(data)
        except (RecordError, TypeError, ValueError) as exc:
            logger.warning("棋谱数据无效：%s", exc)
            self.history_status_label.setText("该局棋谱无法复盘")
            return
        self._open_replay(record)

    def _open_replay(self, record: GameRecord) -> None:
        """请求主窗口打开复盘页；复盘面板由主窗口统一管理。"""
        self.replay_requested.emit([record])

    def _on_history_failed(self, message: str) -> None:
        logger.warning("读取最近对局失败：%s", message)
        if self.stack.currentWidget() is self.lobby_page:
            self.history_status_label.setText(
                message or "无法加载最近对局"
            )
        self._refresh_lobby_controls()

    def _on_history_unauthorized(self) -> None:
        if self._auth_session is None or self._auth_client.busy:
            return
        self._refresh_after_auth_refresh = True
        if not self._auth_client.refresh():
            self._refresh_after_auth_refresh = False
            self._force_relogin("登录已过期，请重新登录")

    def _on_history_busy_changed(self, _busy: bool) -> None:
        if hasattr(self, "refresh_history_button"):
            self._refresh_lobby_controls()

    def _on_recent_selection_changed(self) -> None:
        self._refresh_lobby_controls()

    def _on_recent_double_clicked(self, row: int, _column: int) -> None:
        self._request_replay_row(row)

    def _on_replay_history(self) -> None:
        self._request_replay_row(self.recent_games_table.currentRow())

    def _request_replay_row(self, row: int) -> None:
        if self._auth_session is None:
            return
        if row < 0 or row >= len(self._recent_games_entries):
            return
        entry = self._recent_games_entries[row]
        game_id = entry.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            return
        self.history_status_label.setText("正在加载棋谱…")
        self._history_client.fetch_game(
            self._auth_session.access_token, game_id
        )

    # ------------------------------------------------------------------
    # 对战广场：进行中的对局列表
    # ------------------------------------------------------------------
    def _fetch_live_games(self) -> None:
        """拉取进行中的对局列表（对战广场）。

        只在广场页可见时拉取：其它页面要这个列表没有意义，白白发请求。
        """
        if self._auth_session is None or self._session is None:
            return
        if self.stack.currentWidget() is not self.plaza_page:
            return
        if self._live_client.busy:
            return
        self._live_client.fetch_live(self._auth_session.access_token)

    def _on_refresh_live(self) -> None:
        """手动刷新进行中的对局列表。"""
        self._fetch_live_games()

    def _on_live_timer(self) -> None:
        """兜底刷新：推送可能因为断线丢失，定时补一次。"""
        self._fetch_live_games()

    def _on_live_games_changed(self) -> None:
        """服务器推送“对局列表变了”：有新对局开始或已有对局结束。"""
        if self.stack.currentWidget() is not self.plaza_page:
            return
        self._fetch_live_games()

    def _on_live_ready(self, data: object) -> None:
        """收到进行中的对局列表并渲染到对战广场。"""
        if self._auth_session is None or self._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        self._live_games_entries = [
            entry for entry in entries if isinstance(entry, Mapping)
        ]
        total = data.get("total", len(self._live_games_entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(self._live_games_entries)
        self._fill_live_games(self._live_games_entries, total)
        self._refresh_plaza_controls()

    def _on_live_failed(self, message: str) -> None:
        """读取进行中对局失败：提示在广场页，不影响其它区块。"""
        logger.warning("读取进行中对局失败：%s", message)
        if self.stack.currentWidget() is self.plaza_page:
            self.live_status_label.setText(
                message or "无法加载进行中的对局"
            )
        self._refresh_plaza_controls()

    def _fill_live_games(self, entries: list, total: int) -> None:
        """把进行中对局列表交给广场页渲染。"""
        self.plaza_page.set_entries(entries, total)

    def _on_watch_game(self, game_id: str) -> None:
        """开始观战指定的进行中对局。"""
        session = self._session
        if session is None or not game_id:
            return
        if session.in_room or session.is_spectator:
            self.plaza_status_label.setText(
                "你已经在对局中，无法开始观战"
            )
            return
        if session.watch_game(game_id):
            # 真正的切换由服务器下发的观战快照触发（_sync_online_ui）。
            self._watching_from_plaza = True
            self.plaza_status_label.setText("正在进入观战…")

    def _clear_profile_ui(self) -> None:
        self._profile_user = None
        self._season_info = None
        self._season_rank = None
        self._leaderboard_entries = []
        self.leaderboard_table.setRowCount(0)
        self.leaderboard_status_label.setText("登录后自动加载排行榜")
        self._update_rating_summary()
        self._recent_games_entries = []
        self._live_games_entries = []
        if hasattr(self, "live_games_layout"):
            self._fill_live_games([], 0)
        if hasattr(self, "recent_games_table"):
            self.recent_games_table.setRowCount(0)
            self.history_status_label.setText(
                "登录后自动加载最近对局"
            )
            self.replay_history_button.setEnabled(False)

    # ------------------------------------------------------------------
    # 登录/注册/登出
    # ------------------------------------------------------------------
    def _on_login_clicked(self) -> None:
        if self._auth_client.busy or not self._api_base_url:
            return
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self._set_auth_status("请输入用户名和密码", error=True)
            return
        self._set_auth_status("正在登录…")
        self._set_auth_form_busy(True)
        if not self._auth_client.login(username, password):
            self._set_auth_status("无法发起登录请求", error=True)
            self._set_auth_form_busy(False)

    def _on_register_clicked(self) -> None:
        if self._auth_client.busy or not self._api_base_url:
            return
        username = self.username_edit.text().strip()
        password = self.password_edit.text()
        if not username or not password:
            self._set_auth_status("注册需要用户名和密码", error=True)
            return
        self._set_auth_status("正在注册…")
        self._set_auth_form_busy(True)
        if not self._auth_client.register(username, password):
            self._set_auth_status("无法发起注册请求", error=True)
            self._set_auth_form_busy(False)

    def _load_saved_login(self) -> None:
        """启动时恢复“记住密码”保存的账号与密码。"""
        remember, username, password = load_login(self._settings)
        if username:
            self.username_edit.setText(username)
        if password:
            self.password_edit.setText(password)
        self.remember_password_checkbox.setChecked(remember)

    def _try_restore_session(self) -> None:
        """启动时用已保存的 refresh token 静默恢复登录。"""
        if (
            self._closing
            or self._restoring_session
            or self._auth_session is not None
            or not self.config.online_configured
            or not self.remember_password_checkbox.isChecked()
        ):
            return
        token = load_refresh_token(self._settings)
        if not token:
            return
        self._restoring_session = True
        self._set_auth_status("正在恢复登录…")
        self._set_auth_form_busy(True)
        if not self._auth_client.refresh(token):
            if self._restoring_session:
                self._restoring_session = False
                self._set_auth_form_busy(False)
                self._set_auth_status("")

    def _save_refresh_token(self, token: str) -> None:
        """按“记住密码”状态保存或清除 refresh token。"""
        if not self.remember_password_checkbox.isChecked():
            clear_refresh_token(self._settings)
            return
        if not save_refresh_token(self._settings, token):
            logger.warning(
                "refresh token 无法通过 DPAPI 安全保存，"
                "下次启动需要重新登录"
            )

    def _restore_saved_login(self) -> None:
        """回到登录页时重新填入已保存的密码。

        登录成功后密码框会被清空；不重新填入的话，用户退出登录或被强制
        重新登录后还得再输一遍密码，尽管它已经安全地存在本机。
        """
        remember, _username, password = load_login(self._settings)
        if not remember or not password:
            return
        if not self.password_edit.text():
            self.password_edit.setText(password)

    def _save_saved_login(self, username: str) -> str:
        """按复选框状态保存或清除登录凭据。

        返回一条给用户看的提示（没有则返回空串）：密码没能安全加密时
        不能让界面继续显示“已记住”，否则用户会以为密码已经存好。
        """
        if not self.remember_password_checkbox.isChecked():
            clear_login(self._settings)
            return ""
        if not username:
            return ""
        if not save_login(
            self._settings, username, self.password_edit.text()
        ):
            logger.warning(
                "登录密码无法通过 DPAPI 安全保存，本次仅记住用户名"
            )
            return "当前环境无法安全保存密码，本次仅记住用户名"
        return ""

    def _on_remember_password_toggled(self, checked: bool) -> None:
        """取消勾选时立即删除已保存的凭据。"""
        if not checked:
            clear_login(self._settings)

    def _set_auth_form_busy(self, busy: bool) -> None:
        enabled = not busy and self.stack.currentWidget() is self.auth_page
        for widget in (
            self.username_edit,
            self.password_edit,
            self.remember_password_checkbox,
            self.login_button,
            self.register_button,
        ):
            widget.setEnabled(enabled)

    def _on_auth_busy_changed(self, busy: bool) -> None:
        if busy:
            self._set_auth_form_busy(True)
        elif self.stack.currentWidget() is self.auth_page:
            self._set_auth_form_busy(False)

    def _on_auth_session_ready(
        self, session: AuthSession, *, restored: bool = False
    ) -> None:
        self._auth_session = session
        self._logging_out = False
        self._reconnect_attempts = 0
        # 密码编辑框马上就要清空；先按用户的选择保存凭据。
        warning = self._save_saved_login(session.user.username)
        self._save_refresh_token(session.refresh_token)
        self.password_edit.clear()
        self._clear_profile_ui()
        status = (
            "已恢复登录，正在连接游戏服务器…"
            if restored
            else "登录成功，正在连接游戏服务器…"
        )
        if warning:
            status = f"{status}（{warning}）"
        self._set_auth_status(status)
        self._set_auth_form_busy(True)
        self._start_ws_session()

    def _on_auth_session_refreshed(self, session: AuthSession) -> None:
        self._auth_session = session
        if self._restoring_session:
            self._restoring_session = False
            # _on_auth_session_ready 会保存凭据与轮换后的 token。
            self._on_auth_session_ready(session, restored=True)
            return
        # 服务端每次刷新都会轮换 refresh token，必须同步保存新值。
        self._save_refresh_token(session.refresh_token)
        if self._reconnect_after_refresh:
            self._reconnect_after_refresh = False
            self._open_transport(reset_status=False)
        if self._refresh_after_auth_refresh:
            self._refresh_after_auth_refresh = False
            self._refresh_profile_data()
        self._update_rating_summary()

    def _on_http_failed(self, message: str) -> None:
        if self._restoring_session:
            self._restoring_session = False
            status = self._auth_client.last_error_status
            if status in (401, 403):
                clear_refresh_token(self._settings)
                self._set_auth_status(
                    "自动登录已失效，请重新登录", error=True
                )
            else:
                # 网络暂时不可用时保留 token，下次启动还能再试。
                self._set_auth_status(
                    "无法自动恢复登录，请手动登录", error=True
                )
            self._set_auth_form_busy(False)
            return
        if self._reconnect_after_refresh:
            self._reconnect_after_refresh = False
            self._force_relogin(message)
            return
        if self._refresh_after_auth_refresh:
            self._refresh_after_auth_refresh = False
            self._force_relogin(message)
            return
        if self._logging_out:
            self._logging_out = False
            self._show_auth_page()
            self._set_auth_status(message, error=True)
            self._set_auth_form_busy(False)
            return
        if self.stack.currentWidget() is self.auth_page:
            self._set_auth_status(message, error=True)
            self._set_auth_form_busy(False)
        elif self.stack.currentWidget() is self.lobby_page:
            self._set_lobby_status(message, error=True)
            self._refresh_lobby_controls()
        else:
            self.game_error_label.setText(message)

    def _logout(self) -> None:
        if self._logging_out:
            return
        self._logging_out = True
        self._heartbeat_timer.stop()
        self._reconnect_timer.stop()
        self._teardown_ws()
        self._profile_client.abort_all()
        self._history_client.abort_all()
        self._live_client.abort_all()
        self._social_client.abort_all()
        self._auth_client.logout()
        self._auth_session = None
        clear_refresh_token(self._settings)
        self._close_social_windows()
        self._clear_profile_ui()
        self._show_auth_page()
        self._set_auth_status("已退出登录")

    def _on_logged_out(self) -> None:
        self._logging_out = False
        self._auth_session = None
        clear_refresh_token(self._settings)
        self._profile_client.abort_all()
        self._history_client.abort_all()
        self._live_client.abort_all()
        self._social_client.abort_all()
        self._close_social_windows()
        self._clear_profile_ui()
        self._show_auth_page()
        self._set_auth_status("已退出登录")

    # ------------------------------------------------------------------
    # WebSocket 生命周期
    # ------------------------------------------------------------------
    def _start_ws_session(self) -> None:
        if not self._auth_session:
            return
        self._teardown_ws()
        transport = QtWebSocketTransport()
        session = OnlineGameSession(transport)
        self._transport = transport
        self._session = session

        transport.connected.connect(self._on_ws_connected)
        transport.disconnected.connect(self._on_ws_disconnected)
        transport.error.connect(self._on_ws_error)
        session.authenticated.connect(self._on_ws_authenticated)
        session.state_changed.connect(self._on_online_state_changed)
        session.stone_placed.connect(self._on_online_stone_placed)
        session.game_over.connect(self._on_online_game_over)
        session.error.connect(self._on_online_error)
        session.undo_request_received.connect(
            self._on_undo_request_received
        )
        session.undo_status_changed.connect(self._on_undo_status_changed)
        session.room_created.connect(self._on_room_created)
        session.room_closed.connect(self._on_room_closed)
        session.queue_status_changed.connect(
            self._on_queue_status_changed
        )
        session.matched.connect(self._on_match_found)
        session.chat_received.connect(self._on_chat_received)
        session.chat_failed.connect(self._on_chat_failed)
        session.room_invited.connect(self._on_room_invited)
        session.room_invite_result.connect(self._on_room_invite_result)
        session.friend_event.connect(self._on_friend_event)
        session.direct_message.connect(self._on_direct_message)
        session.live_games_changed.connect(self._on_live_games_changed)

        self._open_transport(reset_status=True)

    def _open_transport(self, reset_status: bool = True) -> None:
        if (
            self._closing
            or self._transport is None
            or self._auth_session is None
        ):
            return
        if reset_status:
            self._set_auth_status("正在连接游戏服务器…")
        try:
            self._transport.open(self._ws_url)
        except TransportError as exc:
            logger.warning("打开 WebSocket 失败：%s", exc)
            self._set_auth_status("无法连接游戏服务器", error=True)
            self._set_auth_form_busy(False)

    def _on_ws_connected(self) -> None:
        if self._closing or self._auth_session is None or self._session is None:
            return
        self._ws_connected = True
        self.connection_label.setText("正在认证…")
        if not self._session.authenticate(self._auth_session.access_token):
            self._set_auth_status("发送认证信息失败", error=True)

    def _on_ws_authenticated(self, _user: object) -> None:
        if self._closing or self._session is None:
            return
        self._reconnect_attempts = 0
        self._reconnect_after_refresh = False
        self._ws_connected = True
        if not self._heartbeat_timer.isActive():
            self._heartbeat_timer.start()
        username = self._session.username or (
            self._auth_session.user.username if self._auth_session else ""
        )
        self.lobby_welcome_label.setText(f"已登录：{username}")
        self._update_self_avatar()
        self._show_lobby_page("已连接联机服务器")
        self._set_match_status("")
        self._refresh_profile_data()
        self._refresh_friends()
        self._refresh_lobby_controls()

    def _on_ws_disconnected(self, reason: str) -> None:
        if self.sender() is not self._transport:
            return
        self._ws_connected = False
        self._heartbeat_timer.stop()
        self.connection_label.setText("连接中断")
        self._refresh_lobby_controls()
        if self.stack.currentWidget() is self.lobby_page:
            self._set_match_status("连接中断，正在尝试重连…", error=True)
        if self._closing or self._logging_out:
            return

        if self._session is not None and self._session.is_authenticated:
            self._schedule_reconnect(reason)
        else:
            # 初次连接/认证失败：回到登录页，允许再次尝试。
            self._show_auth_page()
            self._set_auth_status(
                reason or "无法连接游戏服务器，请检查服务端是否启动",
                error=True,
            )
            self._set_auth_form_busy(False)

    def _on_ws_error(self, message: str) -> None:
        if self.sender() is not self._transport:
            return
        if self.stack.currentWidget() is self.auth_page:
            self._set_auth_status(message, error=True)
            if self._session is None or not self._session.is_authenticated:
                self._set_auth_form_busy(False)
        else:
            self.game_error_label.setText(message)

    def _schedule_reconnect(self, reason: str) -> None:
        if self._reconnect_timer.isActive():
            return
        self._reconnect_attempts += 1
        if self._reconnect_attempts > 5:
            self._force_relogin("连接中断且多次重连失败，请重新登录")
            return
        self.connection_label.setText(
            f"连接中断，{self._reconnect_attempts}/5 次重连中…"
        )
        if self._session:
            self.game_error_label.setText(reason)
        self._reconnect_timer.start(2000)

    def _attempt_reconnect(self) -> None:
        if (
            self._closing
            or self._logging_out
            or self._auth_session is None
            or self._transport is None
            or self._session is None
        ):
            return
        if self._transport.is_connected:
            return
        if self._auth_session.is_access_expired():
            self._reconnect_after_refresh = True
            if not self._auth_client.busy:
                if not self._auth_client.refresh():
                    self._force_relogin("登录已过期，请重新登录")
            return
        try:
            self._transport.open(self._ws_url)
        except TransportError:
            self._schedule_reconnect("重连失败")

    def _force_relogin(self, message: str) -> None:
        self._reconnect_timer.stop()
        self._heartbeat_timer.stop()
        self._teardown_ws()
        self._profile_client.abort_all()
        self._history_client.abort_all()
        self._live_client.abort_all()
        self._social_client.abort_all()
        self._auth_session = None
        clear_refresh_token(self._settings)
        self._close_social_windows()
        self._clear_profile_ui()
        self._show_auth_page()
        self._set_auth_status(message, error=True)
        self._set_auth_form_busy(False)

    def _teardown_ws(self) -> None:
        self._ws_connected = False
        self._audio_game_id = None
        session = self._session
        self._session = None
        self._transport = None
        if session is not None:
            # OnlineGameSession.shutdown 会关闭 transport；其信号回调检查
            # 当前 sender/引用，不会误触发重连。
            session.shutdown()

    def _send_heartbeat(self) -> None:
        if self._session is not None and self._session.is_authenticated:
            self._session.send_ping()

    # ------------------------------------------------------------------
    # 大厅/房间操作
    # ------------------------------------------------------------------
    def _on_create_room(self) -> None:
        if self._session is None:
            return
        if self._room_action_pending:
            return
        self._room_action_pending = True
        self._set_lobby_status("正在创建自建房…")
        self._refresh_lobby_controls()
        time_control = self.turn_seconds_combo.currentData()
        if not isinstance(time_control, TimeControl):
            time_control = None
        logger.info(
            "请求创建自建房：允许观战=%s，时限=%s",
            self.allow_spectators_checkbox.isChecked(),
            time_control.label() if time_control is not None else "服务器默认",
        )
        if not self._session.create_room(
            self.allow_spectators_checkbox.isChecked(),
            time_control=time_control,
        ):
            self._room_action_pending = False
            self._set_lobby_status("无法创建房间，请检查连接", error=True)
            self._refresh_lobby_controls()

    def _on_quick_match(self) -> None:
        if self._session is None or not self._ws_connected:
            return
        if self._room_action_pending:
            return
        if self._session.queued:
            self._room_action_pending = True
            self._set_lobby_status("")
            self._set_match_status("正在取消匹配…")
            self._refresh_lobby_controls()
            if not self._session.cancel_match():
                self._room_action_pending = False
                self._set_match_status("无法取消匹配，请检查连接", error=True)
                self._refresh_lobby_controls()
            return

        self._room_action_pending = True
        self._auto_requeue = False
        self._set_lobby_status("")
        self._set_match_status("正在进入排位匹配队列…")
        self._refresh_lobby_controls()
        # 排位固定每手 30 秒，不需要向服务器提交时限设置。
        if not self._session.quick_match():
            self._room_action_pending = False
            self._set_match_status("无法开始匹配，请检查连接", error=True)
            self._refresh_lobby_controls()

    def _on_queue_status_changed(self, queued: bool, size: int) -> None:
        if self.sender() is not self._session:
            return
        self._room_action_pending = False
        if queued:
            self._set_lobby_status("")
            self._set_match_status(
                f"正在匹配排位对手… 当前队列 {size} 人，请稍候"
            )
        else:
            if self._session is not None and self._session.snapshot is None:
                self._set_match_status("已退出排位匹配")
        self._refresh_lobby_controls()

    def _on_match_found(self, _game_id: str, opponent_name: str) -> None:
        if self.sender() is not self._session:
            return
        self._room_action_pending = False
        # 新的排位对局开始，旧房间的聊天记录不再保留。
        self._clear_chat_area()
        self._set_lobby_status("")
        self._set_match_status(
            f"已匹配到 {opponent_name}，正在载入棋局…"
        )
        self._refresh_lobby_controls()

    def _on_room_created(self, code: str) -> None:
        self._room_action_pending = False
        # 新房间 = 新的对话上下文，旧房间的聊天记录不再保留。
        self._clear_chat_area()
        self._set_match_status("")
        self._show_game_page()

    def _on_room_closed(self, reason: str) -> None:
        self._room_action_pending = False
        self._audio_game_id = None
        self.clock_label.setText("—")
        self._deadline_monotonic = None
        self.game_rating_label.clear()
        self.game_rating_label.setVisible(False)
        self._set_match_status("")
        # 房间关闭后清空对话日志与在飞弹幕。
        self._clear_chat_area()
        self._leave_game_page(reason)
        if self._auto_requeue:
            self._auto_requeue = False
            QTimer.singleShot(0, self._on_quick_match)

    # ------------------------------------------------------------------
    # 表情/评论
    # ------------------------------------------------------------------
    def _on_chat_received(self, message: ChatMessage) -> None:
        """收到服务端广播的表情/评论：飞弹幕并写入对话日志。"""
        if self.sender() is not self._session or self._closing:
            return
        if self._session is not None and self._session.is_spectator:
            # 观战者看不到对局内的聊天（弹幕与对话日志都不显示）。
            return
        is_self = bool(
            self._session is not None
            and message.sender_id == self._session.user_id
        )
        if message.kind == CHAT_KIND_EMOJI:
            self.danmaku_overlay.push_emoji(message.content, is_self)
        elif message.kind == CHAT_KIND_TEXT:
            self.danmaku_overlay.push_text(message.content, is_self)
        self.chat_log.append_message(message, is_self)

    def _on_emoji_picked(self, emoji: str) -> None:
        """发送用户选中的预设表情。"""
        if self._session is not None:
            self._session.send_emoji(emoji)

    def _on_chat_failed(self, reason: str) -> None:
        """发言失败：提示在聊天区域，不打断对局。

        提示同时出现在两处：对话日志里留一条记录，日志收起时则改用
        输入框的占位提示——对话日志默认是收起的，不能让失败提示跟着
        一起消失。两种提示都不会在对局页上多占一行垂直空间，避免把
        棋盘挤小、裁掉棋盘下沿。
        """
        if self.sender() is not self._session or self._closing:
            return
        text = reason or "发言未发送"
        self.chat_log.append_notice(f"（{text}）")
        if not self._chat_log_expanded:
            self._flash_chat_hint(text)

    def _flash_chat_hint(self, text: str) -> None:
        """把提示临时显示在输入框的占位文字上。"""
        self.chat_input.setPlaceholderText(text)
        QTimer.singleShot(3000, self._restore_chat_hint)

    def _restore_chat_hint(self) -> None:
        """恢复输入框的常规占位提示。"""
        if not self._closing:
            self.chat_input.setPlaceholderText(CHAT_INPUT_HINT)

    def _on_chat_log_toggled(self, checked: bool) -> None:
        """对话日志的展开/收起。"""
        self._apply_chat_log_visibility(checked)
        self._save_chat_log_visible(checked)

    def _apply_chat_log_visibility(self, visible: bool) -> None:
        """显示或隐藏对话日志，并同步按钮状态。"""
        expanded = bool(visible)
        # 记录逻辑状态：chat_log.isVisible() 还会受父级页面是否显示
        # 的影响，对局页不在前台时它恒为 False，不能拿来判断"用户
        # 是否展开了日志"。
        self._chat_log_expanded = expanded
        self.chat_log.setVisible(expanded)
        button = self.chat_log_toggle_button
        # 程序化设置勾选状态时不要触发 toggled，否则会绕一圈回来。
        button.blockSignals(True)
        button.setChecked(expanded)
        button.blockSignals(False)
        button.setText("隐藏日志" if expanded else "对话日志")
        button.setToolTip(
            "收起对话日志，让棋盘占满整行"
            if expanded
            else "展开对话日志，查看双方的发言"
        )

    def _load_chat_log_visible(self) -> bool:
        """读取上次的对话日志展开状态；没有记录时默认收起。"""
        value = self._settings.value(SETTINGS_CHAT_LOG_VISIBLE, False)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return False

    def _save_chat_log_visible(self, visible: bool) -> None:
        """记住对话日志的展开状态。"""
        self._settings.setValue(SETTINGS_CHAT_LOG_VISIBLE, bool(visible))
        self._settings.sync()

    def _on_preset_comment_picked(self, text: str) -> None:
        """发送用户选中的预设评论。"""
        if self._session is not None:
            self._session.send_comment(text)

    def _on_send_comment(self) -> None:
        """发送手动输入的评论；发送成功后清空输入框。"""
        if self._session is None:
            return
        text = self.chat_input.text().strip()
        if not text:
            return
        if self._session.send_comment(text):
            self.chat_input.clear()

    def _refresh_chat_controls(self) -> None:
        """按当前会话状态启用或禁用聊天输入控件。

        这里用的是 chat_available 而不是 can_send_chat：后者还包含
        发言冷却，如果拿它去禁用输入框，Qt 会把焦点从被禁用的控件
        上摘走，用户每发一条消息都得重新点回输入框。
        """
        chat_input = getattr(self, "chat_input", None)
        if chat_input is None:
            return
        session = self._session
        enabled = (
            session is not None
            and self._ws_connected
            and session.chat_available
            # 观战者不发言。
            and not session.is_spectator
        )
        chat_input.setEnabled(enabled)
        self.chat_send_button.setEnabled(enabled)
        self.emoji_button.set_emoji_enabled(enabled)
        self.preset_comment_button.setEnabled(enabled)

    def _clear_chat_area(self) -> None:
        """清空对话日志与在飞的弹幕。"""
        self.chat_log.clear_log()
        self.danmaku_overlay.clear()

    def _on_board_clicked(self, row: int, col: int) -> None:
        if self._session is not None and self._session.accepts_human_input:
            self._session.request_move(row, col)

    # ------------------------------------------------------------------
    # 沙盘推演
    # ------------------------------------------------------------------
    def _on_sandbox_toggled(self) -> None:
        """「沙盘推演」按钮：打开或关闭沙盘。"""
        if self._sandbox_open:
            self._close_sandbox()
        else:
            self._open_sandbox()

    def _open_sandbox(self) -> bool:
        """打开沙盘：载入当前对局局面，并遮挡真实棋盘。

        沙盘完全在本地运行，不会向服务器发送任何落子请求。
        """
        session = self._session
        if session is None or session.snapshot is None:
            # 还没有对局（或对局尚未开始），没有可推演的局面。
            return False
        self.sandbox_panel.load_position(session.view)
        self.sandbox_panel.setVisible(True)
        self._sandbox_open = True
        self.sandbox_button.setText("退出沙盘")
        # 真实棋盘被沙盘盖住，不能再接受点击。
        self.board_widget.set_interaction_enabled(False)
        return True

    def _close_sandbox(self) -> None:
        """关闭沙盘：恢复真实棋盘。"""
        self._hide_sandbox()
        # 整页刷新：真实棋盘的交互状态按会话重新计算。
        self._sync_game_page()

    def _hide_sandbox(self) -> None:
        """隐藏沙盘并复位沙盘状态，但不触发整页刷新。"""
        self.sandbox_panel.setVisible(False)
        self._sandbox_open = False
        self.sandbox_button.setText("沙盘推演")

    def _sync_sandbox_button(
        self, session: Optional[OnlineGameSession]
    ) -> None:
        """同步沙盘按钮的可用状态：没有对局时不可用。"""
        has_game = session is not None and session.snapshot is not None
        # 沙盘已经打开时按钮必须保持可用，否则用户会被困在沙盘里
        # 无法退出（例如中途断线导致会话消失）。
        self.sandbox_button.setEnabled(
            (has_game and self._ws_connected) or self._sandbox_open
        )

    def _on_undo_clicked(self) -> None:
        """正常时发起悔棋请求；对手请求待回应时，此按钮表示同意。"""
        if self._session is None:
            return
        if self._session.undo_pending:
            if not self._session.undo_requested_by_you:
                self._session.respond_undo(True)
            return
        self._session.request_undo()

    def _on_undo_declined(self) -> None:
        if self._session is not None and self._session.undo_pending:
            self._session.respond_undo(False)

    def _on_undo_request_received(self, name: str) -> None:
        if self.sender() is not self._session or self._closing:
            return
        if self._session is not None and self._session.is_spectator:
            # 观战者不参与悔棋协商。
            return
        self._show_undo_notice(f"{name or '对手'} 请求悔棋，是否同意？")
        self._sync_game_page()

    def _on_undo_status_changed(self, state: str, reason: str) -> None:
        if self.sender() is not self._session or self._closing:
            return
        if state == "requested":
            if self._session is not None and self._session.undo_requested_by_you:
                self._show_undo_notice("已发送悔棋请求，等待对手回应…")
        elif state == "accepted":
            self._show_undo_notice(reason or "悔棋成功")
        elif state == "declined":
            self._show_undo_notice(reason or "对手拒绝了悔棋请求")
        elif state == "expired":
            self._show_undo_notice(reason or "悔棋请求已超时")
        elif state == "cancelled":
            self._show_undo_notice(reason or "悔棋请求已取消")
        self._sync_game_page()

    def _show_undo_notice(self, text: str) -> None:
        self.undo_status_label.setText(text)
        self.undo_status_label.setVisible(bool(text))
        if text:
            self._undo_notice_timer.start(5000)

    def _clear_undo_notice(self) -> None:
        self.undo_status_label.setText("")
        self.undo_status_label.setVisible(False)

    def _set_undo_text(self, text: str) -> None:
        if self.undo_button.text() != text:
            self.undo_button.setText(text)

    def _refresh_undo_controls(self) -> None:
        """刷新悔棋按钮的次数/冷却/待回应状态。"""
        session = self._session
        if session is None:
            return
        if session.undo_pending:
            if session.undo_requested_by_you:
                self._set_undo_text("等待对方同意")
                enabled = False
            else:
                self._set_undo_text("同意悔棋")
                enabled = self._ws_connected
            self.undo_button.setEnabled(enabled)
            self.undo_button.setToolTip("")
            return

        remaining = session.undo_remaining
        cooldown = session.undo_cooldown_remaining
        if session.undo_limit <= 0:
            self._set_undo_text("本局未开启悔棋")
            enabled = False
        elif remaining <= 0:
            self._set_undo_text("本局悔棋已用完")
            enabled = False
        elif cooldown > 0:
            self._set_undo_text(f"悔棋冷却 {cooldown}s")
            enabled = False
        else:
            self._set_undo_text("请求悔棋")
            enabled = self._ws_connected and session.can_undo
        self.undo_button.setEnabled(enabled)
        if session.undo_limit > 0:
            tooltip = (
                f"本局剩余 {remaining}/{session.undo_limit} 次；"
                f"每 {session.undo_cooldown_seconds} 秒最多请求一次"
            )
        else:
            tooltip = ""
        if self.undo_button.toolTip() != tooltip:
            self.undo_button.setToolTip(tooltip)

    def _on_resign(self) -> None:
        if self._session is None or self._closing:
            return
        snapshot = self._session.snapshot
        if snapshot is None or snapshot.is_game_over:
            return
        dialog = ConfirmDialog(
            "确认认输",
            "认输后本局直接判负，确定要认输吗？",
            confirm_text="认输",
            cancel_text="继续对局",
            seal_text="负",
            parent=self,
        )
        try:
            dialog.exec()
            confirmed = dialog.confirm_requested
        finally:
            dialog.deleteLater()
        if not confirmed or self._session is None:
            return
        snapshot = self._session.snapshot
        if snapshot is not None and not snapshot.is_game_over:
            self._session.resign()

    def _on_rematch(self) -> None:
        if self._session is None:
            return
        if self._session.rated:
            # 排位赛不能与同一对手直接再战，离开房间后重新排队。
            self._auto_requeue = True
            if not self._session.leave_room():
                self._auto_requeue = False
                self.game_error_label.setText(
                    "无法离开排位房间，请稍后重试"
                )
            return
        self._session.request_rematch()

    def _on_leave_room(self) -> None:
        if self._session is not None:
            self._auto_requeue = False
            self._session.leave_room()

    # ------------------------------------------------------------------
    # 举报与回放审计
    # ------------------------------------------------------------------
    def _on_report_clicked(self) -> None:
        if (
            self._session is None
            or self._auth_session is None
            or self._session.snapshot is None
            or not self._session.snapshot.is_game_over
        ):
            return
        snapshot = self._session.snapshot
        if self._report_panel is not None:
            self._report_panel.close()
        panel = ReportPanel(
            snapshot.game_id,
            self._session.opponent_username,
            parent=self,
        )
        panel.submitted.connect(self._on_report_submit)
        panel.closed.connect(self._on_report_panel_closed)
        self._report_panel = panel
        self._report_game_id = snapshot.game_id
        self._report_awaiting_response = False
        panel.show()
        panel.raise_()
        token = self._auth_session.access_token
        if self._api_base_url:
            self._report_client.fetch_audit(token, snapshot.game_id)
        else:
            panel.set_audit(0, ok=False)

    def _on_report_submit(self, category: str, description: str) -> None:
        if (
            self._report_panel is None
            or self._auth_session is None
            or not self._report_game_id
        ):
            return
        self._report_awaiting_response = True
        if not self._report_client.submit_report(
            self._auth_session.access_token,
            self._report_game_id,
            category,
            description,
        ):
            self._report_awaiting_response = False
            self._report_panel.set_result(
                "无法提交举报，请检查连接", success=False
            )

    def _on_report_submitted(self, _data: object) -> None:
        self._report_awaiting_response = False
        if self._report_panel is None:
            return
        self._report_panel.set_result(
            "举报已提交，管理员会结合回放审计处理。", success=True
        )

    def _on_report_audit_ready(self, data: object) -> None:
        if self._report_panel is None or not isinstance(data, Mapping):
            return
        game_id = data.get("game_id")
        if isinstance(game_id, str) and game_id != self._report_game_id:
            return
        raw_findings = data.get("findings")
        findings = (
            raw_findings if isinstance(raw_findings, list) else []
        )
        details = "；".join(
            str(item.get("message", ""))
            for item in findings[:3]
            if isinstance(item, Mapping)
        )
        self._report_panel.set_audit(
            len(findings),
            ok=bool(data.get("ok", not findings)),
            details=details,
        )

    def _on_report_failed(self, message: str) -> None:
        if self._report_panel is None:
            return
        if self._report_awaiting_response:
            self._report_awaiting_response = False
            self._report_panel.set_result(
                message or "举报提交失败", success=False
            )
        else:
            self._report_panel.audit_label.setText(
                "回放审计暂时不可用，举报仍可提交。"
            )

    def _on_report_panel_closed(self) -> None:
        panel = self._report_panel
        self._report_panel = None
        self._report_game_id = ""
        if panel is not None:
            panel.deleteLater()

    # ------------------------------------------------------------------
    # 会话/界面同步
    # ------------------------------------------------------------------
    def _on_online_state_changed(self) -> None:
        if self.sender() is not self._session or self._closing:
            return
        if self._session.snapshot is not None or self._session.waiting_for_opponent:
            self._room_action_pending = False
        self._sync_online_ui()

    def _on_online_stone_placed(self) -> None:
        """服务器确认落子后播放音效；仅发送意图的本地点击不会触发。"""
        if self.sender() is not self._session or self._closing:
            return
        get_audio_manager().play_move()

    def _on_online_game_over(self) -> None:
        if self.sender() is not self._session:
            return
        self._sync_online_ui()
        if not self._closing:
            self._play_online_result_sound()
            QTimer.singleShot(0, self._show_online_victory_notice)
        if self._session is not None and not self._session.is_spectator:
            # 观战不会改变本人的段位，无需重新拉取资料。
            QTimer.singleShot(0, self._refresh_profile_data)

    def _show_online_victory_notice(self) -> None:
        """对手认输/逃跑后的胜利弹窗；同一局只弹一次。"""
        if self._closing or self._session is None:
            return
        snapshot = self._session.snapshot
        notice = online_victory_notice(snapshot)
        if notice is None or snapshot is None:
            return
        if self._victory_notice_game_id == snapshot.game_id:
            return
        if (
            not self.isVisible()
            or self.stack.currentWidget() is not self.game_page
        ):
            return
        self._victory_notice_game_id = snapshot.game_id
        title, message = notice
        overlay = NoticeOverlay(
            title, message, parent=self, seal_text="胜"
        )
        try:
            overlay.exec()
        finally:
            overlay.deleteLater()

    def _play_online_result_sound(self) -> None:
        """按服务器快照判断胜负，播放对应的终局音效。"""
        snapshot = self._session.snapshot if self._session is not None else None
        if snapshot is None or not snapshot.is_game_over:
            return
        if snapshot.is_spectator:
            # 观战者不是参与者，不该听到自己“赢了”或“输了”的音效。
            return
        audio = get_audio_manager()
        if snapshot.winner is None:
            audio.play_draw()
        elif snapshot.winner is snapshot.your_color:
            audio.play_win()
        else:
            audio.play_lose()

    def _maybe_play_online_start(self, snapshot: GameSnapshot) -> None:
        """每个新 game_id 的第一份权威快照播放一次开局音。"""
        if self._audio_game_id == snapshot.game_id:
            return
        self._audio_game_id = snapshot.game_id
        if not self._closing:
            get_audio_manager().play_start()

    def _on_online_error(self, message: str) -> None:
        if self.sender() is not self._session:
            return
        self._room_action_pending = False
        if self.stack.currentWidget() is self.auth_page:
            self._set_auth_status(message, error=True)
        elif self.stack.currentWidget() is self.lobby_page:
            self._set_lobby_status(message, error=True)
            self._refresh_lobby_controls()
        else:
            self.game_error_label.setText(message)

    def _sync_online_ui(self) -> None:
        self._refresh_chat_controls()
        if self._session is None:
            return
        session = self._session
        if not session.is_authenticated:
            return
        if session.phase in {
            OnlineSessionPhase.WAITING,
            OnlineSessionPhase.PLAYING,
            OnlineSessionPhase.SPECTATING,
        }:
            self._show_game_page()
            self._sync_game_page()
        elif self.stack.currentWidget() is self.game_page:
            self._leave_game_page()
        else:
            self._update_rating_summary()
            self._refresh_lobby_controls()

    def _leave_game_page(self, message: str = "") -> None:
        """离开对局页：从对战广场进去观战的，回广场；其余回大厅。"""
        # 沙盘不能跟着离开对局页：先收起来。
        self._close_sandbox()
        if self._watching_from_plaza:
            self._watching_from_plaza = False
            self._show_plaza_page(message)
        else:
            self._show_lobby_page(message)

    def _sync_game_page(self) -> None:
        self._refresh_chat_controls()
        session = self._session
        # 没有会话时按钮同样要禁用。
        self._sync_sandbox_button(session)
        if session is None:
            return
        snapshot = session.snapshot
        if snapshot is not None and (
            session.is_spectator or snapshot.your_color is None
        ):
            # 观战者不是对局双方：走独立的只读分支，不能按玩家视角渲染。
            # 快照里没有执子颜色同样说明这是旁观者视角。
            self._sync_spectator_page(snapshot)
            return
        # 玩家视角：聊天控件重新可见（观战时是被隐藏的）。
        self._set_chat_visibility(True)
        waiting = session.waiting_for_opponent
        rated = session.rated or (
            snapshot.rated if snapshot is not None else False
        )
        your_color = (
            snapshot.your_color
            if snapshot is not None
            else (session.human_player or Player.BLACK)
        )
        opponent_color = your_color.opponent
        you_name = f"你，{session.your_username}"
        opponent_name = (
            "对手，等待好友加入"
            if waiting
            else f"对手，{session.opponent_username}"
        )
        if rated:
            room_title = "排位赛，积分对局"
        elif waiting:
            room_title = "自建房，等待好友"
        else:
            room_title = "自建房，休闲对局"
        time_control = session.time_control
        if time_control is not None:
            # 把服务端实际采用的时限显示出来，选项未生效时能一眼看出。
            room_title = f"{room_title}，{time_control.label()}"
        self.room_title_label.setText(room_title)
        self.invite_friend_button.setVisible(waiting)
        self.invite_friend_button.setEnabled(
            waiting and self._ws_connected
        )
        self.connection_label.setText(
            "已连接" if self._ws_connected else "连接中断"
        )
        self.game_error_label.setText("")

        your_rating = (
            snapshot.your_rating_before
            if snapshot is not None
            else session.your_rating
        )
        opponent_rating = (
            snapshot.opponent_rating_before
            if snapshot is not None
            else session.opponent_rating
        )

        self_id = self._auth_session.user.id if self._auth_session else ""
        self_avatar = (
            self._auth_session.user.avatar if self._auth_session else ""
        )
        self_avatar_version = (
            self._auth_session.user.avatar_version
            if self._auth_session
            else 0
        )
        self_achievements = (
            self._auth_session.user.achievement_keys
            if self._auth_session
            else ()
        )
        if snapshot is not None:
            black_id = snapshot.black_user_id
            white_id = snapshot.white_user_id
            black_avatar = snapshot.black_avatar
            white_avatar = snapshot.white_avatar
            black_avatar_version = snapshot.black_avatar_version
            white_avatar_version = snapshot.white_avatar_version
            black_achievements = snapshot.black_achievements
            white_achievements = snapshot.white_achievements
        else:
            black_id = self_id if your_color is Player.BLACK else ""
            white_id = self_id if your_color is Player.WHITE else ""
            black_avatar = (
                self_avatar if your_color is Player.BLACK else ""
            )
            white_avatar = (
                self_avatar if your_color is Player.WHITE else ""
            )
            black_avatar_version = (
                self_avatar_version
                if your_color is Player.BLACK
                else 0
            )
            white_avatar_version = (
                self_avatar_version
                if your_color is Player.WHITE
                else 0
            )
            black_achievements = (
                self_achievements if your_color is Player.BLACK else ()
            )
            white_achievements = (
                self_achievements if your_color is Player.WHITE else ()
            )
        you_id = black_id if your_color is Player.BLACK else white_id
        opponent_id = (
            white_id if your_color is Player.BLACK else black_id
        )
        you_avatar = (
            black_avatar
            if your_color is Player.BLACK
            else white_avatar
        )
        opponent_avatar = (
            white_avatar
            if your_color is Player.BLACK
            else black_avatar
        )
        you_avatar_version = (
            black_avatar_version
            if your_color is Player.BLACK
            else white_avatar_version
        )
        opponent_avatar_version = (
            white_avatar_version
            if your_color is Player.BLACK
            else black_avatar_version
        )
        you_achievements = (
            black_achievements
            if your_color is Player.BLACK
            else white_achievements
        )
        opponent_achievements = (
            white_achievements
            if your_color is Player.BLACK
            else black_achievements
        )

        if waiting:
            self.you_card.set_content(
                you_name,
                your_color,
                False,
                "等待对手",
                your_rating,
                avatar=you_avatar,
                user_id=you_id,
                avatar_version=you_avatar_version,
                achievements=you_achievements,
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                False,
                "等待对手加入",
                opponent_rating,
                avatar="",
                user_id="",
            )
            self._set_game_pill("等待对手", result=False)
            self.clock_label.setText("等待好友加入…")
            self.move_label.setText("第 0 手")
            self.game_rating_label.clear()
            self.game_rating_label.setVisible(False)
            self._update_achievement_notice(None)
            self._clear_undo_notice()
            self.board_widget.set_view(session.view)
            self.board_widget.set_interaction_enabled(False)
            self._set_action_visibility(
                resign=False,
                rematch=False,
                leave=True,
                undo=False,
                reject_undo=False,
            )
            self.rematch_button.setEnabled(False)
            self.leave_button.setText("离开房间")
            self.leave_button.setEnabled(self._ws_connected)
            return

        self.you_card.set_content(
            you_name,
            your_color,
            False,
            "等待",
            your_rating,
            avatar=you_avatar,
            user_id=you_id,
            avatar_version=you_avatar_version,
            achievements=you_achievements,
        )
        self.opponent_card.set_content(
            opponent_name,
            opponent_color,
            False,
            "等待" if session.opponent_online else "已离线",
            opponent_rating,
            avatar=opponent_avatar,
            user_id=opponent_id,
            avatar_version=opponent_avatar_version,
            achievements=opponent_achievements,
        )
        if snapshot is None:
            self.clock_label.setText("—")
            self.move_label.setText("第 0 手")
            self.game_rating_label.clear()
            self.game_rating_label.setVisible(False)
            self._update_achievement_notice(None)
            self.board_widget.set_interaction_enabled(False)
            return

        self._last_snapshot = snapshot
        self._maybe_play_online_start(snapshot)
        # 依据服务器时间计算倒计时，不在客户端自行判定超时胜负。
        self._apply_snapshot_deadline(snapshot)

        current = snapshot.current_player
        over = snapshot.is_game_over
        winner = snapshot.winner
        if over:
            self._clear_undo_notice()
            if snapshot.your_rating_after is not None:
                your_rating = snapshot.your_rating_after
            if snapshot.opponent_rating_after is not None:
                opponent_rating = snapshot.opponent_rating_after
            self.you_card.set_content(
                you_name,
                your_color,
                winner is your_color,
                "获胜"
                if winner is your_color
                else ("平局" if winner is None else "本局结束"),
                your_rating,
                avatar=you_avatar,
                user_id=you_id,
                avatar_version=you_avatar_version,
                achievements=you_achievements,
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                winner is opponent_color,
                "获胜"
                if winner is opponent_color
                else ("平局" if winner is None else "本局结束"),
                opponent_rating,
                avatar=opponent_avatar,
                user_id=opponent_id,
                avatar_version=opponent_avatar_version,
                achievements=opponent_achievements,
            )
            if winner is None:
                self._set_game_pill("本局平局", result=True)
            elif winner is your_color:
                self._set_game_pill("你获胜", result=True)
            else:
                self._set_game_pill("对手获胜", result=True)
            self.clock_label.setText("对局结束")

            if rated and snapshot.your_rating_after is not None:
                delta = snapshot.rating_delta
                before = snapshot.your_rating_before
                if delta is not None and before is not None:
                    sign = "+" if delta > 0 else ""
                    self.game_rating_label.setText(
                        "排位积分："
                        f"{before} → {snapshot.your_rating_after}"
                        f"（{sign}{delta}）"
                    )
                elif delta is not None:
                    sign = "+" if delta > 0 else ""
                    self.game_rating_label.setText(
                        f"当前排位积分 {snapshot.your_rating_after}"
                        f"（{sign}{delta}）"
                    )
                else:
                    self.game_rating_label.setText(
                        f"当前排位积分 {snapshot.your_rating_after}"
                    )
                self.game_rating_label.setVisible(True)
            else:
                self.game_rating_label.clear()
                self.game_rating_label.setVisible(False)
            self._update_achievement_notice(snapshot)
        else:
            self.game_rating_label.clear()
            self.game_rating_label.setVisible(False)
            self._update_achievement_notice(None)
            self.you_card.set_content(
                you_name,
                your_color,
                current is your_color,
                "等待服务器确认"
                if session.busy and current is your_color
                else ("落子中" if current is your_color else "等待"),
                your_rating,
                you_avatar,
                you_id,
                you_avatar_version,
                you_achievements,
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                current is opponent_color,
                "已离线"
                if not session.opponent_online
                else ("落子中" if current is opponent_color else "等待"),
                opponent_rating,
                opponent_avatar,
                opponent_id,
                opponent_avatar_version,
                opponent_achievements,
            )
            if not self._ws_connected:
                self._set_game_pill("连接中断", result=False)
            elif not session.opponent_online:
                self._set_game_pill("对手离线", result=False)
            elif session.busy:
                self._set_game_pill("等待服务器", result=False)
            elif current is your_color:
                self._set_game_pill("你的回合", result=False)
            else:
                self._set_game_pill("对手回合", result=False)

        self.move_label.setText(f"第 {snapshot.move_no} 手")
        if not self._ws_connected:
            self.game_error_label.setText("网络连接中断，正在尝试恢复")
        self.board_widget.set_view(session.view)
        if self._sandbox_open:
            # 真实对局推进了，把新局面同步给沙盘基线。
            self.sandbox_panel.sync_base(session.view)
        self.board_widget.set_interaction_enabled(
            self._ws_connected
            and session.accepts_human_input
            and not self._sandbox_open
        )
        self.board_widget.update()
        self._update_clock_label()

        undo_pending = session.undo_pending
        undo_mine = session.undo_requested_by_you
        reject_undo = not over and undo_pending and not undo_mine
        self._set_action_visibility(
            resign=not over,
            rematch=over,
            leave=over,
            undo=not over,
            reject_undo=reject_undo,
            report=over,
        )
        self.resign_button.setEnabled(not over and self._ws_connected)
        self.report_button.setEnabled(over and self._ws_connected)
        self.leave_button.setEnabled(over and self._ws_connected)
        self.undo_reject_button.setEnabled(reject_undo and self._ws_connected)
        if not over:
            self._refresh_undo_controls()
            self.leave_button.setText("离开房间")
            return
        if rated:
            self.leave_button.setText("返回大厅")
            self.rematch_button.setText("继续排位")
            self.rematch_button.setEnabled(self._ws_connected)
            return

        self.leave_button.setText("离开房间")
        yours_requested, opponent_requested = session.rematch_state
        if yours_requested:
            self.rematch_button.setText("等待对手同意")
            self.rematch_button.setEnabled(False)
        else:
            self.rematch_button.setText(
                "接受对手再战" if opponent_requested else "申请再战"
            )
            self.rematch_button.setEnabled(self._ws_connected)

    def _apply_snapshot_deadline(self, snapshot: GameSnapshot) -> None:
        """依据服务器时间计算本手倒计时，不在客户端自行判定超时胜负。"""
        self._snapshot_monotonic = time.monotonic()
        if (
            not snapshot.is_game_over
            and snapshot.turn_deadline_ms is not None
            and snapshot.server_now_ms is not None
        ):
            seconds = (
                snapshot.turn_deadline_ms - snapshot.server_now_ms
            ) / 1000.0
            self._deadline_monotonic = time.monotonic() + max(0.0, seconds)
        else:
            self._deadline_monotonic = None

    def _set_chat_visibility(self, visible: bool) -> None:
        """整体显示或隐藏聊天控件。

        旁观者不参与对局内的聊天，也看不到双方的发言，因此聊天输入栏与
        对话日志要一起藏起来。
        """
        for widget in (
            self.emoji_button,
            self.preset_comment_button,
            self.chat_input,
            self.chat_send_button,
            self.chat_log_toggle_button,
        ):
            widget.setVisible(visible)
        if visible:
            self._apply_chat_log_visibility(self._chat_log_expanded)
        else:
            self.chat_log.setVisible(False)

    def _sync_spectator_page(self, snapshot: GameSnapshot) -> None:
        """观战页：只读展示棋盘与双方信息，隐藏一切玩家专属的操作。"""
        session = self._session
        self._sync_sandbox_button(session)
        rated = snapshot.rated
        spectator_title = "观战 · 排位赛" if rated else "观战 · 自建房"
        if snapshot.time_control is not None:
            spectator_title = (
                f"{spectator_title}，{snapshot.time_control.label()}"
            )
        self.room_title_label.setText(spectator_title)
        # 旁观者不能邀请好友进自己的房间。
        self.invite_friend_button.setVisible(False)
        self.connection_label.setText(
            "已连接" if self._ws_connected else "连接中断"
        )
        self.game_error_label.setText("")

        current = snapshot.current_player
        over = snapshot.is_game_over
        winner = snapshot.winner
        if over:
            if winner is None:
                black_status = white_status = "本局平局"
            elif winner is Player.BLACK:
                black_status, white_status = "获胜", "本局结束"
            else:
                black_status, white_status = "本局结束", "获胜"
        else:
            black_status = "落子中" if current is Player.BLACK else "等待"
            white_status = "落子中" if current is Player.WHITE else "等待"

        # 两张身份卡展示的是对局中的两名玩家，而不是旁观者自己。
        self.you_card.set_content(
            snapshot.black_name or "黑方",
            Player.BLACK,
            current is Player.BLACK,
            black_status,
            avatar=snapshot.black_avatar,
            user_id=snapshot.black_user_id,
            avatar_version=snapshot.black_avatar_version,
            achievements=snapshot.black_achievements,
        )
        self.opponent_card.set_content(
            snapshot.white_name or "白方",
            Player.WHITE,
            current is Player.WHITE,
            white_status,
            avatar=snapshot.white_avatar,
            user_id=snapshot.white_user_id,
            avatar_version=snapshot.white_avatar_version,
            achievements=snapshot.white_achievements,
        )

        if over:
            if winner is None:
                self._set_game_pill("本局平局", result=True)
            elif winner is Player.BLACK:
                self._set_game_pill("黑方获胜", result=True)
            else:
                self._set_game_pill("白方获胜", result=True)
            self.clock_label.setText("对局结束")
        elif not self._ws_connected:
            self._set_game_pill("连接中断", result=False)
        elif current is Player.BLACK:
            self._set_game_pill("黑方回合", result=False)
        else:
            self._set_game_pill("白方回合", result=False)

        self.move_label.setText(f"第 {snapshot.move_no} 手")
        self._last_snapshot = snapshot
        self._maybe_play_online_start(snapshot)
        self._apply_snapshot_deadline(snapshot)
        # 观战者只读：棋盘不接受任何点击。
        if session is not None:
            self.board_widget.set_view(session.view)
            if self._sandbox_open:
                # 观战也是本地工具：真实局面推进时同步沙盘基线。
                self.sandbox_panel.sync_base(session.view)
        self.board_widget.set_interaction_enabled(False)
        self.board_widget.update()
        self._update_clock_label()

        # 观战者不参与积分与成就结算，这两个标签始终隐藏。
        self.game_rating_label.clear()
        self.game_rating_label.setVisible(False)
        self.game_achievement_label.clear()
        self.game_achievement_label.setVisible(False)
        self._clear_undo_notice()

        # 只有“退出观战”可用，其余玩家操作全部隐藏。
        self._set_action_visibility(
            resign=False,
            rematch=False,
            leave=True,
            undo=False,
            reject_undo=False,
        )
        self.leave_button.setText("退出观战")
        self.leave_button.setEnabled(self._ws_connected)
        # 旁观者不发言，也看不到双方的聊天。
        self._set_chat_visibility(False)

    def _update_achievement_notice(
        self, snapshot: Optional[GameSnapshot]
    ) -> None:
        """终局后展示本局新解锁的成就；同一局只弹一次。"""
        if snapshot is None or not snapshot.is_game_over:
            self._achievement_notice_game_id = ""
            self.game_achievement_label.clear()
            self.game_achievement_label.setVisible(False)
            return
        keys = snapshot.your_new_achievements
        if not keys:
            self.game_achievement_label.clear()
            self.game_achievement_label.setVisible(False)
            return
        if self._achievement_notice_game_id == snapshot.game_id:
            return
        self._achievement_notice_game_id = snapshot.game_id
        names = achievement_names(keys)
        self.game_achievement_label.setText(
            f"新成就解锁：{names}" if names else ""
        )
        self.game_achievement_label.setVisible(bool(names))

    def _set_game_pill(self, text: str, result: bool) -> None:
        self.game_status_pill.setText(text)
        value = "true" if result else "false"
        if self.game_status_pill.property("result") != value:
            self.game_status_pill.setProperty("result", value)
            self.game_status_pill.style().unpolish(self.game_status_pill)
            self.game_status_pill.style().polish(self.game_status_pill)
            self.game_status_pill.update()

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
        self.undo_button.setVisible(undo)
        self.undo_reject_button.setVisible(reject_undo)
        self.resign_button.setVisible(resign)
        self.rematch_button.setVisible(rematch)
        self.report_button.setVisible(report)
        self.leave_button.setVisible(leave)

    def _update_clock_label(self) -> None:
        # 聊天控件状态每次心跳都刷新一次（即使当前没有对局）。
        self._refresh_chat_controls()
        if (
            self._session is None
            or self._session.snapshot is None
            or self.stack.currentWidget() is not self.game_page
        ):
            return
        snapshot = self._session.snapshot
        if not self._session.is_spectator:
            self._refresh_undo_controls()
        if snapshot.is_game_over:
            self.clock_label.setText("对局结束")
            return
        if not self._ws_connected:
            self.clock_label.setText("连接中断")
            return
        if self._session.undo_pending and not self._session.is_spectator:
            self.clock_label.setText(
                "等待对手回应悔棋"
                if self._session.undo_requested_by_you
                else "请回应悔棋请求"
            )
            return
        time_control = snapshot.time_control
        if time_control is not None and time_control.is_fischer:
            self.clock_label.setText(self._fischer_clock_text(snapshot))
            return
        if self._deadline_monotonic is None:
            self.clock_label.setText("等待服务器…")
            return
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            self.clock_label.setText("本手时间已到，等待服务器判定…")
        else:
            self.clock_label.setText(f"本手倒计时：{int(remaining + 0.999)} 秒")

    def _fischer_clock_text(self, snapshot: GameSnapshot) -> str:
        """Fischer 双方余量：只减去走子方从快照以来的本地耗时。"""
        black = snapshot.black_remaining_ms or 0
        white = snapshot.white_remaining_ms or 0
        if (
            self._snapshot_monotonic is not None
            and snapshot.turn_deadline_ms is not None
        ):
            elapsed_ms = int(
                max(0.0, time.monotonic() - self._snapshot_monotonic)
                * 1000
            )
            if snapshot.current_player is Player.BLACK:
                black = max(0, black - elapsed_ms)
            else:
                white = max(0, white - elapsed_ms)
        if self._session is not None and self._session.is_spectator:
            return f"黑 {_format_clock_ms(black)} · 白 {_format_clock_ms(white)}"
        your_color = (
            self._session.snapshot.your_color
            if self._session is not None
            and self._session.snapshot is not None
            else None
        )
        if your_color is Player.WHITE:
            yours, opponent = white, black
        else:
            yours, opponent = black, white
        return (
            f"你 {_format_clock_ms(yours)} · "
            f"对手 {_format_clock_ms(opponent)}"
        )

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
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
