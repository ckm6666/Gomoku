"""宣纸卡片对话框基类与装饰控件。

个人信息、好友与头像选择窗口共用：水墨背景上放一张宣纸卡片，卡片自带
双线墨框、金线、回纹角与朱砂印，与主菜单/联机大厅保持同一套视觉语言。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from gomoku.client.theme_palette import CINNABAR, GOLD
from gomoku.client.ui_theme import (
    ONLINE_BACKDROP_SPEC,
    InkBackdrop,
    RicePaperCard,
)


class PaperDialog(QDialog):
    """水墨背景 + 宣纸卡片的对话框基类。"""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        seal_text: Optional[str] = None,
        window_title: str = "",
        min_width: int = 440,
        min_height: int = 0,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("paperDialog")
        self.setWindowTitle(window_title)
        self.setModal(False)
        if min_height > 0:
            self.setMinimumSize(min_width, min_height)
        else:
            self.setMinimumWidth(min_width)

        # 水墨山水背景；卡片浮在其上。
        self._backdrop = InkBackdrop(self, spec=ONLINE_BACKDROP_SPEC)
        self._backdrop.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self._backdrop.lower()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        self.card = RicePaperCard(self, seal_text=seal_text)
        outer.addWidget(self.card)

        self.content = QVBoxLayout(self.card)
        self.content.setContentsMargins(30, 26, 30, 24)
        self.content.setSpacing(12)

    def _sync_backdrop(self) -> None:
        self._backdrop.setGeometry(self.rect())
        self._backdrop.lower()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self._sync_backdrop()
        super().showEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self._sync_backdrop()
        super().resizeEvent(event)


class InkDivider(QWidget):
    """金线分隔，中心缀一枚朱砂小菱。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setFixedHeight(16)
        self.setMinimumWidth(60)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        del event
        width = float(self.width())
        height = float(self.height())
        if width <= 8 or height <= 0:
            return
        y = height / 2.0
        center = width / 2.0
        gap = 9.0
        margin = 2.0

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(QPen(QColor(GOLD), 1.0))
        painter.drawLine(
            QPointF(margin, y), QPointF(max(margin, center - gap), y)
        )
        painter.drawLine(
            QPointF(min(width - margin, center + gap), y),
            QPointF(width - margin, y),
        )

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(CINNABAR))
        diamond = QPolygonF(
            [
                QPointF(center, y - 4.0),
                QPointF(center + 4.0, y),
                QPointF(center, y + 4.0),
                QPointF(center - 4.0, y),
            ]
        )
        painter.drawPolygon(diamond)
        painter.end()


__all__ = ["PaperDialog", "InkDivider"]
