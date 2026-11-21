"""联机对战独立窗口：登录、好友房大厅和在线棋盘。

离线双人/AI 窗口保持不变；用户通过主窗口“联机对战”按钮打开本窗口。
登录成功后使用 HTTPS 认证 + WebSocket 好友房；在线落子只由服务器权威快照
驱动，不在本地抢先更新。
"""

from __future__ import annotations

import logging
import time
from typing import Mapping, Optional

from PySide6.QtCore import QUrl, Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
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
from gomoku.client.board_view import BoardView
from gomoku.client.ui_theme import ONLINE_BACKDROP_SPEC, InkBackdrop
from gomoku.client.board_widget import BoardWidget
from gomoku.client.config import AppConfig
from gomoku.client.constants import (
    WINDOW_DEFAULT_HEIGHT,
    WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_SCREEN_MARGIN,
)
from gomoku.client.net.http_api import AuthHTTPClient
from gomoku.client.net.profile_api import ProfileHTTPClient
from gomoku.client.net.websocket_transport import QtWebSocketTransport
from gomoku.client.sessions.online import OnlineGameSession
from gomoku.client.sound_control import SoundButton
from gomoku.shared.errors import TransportError
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.rules import BOARD_SIZE, Cell, Player

logger = logging.getLogger(__name__)

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


class OnlinePlayerCard(QFrame):
    """在线对局身份卡，可随再战交换黑白更新颜色。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("playerCard")
        self.setProperty("active", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(58)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(12)

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

        layout.addWidget(self._stone, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text_layout, 1)

    def set_content(
        self,
        name: str,
        color: Player,
        active: bool,
        status: str,
        rating: Optional[int] = None,
    ) -> None:
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


class OnlineWindow(QMainWindow):
    """登录、好友房和联机对局窗口。"""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        super().__init__()
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

        self.setWindowTitle("五子棋 - 联机对战")
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)
        self._build_ui()
        self._apply_initial_geometry()
        self._show_auth_page()
        self._refresh_auth_availability()

    # ------------------------------------------------------------------
    # 地址与几何
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

    def _apply_initial_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(WINDOW_DEFAULT_WIDTH, WINDOW_DEFAULT_HEIGHT)
            return
        available = screen.availableGeometry()
        width = min(
            WINDOW_DEFAULT_WIDTH,
            max(WINDOW_MIN_WIDTH, available.width() - WINDOW_SCREEN_MARGIN),
        )
        height = min(
            WINDOW_DEFAULT_HEIGHT,
            max(WINDOW_MIN_HEIGHT, available.height() - WINDOW_SCREEN_MARGIN),
        )
        self.resize(width, height)
        rect = self.frameGeometry()
        rect.moveCenter(available.center())
        self.move(rect.topLeft())

    # ------------------------------------------------------------------
    # 页面构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = InkBackdrop(self, spec=ONLINE_BACKDROP_SPEC)
        central.setObjectName("onlineCentral")
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        # 声音设置在登录、大厅和对局页都保持可见。
        utility_row = QHBoxLayout()
        utility_row.setContentsMargins(0, 0, 2, 0)
        utility_row.addStretch(1)
        self.sound_button = SoundButton(central)
        utility_row.addWidget(
            self.sound_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        root.addLayout(utility_row)

        self.stack = QStackedWidget(central)
        self.stack.setObjectName("onlineStack")
        self.auth_page = self._build_auth_page()
        self.lobby_page = self._build_lobby_page()
        self.game_page = self._build_game_page()
        self.stack.addWidget(self.auth_page)
        self.stack.addWidget(self.lobby_page)
        self.stack.addWidget(self.game_page)
        root.addWidget(self.stack)
        self.setCentralWidget(central)

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
            "登录后可参加排位匹配、查看排行榜，也可创建好友房。",
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
        title = QLabel("对战大厅", card)
        title.setObjectName("onlineTitle")
        self.rating_badge_label = QLabel("未定级", card)
        self.rating_badge_label.setObjectName("rankBadge")
        self.rating_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rating_badge_label.setMinimumWidth(64)
        header.addWidget(title)
        header.addStretch(1)
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
            "按积分匹配对手；胜者加分、负者扣分，好友房不计分。", card
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
        friend_title = QLabel("好友房，休闲对局（不计分）", card)
        friend_title.setObjectName("sectionTitle")
        friend_hint = QLabel(
            "创建房间并把房间码发给好友，或输入好友的房间码加入。", card
        )
        friend_hint.setObjectName("hintLabel")
        friend_hint.setWordWrap(True)

        self.create_room_button = QPushButton("创建好友房", card)
        self.create_room_button.setObjectName("undoButton")
        self.create_room_button.setCursor(Qt.CursorShape.PointingHandCursor)

        room_row = QHBoxLayout()
        room_row.setSpacing(8)
        self.room_code_edit = QLineEdit(card)
        self.room_code_edit.setObjectName("onlineInput")
        self.room_code_edit.setPlaceholderText("输入 8 位好友房码")
        self.room_code_edit.setMaxLength(12)
        self.room_code_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.join_room_button = QPushButton("加入", card)
        self.join_room_button.setObjectName("undoButton")
        self.join_room_button.setCursor(Qt.CursorShape.PointingHandCursor)
        room_row.addWidget(self.room_code_edit, 1)
        room_row.addWidget(self.join_room_button)
        layout.addWidget(friend_title)
        layout.addWidget(friend_hint)
        layout.addWidget(self.create_room_button)
        layout.addLayout(room_row)

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
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.leaderboard_table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
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
        self.join_room_button.clicked.connect(self._on_join_room)
        self.room_code_edit.returnPressed.connect(self._on_join_room)
        self.refresh_leaderboard_button.clicked.connect(
            self._on_refresh_leaderboard
        )
        self.logout_button.clicked.connect(self._logout)

        return page

    def _build_game_page(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("onlinePage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self.room_title_label = QLabel("好友房", page)
        self.room_title_label.setObjectName("onlineRoomTitle")
        self.connection_label = QLabel("未连接", page)
        self.connection_label.setObjectName("onlineStatus")
        self.connection_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        top.addWidget(self.room_title_label)
        top.addStretch(1)
        top.addWidget(self.connection_label)
        layout.addLayout(top)

        cards = QHBoxLayout()
        cards.setSpacing(12)
        self.you_card = OnlinePlayerCard(page)
        self.opponent_card = OnlinePlayerCard(page)
        cards.addWidget(self.you_card, 1)
        cards.addWidget(self.opponent_card, 1)
        layout.addLayout(cards)

        self.board_widget = BoardWidget(_empty_view(), page)
        self.board_widget.set_interaction_enabled(False)
        self.board_widget.cell_clicked.connect(self._on_board_clicked)
        layout.addWidget(self.board_widget, 1)

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
        layout.addLayout(info)

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
        for widget in (
            self.create_room_button,
            self.room_code_edit,
            self.join_room_button,
        ):
            widget.setEnabled(friend_enabled)
        self.refresh_leaderboard_button.setEnabled(
            connected and not self._profile_client.busy
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
        self._update_rating_summary()

    def _on_leaderboard_ready(self, data: object) -> None:
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        self._leaderboard_entries = entries
        total = data.get("total", len(entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(entries)
        self._fill_leaderboard(entries, total)

    def _fill_leaderboard(self, entries: list, total: int) -> None:
        table = self.leaderboard_table
        valid = [entry for entry in entries if isinstance(entry, Mapping)]
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

    def _clear_profile_ui(self) -> None:
        self._profile_user = None
        self._leaderboard_entries = []
        self.leaderboard_table.setRowCount(0)
        self.leaderboard_status_label.setText("登录后自动加载排行榜")
        self._update_rating_summary()

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
        self._auth_client.logout()
        self._auth_session = None
        self._clear_profile_ui()
        self._show_auth_page()
        self._set_auth_status("已退出登录")

    def _on_logged_out(self) -> None:
        self._logging_out = False
        self._auth_session = None
        self._profile_client.abort_all()
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
        self._show_lobby_page("已连接联机服务器")
        self._set_match_status("")
        self._refresh_profile_data()
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
        self._auth_session = None
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
        self._set_lobby_status("正在创建好友房…")
        self._refresh_lobby_controls()
        if not self._session.create_room():
            self._room_action_pending = False
            self._set_lobby_status("无法创建房间，请检查连接", error=True)
            self._refresh_lobby_controls()

    def _on_join_room(self) -> None:
        if self._session is None:
            return
        code = self.room_code_edit.text().strip().upper()
        if not code:
            self._set_lobby_status("请输入好友的房间码", error=True)
            return
        if self._room_action_pending:
            return
        self._room_action_pending = True
        self._set_lobby_status("正在加入好友房…")
        self._refresh_lobby_controls()
        if not self._session.join_room(code):
            self._room_action_pending = False
            self._set_lobby_status("无法加入房间，请检查连接", error=True)
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
        self._set_lobby_status("")
        self._set_match_status(
            f"已匹配到 {opponent_name}，正在载入棋局…"
        )
        self._refresh_lobby_controls()

    def _on_room_created(self, code: str) -> None:
        self._room_action_pending = False
        self.room_code_edit.clear()
        self._set_match_status("")
        self._show_game_page()

    def _on_room_closed(self, reason: str) -> None:
        self._room_action_pending = False
        self._audio_game_id = None
        self.clock_label.setText("—")
        self._deadline_monotonic = None
        self.room_code_edit.clear()
        self.game_rating_label.clear()
        self.game_rating_label.setVisible(False)
        self._set_match_status("")
        self._show_lobby_page(reason)
        if self._auto_requeue:
            self._auto_requeue = False
            QTimer.singleShot(0, self._on_quick_match)

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
        if self._session is not None and self._session.rated:
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
        if self._session is None:
            return
        session = self._session
        if not session.is_authenticated:
            return
        if session.waiting_for_opponent or session.snapshot is not None:
            self.room_code_edit.clear()
            self._show_game_page()
            self._sync_game_page()
        elif self.stack.currentWidget() is self.game_page:
            self._show_lobby_page("")
        else:
            self._update_rating_summary()
            self._refresh_lobby_controls()

    def _sync_game_page(self) -> None:
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
        self.room_title_label.setText(
            "排位赛，积分对局"
            if rated
            else f"好友房，{session.room_code or '------'}"
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

        if waiting:
            self.you_card.set_content(
                you_name, your_color, False, "等待对手", your_rating
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                False,
                "等待对手加入",
                opponent_rating,
            )
            self._set_game_pill("等待对手", result=False)
            self.clock_label.setText("把房间码分享给好友")
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
            you_name, your_color, False, "等待", your_rating
        )
        self.opponent_card.set_content(
            opponent_name,
            opponent_color,
            False,
            "等待" if session.opponent_online else "已离线",
            opponent_rating,
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
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                winner is opponent_color,
                "获胜"
                if winner is opponent_color
                else ("平局" if winner is None else "本局结束"),
                opponent_rating,
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
            )
            self.opponent_card.set_content(
                opponent_name,
                opponent_color,
                current is opponent_color,
                "已离线"
                if not session.opponent_online
                else ("落子中" if current is opponent_color else "等待"),
                opponent_rating,
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
    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._heartbeat_timer.stop()
        self._clock_timer.stop()
        self._reconnect_timer.stop()
        self._auth_client.abort()
        self._profile_client.abort_all()
        self._teardown_ws()
        super().closeEvent(event)


__all__ = ["OnlineWindow", "OnlinePlayerCard"]
