"""对战大厅页构建器。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarButton
from gomoku.shared.time_control import time_control_presets


def build_lobby_page(panel: Any) -> QWidget:
    """构建大厅页，并把控件挂到 panel 上供后续逻辑使用。"""
    page = QWidget(panel)
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
    panel.self_avatar = AvatarButton("", "", 42, card)
    panel.self_avatar.clicked_id.connect(panel._open_user_profile)
    panel.self_avatar.setVisible(False)
    title = QLabel("对战大厅", card)
    title.setObjectName("onlineTitle")
    panel.rating_badge_label = QLabel("未定级", card)
    panel.rating_badge_label.setObjectName("rankBadge")
    panel.rating_badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    panel.rating_badge_label.setMinimumWidth(64)
    panel.friends_button = QPushButton("好友", card)
    panel.friends_button.setObjectName("smallButton")
    panel.friends_button.setCursor(Qt.CursorShape.PointingHandCursor)
    panel.friends_button.setAutoDefault(False)
    # 对战广场：进行中对局的观战入口，单独占一页，不占大厅。
    panel.plaza_button = QPushButton("对战广场", card)
    panel.plaza_button.setObjectName("smallButton")
    panel.plaza_button.setCursor(Qt.CursorShape.PointingHandCursor)
    panel.plaza_button.setAutoDefault(False)
    header.addWidget(panel.self_avatar)
    header.addWidget(title)
    header.addStretch(1)
    header.addWidget(panel.plaza_button)
    header.addWidget(panel.friends_button)
    header.addWidget(panel.rating_badge_label)
    layout.addLayout(header)

    panel.lobby_welcome_label = QLabel("已登录", card)
    panel.lobby_welcome_label.setObjectName("onlineSubtitle")
    panel.rating_summary_label = QLabel(
        "积分 1200，尚未完成排位赛", card
    )
    panel.rating_summary_label.setObjectName("rankSummary")
    layout.addWidget(panel.lobby_welcome_label)
    layout.addWidget(panel.rating_summary_label)

    layout.addSpacing(4)
    ranked_title = QLabel("排位匹配，计入段位积分", card)
    ranked_title.setObjectName("sectionTitle")
    ranked_hint = QLabel(
        "按积分匹配对手，排位固定每手 30 秒；胜者加分、负者扣分，"
        "自建房不计分。",
        card,
    )
    ranked_hint.setObjectName("hintLabel")
    ranked_hint.setWordWrap(True)

    panel.quick_match_button = QPushButton("开始排位匹配", card)
    panel.quick_match_button.setObjectName("restartButton")
    panel.quick_match_button.setCursor(
        Qt.CursorShape.PointingHandCursor
    )
    panel.quick_match_button.setMinimumHeight(40)
    panel.match_status_label = QLabel(card)
    panel.match_status_label.setObjectName("onlineStatus")
    panel.match_status_label.setWordWrap(True)
    panel.match_status_label.setMinimumHeight(20)
    layout.addWidget(ranked_title)
    layout.addWidget(ranked_hint)
    # 排位赛固定每手 30 秒，不提供自定义入口；自建房才可选时限。
    ranked_time_label = QLabel("时限：每手 30 秒（排位固定）", card)
    ranked_time_label.setObjectName("formLabel")
    layout.addWidget(ranked_time_label)
    layout.addWidget(panel.quick_match_button)
    layout.addWidget(panel.match_status_label)

    layout.addSpacing(4)
    friend_title = QLabel("自建房，休闲对局（不计分）", card)
    friend_title.setObjectName("sectionTitle")
    friend_hint = QLabel(
        "创建后直接从好友列表邀请在线好友加入，无需房间码。", card
    )
    friend_hint.setObjectName("hintLabel")
    friend_hint.setWordWrap(True)

    panel.allow_spectators_checkbox = QCheckBox(
        "允许观战（会出现在对战广场）", card
    )
    panel.allow_spectators_checkbox.setObjectName(
        "allowSpectatorsCheck"
    )
    panel.allow_spectators_checkbox.setChecked(True)
    panel.allow_spectators_checkbox.setVisible(False)
    time_row = QHBoxLayout()
    time_label = QLabel("时限", card)
    time_label.setObjectName("formLabel")
    panel.turn_seconds_combo = QComboBox(card)
    for control in time_control_presets():
        panel.turn_seconds_combo.addItem(control.label(), control)
    panel.turn_seconds_combo.setCurrentIndex(1)
    time_row.addWidget(time_label)
    time_row.addWidget(panel.turn_seconds_combo)
    time_row.addStretch(1)
    # 服务端太旧时不支持自定义时限，由面板在联机后显示原因。
    panel.time_control_hint = QLabel("", card)
    panel.time_control_hint.setObjectName("hintLabel")
    panel.time_control_hint.setWordWrap(True)
    panel.time_control_hint.setVisible(False)
    panel.create_room_button = QPushButton("创建自建房", card)
    panel.create_room_button.setObjectName("undoButton")
    panel.create_room_button.setCursor(Qt.CursorShape.PointingHandCursor)
    layout.addWidget(friend_title)
    layout.addWidget(friend_hint)
    layout.addWidget(panel.allow_spectators_checkbox)
    layout.addLayout(time_row)
    layout.addWidget(panel.time_control_hint)
    layout.addWidget(panel.create_room_button)

    layout.addSpacing(4)
    leaderboard_header = QHBoxLayout()
    leaderboard_title = QLabel("排行榜，前 50 名", card)
    leaderboard_title.setObjectName("sectionTitle")
    panel.refresh_leaderboard_button = QPushButton("刷新", card)
    panel.refresh_leaderboard_button.setObjectName("smallButton")
    panel.refresh_leaderboard_button.setCursor(
        Qt.CursorShape.PointingHandCursor
    )
    leaderboard_header.addWidget(leaderboard_title)
    leaderboard_header.addStretch(1)
    leaderboard_header.addWidget(panel.refresh_leaderboard_button)
    layout.addLayout(leaderboard_header)

    panel.leaderboard_table = QTableWidget(0, 5, card)
    panel.leaderboard_table.setObjectName("leaderboardTable")
    panel.leaderboard_table.setHorizontalHeaderLabels(
        ["排名", "玩家", "段位", "积分", "战绩"]
    )
    panel.leaderboard_table.verticalHeader().setVisible(False)
    panel.leaderboard_table.setEditTriggers(
        QAbstractItemView.EditTrigger.NoEditTriggers
    )
    panel.leaderboard_table.setSelectionMode(
        QAbstractItemView.SelectionMode.SingleSelection
    )
    panel.leaderboard_table.setSelectionBehavior(
        QAbstractItemView.SelectionBehavior.SelectRows
    )
    panel.leaderboard_table.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
    panel.leaderboard_table.setIconSize(QSize(20, 20))
    panel.leaderboard_table.setShowGrid(False)
    panel.leaderboard_table.setAlternatingRowColors(True)
    panel.leaderboard_table.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    panel.leaderboard_table.verticalHeader().setDefaultSectionSize(30)
    panel.leaderboard_table.setFixedHeight(228)
    header_view = panel.leaderboard_table.horizontalHeader()
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
    panel.leaderboard_table.setColumnWidth(0, 46)
    panel.leaderboard_table.setColumnWidth(2, 58)
    panel.leaderboard_table.setColumnWidth(3, 58)
    panel.leaderboard_table.setColumnWidth(4, 96)
    header_item = panel.leaderboard_table.horizontalHeaderItem(4)
    if header_item is not None:
        header_item.setToolTip("胜-负-平")
    layout.addWidget(panel.leaderboard_table)

    panel.leaderboard_status_label = QLabel(
        "登录后自动加载排行榜", card
    )
    panel.leaderboard_status_label.setObjectName("hintLabel")
    panel.leaderboard_status_label.setWordWrap(True)
    layout.addWidget(panel.leaderboard_status_label)

    layout.addSpacing(4)
    recent_header = QHBoxLayout()
    recent_title = QLabel("最近对局", card)
    recent_title.setObjectName("sectionTitle")
    panel.replay_history_button = QPushButton("复盘选中对局", card)
    panel.replay_history_button.setObjectName("undoButton")
    panel.replay_history_button.setCursor(
        Qt.CursorShape.PointingHandCursor
    )
    panel.replay_history_button.setEnabled(False)
    panel.refresh_history_button = QPushButton("刷新", card)
    panel.refresh_history_button.setObjectName("smallButton")
    panel.refresh_history_button.setCursor(
        Qt.CursorShape.PointingHandCursor
    )
    recent_header.addWidget(recent_title)
    recent_header.addStretch(1)
    recent_header.addWidget(panel.replay_history_button)
    recent_header.addWidget(panel.refresh_history_button)
    layout.addLayout(recent_header)

    panel.recent_games_table = QTableWidget(0, 5, card)
    panel.recent_games_table.setObjectName("leaderboardTable")
    panel.recent_games_table.setHorizontalHeaderLabels(
        ["时间", "对手", "结果", "模式", "手数"]
    )
    panel.recent_games_table.verticalHeader().setVisible(False)
    panel.recent_games_table.setEditTriggers(
        QAbstractItemView.EditTrigger.NoEditTriggers
    )
    panel.recent_games_table.setSelectionMode(
        QAbstractItemView.SelectionMode.SingleSelection
    )
    panel.recent_games_table.setSelectionBehavior(
        QAbstractItemView.SelectionBehavior.SelectRows
    )
    panel.recent_games_table.setShowGrid(False)
    panel.recent_games_table.setAlternatingRowColors(True)
    panel.recent_games_table.setHorizontalScrollBarPolicy(
        Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    )
    panel.recent_games_table.verticalHeader().setDefaultSectionSize(30)
    panel.recent_games_table.setFixedHeight(198)
    recent_header_view = panel.recent_games_table.horizontalHeader()
    recent_header_view.setHighlightSections(False)
    recent_header_view.setStretchLastSection(False)
    recent_header_view.setMinimumSectionSize(42)
    for column, width in ((0, 92), (2, 48), (3, 64), (4, 48)):
        recent_header_view.setSectionResizeMode(
            column, QHeaderView.ResizeMode.Fixed
        )
        panel.recent_games_table.setColumnWidth(column, width)
    recent_header_view.setSectionResizeMode(
        1, QHeaderView.ResizeMode.Stretch
    )
    layout.addWidget(panel.recent_games_table)

    panel.history_status_label = QLabel(
        "登录后自动加载最近对局", card
    )
    panel.history_status_label.setObjectName("hintLabel")
    panel.history_status_label.setWordWrap(True)
    layout.addWidget(panel.history_status_label)

    footer = QHBoxLayout()
    panel.lobby_status_label = QLabel(card)
    panel.lobby_status_label.setObjectName("onlineStatus")
    panel.lobby_status_label.setWordWrap(True)
    panel.lobby_status_label.setMinimumHeight(22)
    panel.logout_button = QPushButton("退出登录", card)
    panel.logout_button.setObjectName("undoButton")
    panel.logout_button.setCursor(Qt.CursorShape.PointingHandCursor)
    footer.addWidget(panel.lobby_status_label, 1)
    footer.addWidget(panel.logout_button)
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

    panel.quick_match_button.clicked.connect(panel._on_quick_match)
    panel.create_room_button.clicked.connect(panel._on_create_room)
    panel.friends_button.clicked.connect(panel._open_friends_panel)
    panel.plaza_button.clicked.connect(panel._on_plaza_clicked)
    panel.leaderboard_table.cellClicked.connect(
        panel._on_leaderboard_cell_clicked
    )
    panel.refresh_leaderboard_button.clicked.connect(
        panel._on_refresh_leaderboard
    )
    panel.refresh_history_button.clicked.connect(
        panel._on_refresh_history
    )
    panel.replay_history_button.clicked.connect(
        panel._on_replay_history
    )
    panel.recent_games_table.itemSelectionChanged.connect(
        panel._on_recent_selection_changed
    )
    panel.recent_games_table.cellDoubleClicked.connect(
        panel._on_recent_double_clicked
    )
    panel.logout_button.clicked.connect(panel._logout)

    return page
