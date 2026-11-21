"""个人信息面板与头像选择器（宣纸金线风格）。"""

from __future__ import annotations

from typing import Mapping, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarView, avatar_pixmap
from gomoku.client.paper_panel import InkDivider, PaperPanel
from gomoku.shared.avatar import (
    AVATAR_BY_KEY,
    AVATARS,
    normalize_avatar,
)


class AvatarPickerPanel(PaperPanel):
    """预置头像选择器。"""

    avatar_selected = Signal(str)

    def __init__(
        self,
        current: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            parent,
            seal_text="容",
            window_title="选择头像",
            min_width=380,
            modal=True,
        )
        current_key = normalize_avatar(current)

        title = QLabel("选择你的头像", self.card)
        title.setObjectName("socialPageTitle")
        subtitle = QLabel(
            "十二枚古典印章，择一枚作为你的标记", self.card
        )
        subtitle.setObjectName("socialSubtitle")
        subtitle.setWordWrap(True)
        self.content.addWidget(title)
        self.content.addWidget(subtitle)
        self.content.addWidget(InkDivider(self.card))

        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        for index, preset in enumerate(AVATARS):
            button = QPushButton(self.card)
            button.setObjectName("avatarChoice")
            button.setFixedSize(64, 64)
            button.setIcon(
                QIcon(
                    avatar_pixmap(
                        preset.key,
                        "",
                        58,
                        self.devicePixelRatioF(),
                        ring=True,
                    )
                )
            )
            button.setIconSize(QSize(58, 58))
            button.setToolTip(preset.label)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            if preset.key == current_key:
                button.setProperty("current", "true")
            button.clicked.connect(
                lambda _checked=False, key=preset.key: self._choose(key)
            )
            grid.addWidget(button, index // 4, index % 4)
        self.content.addLayout(grid)

        current_label = QLabel(
            f"当前头像：{AVATAR_BY_KEY[current_key].label}", self.card
        )
        current_label.setObjectName("avatarPickerHint")
        current_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(current_label)
        self.content.addWidget(InkDivider(self.card))

        cancel_button = QPushButton("取消", self.card)
        cancel_button.setObjectName("socialSecondaryButton")
        cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_button.clicked.connect(self.reject)
        self.content.addWidget(
            cancel_button, 0, Qt.AlignmentFlag.AlignRight
        )

    def _choose(self, key: str) -> None:
        self.avatar_selected.emit(key)
        self.accept()


class ProfilePanel(PaperPanel):
    """展示用户资料；按钮只发信号，网络请求由联机窗口处理。"""

    add_friend_requested = Signal(str)
    message_requested = Signal(str)
    remove_friend_requested = Signal(str)
    avatar_requested = Signal()
    open_friends_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            parent,
            seal_text=None,
            window_title="个人信息",
            min_width=480,
        )
        self._user: dict = {}
        self._friendship = "none"
        self._is_self = False

        # 顶部身份区：大印章头像 + 名号 + 在线状态 + 段位徽章。
        header = QHBoxLayout()
        header.setSpacing(16)
        self.avatar_view = AvatarView(
            "", "", 88, self.card, ring=True
        )
        header.addWidget(
            self.avatar_view, 0, Qt.AlignmentFlag.AlignTop
        )

        identity = QVBoxLayout()
        identity.setSpacing(3)
        self.name_label = QLabel("玩家", self.card)
        self.name_label.setObjectName("profileName")
        self.online_label = QLabel("离线", self.card)
        self.online_label.setObjectName("profileOnline")
        self.online_label.setProperty("online", "false")
        tagline = QLabel("以棋会友 · 落子无悔", self.card)
        tagline.setObjectName("profileTagline")
        identity.addWidget(self.name_label)
        identity.addWidget(self.online_label)
        identity.addWidget(tagline)
        header.addLayout(identity, 1)

        self.rank_label = QLabel("未定级", self.card)
        self.rank_label.setObjectName("profileRankBadge")
        self.rank_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rank_label.setMinimumWidth(80)
        self.rank_label.setFixedHeight(32)
        header.addWidget(
            self.rank_label, 0, Qt.AlignmentFlag.AlignTop
        )
        self.content.addLayout(header)
        self.content.addWidget(InkDivider(self.card))

        # 对局资料区：六枚数据小笺。
        section_row = QHBoxLayout()
        section_title = QLabel("对局资料", self.card)
        section_title.setObjectName("socialSectionTitle")
        section_note = QLabel("段位战绩", self.card)
        section_note.setObjectName("socialSectionCount")
        section_row.addWidget(section_title)
        section_row.addWidget(section_note)
        section_row.addStretch(1)
        self.content.addLayout(section_row)

        stats = QGridLayout()
        stats.setHorizontalSpacing(10)
        stats.setVerticalSpacing(10)
        self._stat_values: dict[str, QLabel] = {}
        stat_specs = (
            ("rating", "积分"),
            ("peak", "最高分"),
            ("games", "排位场次"),
            ("record", "战绩"),
            ("win_rate", "胜率"),
            ("created", "注册时间"),
        )
        for index, (key, label) in enumerate(stat_specs):
            tile = QFrame(self.card)
            tile.setObjectName("profileStatTile")
            tile.setAttribute(
                Qt.WidgetAttribute.WA_StyledBackground, True
            )
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(10, 8, 10, 8)
            tile_layout.setSpacing(2)
            name = QLabel(label, tile)
            name.setObjectName("profileStatName")
            name.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value = QLabel("—", tile)
            value.setObjectName("profileStatValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            value.setWordWrap(True)
            tile_layout.addWidget(name)
            tile_layout.addWidget(value)
            stats.addWidget(tile, index // 3, index % 3)
            self._stat_values[key] = value
        self.content.addLayout(stats)

        self.footer_hint = QLabel("", self.card)
        self.footer_hint.setObjectName("socialSubtitle")
        self.footer_hint.setWordWrap(True)
        self.content.addWidget(self.footer_hint)

        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.content.addLayout(self.actions)

    # ------------------------------------------------------------------
    @property
    def user_id(self) -> str:
        """当前展示的用户 id；没有资料时为空串。"""
        return str(self._user.get("id", ""))

    # ------------------------------------------------------------------
    def set_profile(
        self,
        profile: Mapping,
        *,
        is_self: bool = False,
    ) -> None:
        user = profile.get("user")
        self._user = dict(user) if isinstance(user, Mapping) else {}
        friendship = profile.get("friendship", "none")
        self._friendship = (
            friendship if isinstance(friendship, str) else "none"
        )
        self._is_self = is_self or self._friendship == "self"

        user_id = str(self._user.get("id", ""))
        username = str(self._user.get("username", "玩家"))
        avatar = self._user.get("avatar", "")
        online = bool(profile.get("online"))
        self.avatar_view.set_avatar(avatar, user_id)
        self.avatar_view.set_online(online)
        self.name_label.setText(username)
        self.online_label.setText("● 在线" if online else "○ 离线")
        self.online_label.setProperty(
            "online", "true" if online else "false"
        )
        self.online_label.style().unpolish(self.online_label)
        self.online_label.style().polish(self.online_label)
        self.rank_label.setText(str(self._user.get("rank_name", "未定级")))
        self._stat_values["rating"].setText(
            str(self._user.get("rating", "—"))
        )
        self._stat_values["peak"].setText(
            str(self._user.get("peak_rating", "—"))
        )
        self._stat_values["games"].setText(
            str(self._user.get("rated_games", 0))
        )
        self._stat_values["record"].setText(
            f"{self._user.get('wins', 0)} 胜 / "
            f"{self._user.get('losses', 0)} 负 / "
            f"{self._user.get('draws', 0)} 平"
        )
        win_rate = self._user.get("win_rate", 0.0)
        try:
            win_rate_text = f"{float(win_rate):.1f}%"
        except (TypeError, ValueError):
            win_rate_text = "—"
        self._stat_values["win_rate"].setText(win_rate_text)
        created = str(self._user.get("created_at", ""))
        self._stat_values["created"].setText(
            created[:10] if created else "—"
        )
        self.footer_hint.setText(self._footer_text())
        self._rebuild_actions()

    def _footer_text(self) -> str:
        if self._is_self:
            return "点击“更换头像”可挑选新的印章。"
        if self._friendship == "friends":
            return "你们已是好友，可以私聊或邀请一局自建房对弈。"
        if self._friendship == "pending_out":
            return "好友申请已发送，等待对方回应。"
        if self._friendship == "pending_in":
            return "对方已向你发送好友申请，可在好友窗口处理。"
        return "加为好友后即可私聊与邀请对局。"

    def set_avatar(self, avatar: str) -> None:
        self.avatar_view.set_avatar(
            avatar, str(self._user.get("id", ""))
        )

    # ------------------------------------------------------------------
    def _clear_actions(self) -> None:
        while self.actions.count():
            item = self.actions.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_action(
        self,
        text: str,
        callback,
        *,
        primary: bool,
    ) -> QPushButton:
        button = QPushButton(text, self.card)
        button.setObjectName(
            "socialPrimaryButton" if primary else "socialSecondaryButton"
        )
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.clicked.connect(lambda _checked=False: callback())
        self.actions.addWidget(button)
        return button

    def _rebuild_actions(self) -> None:
        self._clear_actions()
        user_id = str(self._user.get("id", ""))
        self.actions.addStretch(1)

        if self._is_self:
            self._add_action(
                "更换头像",
                self.avatar_requested.emit,
                primary=True,
            )
        elif self._friendship == "friends":
            self._add_action(
                "发消息",
                lambda: self.message_requested.emit(user_id),
                primary=True,
            )
            self._add_action(
                "删除好友",
                lambda: self.remove_friend_requested.emit(user_id),
                primary=False,
            )
        elif self._friendship == "pending_out":
            pending = self._add_action(
                "好友申请已发送", lambda: None, primary=False
            )
            pending.setEnabled(False)
        elif self._friendship == "pending_in":
            self._add_action(
                "去好友申请处理",
                self.open_friends_requested.emit,
                primary=True,
            )
        else:
            self._add_action(
                "加为好友",
                lambda: self.add_friend_requested.emit(user_id),
                primary=True,
            )

        close_button = QPushButton("关闭", self.card)
        close_button.setObjectName("socialSecondaryButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.close)
        self.actions.addWidget(close_button)


__all__ = ["ProfilePanel", "AvatarPickerPanel"]
