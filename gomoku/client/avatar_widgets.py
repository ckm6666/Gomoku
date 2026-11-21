"""预置头像绘制与可点击控件。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from gomoku.client.theme_palette import GOLD, PAPER_LIGHT
from gomoku.shared.avatar import avatar_preset


def avatar_pixmap(
    avatar: object,
    user_id: str = "",
    size: int = 32,
    dpr: float = 1.0,
    *,
    ring: bool = False,
) -> QPixmap:
    """绘制一个圆形印章头像；大尺寸可加金色外环。"""
    preset = avatar_preset(avatar, user_id)
    safe_size = max(8, int(size))
    scale = max(1.0, float(dpr))
    pixels = max(1, round(safe_size * scale))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)

    inset = max(2.0, safe_size * 0.05) if ring else 0.8
    rect = QRectF(
        inset,
        inset,
        safe_size - inset * 2.0,
        safe_size - inset * 2.0,
    )

    if ring:
        ring_pen = QPen(QColor(GOLD), max(1.2, safe_size * 0.032))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(ring_pen)
        painter.drawEllipse(
            QRectF(0.9, 0.9, safe_size - 1.8, safe_size - 1.8)
        )
        painter.setPen(
            QPen(QColor(PAPER_LIGHT), max(0.9, safe_size * 0.018))
        )
        painter.drawEllipse(
            rect.adjusted(-1.3, -1.3, 1.3, 1.3)
        )

    painter.setPen(
        QPen(QColor("#F3EBDD"), max(1.0, safe_size * 0.055))
    )
    painter.setBrush(QColor(preset.background))
    painter.drawEllipse(rect)

    font = QFont()
    font.setPixelSize(
        max(10, int(safe_size * (0.42 if ring else 0.46)))
    )
    font.setBold(True)
    painter.setFont(font)
    painter.setPen(QColor(preset.foreground))
    painter.drawText(
        rect,
        Qt.AlignmentFlag.AlignCenter,
        preset.glyph,
    )
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def avatar_icon(
    avatar: object, user_id: str = "", size: int = 20
) -> QIcon:
    """返回适合表格/按钮使用的头像图标。"""
    return QIcon(avatar_pixmap(avatar, user_id, size))


class AvatarView(QLabel):
    """只读头像控件，可在右下角显示在线状态点。"""

    def __init__(
        self,
        avatar: object = "",
        user_id: str = "",
        size: int = 32,
        parent: Optional[QWidget] = None,
        *,
        ring: bool = False,
        online: Optional[bool] = None,
    ) -> None:
        super().__init__(parent)
        self._size = max(8, int(size))
        self._user_id = user_id
        self._ring = ring
        self._online = online
        self.setFixedSize(self._size, self._size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_avatar(avatar, user_id)

    def set_avatar(self, avatar: object, user_id: str = "") -> None:
        self._user_id = user_id
        self.setPixmap(
            avatar_pixmap(
                avatar,
                user_id,
                self._size,
                self.devicePixelRatioF(),
                ring=self._ring,
            )
        )

    def set_online(self, online: Optional[bool]) -> None:
        self._online = online
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().paintEvent(event)
        if self._online is None:
            return
        radius = max(3.0, self._size * 0.10)
        center = QPointF(
            self._size - radius - 1.5,
            self._size - radius - 1.5,
        )
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(
            QPen(
                QColor(PAPER_LIGHT),
                max(1.0, self._size * 0.045),
            )
        )
        painter.setBrush(
            QColor("#4F8A5B" if self._online else "#A89C8B")
        )
        painter.drawEllipse(center, radius, radius)
        painter.end()


class AvatarButton(QPushButton):
    """可点击头像；点击后发出用户 id。"""

    clicked_id = Signal(str)

    def __init__(
        self,
        avatar: object = "",
        user_id: str = "",
        size: int = 32,
        parent: Optional[QWidget] = None,
        *,
        ring: bool = False,
    ) -> None:
        super().__init__(parent)
        self._size = max(8, int(size))
        self._user_id = user_id
        self._ring = ring
        self.setFixedSize(self._size, self._size)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 0; }"
        )
        self.clicked.connect(self._emit_clicked)
        self.set_avatar(avatar, user_id)

    def _emit_clicked(self) -> None:
        if self._user_id:
            self.clicked_id.emit(self._user_id)

    def set_avatar(self, avatar: object, user_id: str = "") -> None:
        self._user_id = user_id
        self.setIcon(
            QIcon(
                avatar_pixmap(
                    avatar,
                    user_id,
                    self._size,
                    self.devicePixelRatioF(),
                    ring=self._ring,
                )
            )
        )
        self.setIconSize(QSize(self._size, self._size))
        self.setEnabled(bool(user_id))
        self.setVisible(bool(user_id))


__all__ = [
    "avatar_pixmap",
    "avatar_icon",
    "AvatarView",
    "AvatarButton",
]
