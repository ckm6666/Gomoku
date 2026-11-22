"""个人信息面板与头像选择器（宣纸金线风格）。"""

from __future__ import annotations

from typing import Mapping, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.achievement_widgets import achievement_pixmap
from gomoku.client.avatar_images import (
    AvatarImageError,
    prepare_avatar_image,
)
from gomoku.client.avatar_widgets import AvatarView, avatar_pixmap
from gomoku.client.paper_panel import InkDivider, PaperPanel
from gomoku.shared.achievements import (
    ACHIEVEMENT_BY_KEY,
    ACHIEVEMENT_TOTAL,
    normalize_achievement_keys,
    stats_from_mapping,
    top_achievement_keys,
    unlocked_achievement_keys,
)
from gomoku.shared.avatar import (
    AVATAR_BY_KEY,
    AVATARS,
    normalize_avatar,
)


class AvatarPickerPanel(PaperPanel):
    """预置头像选择器。"""

    avatar_selected = Signal(str)
    image_selected = Signal(str)

    def __init__(
        self,
        current: str = "",
        parent: Optional[QWidget] = None,
        *,
        current_version: int = 0,
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
            "十二枚古典印章，择一枚作为你的标记；"
            "也可以上传一张本地图片（自动裁成方形）",
            self.card,
        )
        subtitle.setObjectName("socialSubtitle")
        subtitle.setWordWrap(True)
        self.content.addWidget(title)
        self.content.addWidget(subtitle)
        self.content.addWidget(InkDivider(self.card))

        self.upload_button = QPushButton("上传本地图片", self.card)
        self.upload_button.setObjectName("socialPrimaryButton")
        self.upload_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.upload_button.setAutoDefault(False)
        self.upload_button.clicked.connect(self._pick_image)
        self.content.addWidget(self.upload_button)

        self.upload_hint = QLabel(
            "支持 JPEG/PNG，图片会被裁成 192×192 的圆形头像",
            self.card,
        )
        self.upload_hint.setObjectName("avatarPickerHint")
        self.upload_hint.setWordWrap(True)
        self.upload_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.content.addWidget(self.upload_hint)
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

        has_custom_image = (
            isinstance(current_version, int)
            and not isinstance(current_version, bool)
            and current_version > 0
        )
        current_label = QLabel(
            "当前头像：自定义图片（选择印章会移除）"
            if has_custom_image
            else f"当前头像：{AVATAR_BY_KEY[current_key].label}",
            self.card,
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

    def _pick_image(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(
            self,
            "选择头像图片",
            "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp)",
        )
        if not path:
            return
        try:
            data_url = prepare_avatar_image(path)
        except AvatarImageError as exc:
            self.upload_hint.setText(str(exc))
            return
        self.image_selected.emit(data_url)
        self.accept()

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
    achievements_requested = Signal()

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

        # 成就勋章：只展示最稀有的几枚，完整成就墙由“全部成就”打开。
        achievement_row = QHBoxLayout()
        achievement_title = QLabel("成就勋章", self.card)
        achievement_title.setObjectName("socialSectionTitle")
        self.achievement_count_label = QLabel(
            f"0 / {ACHIEVEMENT_TOTAL}", self.card
        )
        self.achievement_count_label.setObjectName(
            "socialSectionCount"
        )
        self.achievements_button = QPushButton("全部成就", self.card)
        self.achievements_button.setObjectName("socialSecondaryButton")
        self.achievements_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.achievements_button.setAutoDefault(False)
        self.achievements_button.clicked.connect(
            self.achievements_requested.emit
        )
        achievement_row.addWidget(achievement_title)
        achievement_row.addWidget(self.achievement_count_label)
        achievement_row.addStretch(1)
        achievement_row.addWidget(self.achievements_button)
        self.content.addLayout(achievement_row)

        self.medal_container = QWidget(self.card)
        self.medal_layout = QHBoxLayout(self.medal_container)
        self.medal_layout.setContentsMargins(0, 0, 0, 0)
        self.medal_layout.setSpacing(7)
        self.content.addWidget(self.medal_container)

        self.achievement_hint_label = QLabel("", self.card)
        self.achievement_hint_label.setObjectName("socialSubtitle")
        self.achievement_hint_label.setWordWrap(True)
        self.content.addWidget(self.achievement_hint_label)

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
        avatar_version = self._user.get("avatar_version", 0)
        if not isinstance(avatar_version, int) or isinstance(
            avatar_version, bool
        ):
            avatar_version = 0
        online = bool(profile.get("online"))
        self.avatar_view.set_avatar(
            avatar, user_id, max(0, avatar_version)
        )
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
        self._update_achievements()
        self.footer_hint.setText(self._footer_text())
        self._rebuild_actions()

    def _update_achievements(self) -> None:
        """刷新勋章行与成就计数。"""
        stats = stats_from_mapping(self._user)
        raw_keys = self._user.get("achievements")
        if isinstance(raw_keys, (list, tuple)):
            keys = normalize_achievement_keys(raw_keys)
        else:
            keys = unlocked_achievement_keys(stats)
        self.achievement_count_label.setText(
            f"{len(keys)} / {ACHIEVEMENT_TOTAL}"
        )
        self.achievement_hint_label.setText(
            f"当前连胜 {stats.current_streak} · "
            f"最高连胜 {stats.best_streak} · "
            f"排位 {stats.wins} 胜"
        )

        while self.medal_layout.count():
            item = self.medal_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        top_keys = top_achievement_keys(keys, 6)
        for key in top_keys:
            spec = ACHIEVEMENT_BY_KEY.get(key)
            if spec is None:
                continue
            label = QLabel(self.medal_container)
            label.setPixmap(
                achievement_pixmap(
                    spec, 32, self.devicePixelRatioF()
                )
            )
            label.setFixedSize(32, 32)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setToolTip(f"{spec.name} · {spec.description}")
            self.medal_layout.addWidget(label)
        if not top_keys:
            empty = QLabel(
                "完成排位对局即可点亮第一枚勋章",
                self.medal_container,
            )
            empty.setObjectName("socialSubtitle")
            self.medal_layout.addWidget(empty)
        self.medal_layout.addStretch(1)

    def _footer_text(self) -> str:
        if self._is_self:
            return (
                "点击“更换头像”可挑选印章或上传本地图片；"
                "排位赛的战绩会点亮成就勋章。"
            )
        if self._friendship == "friends":
            return "你们已是好友，可以私聊或邀请一局自建房对弈。"
        if self._friendship == "pending_out":
            return "好友申请已发送，等待对方回应。"
        if self._friendship == "pending_in":
            return "对方已向你发送好友申请，可在好友窗口处理。"
        return "加为好友后即可私聊与邀请对局。"

    def set_avatar(self, avatar: str, avatar_version: int = 0) -> None:
        self.avatar_view.set_avatar(
            avatar,
            str(self._user.get("id", "")),
            max(0, int(avatar_version)),
        )
        self._user["avatar"] = avatar
        self._user["avatar_version"] = max(0, int(avatar_version))

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
