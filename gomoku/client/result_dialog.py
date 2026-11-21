"""古风终局弹窗：胜利 / 惜败 / 和棋。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.ui_theme import (
    make_ink_divider,
    make_seal_pixmap,
    render_result_backdrop,
)

ResultKind = Literal["win", "lose", "draw"]


@dataclass(frozen=True)
class ResultPresentation:
    """终局弹窗的展示内容。"""

    kind: ResultKind
    title: str
    subtitle: str
    detail: str
    seal_text: str
    primary_text: str = "再来一局"
    secondary_text: str = "查看棋盘"


class ResultDialog(QDialog):
    """模态终局弹窗；接受表示再来一局，拒绝表示查看棋盘。"""

    def __init__(
        self,
        presentation: ResultPresentation,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._presentation = presentation
        self._background: Optional[QPixmap] = None
        self._background_key: Optional[tuple[int, int, float, str]] = None

        self.setObjectName("resultDialog")
        self.setWindowTitle("对局结果")
        self.setModal(True)
        self.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint, False
        )
        self.resize(520, 430)
        self.setMinimumSize(460, 380)
        self.setSizeGripEnabled(False)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(46, 28, 46, 26)
        layout.setSpacing(6)

        dpr = self.devicePixelRatioF()
        self.seal_label = QLabel(self)
        self.seal_label.setObjectName("resultSeal")
        self.seal_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seal_label.setPixmap(
            make_seal_pixmap(
                presentation.seal_text, 54, variant=presentation.kind, dpr=dpr
            )
        )

        self.title_label = QLabel(presentation.title, self)
        self.title_label.setObjectName("resultTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.subtitle_label = QLabel(presentation.subtitle, self)
        self.subtitle_label.setObjectName("resultSubtitle")
        self.subtitle_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.subtitle_label.setWordWrap(True)

        self.divider_label = QLabel(self)
        self.divider_label.setObjectName("resultDivider")
        self.divider_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.divider_label.setPixmap(make_ink_divider(190, 16, dpr=dpr))

        self.detail_label = QLabel(presentation.detail, self)
        self.detail_label.setObjectName("resultDetail")
        self.detail_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.detail_label.setWordWrap(True)

        layout.addWidget(self.seal_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.subtitle_label)
        layout.addWidget(self.divider_label)
        layout.addWidget(self.detail_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.restart_button = QPushButton(
            presentation.primary_text, self
        )
        self.restart_button.setObjectName("resultPrimaryButton")
        self.restart_button.setDefault(True)
        self.restart_button.setAutoDefault(True)
        self.restart_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restart_button.setMinimumWidth(112)
        self.restart_button.setMinimumHeight(38)
        self.view_button = QPushButton(
            presentation.secondary_text, self
        )
        self.view_button.setObjectName("resultSecondaryButton")
        self.view_button.setAutoDefault(False)
        self.view_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.view_button.setMinimumWidth(112)
        self.view_button.setMinimumHeight(38)
        buttons.addWidget(self.restart_button)
        buttons.addWidget(self.view_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.restart_button.clicked.connect(self.accept)
        self.view_button.clicked.connect(self.reject)
        self.restart_button.setFocus()

    # ------------------------------------------------------------------
    # 语义
    # ------------------------------------------------------------------
    @property
    def restart_requested(self) -> bool:
        """用户是否选择再来一局。"""
        return self.result() == int(QDialog.DialogCode.Accepted)

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        del event
        width = self.width()
        height = self.height()
        if width <= 0 or height <= 0:
            return
        dpr = max(1.0, self.devicePixelRatioF())
        key = (
            width,
            height,
            round(dpr, 3),
            self._presentation.kind,
        )
        if self._background is None or self._background_key != key:
            self._background = render_result_backdrop(
                QSize(width, height), dpr, self._presentation.kind
            )
            self._background_key = key
        painter = QPainter(self)
        painter.drawPixmap(
            QRectF(0, 0, width, height),
            self._background,
            QRectF(self._background.rect()),
        )
        painter.end()


__all__ = ["ResultDialog", "ResultPresentation", "ResultKind"]
