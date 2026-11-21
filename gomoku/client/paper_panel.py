"""窗口内宣纸浮层面板：好友列表、个人资料与私聊。

设计原则
--------
应用全局只保留一个原生窗口（见 :mod:`gomoku.client.modal_overlay`）。
好友、资料与私聊因此不是 ``QDialog``，而是浮在联机面板之上的子控件：
卡片由宿主承载，任务栏始终只有一个条目，也不会打断正在进行的对局。

:class:`PaperPanel` 提供两种形态：

* **非模态**（默认）：卡片浮在宿主右上方，可以同时打开多张（好友列表 +
  多个私聊），互不阻塞；新建的卡片按固定步长层叠错开。
* **模态**（``modal=True``）：铺满宿主并用墨色遮罩拦截鼠标，
  ``exec()`` 进入嵌套事件循环，语义与 ``QDialog.exec()`` 一致。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QEventLoop, QPointF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QVBoxLayout, QWidget

from gomoku.client.theme_palette import CINNABAR, GOLD
from gomoku.client.ui_theme import RicePaperCard

#: 模态遮罩颜色：半透明墨色，压暗底层棋盘。
SCRIM_COLOR: QColor = QColor(26, 22, 17, 118)

#: 结果码，与 ``QDialog.DialogCode`` 数值一致。
RESULT_ACCEPTED: int = 1
RESULT_REJECTED: int = 0

#: 卡片与宿主边缘的间距（逻辑像素）。
PANEL_MARGIN: int = 14

#: 多张卡片同时展开时的层叠步长（逻辑像素）。
PANEL_CASCADE: int = 26


class PaperPanel(QWidget):
    """浮在宿主之上的宣纸卡片。

    面板本身是宿主的子控件，不是原生窗口，因此不会在任务栏里多出一条
    条目。内容统一放在 ``self.card``（宣纸卡片）里，用 ``self.content``
    往里加控件。
    """

    #: 面板被关闭时发出（用户点“关闭”或按 Esc）。
    closed = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        seal_text: Optional[str] = None,
        window_title: str = "",
        min_width: int = 440,
        min_height: int = 0,
        modal: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("paperPanel")
        self._window_title = window_title
        self._modal = modal
        self._result: int = RESULT_REJECTED
        self._loop: Optional[QEventLoop] = None

        self.setWindowTitle(window_title)
        # 卡片是圆角+投影的，宿主区域需要能透出来。
        self.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self.setAutoFillBackground(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(
            PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN, PANEL_MARGIN
        )
        outer.setSpacing(0)
        self.card = RicePaperCard(self, seal_text=seal_text, opaque=True)
        if modal:
            outer.addStretch(1)
            outer.addWidget(self.card, 0, Qt.AlignmentFlag.AlignCenter)
            outer.addStretch(1)
        else:
            outer.addWidget(self.card, 1)

        self.content = QVBoxLayout(self.card)
        self.content.setContentsMargins(30, 26, 30, 24)
        self.content.setSpacing(12)

        self._min_width = min_width
        self._min_height = min_height
        # 卡片是最小尺寸的真正载体：面板本身可以被压小（宿主窗口很窄时），
        # 最小尺寸落在卡片上，布局再把内容挤到卡片里。
        self.card.setMinimumWidth(min_width)
        if min_height > 0:
            self.card.setMinimumHeight(min_height)

        if parent is not None:
            parent.installEventFilter(self)

    # ------------------------------------------------------------------
    # 摆放
    # ------------------------------------------------------------------
    def _cascade_offset(self, host: QWidget) -> int:
        """已展开的兄弟卡片数量，用于层叠错开。"""
        visible = 0
        for child in host.children():
            if (
                isinstance(child, PaperPanel)
                and child is not self
                and child.isVisible()
            ):
                visible += 1
        return visible * PANEL_CASCADE

    def _fit_card(self, host_rect) -> None:
        """宿主比卡片还小时，先把卡片的最小尺寸让出去。

        否则面板会被最小尺寸撑大，从窗口边缘溢出去。
        """
        max_width = max(160, host_rect.width() - PANEL_MARGIN * 2)
        max_height = max(160, host_rect.height() - PANEL_MARGIN * 2)
        self.card.setMinimumWidth(min(self._min_width, max_width))
        self.card.setMinimumHeight(min(self._min_height, max_height))

    def _place(self) -> None:
        """把卡片摆到宿主里的目标位置。"""
        host = self.parentWidget()
        if host is None:
            return
        host_rect = host.rect()
        self._fit_card(host_rect)
        if self._modal:
            self.setGeometry(host_rect)
            return
        size = self.sizeHint().boundedTo(
            QSize(
                max(160, host_rect.width() - PANEL_MARGIN * 2),
                max(160, host_rect.height() - PANEL_MARGIN * 2),
            )
        )
        size.setWidth(max(size.width(), 160))
        size.setHeight(max(size.height(), 160))
        offset = self._cascade_offset(host)
        x = host_rect.right() - size.width() - PANEL_MARGIN - offset
        y = PANEL_MARGIN + offset
        # 贴回宿主内部：右边缘和下边缘都不要越界。
        x = min(
            x,
            host_rect.right() - size.width() - PANEL_MARGIN,
        )
        y = min(
            y,
            host_rect.bottom() - size.height() - PANEL_MARGIN,
        )
        self.setGeometry(
            max(PANEL_MARGIN, x),
            max(PANEL_MARGIN, y),
            size.width(),
            size.height(),
        )

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        self._place()
        super().showEvent(event)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 接口
        """宿主尺寸变化时重新摆放卡片。"""
        if obj is self.parentWidget() and event.type() == QEvent.Type.Resize:
            self._place()
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def show(self) -> None:  # noqa: D102 - Qt 接口
        self._place()
        super().show()

    def close(self) -> bool:  # noqa: D102 - Qt 接口
        """关闭面板：隐藏并发出 ``closed``。"""
        self.hide()
        self.closed.emit()
        return True

    def exec(self) -> int:
        """进入嵌套事件循环，直到用户接受或拒绝。"""
        self._result = RESULT_REJECTED
        self.show()
        loop = QEventLoop(self)
        self._loop = loop
        try:
            loop.exec()
        finally:
            self._loop = None
        self.hide()
        return self._result

    # ------------------------------------------------------------------
    # 语义（与 QDialog 保持一致的部分）
    # ------------------------------------------------------------------
    def accept(self) -> None:
        """确认：结果码置为接受。"""
        self.done(RESULT_ACCEPTED)

    def reject(self) -> None:
        """取消：结果码置为拒绝。"""
        self.done(RESULT_REJECTED)

    def done(self, code: int) -> None:
        """结束模态循环并记录结果码。"""
        self._result = int(code)
        if self._loop is not None:
            self._loop.quit()
        self.hide()

    def result(self) -> int:
        """返回最近一次结果码（1 接受 / 0 拒绝）。"""
        return self._result

    def isModal(self) -> bool:  # noqa: N802 - Qt 接口
        """是否模态。"""
        return self._modal

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        del event
        if not self._modal:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), SCRIM_COLOR)
        painter.end()


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


__all__ = ["InkDivider", "PaperPanel"]
