"""登录/注册页构建器。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def _centered_card_page(panel: Any, card: QFrame) -> QWidget:
    page = QWidget(panel)
    page.setObjectName("onlinePage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 24, 24, 24)
    layout.addStretch(1)
    layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
    layout.addStretch(1)
    return page


def build_auth_page(panel: Any) -> QWidget:
    """构建登录/注册页，并把控件挂到 panel 上供后续逻辑使用。"""
    card = QFrame(panel)
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
    panel.server_label = QLabel(card)
    panel.server_label.setObjectName("hintLabel")
    panel.server_label.setWordWrap(True)
    panel.server_label.setText(
        f"服务器：{panel._api_base_url or '未配置'}"
    )

    panel.username_edit = QLineEdit(card)
    panel.username_edit.setObjectName("onlineInput")
    panel.username_edit.setPlaceholderText("用户名")
    panel.password_edit = QLineEdit(card)
    panel.password_edit.setObjectName("onlineInput")
    panel.password_edit.setPlaceholderText("密码（至少 8 位）")
    panel.password_edit.setEchoMode(QLineEdit.EchoMode.Password)

    card_layout.addWidget(title)
    card_layout.addWidget(subtitle)
    card_layout.addWidget(panel.server_label)
    for label_text, widget in (
        ("用户名", panel.username_edit),
        ("密码", panel.password_edit),
    ):
        label = QLabel(label_text, card)
        label.setObjectName("formLabel")
        label.setBuddy(widget)
        card_layout.addWidget(label)
        card_layout.addWidget(widget)

    remember_row = QHBoxLayout()
    remember_row.setSpacing(8)
    panel.remember_password_checkbox = QCheckBox("记住密码", card)
    panel.remember_password_checkbox.setObjectName("rememberPasswordCheck")
    panel.remember_password_checkbox.setToolTip(
        "密码使用 Windows 凭据保护（DPAPI）加密后保存在本机，"
        "仅当前 Windows 用户可解密。"
    )
    panel.remember_password_hint = QLabel(
        "账号密码加密保存在本机，下次启动自动填充", card
    )
    panel.remember_password_hint.setObjectName("rememberPasswordHint")
    panel.remember_password_hint.setWordWrap(True)
    remember_row.addWidget(panel.remember_password_checkbox)
    remember_row.addWidget(panel.remember_password_hint, 1)
    card_layout.addLayout(remember_row)

    buttons = QHBoxLayout()
    buttons.setSpacing(10)
    panel.login_button = QPushButton("登录", card)
    panel.login_button.setObjectName("restartButton")
    panel.login_button.setCursor(Qt.CursorShape.PointingHandCursor)
    panel.register_button = QPushButton("注册", card)
    panel.register_button.setObjectName("undoButton")
    panel.register_button.setCursor(Qt.CursorShape.PointingHandCursor)
    buttons.addWidget(panel.login_button)
    buttons.addWidget(panel.register_button)
    card_layout.addLayout(buttons)

    panel.auth_status_label = QLabel(card)
    panel.auth_status_label.setObjectName("onlineStatus")
    panel.auth_status_label.setWordWrap(True)
    panel.auth_status_label.setMinimumHeight(22)
    card_layout.addWidget(panel.auth_status_label)

    panel.login_button.clicked.connect(panel._on_login_clicked)
    panel.register_button.clicked.connect(panel._on_register_clicked)
    panel.password_edit.returnPressed.connect(panel._on_login_clicked)
    panel.remember_password_checkbox.toggled.connect(
        panel._on_remember_password_toggled
    )

    return _centered_card_page(panel, card)
