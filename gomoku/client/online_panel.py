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

from PySide6.QtCore import QSettings, QSize, QUrl, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.audio import get_audio_manager
from gomoku.client.auth import AuthSession
from gomoku.client.avatar_widgets import AvatarButton, avatar_icon
from gomoku.client.board_view import BoardView
from gomoku.client.chat_widgets import (
    ChatLogPanel,
    EmojiPickerButton,
    PresetCommentButton,
)
from gomoku.client.confirm_dialog import ConfirmDialog
from gomoku.client.friends_panel import FriendsPanel
from gomoku.client.history_records import record_from_game_detail
from gomoku.client.net.social_api import SocialHTTPClient
from gomoku.client.private_chat_panel import PrivateChatPanel
from gomoku.client.profile_panel import AvatarPickerPanel, ProfilePanel
from gomoku.client.ui_theme import ONLINE_BACKDROP_SPEC, InkBackdrop
from gomoku.client.board_widget import BoardWidget
from gomoku.client.config import AppConfig
from gomoku.client.danmaku import DanmakuOverlay
from gomoku.client.net.game_history_api import GameHistoryHTTPClient
from gomoku.client.net.http_api import AuthHTTPClient
from gomoku.client.net.profile_api import ProfileHTTPClient
from gomoku.client.net.websocket_transport import QtWebSocketTransport
from gomoku.client.sessions.online import OnlineGameSession
from gomoku.client.sound_control import SoundButton
from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.chat import (
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    MAX_COMMENT_LENGTH,
    ChatMessage,
)
from gomoku.shared.errors import TransportError
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.record import GameRecord, RecordError
from gomoku.shared.rules import BOARD_SIZE, Cell, Player
from gomoku.shared.social import DirectMessage, FriendEvent

logger = logging.getLogger(__name__)

#: 输入框的常规占位提示。
CHAT_INPUT_HINT = "说点什么…（回车发送）"
#: 对话日志展开状态在 QSettings 里的键名。
SETTINGS_CHAT_LOG_VISIBLE = "chat_log_visible"

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


def _empty_view() -> BoardView:
    return BoardView(
        board=_EMPTY_BOARD,
        current_player=Player.BLACK,
        last_move=None,
        winner=None,
        winning_line=(),
        is_draw=False,
        is_game_over=False,
        move_count=0,
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


class OnlinePlayerCard(QFrame):
    """在线对局身份卡，可随再战交换黑白更新颜色。"""

    profile_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerCard")
        self.setProperty("active", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(58)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

        self.avatar_button = AvatarButton("", "", 36, self)
        self.avatar_button.clicked_id.connect(self.profile_requested)

        self._stone = QLabel(self)
        self._stone.setObjectName("stoneBadgeBlack")
        self._stone.setFixedSize(26, 26)
        self._stone.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self._stone.setAccessibleName("棋子颜色")

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)
        self._name = QLabel("玩家", self)
        self._name.setObjectName("playerName")
        self._caption = QLabel("等待", self)
        self._caption.setObjectName("playerCaption")
        text_layout.addWidget(self._name)
        text_layout.addWidget(self._caption)

        layout.addWidget(
            self.avatar_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self._stone, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text_layout, 1)

    def set_content(
        self,
        name: str,
        color: Player,
        active: bool,
        status: str,
        rating: Optional[int] = None,
        avatar: str = "",
        user_id: str = "",
    ) -> None:
        self.avatar_button.set_avatar(avatar, user_id)
        badge_name = (
            "stoneBadgeBlack" if color is Player.BLACK else "stoneBadgeWhite"
        )
        if self._stone.objectName() != badge_name:
            self._stone.setObjectName(badge_name)
            self._stone.style().unpolish(self._stone)
            self._stone.style().polish(self._stone)

        self._name.setText(name or "玩家")
        color_text = "执黑，先手" if color is Player.BLACK else "执白，后手"
        rating_text = f"积分 {rating}，" if rating is not None else ""
        self._caption.setText(f"{color_text}，{rating_text}{status}")

        active_value = "true" if active else "false"
        if self.property("active") != active_value:
            self.setProperty("active", active_value)
            for widget in (self, self._name, self._caption):
                widget.style().unpolish(widget)
                widget.style().polish(widget)
            self.update()


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
        self._profile_user: Optional[dict] = None
        self._leaderboard_entries: list = []
        self._auto_requeue = False
        self._refresh_after_auth_refresh = False
        # 对话日志的展开状态（逻辑值，不受页面是否在前台影响）。
        self._chat_log_expanded = False
        # 用户偏好（对话日志展开状态）通过 QSettings 持久化。
        self._settings = QSettings()

        self._api_base_url, self._ws_url = self._resolve_urls()
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
        self._friends_panel: Optional[FriendsPanel] = None
        self._profile_panel: Optional[ProfilePanel] = None
        self._chat_panels: dict[str, PrivateChatPanel] = {}
        self._pending_profile_user_id = ""
        self._pending_chat_user: Optional[dict] = None

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

        self._build_ui()
        self._show_auth_page()
        self._refresh_auth_availability()

    # ------------------------------------------------------------------
    # 地址
    # ------------------------------------------------------------------
    def _resolve_urls(self) -> tuple[str, str]:
        """解析 REST/WS 地址；开发环境默认本机 8000 端口。"""
        api = (self.config.api_base_url or "").rstrip("/")
        ws_base = (self.config.ws_base_url or "").rstrip("/")
        is_dev = self.config.environment.lower() in {
            "development",
            "dev",
            "test",
            "local",
        }
        if not api and is_dev:
            api = "http://127.0.0.1:8000"
        if not ws_base and api:
            parsed = QUrl(api)
            ws_scheme = "wss" if parsed.scheme() == "https" else "ws"
            ws_base = f"{ws_scheme}://{parsed.authority()}"
        if not ws_base:
            return api, ""
        if ws_base.endswith("/api/v1/ws"):
            return api, ws_base
        return api, ws_base + "/api/v1/ws"

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
        root.addLayout(utility_row)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName("onlineStack")
        self.auth_page = self._build_auth_page()
        self.lobby_page = self._build_lobby_page()
        self.game_page = self._build_game_page()
        self.stack.addWidget(self.auth_page)
        self.stack.addWidget(self.lobby_page)
        self.stack.addWidget(self.game_page)
        root.addWidget(self.stack)

    def _on_back_clicked(self) -> None:
        """“返回主菜单”按钮：交由主窗口切页。"""
        self.back_requested.emit()

    def in_room(self) -> bool:
        """当前是否在房间或匹配队列中。"""
        session = self._session
        if session is None:
            return False
        return bool(session.room_code) or session.queued

    def _make_centered_card_page(self, card: QFrame) -> QWidget:
        page = QWidget(self)
        page.setObjectName("onlinePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addStretch(1)
        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    def _build_auth_page(self) -> QWidget:
        card = QFrame(self)
        card.setObjectName("onlineCard")
        card.setMaximumWidth(470)
        card.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(28, 24, 28, 24)
        card_layout.setSpacing(9)

        title = QLabel("五子棋 - 联机对战", card)
        title.setObjectName("onlineTitle")
        subtitle = QLabel(
            "登录后可参加排位匹配、查看排行榜，也可创建自建房邀请好友。",
            card,
        )
        subtitle.setObjectName("onlineSubtitle")
        subtitle.setWordWrap(True)
        self.server_label = QLabel(card)
        self.server_label.setObjectName("hintLabel")
        self.server_label.setWordWrap(True)
        self.server_label.setText(
            f"服务器：{self._api_base_url or '未配置'}"
        )

        self.username_edit = QLineEdit(card)
        self.username_edit.setObjectName("onlineInput")
        self.username_edit.setPlaceholderText("用户名")
        self.password_edit = QLineEdit(card)
        self.password_edit.setObjectName("onlineInput")
        self.password_edit.setPlaceholderText("密码（至少 8 位）")
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addWidget(self.server_label)
        for label_text, widget in (
            ("用户名", self.username_edit),
            ("密码", self.password_edit),
        ):
            label = QLabel(label_text, card)
            label.setObjectName("formLabel")
            label.setBuddy(widget)
            card_layout.addWidget(label)
            card_layout.addWidget(widget)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.login_button = QPushButton("登录", card)
        self.login_button.setObjectName("restartButton")
        self.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.register_button = QPushButton("注册", card)
        self.register_button.setObjectName("undoButton")
        self.register_button.setCursor(Qt.CursorShape.PointingHandCursor)
        buttons.addWidget(self.login_button)
        buttons.addWidget(self.register_button)
        card_layout.addLayout(buttons)

        self.auth_status_label = QLabel(card)
        self.auth_status_label.setObjectName("onlineStatus")
        self.auth_status_label.setWordWrap(True)
        self.auth_status_label.setMinimumHeight(22)
        card_layout.addWidget(self.auth_status_label)

        self.login_button.clicked.connect(self._on_login_clicked)
        self.register_button.clicked.connect(self._on_register_clicked)
        self.password_edit.returnPressed.connect(self._on_login_clicked)

        return self._make_centered_card_page(card)

    def _build_lobby_page(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("onlinePage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(page)
        scroll.setObjectName("onlineScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        viewport = scroll.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )

        container = QWidget(scroll)
        container.setObjectName("onlineScrollBody")
        container.setAutoFillBackground(False)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(18, 10, 18, 10)
        outer.setSpacing(12)
        outer.addStretch(1)

        card = QFrame(container)
        card.setObjectName("onlineCard")
        card.setMaximumWidth(660)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(9)

        header = QHBoxLayout()
        self.self_avatar = AvatarButton("", "", 42, card)
        self.self_avatar.clicked_id.connect(self._open_user_profile)
        self.self_avatar.setVisible(False)
        title = QLabel("对战大厅", card)
        title.setObjectName("onlineTitle")
        self.rating_badge_label = QLabel("未定级", card)
        self.rating_badge_label.setObjectName("rankBadge")
        self.rating_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rating_badge_label.setMinimumWidth(64)
        self.friends_button = QPushButton("好友", card)
        self.friends_button.setObjectName("smallButton")
        self.friends_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.friends_button.setAutoDefault(False)
        header.addWidget(self.self_avatar)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.friends_button)
        header.addWidget(self.rating_badge_label)
        layout.addLayout(header)

        self.lobby_welcome_label = QLabel("已登录", card)
        self.lobby_welcome_label.setObjectName("onlineSubtitle")
        self.rating_summary_label = QLabel(
            "积分 1200，尚未完成排位赛", card
        )
        self.rating_summary_label.setObjectName("rankSummary")
        layout.addWidget(self.lobby_welcome_label)
        layout.addWidget(self.rating_summary_label)

        layout.addSpacing(4)
        ranked_title = QLabel("排位匹配，计入段位积分", card)
        ranked_title.setObjectName("sectionTitle")
        ranked_hint = QLabel(
            "按积分匹配对手；胜者加分、负者扣分，自建房不计分。", card
        )
        ranked_hint.setObjectName("hintLabel")
        ranked_hint.setWordWrap(True)

        self.quick_match_button = QPushButton("开始排位匹配", card)
        self.quick_match_button.setObjectName("restartButton")
        self.quick_match_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.quick_match_button.setMinimumHeight(40)
        self.match_status_label = QLabel(card)
        self.match_status_label.setObjectName("onlineStatus")
        self.match_status_label.setWordWrap(True)
        self.match_status_label.setMinimumHeight(20)
        layout.addWidget(ranked_title)
        layout.addWidget(ranked_hint)
        layout.addWidget(self.quick_match_button)
        layout.addWidget(self.match_status_label)

        layout.addSpacing(4)
        friend_title = QLabel("自建房，休闲对局（不计分）", card)
        friend_title.setObjectName("sectionTitle")
        friend_hint = QLabel(
            "创建后直接从好友列表邀请在线好友加入，无需房间码。", card
        )
        friend_hint.setObjectName("hintLabel")
        friend_hint.setWordWrap(True)

        self.create_room_button = QPushButton("创建自建房", card)
        self.create_room_button.setObjectName("undoButton")
        self.create_room_button.setCursor(Qt.CursorShape.PointingHandCursor)
        layout.addWidget(friend_title)
        layout.addWidget(friend_hint)
        layout.addWidget(self.create_room_button)

        layout.addSpacing(4)
        leaderboard_header = QHBoxLayout()
        leaderboard_title = QLabel("排行榜，前 50 名", card)
        leaderboard_title.setObjectName("sectionTitle")
        self.refresh_leaderboard_button = QPushButton("刷新", card)
        self.refresh_leaderboard_button.setObjectName("smallButton")
        self.refresh_leaderboard_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        leaderboard_header.addWidget(leaderboard_title)
        leaderboard_header.addStretch(1)
        leaderboard_header.addWidget(self.refresh_leaderboard_button)
        layout.addLayout(leaderboard_header)

        self.leaderboard_table = QTableWidget(0, 5, card)
        self.leaderboard_table.setObjectName("leaderboardTable")
        self.leaderboard_table.setHorizontalHeaderLabels(
            ["排名", "玩家", "段位", "积分", "战绩"]
        )
        self.leaderboard_table.verticalHeader().setVisible(False)
        self.leaderboard_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.leaderboard_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.leaderboard_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.leaderboard_table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.leaderboard_table.setIconSize(QSize(20, 20))
        self.leaderboard_table.setShowGrid(False)
        self.leaderboard_table.setAlternatingRowColors(True)
        self.leaderboard_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.leaderboard_table.verticalHeader().setDefaultSectionSize(30)
        self.leaderboard_table.setFixedHeight(228)
        header_view = self.leaderboard_table.horizontalHeader()
        header_view.setHighlightSections(False)
        header_view.setStretchLastSection(False)
        header_view.setMinimumSectionSize(42)
        header_view.setSectionResizeMode(
            0, QHeaderView.ResizeMode.Fixed
        )
        header_view.setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        header_view.setSectionResizeMode(
            2, QHeaderView.ResizeMode.Fixed
        )
        header_view.setSectionResizeMode(
            3, QHeaderView.ResizeMode.Fixed
        )
        header_view.setSectionResizeMode(
            4, QHeaderView.ResizeMode.Fixed
        )
        self.leaderboard_table.setColumnWidth(0, 46)
        self.leaderboard_table.setColumnWidth(2, 58)
        self.leaderboard_table.setColumnWidth(3, 58)
        self.leaderboard_table.setColumnWidth(4, 96)
        header_item = self.leaderboard_table.horizontalHeaderItem(4)
        if header_item is not None:
            header_item.setToolTip("胜-负-平")
        layout.addWidget(self.leaderboard_table)

        self.leaderboard_status_label = QLabel(
            "登录后自动加载排行榜", card
        )
        self.leaderboard_status_label.setObjectName("hintLabel")
        self.leaderboard_status_label.setWordWrap(True)
        layout.addWidget(self.leaderboard_status_label)

        layout.addSpacing(4)
        recent_header = QHBoxLayout()
        recent_title = QLabel("最近对局", card)
        recent_title.setObjectName("sectionTitle")
        self.replay_history_button = QPushButton("复盘选中对局", card)
        self.replay_history_button.setObjectName("undoButton")
        self.replay_history_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.replay_history_button.setEnabled(False)
        self.refresh_history_button = QPushButton("刷新", card)
        self.refresh_history_button.setObjectName("smallButton")
        self.refresh_history_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        recent_header.addWidget(recent_title)
        recent_header.addStretch(1)
        recent_header.addWidget(self.replay_history_button)
        recent_header.addWidget(self.refresh_history_button)
        layout.addLayout(recent_header)

        self.recent_games_table = QTableWidget(0, 5, card)
        self.recent_games_table.setObjectName("leaderboardTable")
        self.recent_games_table.setHorizontalHeaderLabels(
            ["时间", "对手", "结果", "模式", "手数"]
        )
        self.recent_games_table.verticalHeader().setVisible(False)
        self.recent_games_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.recent_games_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.recent_games_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.recent_games_table.setShowGrid(False)
        self.recent_games_table.setAlternatingRowColors(True)
        self.recent_games_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.recent_games_table.verticalHeader().setDefaultSectionSize(30)
        self.recent_games_table.setFixedHeight(198)
        recent_header_view = self.recent_games_table.horizontalHeader()
        recent_header_view.setHighlightSections(False)
        recent_header_view.setStretchLastSection(False)
        recent_header_view.setMinimumSectionSize(42)
        for column, width in ((0, 92), (2, 48), (3, 64), (4, 48)):
            recent_header_view.setSectionResizeMode(
                column, QHeaderView.ResizeMode.Fixed
            )
            self.recent_games_table.setColumnWidth(column, width)
        recent_header_view.setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.recent_games_table)

        self.history_status_label = QLabel(
            "登录后自动加载最近对局", card
        )
        self.history_status_label.setObjectName("hintLabel")
        self.history_status_label.setWordWrap(True)
        layout.addWidget(self.history_status_label)

        footer = QHBoxLayout()
        self.lobby_status_label = QLabel(card)
        self.lobby_status_label.setObjectName("onlineStatus")
        self.lobby_status_label.setWordWrap(True)
        self.lobby_status_label.setMinimumHeight(22)
        self.logout_button = QPushButton("退出登录", card)
        self.logout_button.setObjectName("undoButton")
        self.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
        footer.addWidget(self.lobby_status_label, 1)
        footer.addWidget(self.logout_button)
        layout.addSpacing(2)
        layout.addLayout(footer)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.addStretch(1)
        card_row.addWidget(card, 20)
        card_row.addStretch(1)
        outer.addLayout(card_row)
        outer.addStretch(1)
        scroll.setWidget(container)
        page_layout.addWidget(scroll)

        self.quick_match_button.clicked.connect(self._on_quick_match)
        self.create_room_button.clicked.connect(self._on_create_room)
        self.friends_button.clicked.connect(self._open_friends_panel)
        self.leaderboard_table.cellClicked.connect(
            self._on_leaderboard_cell_clicked
        )
        self.refresh_leaderboard_button.clicked.connect(
            self._on_refresh_leaderboard
        )
        self.refresh_history_button.clicked.connect(
            self._on_refresh_history
        )
        self.replay_history_button.clicked.connect(
            self._on_replay_history
        )
        self.recent_games_table.itemSelectionChanged.connect(
            self._on_recent_selection_changed
        )
        self.recent_games_table.cellDoubleClicked.connect(
            self._on_recent_double_clicked
        )
        self.logout_button.clicked.connect(self._logout)

        return page

    def _build_game_page(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("onlinePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(6, 12, 6, 12)
        layout.setSpacing(8)

        top = QHBoxLayout()
        self.room_title_label = QLabel("自建房", page)
        self.room_title_label.setObjectName("onlineRoomTitle")
        self.invite_friend_button = QPushButton("邀请好友", page)
        self.invite_friend_button.setObjectName("smallButton")
        self.invite_friend_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.invite_friend_button.setAutoDefault(False)
        self.invite_friend_button.setVisible(False)
        self.connection_label = QLabel("未连接", page)
        self.connection_label.setObjectName("onlineStatus")
        self.connection_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        top.addWidget(self.room_title_label)
        top.addWidget(self.invite_friend_button)
        top.addStretch(1)
        top.addWidget(self.connection_label)
        layout.addLayout(top)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.you_card = OnlinePlayerCard(page)
        self.opponent_card = OnlinePlayerCard(page)
        self.you_card.profile_requested.connect(self._open_user_profile)
        self.opponent_card.profile_requested.connect(
            self._open_user_profile
        )
        cards.addWidget(self.you_card, 1)
        cards.addWidget(self.opponent_card, 1)
        layout.addLayout(cards)

        # 棋盘与对话日志并排：左侧棋盘，右侧只读对话日志。
        play_area = QHBoxLayout()
        play_area.setSpacing(6)

        left_column = QVBoxLayout()
        left_column.setSpacing(8)

        # 棋盘容器：弹幕覆盖层与棋盘叠放在同一个网格单元格中。
        self.board_container = QWidget(page)
        self.board_container.setObjectName("boardContainer")
        board_grid = QGridLayout(self.board_container)
        board_grid.setContentsMargins(0, 0, 0, 0)
        board_grid.setSpacing(0)

        self.board_widget = BoardWidget(_empty_view(), self.board_container)
        self.board_widget.set_interaction_enabled(False)
        self.board_widget.cell_clicked.connect(self._on_board_clicked)
        board_grid.addWidget(self.board_widget, 0, 0)

        self.danmaku_overlay = DanmakuOverlay(self.board_container)
        board_grid.addWidget(self.danmaku_overlay, 0, 0)
        # 让弹幕以棋面（居中的正方形）为活动范围，而不是控件留白。
        self.danmaku_overlay.set_board_widget(self.board_widget)

        left_column.addWidget(self.board_container, 1)

        info = QHBoxLayout()
        info.setSpacing(10)
        self.game_status_pill = QLabel("等待连接", page)
        self.game_status_pill.setObjectName("turnPill")
        self.game_status_pill.setProperty("result", "false")
        self.game_status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.game_status_pill.setMinimumWidth(112)
        self.clock_label = QLabel("—", page)
        self.clock_label.setObjectName("onlineClock")
        self.move_label = QLabel("第 0 手", page)
        self.move_label.setObjectName("moveInfo")
        info.addWidget(self.game_status_pill)
        info.addWidget(self.clock_label)
        info.addStretch(1)
        info.addWidget(self.move_label)
        left_column.addLayout(info)

        self.chat_log = ChatLogPanel(page)
        self.chat_log.setObjectName("chatLogPanel")
        self.chat_log.setMinimumWidth(210)
        self.chat_log.setMaximumWidth(300)
        self.chat_log.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
        )

        play_area.addLayout(left_column, 1)
        play_area.addWidget(self.chat_log, 0)
        layout.addLayout(play_area, 1)

        # 聊天输入栏：表情、快捷评论与手动输入。
        chat_bar = QHBoxLayout()
        chat_bar.setSpacing(8)
        self.emoji_button = EmojiPickerButton(page)
        self.preset_comment_button = PresetCommentButton(page)
        self.chat_log_toggle_button = QPushButton("对话日志", page)
        self.chat_log_toggle_button.setObjectName("chatLogToggleButton")
        self.chat_log_toggle_button.setCheckable(True)
        self.chat_input = QLineEdit(page)
        self.chat_input.setObjectName("chatInput")
        self.chat_input.setMaxLength(MAX_COMMENT_LENGTH)
        self.chat_input.setPlaceholderText(CHAT_INPUT_HINT)
        self.chat_send_button = QPushButton("发送", page)
        self.chat_send_button.setObjectName("chatSendButton")
        for button in (
            self.emoji_button,
            self.preset_comment_button,
            self.chat_log_toggle_button,
            self.chat_send_button,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
        chat_bar.addWidget(self.emoji_button)
        chat_bar.addWidget(self.preset_comment_button)
        chat_bar.addWidget(self.chat_input, 1)
        chat_bar.addWidget(self.chat_send_button)
        chat_bar.addWidget(self.chat_log_toggle_button)
        layout.addLayout(chat_bar)

        self.game_rating_label = QLabel(page)
        self.game_rating_label.setObjectName("onlineRatingResult")
        self.game_rating_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.game_rating_label.setWordWrap(True)
        self.game_rating_label.setVisible(False)
        layout.addWidget(self.game_rating_label)

        self.game_error_label = QLabel(page)
        self.game_error_label.setObjectName("onlineError")
        self.game_error_label.setWordWrap(True)
        self.game_error_label.setMinimumHeight(20)
        layout.addWidget(self.game_error_label)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)
        self.resign_button = QPushButton("认输", page)
        self.resign_button.setObjectName("undoButton")
        self.rematch_button = QPushButton("申请再战", page)
        self.rematch_button.setObjectName("restartButton")
        self.leave_button = QPushButton("离开房间", page)
        self.leave_button.setObjectName("undoButton")
        for button in (
            self.resign_button,
            self.rematch_button,
            self.leave_button,
        ):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            actions.addWidget(button)
        layout.addLayout(actions)

        self.resign_button.clicked.connect(self._on_resign)
        self.rematch_button.clicked.connect(self._on_rematch)
        self.leave_button.clicked.connect(self._on_leave_room)
        self.invite_friend_button.clicked.connect(
            self._on_invite_friend_clicked
        )
        self.emoji_button.emoji_picked.connect(self._on_emoji_picked)
        self.preset_comment_button.comment_picked.connect(
            self._on_preset_comment_picked
        )
        self.chat_send_button.clicked.connect(self._on_send_comment)
        self.chat_input.returnPressed.connect(self._on_send_comment)
        self.chat_log_toggle_button.toggled.connect(
            self._on_chat_log_toggled
        )
        # 对话日志默认收起：棋盘可以占满整行，需要时再点开。
        self._apply_chat_log_visibility(self._load_chat_log_visible())
        self._refresh_chat_controls()
        return page

    # ------------------------------------------------------------------
    # 页面/状态辅助
    # ------------------------------------------------------------------
    def _show_auth_page(self) -> None:
        self.stack.setCurrentWidget(self.auth_page)
        self.auth_status_label.setText("")
        self._refresh_auth_availability()

    def _show_lobby_page(self, message: str = "") -> None:
        self.stack.setCurrentWidget(self.lobby_page)
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

    def _show_game_page(self) -> None:
        if self.stack.currentWidget() is not self.game_page:
            self.stack.setCurrentWidget(self.game_page)

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
        self.logout_button.setEnabled(
            not self._auth_client.busy and not pending
        )

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
        self.rating_summary_label.setText(
            f"积分 {rating}，最高 {data.get('peak_rating', rating)}，{record}"
            if rated_games
            else f"积分 {rating}，{record}"
        )

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
        self._update_self_avatar()
        self._update_rating_summary()

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
        self.self_avatar.set_avatar(user.avatar, user.id)
        self.self_avatar.setVisible(True)

    def _self_profile(self) -> dict:
        user = dict(self._profile_user or {})
        if self._auth_session is not None:
            auth_user = self._auth_session.user
            user.setdefault("id", auth_user.id)
            user.setdefault("username", auth_user.username)
            user.setdefault("avatar", auth_user.avatar)
            user.setdefault("rating", auth_user.rating)
            user.setdefault("peak_rating", auth_user.peak_rating)
            user.setdefault("rated_games", auth_user.rated_games)
            user.setdefault("wins", auth_user.wins)
            user.setdefault("losses", auth_user.losses)
            user.setdefault("draws", auth_user.draws)
            user.setdefault("rank_name", auth_user.rank_name)
            user.setdefault("win_rate", auth_user.win_rate)
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
            window.destroyed.connect(
                lambda *_: setattr(self, "_profile_panel", None)
            )
            self._profile_panel = window
        self._profile_panel.set_profile(profile)
        self._profile_panel.show()

    def _open_avatar_picker(self) -> None:
        if self._auth_session is None:
            return
        picker = AvatarPickerPanel(
            self._auth_session.user.avatar, self
        )
        picker.avatar_selected.connect(self._on_avatar_selected)
        picker.exec()

    def _on_avatar_selected(self, avatar: str) -> None:
        if self._auth_session is None:
            return
        self._social_client.update_avatar(
            self._auth_session.access_token, avatar
        )

    def _apply_avatar_update(self, data: object) -> None:
        if not isinstance(data, Mapping) or self._auth_session is None:
            return
        avatar = data.get("avatar")
        if not isinstance(avatar, str):
            return
        user = replace(
            self._auth_session.user,
            avatar=normalize_avatar(
                avatar, self._auth_session.user.id
            ),
        )
        self._auth_session = replace(self._auth_session, user=user)
        if self._profile_user is not None:
            self._profile_user["avatar"] = user.avatar
        self._update_self_avatar()
        if (
            self._profile_panel is not None
            and self._profile_panel.user_id == user.id
        ):
            # 资料浮层此刻显示的不是自己时，不能把本人的头像盖上去。
            self._profile_panel.set_avatar(user.avatar)
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
        username, _ = self._username_avatar(user_id)
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
            return
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
    ) -> tuple[str, str]:
        current = getattr(self, "_current_profile", None)
        if isinstance(current, Mapping):
            user = current.get("user")
            if isinstance(user, Mapping) and user.get("id") == user_id:
                return (
                    str(user.get("username", fallback or "好友")),
                    str(user.get("avatar", "")),
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
                    )
        return fallback or "好友", ""

    # ------------------------------------------------------------------
    # 私聊
    # ------------------------------------------------------------------
    def _open_private_chat(
        self, user_id: str, username: str = ""
    ) -> None:
        if not user_id:
            return
        resolved_name, avatar = self._username_avatar(user_id, username)
        existing = self._chat_panels.get(user_id)
        if existing is not None:
            # 聊天窗可以被关闭，重新点“聊天”时把旧窗拉回来：刷新一下
            # 好友资料，再补拉一次历史（关闭期间对方可能发过消息）。
            existing.set_friend(resolved_name, avatar)
            existing.show()
            if self._auth_session is not None:
                self._social_client.fetch_messages(
                    self._auth_session.access_token, user_id, limit=200
                )
            return
        window = PrivateChatPanel(
            user_id, resolved_name, avatar, self
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
        else:
            self._show_status(f"收到 {message.sender_name} 的新消息")
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
        for panel in (self._friends_panel, self._profile_panel):
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

    def _clear_profile_ui(self) -> None:
        self._profile_user = None
        self._leaderboard_entries = []
        self.leaderboard_table.setRowCount(0)
        self.leaderboard_status_label.setText("登录后自动加载排行榜")
        self._update_rating_summary()
        self._recent_games_entries = []
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

    def _set_auth_form_busy(self, busy: bool) -> None:
        enabled = not busy and self.stack.currentWidget() is self.auth_page
        for widget in (
            self.username_edit,
            self.password_edit,
            self.login_button,
            self.register_button,
        ):
            widget.setEnabled(enabled)

    def _on_auth_busy_changed(self, busy: bool) -> None:
        if busy:
            self._set_auth_form_busy(True)
        elif self.stack.currentWidget() is self.auth_page:
            self._set_auth_form_busy(False)

    def _on_auth_session_ready(self, session: AuthSession) -> None:
        self._auth_session = session
        self._logging_out = False
        self._reconnect_attempts = 0
        self.password_edit.clear()
        self._clear_profile_ui()
        self._set_auth_status("登录成功，正在连接游戏服务器…")
        self._set_auth_form_busy(True)
        self._start_ws_session()

    def _on_auth_session_refreshed(self, session: AuthSession) -> None:
        self._auth_session = session
        if self._reconnect_after_refresh:
            self._reconnect_after_refresh = False
            self._open_transport(reset_status=False)
        if self._refresh_after_auth_refresh:
            self._refresh_after_auth_refresh = False
            self._refresh_profile_data()
        self._update_rating_summary()

    def _on_http_failed(self, message: str) -> None:
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
        self._social_client.abort_all()
        self._auth_client.logout()
        self._auth_session = None
        self._close_social_windows()
        self._clear_profile_ui()
        self._show_auth_page()
        self._set_auth_status("已退出登录")

    def _on_logged_out(self) -> None:
        self._logging_out = False
        self._auth_session = None
        self._profile_client.abort_all()
        self._history_client.abort_all()
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
        self._social_client.abort_all()
        self._auth_session = None
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
        if not self._session.create_room():
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
        self._show_lobby_page(reason)
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
        enabled = (
            self._session is not None
            and self._ws_connected
            and self._session.chat_available
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

    def _on_resign(self) -> None:
        if self._session is not None:
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
        if self._session is not None:
            QTimer.singleShot(0, self._refresh_profile_data)

    def _play_online_result_sound(self) -> None:
        """按服务器快照判断胜负，播放对应的终局音效。"""
        snapshot = self._session.snapshot if self._session is not None else None
        if snapshot is None or not snapshot.is_game_over:
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
        if session.waiting_for_opponent or session.snapshot is not None:
            self._show_game_page()
            self._sync_game_page()
        elif self.stack.currentWidget() is self.game_page:
            self._show_lobby_page("")
        else:
            self._update_rating_summary()
            self._refresh_lobby_controls()

    def _sync_game_page(self) -> None:
        self._refresh_chat_controls()
        session = self._session
        if session is None:
            return
        snapshot = session.snapshot
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
        if snapshot is not None:
            black_id = snapshot.black_user_id
            white_id = snapshot.white_user_id
            black_avatar = snapshot.black_avatar
            white_avatar = snapshot.white_avatar
        else:
            black_id = self_id if your_color is Player.BLACK else ""
            white_id = self_id if your_color is Player.WHITE else ""
            black_avatar = (
                self_avatar if your_color is Player.BLACK else ""
            )
            white_avatar = (
                self_avatar if your_color is Player.WHITE else ""
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

        if waiting:
            self.you_card.set_content(
                you_name,
                your_color,
                False,
                "等待对手",
                your_rating,
                avatar=you_avatar,
                user_id=you_id,
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
            self.board_widget.set_view(session.view)
            self.board_widget.set_interaction_enabled(False)
            self._set_action_visibility(
                resign=False, rematch=False, leave=True
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
        )
        self.opponent_card.set_content(
            opponent_name,
            opponent_color,
            False,
            "等待" if session.opponent_online else "已离线",
            opponent_rating,
            avatar=opponent_avatar,
            user_id=opponent_id,
        )
        if snapshot is None:
            self.clock_label.setText("—")
            self.move_label.setText("第 0 手")
            self.game_rating_label.clear()
            self.game_rating_label.setVisible(False)
            self.board_widget.set_interaction_enabled(False)
            return

        self._last_snapshot = snapshot
        self._maybe_play_online_start(snapshot)
        # 依据服务器时间计算倒计时，不在客户端自行判定超时胜负。
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

        current = snapshot.current_player
        over = snapshot.is_game_over
        winner = snapshot.winner
        if over:
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
        else:
            self.game_rating_label.clear()
            self.game_rating_label.setVisible(False)
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
        self.board_widget.set_interaction_enabled(
            self._ws_connected and session.accepts_human_input
        )
        self.board_widget.update()
        self._update_clock_label()

        self._set_action_visibility(
            resign=not over, rematch=over, leave=over
        )
        self.resign_button.setEnabled(not over and self._ws_connected)
        self.leave_button.setEnabled(over and self._ws_connected)
        if not over:
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
    ) -> None:
        self.resign_button.setVisible(resign)
        self.rematch_button.setVisible(rematch)
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
        if snapshot.is_game_over:
            self.clock_label.setText("对局结束")
            return
        if not self._ws_connected:
            self.clock_label.setText("连接中断")
            return
        if self._deadline_monotonic is None:
            self.clock_label.setText("等待服务器…")
            return
        remaining = self._deadline_monotonic - time.monotonic()
        if remaining <= 0:
            self.clock_label.setText("本手时间已到，等待服务器判定…")
        else:
            self.clock_label.setText(f"本手倒计时：{int(remaining + 0.999)} 秒")

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
        self._auth_client.abort()
        self._profile_client.abort_all()
        self._history_client.abort_all()
        self._social_client.abort_all()
        self._close_social_windows()
        self._teardown_ws()
        if hasattr(self, "danmaku_overlay"):
            self.danmaku_overlay.clear()


__all__ = ["OnlinePanel", "OnlinePlayerCard"]
