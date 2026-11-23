"""联机对局中的玩家身份卡（头像、执子颜色、积分与勋章）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.achievement_widgets import achievement_pixmap
from gomoku.client.avatar_widgets import AvatarButton
from gomoku.shared.achievements import (
    ACHIEVEMENT_BY_KEY,
    MATCH_MEDAL_LIMIT,
    normalize_achievement_keys,
    top_achievement_keys,
)
from gomoku.shared.rules import Player


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
        name_row = QHBoxLayout()
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.setSpacing(4)
        self._name = QLabel("玩家", self)
        self._name.setObjectName("playerName")
        self._medal_row = QHBoxLayout()
        self._medal_row.setContentsMargins(0, 0, 0, 0)
        self._medal_row.setSpacing(3)
        name_row.addWidget(self._name)
        name_row.addLayout(self._medal_row)
        name_row.addStretch(1)
        self._caption = QLabel("等待", self)
        self._caption.setObjectName("playerCaption")
        text_layout.addLayout(name_row)
        text_layout.addWidget(self._caption)
        self._achievement_keys: tuple[str, ...] = ()

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
        avatar_version: int = 0,
        achievements: tuple[str, ...] = (),
    ) -> None:
        self.avatar_button.set_avatar(
            avatar, user_id, max(0, int(avatar_version))
        )
        self._set_achievements(achievements)
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

    def _set_achievements(self, achievements: object) -> None:
        """在名字右侧显示最稀有的几枚成就勋章。"""
        keys = top_achievement_keys(
            normalize_achievement_keys(achievements),
            MATCH_MEDAL_LIMIT,
        )
        if keys == self._achievement_keys:
            return
        self._achievement_keys = keys
        while self._medal_row.count():
            item = self._medal_row.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        for key in keys:
            spec = ACHIEVEMENT_BY_KEY.get(key)
            if spec is None:
                continue
            badge = QLabel(self)
            badge.setPixmap(
                achievement_pixmap(spec, 20, self.devicePixelRatioF())
            )
            badge.setFixedSize(20, 20)
            badge.setToolTip(f"{spec.name} · {spec.description}")
            self._medal_row.addWidget(badge)


__all__ = ["OnlinePlayerCard"]
