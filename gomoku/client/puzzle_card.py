"""残局入口卡片按钮：宣纸底、金线、朱砂印与书法成语标题。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QPushButton, QSizePolicy, QWidget

from gomoku.client.theme_palette import (
    CINNABAR,
    GOLD,
    GOLD_DARK,
    INK,
    INK_FAINT,
    INK_MUTED,
    PAPER,
    PAPER_DEEP,
    PAPER_LIGHT,
    PAPER_MUTED,
)
from gomoku.client.ui_theme import DISPLAY_FONT_FAMILY, make_seal_pixmap


class PuzzleCardButton(QPushButton):
    """一张可直接点击进入残局的宣纸卡片。"""

    CARD_HEIGHT = 112

    def __init__(
        self,
        title: str,
        side: str,
        limit: int,
        hint: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._side = side
        self._limit = limit

        self.setObjectName("puzzleCard")
        self.setText(f"{title}\n{side} · 限 {limit} 手")
        self.setAccessibleName(f"残局 {title}，{side}，限 {limit} 手")
        self.setToolTip(hint)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self.setMinimumHeight(self.CARD_HEIGHT)
        self.setMaximumHeight(self.CARD_HEIGHT)

    def sizeHint(self) -> QSize:
        return QSize(300, self.CARD_HEIGHT)

    def enterEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        rect = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        hovered = self.underMouse() and self.isEnabled()
        pressed = self.isDown()

        # 柔和投影，让卡片与水墨背景分离。
        shadow = QColor(40, 28, 16, 46 if hovered else 30)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(shadow)
        painter.drawRoundedRect(rect.translated(0.0, 3.0), 10.0, 10.0)

        background = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        if pressed:
            background.setColorAt(0.0, QColor(PAPER_DEEP))
            background.setColorAt(1.0, QColor(PAPER_MUTED))
        elif hovered:
            background.setColorAt(0.0, QColor(PAPER_LIGHT))
            background.setColorAt(1.0, QColor(PAPER))
        else:
            background.setColorAt(0.0, QColor(PAPER_LIGHT))
            background.setColorAt(1.0, QColor("#F7F0E4"))
        painter.setBrush(background)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(rect, 10.0, 10.0)

        # 顶部淡淡的金色纸光。
        sheen = QLinearGradient(rect.topLeft(), rect.topRight())
        sheen.setColorAt(0.0, QColor(255, 252, 242, 0))
        sheen.setColorAt(0.5, QColor(255, 252, 242, 80 if hovered else 44))
        sheen.setColorAt(1.0, QColor(255, 252, 242, 0))
        painter.setBrush(sheen)
        painter.drawRoundedRect(rect, 10.0, 10.0)

        border_color = QColor(GOLD if hovered else "#D3C4AD")
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(border_color, 1.7 if hovered else 1.0))
        painter.drawRoundedRect(rect, 10.0, 10.0)

        # 左侧朱砂/金线书脊。
        accent = QColor(CINNABAR if hovered else GOLD)
        painter.setPen(
            QPen(
                accent,
                3.0,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
            )
        )
        painter.drawLine(
            QPointF(rect.left() + 1.8, rect.top() + 18.0),
            QPointF(rect.left() + 1.8, rect.bottom() - 18.0),
        )

        if self.hasFocus():
            painter.setPen(
                QPen(
                    QColor(GOLD_DARK),
                    1.0,
                    Qt.PenStyle.DashLine,
                )
            )
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(4.0, 4.0, -4.0, -4.0), 7.0, 7.0
            )

        # 右上角朱砂小印。
        seal_size = 30
        seal = make_seal_pixmap(
            "弈", seal_size, dpr=self.devicePixelRatioF()
        )
        seal_rect = QRectF(
            rect.right() - seal_size - 16.0,
            rect.top() + 15.0,
            seal_size,
            seal_size,
        )
        painter.drawPixmap(seal_rect, seal, QRectF(seal.rect()))

        text_left = rect.left() + 22.0
        text_width = max(60.0, rect.width() - 104.0)

        title_font = QFont(DISPLAY_FONT_FAMILY)
        title_font.setPointSize(22)
        title_font.setWeight(QFont.Weight.Normal)
        painter.setFont(title_font)
        painter.setPen(QColor(INK))
        title_metrics = QFontMetrics(title_font)
        title_text = title_metrics.elidedText(
            self._title,
            Qt.TextElideMode.ElideRight,
            int(text_width),
        )
        painter.drawText(
            QRectF(text_left, rect.top() + 14.0, text_width, 34.0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            title_text,
        )

        # 先手方棋子徽标。
        badge_center = QPointF(text_left + 5.0, rect.top() + 69.0)
        is_black = self._side.startswith("黑")
        painter.setPen(
            QPen(
                QColor(GOLD_DARK) if not is_black else QColor(INK),
                1.0,
            )
        )
        painter.setBrush(
            QColor(INK) if is_black else QColor(PAPER_LIGHT)
        )
        painter.drawEllipse(badge_center, 5.0, 5.0)

        caption_font = QFont(self.font())
        caption_font.setPointSize(10)
        caption_font.setWeight(QFont.Weight.Medium)
        painter.setFont(caption_font)
        painter.setPen(QColor(INK_MUTED))
        painter.drawText(
            QRectF(
                text_left + 16.0,
                rect.top() + 57.0,
                text_width - 16.0,
                24.0,
            ),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            f"{self._side} · 限 {self._limit} 手",
        )

        hint_font = QFont(self.font())
        hint_font.setPointSize(9)
        painter.setFont(hint_font)
        painter.setPen(QColor(INK_FAINT))
        painter.drawText(
            QRectF(
                text_left,
                rect.bottom() - 25.0,
                text_width,
                18.0,
            ),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            "点击入局",
        )

        arrow_color = QColor(CINNABAR if hovered else GOLD)
        painter.setPen(
            QPen(
                arrow_color,
                1.8,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        arrow_x = rect.right() - 22.0
        arrow_y = rect.bottom() - 22.0
        painter.drawLine(
            QPointF(arrow_x - 5.0, arrow_y - 5.0),
            QPointF(arrow_x, arrow_y),
        )
        painter.drawLine(
            QPointF(arrow_x - 5.0, arrow_y + 5.0),
            QPointF(arrow_x, arrow_y),
        )
        painter.end()


__all__ = ["PuzzleCardButton"]
