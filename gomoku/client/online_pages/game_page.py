"""联机对局/观战页构建器。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.board_widget import BoardWidget
from gomoku.client.chat_widgets import (
    ChatLogPanel,
    EmojiPickerButton,
    PresetCommentButton,
)
from gomoku.client.danmaku import DanmakuOverlay
from gomoku.client.online_pages.common import CHAT_INPUT_HINT, empty_view
from gomoku.client.online_player_card import OnlinePlayerCard
from gomoku.client.sandbox_panel import SandboxPanel
from gomoku.shared.chat import MAX_COMMENT_LENGTH


def build_game_page(panel: Any) -> QWidget:
    """构建联机对局/观战页，并把控件挂到 panel 上。"""
    page = QWidget(panel)
    page.setObjectName("onlinePage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(6, 12, 6, 12)
    layout.setSpacing(8)

    top = QHBoxLayout()
    panel.room_title_label = QLabel("自建房", page)
    panel.room_title_label.setObjectName("onlineRoomTitle")
    panel.invite_friend_button = QPushButton("邀请好友", page)
    panel.invite_friend_button.setObjectName("smallButton")
    panel.invite_friend_button.setCursor(
        Qt.CursorShape.PointingHandCursor
    )
    panel.invite_friend_button.setAutoDefault(False)
    panel.invite_friend_button.setVisible(False)
    panel.connection_label = QLabel("未连接", page)
    panel.connection_label.setObjectName("onlineStatus")
    panel.connection_label.setAlignment(Qt.AlignmentFlag.AlignRight)
    top.addWidget(panel.room_title_label)
    top.addWidget(panel.invite_friend_button)
    top.addStretch(1)
    top.addWidget(panel.connection_label)
    layout.addLayout(top)

    cards = QHBoxLayout()
    cards.setSpacing(12)
    panel.you_card = OnlinePlayerCard(page)
    panel.opponent_card = OnlinePlayerCard(page)
    panel.you_card.profile_requested.connect(panel._open_user_profile)
    panel.opponent_card.profile_requested.connect(
        panel._open_user_profile
    )
    cards.addWidget(panel.you_card, 1)
    cards.addWidget(panel.opponent_card, 1)
    layout.addLayout(cards)

    # 棋盘与对话日志并排：左侧棋盘，右侧只读对话日志。
    play_area = QHBoxLayout()
    play_area.setSpacing(6)

    left_column = QVBoxLayout()
    left_column.setSpacing(8)

    # 棋盘容器：弹幕覆盖层与棋盘叠放在同一个网格单元格中。
    panel.board_container = QWidget(page)
    panel.board_container.setObjectName("boardContainer")
    board_grid = QGridLayout(panel.board_container)
    board_grid.setContentsMargins(0, 0, 0, 0)
    board_grid.setSpacing(0)

    panel.board_widget = BoardWidget(empty_view(), panel.board_container)
    panel.board_widget.set_interaction_enabled(False)
    panel.board_widget.cell_clicked.connect(panel._on_board_clicked)
    board_grid.addWidget(panel.board_widget, 0, 0)

    panel.danmaku_overlay = DanmakuOverlay(panel.board_container)
    board_grid.addWidget(panel.danmaku_overlay, 0, 0)
    # 让弹幕以棋面（居中的正方形）为活动范围，而不是控件留白。
    panel.danmaku_overlay.set_board_widget(panel.board_widget)

    # 沙盘面板最后加入：与真实棋盘、弹幕叠放在同一个网格单元格中，
    # 打开时盖住真实棋盘。隐藏状态下不占布局空间。
    panel.sandbox_panel = SandboxPanel(panel.board_container)
    board_grid.addWidget(panel.sandbox_panel, 0, 0)
    panel.sandbox_panel.setVisible(False)

    left_column.addWidget(panel.board_container, 1)

    info = QHBoxLayout()
    info.setSpacing(10)
    panel.game_status_pill = QLabel("等待连接", page)
    panel.game_status_pill.setObjectName("turnPill")
    panel.game_status_pill.setProperty("result", "false")
    panel.game_status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
    panel.game_status_pill.setMinimumWidth(112)
    panel.clock_label = QLabel("—", page)
    panel.clock_label.setObjectName("onlineClock")
    panel.undo_status_label = QLabel("", page)
    panel.undo_status_label.setObjectName("onlineUndoStatus")
    panel.undo_status_label.setWordWrap(True)
    panel.undo_status_label.setVisible(False)
    panel.move_label = QLabel("第 0 手", page)
    panel.move_label.setObjectName("moveInfo")
    info.addWidget(panel.game_status_pill)
    info.addWidget(panel.clock_label)
    info.addWidget(panel.undo_status_label)
    info.addStretch(1)
    info.addWidget(panel.move_label)
    left_column.addLayout(info)

    panel.chat_log = ChatLogPanel(page)
    panel.chat_log.setObjectName("chatLogPanel")
    panel.chat_log.setMinimumWidth(210)
    panel.chat_log.setMaximumWidth(300)
    panel.chat_log.setSizePolicy(
        QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding
    )

    play_area.addLayout(left_column, 1)
    play_area.addWidget(panel.chat_log, 0)
    layout.addLayout(play_area, 1)

    # 聊天输入栏：表情、快捷评论与手动输入。
    chat_bar = QHBoxLayout()
    chat_bar.setSpacing(8)
    panel.emoji_button = EmojiPickerButton(page)
    panel.preset_comment_button = PresetCommentButton(page)
    panel.chat_log_toggle_button = QPushButton("对话日志", page)
    panel.chat_log_toggle_button.setObjectName("chatLogToggleButton")
    panel.chat_log_toggle_button.setCheckable(True)
    panel.chat_input = QLineEdit(page)
    panel.chat_input.setObjectName("chatInput")
    panel.chat_input.setMaxLength(MAX_COMMENT_LENGTH)
    panel.chat_input.setPlaceholderText(CHAT_INPUT_HINT)
    panel.chat_send_button = QPushButton("发送", page)
    panel.chat_send_button.setObjectName("chatSendButton")
    for button in (
        panel.emoji_button,
        panel.preset_comment_button,
        panel.chat_log_toggle_button,
        panel.chat_send_button,
    ):
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
    chat_bar.addWidget(panel.emoji_button)
    chat_bar.addWidget(panel.preset_comment_button)
    chat_bar.addWidget(panel.chat_input, 1)
    chat_bar.addWidget(panel.chat_send_button)
    chat_bar.addWidget(panel.chat_log_toggle_button)
    layout.addLayout(chat_bar)

    panel.game_rating_label = QLabel(page)
    panel.game_rating_label.setObjectName("onlineRatingResult")
    panel.game_rating_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    panel.game_rating_label.setWordWrap(True)
    panel.game_rating_label.setVisible(False)
    layout.addWidget(panel.game_rating_label)

    panel.game_achievement_label = QLabel(page)
    panel.game_achievement_label.setObjectName("onlineAchievementResult")
    panel.game_achievement_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    panel.game_achievement_label.setWordWrap(True)
    panel.game_achievement_label.setVisible(False)
    layout.addWidget(panel.game_achievement_label)

    panel.game_error_label = QLabel(page)
    panel.game_error_label.setObjectName("onlineError")
    panel.game_error_label.setWordWrap(True)
    panel.game_error_label.setMinimumHeight(20)
    layout.addWidget(panel.game_error_label)

    actions = QHBoxLayout()
    actions.setSpacing(8)
    actions.addStretch(1)
    # 沙盘推演放在底部操作栏：它是与对局并列的一种玩法，和悔棋、
    # 认输这些操作按钮排在一起最容易找到。
    panel.sandbox_button = QPushButton("沙盘推演", page)
    panel.sandbox_button.setObjectName("undoButton")
    panel.sandbox_button.setToolTip(
        "在本地沙盘上自行推演，不影响真实对局"
    )
    # 还没有对局时按钮不可用（可见但禁用）。
    panel.sandbox_button.setEnabled(False)
    panel.undo_button = QPushButton("请求悔棋", page)
    panel.undo_button.setObjectName("undoButton")
    panel.undo_reject_button = QPushButton("拒绝悔棋", page)
    panel.undo_reject_button.setObjectName("undoButton")
    panel.undo_reject_button.setVisible(False)
    panel.resign_button = QPushButton("认输", page)
    panel.resign_button.setObjectName("undoButton")
    panel.rematch_button = QPushButton("申请再战", page)
    panel.rematch_button.setObjectName("restartButton")
    panel.report_button = QPushButton("举报对手", page)
    panel.report_button.setObjectName("undoButton")
    panel.report_button.setToolTip(
        "对局结束后可提交举报，管理员会结合回放审计处理"
    )
    panel.report_button.setVisible(False)
    panel.leave_button = QPushButton("离开房间", page)
    panel.leave_button.setObjectName("undoButton")
    for button in (
        panel.sandbox_button,
        panel.undo_button,
        panel.undo_reject_button,
        panel.resign_button,
        panel.rematch_button,
        panel.report_button,
        panel.leave_button,
    ):
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        actions.addWidget(button)
    layout.addLayout(actions)

    panel.sandbox_button.clicked.connect(panel._on_sandbox_toggled)
    panel.undo_button.clicked.connect(panel._on_undo_clicked)
    panel.undo_reject_button.clicked.connect(panel._on_undo_declined)
    panel.resign_button.clicked.connect(panel._on_resign)
    panel.rematch_button.clicked.connect(panel._on_rematch)
    panel.report_button.clicked.connect(panel._on_report_clicked)
    panel.leave_button.clicked.connect(panel._on_leave_room)
    panel.invite_friend_button.clicked.connect(
        panel._on_invite_friend_clicked
    )
    panel.emoji_button.emoji_picked.connect(panel._on_emoji_picked)
    panel.preset_comment_button.comment_picked.connect(
        panel._on_preset_comment_picked
    )
    panel.chat_send_button.clicked.connect(panel._on_send_comment)
    panel.chat_input.returnPressed.connect(panel._on_send_comment)
    panel.chat_log_toggle_button.toggled.connect(
        panel._on_chat_log_toggled
    )
    # 对话日志默认收起：棋盘可以占满整行，需要时再点开。
    panel._apply_chat_log_visibility(panel._load_chat_log_visible())
    panel._refresh_chat_controls()
    return page
