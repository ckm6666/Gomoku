"""古风确认弹窗：用于返回主菜单、重新开局、更改先手方等操作。"""

from __future__ import annotations

from typing import Optional

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


class ConfirmDialog(QDialog):
    """水墨宣纸风格的模态确认框。

    确认按钮文字由调用方决定（例如“返回主菜单”/“开始新对局”）；
    取消是默认按钮，Esc/关闭均视为取消，避免误触破坏当前棋局。
    """

    def __init__(
        self,
        title: str,
        message: str,
        *,
        confirm_text: str = "开始新对局",
        cancel_text: str = "取消",
        seal_text: str = "问",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._background: Optional[QPixmap] = None
        self._background_key: Optional[tuple[int, int, float]] = None

        self.setObjectName("confirmDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowFlag(
            Qt.WindowType.WindowContextHelpButtonHint, False
        )
        self.resize(500, 340)
        self.setMinimumSize(440, 300)
        self.setSizeGripEnabled(False)
        self.setAutoFillBackground(False)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(46, 24, 46, 24)
        layout.setSpacing(6)

        dpr = self.devicePixelRatioF()
        self.seal_label = QLabel(self)
        self.seal_label.setObjectName("confirmSeal")
        self.seal_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seal_label.setPixmap(
            make_seal_pixmap(seal_text, 44, variant="confirm", dpr=dpr)
        )

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("confirmTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.message_label = QLabel(message, self)
        self.message_label.setObjectName("confirmMessage")
        self.message_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message_label.setWordWrap(True)

        self.divider_label = QLabel(self)
        self.divider_label.setObjectName("confirmDivider")
        self.divider_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.divider_label.setPixmap(make_ink_divider(170, 14, dpr=dpr))

        layout.addWidget(self.seal_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.divider_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.confirm_button = QPushButton(confirm_text, self)
        self.confirm_button.setObjectName("resultPrimaryButton")
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.confirm_button.setMinimumWidth(112)
        self.confirm_button.setMinimumHeight(38)
        self.cancel_button = QPushButton(cancel_text, self)
        self.cancel_button.setObjectName("resultSecondaryButton")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setMinimumWidth(112)
        self.cancel_button.setMinimumHeight(38)
        buttons.addWidget(self.confirm_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.confirm_button.clicked.connect(self.accept)
        self.cancel_button.clicked.connect(self.reject)
        self.cancel_button.setFocus()

    @property
    def confirm_requested(self) -> bool:
        """用户是否明确确认。"""
        return self.result() == int(QDialog.DialogCode.Accepted)

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        del event
        width = self.width()
        height = self.height()
        if width <= 0 or height <= 0:
            return
        dpr = max(1.0, self.devicePixelRatioF())
        key = (width, height, round(dpr, 3))
        if self._background is None or self._background_key != key:
            self._background = render_result_backdrop(
                QSize(width, height), dpr, "draw"
            )
            self._background_key = key
        painter = QPainter(self)
        painter.drawPixmap(
            QRectF(0, 0, width, height),
            self._background,
            QRectF(self._background.rect()),
        )
        painter.end()


__all__ = ["ConfirmDialog"]
