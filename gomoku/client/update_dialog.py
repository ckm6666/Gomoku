"""更新相关弹窗：新版本提示与下载进度。

与终局弹窗保持一致：更新弹窗不创建独立窗口，而是覆盖在主窗口中央部件
之上的模态遮罩层（见 :mod:`gomoku.client.modal_overlay`），因此应用全局
始终只有一个原生窗口，任务栏不会出现第二个条目。

两个弹窗都提供与 ``QDialog`` 等价的能力：``exec()`` / ``accept()`` /
``reject()`` / ``result()``。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.modal_overlay import ModalOverlay
from gomoku.client.ui_theme import make_seal_pixmap, render_result_backdrop
from gomoku.client.updater import UpdateInfo, format_size

logger = logging.getLogger(__name__)

#: 新版本提示卡片尺寸（逻辑像素）。
CARD_WIDTH: int = 500
CARD_HEIGHT: int = 440

#: 下载进度卡片尺寸（逻辑像素）。
PROGRESS_CARD_WIDTH: int = 430
PROGRESS_CARD_HEIGHT: int = 220


def _format_size(num_bytes: int) -> str:
    """把字节数格式化成人类可读的中文大小。"""
    try:
        return format_size(int(num_bytes))
    except Exception:
        logger.exception("格式化更新包大小失败")
        return f"{int(num_bytes)} 字节"


class UpdateDialog(ModalOverlay):
    """新版本提示遮罩：展示版本差异、更新说明与安装包大小。"""

    download_requested = Signal(object)  # UpdateInfo
    ignore_requested = Signal(object)  # UpdateInfo

    def __init__(
        self,
        info: UpdateInfo,
        current_version: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, object_name="updateDialog")
        self._info = info

        card = self.card
        card.setObjectName("updateCard")
        card.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        card.set_renderer(
            lambda size, dpr: render_result_backdrop(size, dpr, "draw")
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(40, 26, 40, 24)
        layout.setSpacing(8)

        dpr = card.devicePixelRatioF()
        self.seal_label = QLabel(card)
        self.seal_label.setObjectName("updateSeal")
        self.seal_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seal_label.setPixmap(
            make_seal_pixmap("更", 46, variant="confirm", dpr=dpr)
        )

        self.title_label = QLabel("软件更新", card)
        self.title_label.setObjectName("updateTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.version_label = QLabel(
            f"当前版本 {current_version} → 新版本 {info.version}", card
        )
        self.version_label.setObjectName("updateVersion")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.size_label = QLabel(
            f"安装包大小：{_format_size(info.size_bytes)}", card
        )
        self.size_label.setObjectName("updateSize")
        self.size_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.notes_browser = QTextBrowser(card)
        self.notes_browser.setObjectName("updateNotes")
        self.notes_browser.setReadOnly(True)
        self.notes_browser.setOpenExternalLinks(False)
        self.notes_browser.setMinimumHeight(140)
        self.notes_browser.setPlainText(
            (info.notes or "").strip() or "本次更新没有提供更新说明。"
        )

        layout.addWidget(self.seal_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.version_label)
        layout.addWidget(self.size_label)
        layout.addWidget(self.notes_browser, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addStretch(1)
        self.download_button = QPushButton("下载并安装", card)
        self.download_button.setObjectName("resultPrimaryButton")
        self.download_button.setAutoDefault(True)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setMinimumWidth(104)
        self.download_button.setMinimumHeight(38)

        self.later_button = QPushButton("稍后提醒", card)
        self.later_button.setObjectName("resultSecondaryButton")
        self.later_button.setAutoDefault(False)
        self.later_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_button.setMinimumWidth(96)
        self.later_button.setMinimumHeight(38)

        self.ignore_button = QPushButton("忽略此版本", card)
        self.ignore_button.setObjectName("resultSecondaryButton")
        self.ignore_button.setAutoDefault(False)
        self.ignore_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.ignore_button.setMinimumWidth(96)
        self.ignore_button.setMinimumHeight(38)

        buttons.addWidget(self.download_button)
        buttons.addWidget(self.later_button)
        buttons.addWidget(self.ignore_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.download_button.clicked.connect(self._on_download_clicked)
        self.later_button.clicked.connect(self.reject)
        self.ignore_button.clicked.connect(self._on_ignore_clicked)
        self.set_default_action(self._on_download_clicked)
        self.download_button.setFocus()

    # ------------------------------------------------------------------
    # 语义
    # ------------------------------------------------------------------
    @property
    def info(self) -> UpdateInfo:
        """本弹窗展示的版本信息。"""
        return self._info

    def _on_download_clicked(self) -> None:
        """请求下载：先通知外部，再结束模态循环。"""
        try:
            self.download_requested.emit(self._info)
        finally:
            self.accept()

    def _on_ignore_clicked(self) -> None:
        """请求忽略该版本：先通知外部，再结束模态循环。"""
        try:
            self.ignore_requested.emit(self._info)
        finally:
            self.reject()


class DownloadProgressDialog(ModalOverlay):
    """下载进度遮罩：进度条 + 状态文字 + 取消按钮。"""

    cancel_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, object_name="updateProgressDialog")

        card = self.card
        card.setObjectName("updateProgressCard")
        card.setFixedSize(PROGRESS_CARD_WIDTH, PROGRESS_CARD_HEIGHT)
        card.set_renderer(
            lambda size, dpr: render_result_backdrop(size, dpr, "draw")
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 24, 36, 22)
        layout.setSpacing(10)

        self.title_label = QLabel("正在下载更新", card)
        self.title_label.setObjectName("updateProgressTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status_label = QLabel("正在准备下载…", card)
        self.status_label.setObjectName("updateProgressStatus")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.progress_bar = QProgressBar(card)
        self.progress_bar.setObjectName("updateProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setMinimumHeight(18)

        layout.addWidget(self.title_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消", card)
        self.cancel_button.setObjectName("resultSecondaryButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setMinimumWidth(96)
        self.cancel_button.setMinimumHeight(38)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.cancel_button.clicked.connect(self._on_cancel_clicked)

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def set_progress(self, received: int, total: int) -> None:
        """更新进度条；总长度未知（<=0）时显示不确定进度。"""
        received = max(0, int(received))
        if total is not None and int(total) > 0:
            total_bytes = int(total)
            percent = min(100, int(received * 100 / total_bytes))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            self.progress_bar.setFormat(f"{percent}%")
            self.status_label.setText(
                f"正在下载：{_format_size(received)} / "
                f"{_format_size(total_bytes)}"
            )
            return
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("")
        self.status_label.setText(f"正在下载：{_format_size(received)}")

    def _on_cancel_clicked(self) -> None:
        """通知外部取消下载并结束模态循环。"""
        try:
            self.cancel_requested.emit()
        finally:
            self.reject()


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "DownloadProgressDialog",
    "PROGRESS_CARD_HEIGHT",
    "PROGRESS_CARD_WIDTH",
    "UpdateDialog",
]
