"""头像绘制与可点击控件（预置印章 + 自定义上传图片）。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QLabel, QPushButton, QWidget

from gomoku.client.avatar_images import avatar_image_cache
from gomoku.client.theme_palette import GOLD, PAPER_LIGHT
from gomoku.shared.avatar import avatar_preset


def _avatar_geometry(
    safe_size: int, ring: bool
) -> tuple[QRectF, float]:
    """返回头像圆形的内缩矩形与边框宽度。"""
    inset = max(2.0, safe_size * 0.05) if ring else 0.8
    rect = QRectF(
        inset,
        inset,
        safe_size - inset * 2.0,
        safe_size - inset * 2.0,
    )
    border = max(1.0, safe_size * 0.055)
    return rect, border


def _draw_ring(
    painter: QPainter, safe_size: int, rect: QRectF
) -> None:
    """在头像外侧画金线/纸色双环。"""
    ring_pen = QPen(QColor(GOLD), max(1.2, safe_size * 0.032))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(ring_pen)
    painter.drawEllipse(
        QRectF(0.9, 0.9, safe_size - 1.8, safe_size - 1.8)
    )
    painter.setPen(
        QPen(QColor(PAPER_LIGHT), max(0.9, safe_size * 0.018))
    )
    painter.drawEllipse(rect.adjusted(-1.3, -1.3, 1.3, 1.3))


def _image_avatar_pixmap(
    image: QImage,
    size: int,
    dpr: float,
    *,
    ring: bool,
) -> QPixmap:
    """把上传图片裁成圆形头像。"""
    safe_size = max(8, int(size))
    scale = max(1.0, float(dpr))
    pixels = max(1, round(safe_size * scale))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.scale(scale, scale)

    rect, border = _avatar_geometry(safe_size, ring)
    if ring:
        _draw_ring(painter, safe_size, rect)

    clip = QPainterPath()
    clip.addEllipse(rect)
    painter.save()
    painter.setClipPath(clip)
    # 图片本身已是方形；仍按短边居中裁切，兼容历史/外部数据。
    side = min(image.width(), image.height())
    source = QRectF(
        (image.width() - side) / 2.0,
        (image.height() - side) / 2.0,
        side,
        side,
    )
    painter.drawImage(rect, image, source)
    painter.restore()

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor("#F3EBDD"), border))
    painter.drawEllipse(rect)
    painter.end()
    pixmap.setDevicePixelRatio(scale)
    return pixmap


def avatar_pixmap(
    avatar: object,
    user_id: str = "",
    size: int = 32,
    dpr: float = 1.0,
    *,
    ring: bool = False,
    avatar_version: int = 0,
) -> QPixmap:
    """绘制圆形头像；有自定义图片时优先使用，否则回落到预置印章。"""
    safe_size = max(8, int(size))
    scale = max(1.0, float(dpr))

    if avatar_version > 0 and user_id:
        image = avatar_image_cache().image(user_id, avatar_version)
        if image is not None:
            return _image_avatar_pixmap(
                image, safe_size, scale, ring=ring
            )

    preset = avatar_preset(avatar, user_id)
    pixels = max(1, round(safe_size * scale))
    pixmap = QPixmap(pixels, pixels)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)

    rect, border = _avatar_geometry(safe_size, ring)
    if ring:
        _draw_ring(painter, safe_size, rect)

    painter.setPen(QPen(QColor("#F3EBDD"), border))
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
    avatar: object,
    user_id: str = "",
    size: int = 20,
    avatar_version: int = 0,
) -> QIcon:
    """返回适合表格/按钮使用的头像图标。"""
    return QIcon(
        avatar_pixmap(
            avatar,
            user_id,
            size,
            avatar_version=avatar_version,
        )
    )


class AvatarView(QLabel):
    """只读头像控件，可在右下角显示在线状态点、右上角挂未读红点。"""

    def __init__(
        self,
        avatar: object = "",
        user_id: str = "",
        size: int = 32,
        parent: Optional[QWidget] = None,
        *,
        ring: bool = False,
        online: Optional[bool] = None,
        avatar_version: int = 0,
    ) -> None:
        super().__init__(parent)
        self._size = max(8, int(size))
        self._user_id = user_id
        self._avatar: object = avatar
        self._avatar_version = max(0, int(avatar_version))
        self._ring = ring
        self._online = online
        self._unread = 0
        self.setFixedSize(self._size, self._size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        avatar_image_cache().image_loaded.connect(self._on_image_loaded)
        self.set_avatar(avatar, user_id, avatar_version)

    def set_avatar(
        self,
        avatar: object,
        user_id: str = "",
        avatar_version: int = 0,
    ) -> None:
        self._user_id = user_id
        self._avatar = avatar
        self._avatar_version = max(0, int(avatar_version))
        self._refresh_pixmap()

    def _refresh_pixmap(self) -> None:
        self.setPixmap(
            avatar_pixmap(
                self._avatar,
                self._user_id,
                self._size,
                self.devicePixelRatioF(),
                ring=self._ring,
                avatar_version=self._avatar_version,
            )
        )

    def _on_image_loaded(self, user_id: str, version: int) -> None:
        if (
            user_id == self._user_id
            and version == self._avatar_version
        ):
            self._refresh_pixmap()

    def set_online(self, online: Optional[bool]) -> None:
        self._online = online
        self.update()

    @property
    def unread(self) -> int:
        """当前显示的未读条数（0 表示没有未读）。"""
        return self._unread

    def set_unread(self, count: int) -> None:
        """在头像右上角挂一个红点，圆点里写未读消息条数。"""
        try:
            value = int(count)
        except (TypeError, ValueError):
            value = 0
        value = max(0, value)
        if value == self._unread:
            return
        self._unread = value
        self.update()

    def _draw_unread_badge(self, painter: QPainter) -> None:
        """画未读红点：单个数字是圆点，多位数字是胶囊，角标写条数。"""
        if self._unread <= 0:
            return
        text = "99+" if self._unread > 99 else str(self._unread)
        radius = max(5.0, self._size * 0.20)
        font = QFont()
        font.setBold(True)
        font.setPixelSize(max(8, int(radius * 1.15)))
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        # 数字越长角标越宽，但始终保持贴住右上角。
        width = max(radius * 2.0, text_width + radius * 0.9)
        height = radius * 2.0
        rect = QRectF(
            self._size - width - 0.5,
            0.5,
            width,
            height,
        )
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(
            QPen(
                QColor(PAPER_LIGHT),
                max(1.0, self._size * 0.035),
            )
        )
        painter.setBrush(QColor("#C0392B"))
        painter.drawRoundedRect(rect, height / 2.0, height / 2.0)
        painter.setPen(QColor("#FBF7EE"))
        painter.drawText(
            rect,
            Qt.AlignmentFlag.AlignCenter,
            text,
        )

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().paintEvent(event)
        if self._online is None and self._unread <= 0:
            return
        painter = QPainter(self)
        if self._online is not None:
            radius = max(3.0, self._size * 0.10)
            center = QPointF(
                self._size - radius - 1.5,
                self._size - radius - 1.5,
            )
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
        # 未读红点画在右上角：在线点在右下角，两者互不遮挡。
        self._draw_unread_badge(painter)
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
        avatar_version: int = 0,
    ) -> None:
        super().__init__(parent)
        self._size = max(8, int(size))
        self._user_id = user_id
        self._avatar: object = avatar
        self._avatar_version = max(0, int(avatar_version))
        self._ring = ring
        self.setFixedSize(self._size, self._size)
        self.setFlat(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            "QPushButton { border: none; background: transparent; padding: 0; }"
        )
        avatar_image_cache().image_loaded.connect(self._on_image_loaded)
        self.clicked.connect(self._emit_clicked)
        self.set_avatar(avatar, user_id, avatar_version)

    def _emit_clicked(self) -> None:
        if self._user_id:
            self.clicked_id.emit(self._user_id)

    def set_avatar(
        self,
        avatar: object,
        user_id: str = "",
        avatar_version: int = 0,
    ) -> None:
        self._user_id = user_id
        self._avatar = avatar
        self._avatar_version = max(0, int(avatar_version))
        self._refresh_icon()
        self.setEnabled(bool(user_id))
        self.setVisible(bool(user_id))

    def _refresh_icon(self) -> None:
        self.setIcon(
            QIcon(
                avatar_pixmap(
                    self._avatar,
                    self._user_id,
                    self._size,
                    self.devicePixelRatioF(),
                    ring=self._ring,
                    avatar_version=self._avatar_version,
                )
            )
        )
        self.setIconSize(QSize(self._size, self._size))

    def _on_image_loaded(self, user_id: str, version: int) -> None:
        if (
            user_id == self._user_id
            and version == self._avatar_version
        ):
            self._refresh_icon()


__all__ = [
    "avatar_pixmap",
    "avatar_icon",
    "AvatarView",
    "AvatarButton",
]
