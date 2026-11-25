"""发现新版本时的提示弹窗，以及下载更新时的进度弹窗。

弹窗沿用主窗口内的模态遮罩（见 :mod:`gomoku.client.modal_overlay`），
不创建第二个原生窗口。

更新分两步：先在这里确认「立即更新」，下载完成后由主窗口接手
解压、替换与重启。全程不离开游戏界面：客户端不再把用户引到网页去
手动下载安装包。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.modal_overlay import ModalOverlay
from gomoku.client.ui_theme import (
    make_ink_divider,
    make_seal_pixmap,
    render_result_backdrop,
)

#: 更新卡片尺寸（逻辑像素）；比确认框高一些，用来容纳更新说明。
CARD_WIDTH: int = 480
CARD_HEIGHT: int = 400

#: 更新说明正文区最大高度，超出部分在区域内滚动。
NOTES_AREA_HEIGHT: int = 150

#: 下载进度卡片尺寸（逻辑像素）。
PROGRESS_CARD_WIDTH: int = 420
PROGRESS_CARD_HEIGHT: int = 300


def format_size(num_bytes: int) -> str:
    """把字节数格式化成易读的文本。"""
    value = max(0, int(num_bytes))
    if value < 1024:
        return f"{value} B"
    if value < 1024 * 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value / (1024 * 1024):.1f} MB"


class UpdateAvailableDialog(ModalOverlay):
    """「发现新版本」弹窗。

    展示最新版本号与更新说明，并提供「立即更新」按钮：点击后发出
    ``update_requested``（携带产物下载地址），由调用方下载、校验并替换。

    不支持原地替换时（例如源码运行、非 Windows 平台），主按钮不出现，
    弹窗改为说明原因（``unsupported_reason``）并只保留「知道了」——更新
    始终在游戏界面内完成，不会跳到网页让人手动下载。
    """

    #: 用户点了「立即更新」，参数为产物下载地址。
    update_requested = Signal(str)

    def __init__(
        self,
        *,
        version: str,
        current_version: str,
        notes: str,
        asset_name: str = "",
        asset_url: str = "",
        can_update: bool = False,
        unsupported_reason: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, object_name="updateDialog")

        self._asset_url = asset_url
        self._can_update = bool(can_update and asset_url)

        card = self.card
        card.setObjectName("updateCard")
        card.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        card.set_renderer(
            lambda size, dpr: render_result_backdrop(size, dpr, "draw")
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(40, 20, 40, 20)
        layout.setSpacing(6)

        dpr = card.devicePixelRatioF()
        self.seal_label = QLabel(card)
        self.seal_label.setObjectName("confirmSeal")
        self.seal_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seal_label.setPixmap(
            make_seal_pixmap("新", 44, variant="confirm", dpr=dpr)
        )

        self.title_label = QLabel("发现新版本", card)
        self.title_label.setObjectName("confirmTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.version_label = QLabel(
            f"最新版本 {version}（当前 {current_version}）", card
        )
        self.version_label.setObjectName("confirmMessage")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setWordWrap(True)
        # 版本号来自 GitHub 响应，按纯文本渲染，避免其中的标记被解释。
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)

        self.divider_label = QLabel(card)
        self.divider_label.setObjectName("confirmDivider")
        self.divider_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.divider_label.setPixmap(make_ink_divider(170, 14, dpr=dpr))

        # 安装包文件名：告诉用户会下载哪个文件，避免下载错产物。
        self.asset_label = QLabel(
            f"Windows 安装包：{asset_name}" if asset_name else "",
            card,
        )
        self.asset_label.setObjectName("updateAsset")
        self.asset_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.asset_label.setWordWrap(True)
        self.asset_label.setVisible(bool(asset_name))
        # 产物名同样来自远端，按纯文本渲染。
        self.asset_label.setTextFormat(Qt.TextFormat.PlainText)

        layout.addWidget(self.seal_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.version_label)
        layout.addWidget(self.asset_label)
        layout.addWidget(self.divider_label)

        # 更新说明可能很长（GitHub 自动生成的变更日志），放进滚动区，
        # 避免弹窗被长文本撑破。
        self.notes_area = QScrollArea(card)
        self.notes_area.setObjectName("updateNotesArea")
        self.notes_area.setWidgetResizable(True)
        self.notes_area.setFixedHeight(NOTES_AREA_HEIGHT)
        self.notes_area.setFrameShape(QScrollArea.Shape.NoFrame)

        self.notes_label = QLabel(notes or "本次更新未附带说明。", card)
        self.notes_label.setObjectName("updateNotes")
        self.notes_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.notes_label.setWordWrap(True)
        self.notes_label.setTextFormat(Qt.TextFormat.PlainText)
        self.notes_area.setWidget(self.notes_label)
        layout.addWidget(self.notes_area)

        # 不支持原地替换时的原因说明。
        self.reason_label = QLabel(unsupported_reason, card)
        self.reason_label.setObjectName("updateReason")
        self.reason_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reason_label.setWordWrap(True)
        self.reason_label.setTextFormat(Qt.TextFormat.PlainText)
        self.reason_label.setVisible(bool(unsupported_reason))
        layout.addWidget(self.reason_label)

        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.update_button = QPushButton("立即更新", card)
        self.update_button.setObjectName("resultPrimaryButton")
        self.update_button.setAutoDefault(False)
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setMinimumWidth(112)
        self.update_button.setMinimumHeight(38)
        self.update_button.setVisible(self._can_update)
        self.later_button = QPushButton(
            "稍后再说" if self._can_update else "知道了", card
        )
        self.later_button.setObjectName("resultSecondaryButton")
        self.later_button.setDefault(True)
        self.later_button.setAutoDefault(True)
        self.later_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_button.setMinimumWidth(112)
        self.later_button.setMinimumHeight(38)
        buttons.addWidget(self.update_button)
        buttons.addWidget(self.later_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.update_button.clicked.connect(self._on_update_clicked)
        self.later_button.clicked.connect(self.reject)
        self.set_default_action(self.reject)
        self.later_button.setFocus()

    def _on_update_clicked(self) -> None:
        """把更新意图交给调用方去下载并替换。"""
        if not self._can_update:
            return
        self.update_requested.emit(self._asset_url)
        self.accept()


class UpdateProgressDialog(ModalOverlay):
    """「正在下载更新」进度弹窗。

    显示下载进度与已下载/总大小，并提供「取消下载」按钮；取消只是把
    下载意图交给调用方，真正的中止由调用方执行。
    """

    cancel_requested = Signal()

    def __init__(
        self,
        *,
        version: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, object_name="updateProgressDialog")

        card = self.card
        card.setObjectName("updateProgressCard")
        card.setFixedSize(PROGRESS_CARD_WIDTH, PROGRESS_CARD_HEIGHT)
        card.set_renderer(
            lambda size, dpr: render_result_backdrop(size, dpr, "draw")
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(36, 22, 36, 22)
        layout.setSpacing(8)

        dpr = card.devicePixelRatioF()
        self.seal_label = QLabel(card)
        self.seal_label.setObjectName("confirmSeal")
        self.seal_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.seal_label.setPixmap(
            make_seal_pixmap("更", 40, variant="confirm", dpr=dpr)
        )

        self.title_label = QLabel("正在下载更新", card)
        self.title_label.setObjectName("confirmTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.version_label = QLabel(f"正在下载 {version}", card)
        self.version_label.setObjectName("confirmMessage")
        self.version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.version_label.setTextFormat(Qt.TextFormat.PlainText)

        self.progress_bar = QProgressBar(card)
        self.progress_bar.setObjectName("updateProgressBar")
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(10)

        self.status_label = QLabel("正在连接…", card)
        self.status_label.setObjectName("updateProgressText")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)

        layout.addWidget(self.seal_label)
        layout.addWidget(self.title_label)
        layout.addWidget(self.version_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.status_label)
        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消下载", card)
        self.cancel_button.setObjectName("resultSecondaryButton")
        self.cancel_button.setDefault(True)
        self.cancel_button.setAutoDefault(True)
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setMinimumWidth(112)
        self.cancel_button.setMinimumHeight(38)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.cancel_button.clicked.connect(self._on_cancel_clicked)
        self.set_default_action(self._on_cancel_clicked)
        self.cancel_button.setFocus()

    def set_progress(self, received: int, total: int) -> None:
        """更新进度条与状态文本。"""
        received = max(0, int(received))
        if total > 0:
            percent = min(100, int(received * 100 / total))
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(percent)
            self.status_label.setText(
                f"{percent}%（{format_size(received)} / {format_size(total)}）"
            )
        else:
            # 大小未知：进度条走「不确定」模式，只显示已下载多少。
            self.progress_bar.setRange(0, 0)
            self.status_label.setText(f"已下载 {format_size(received)}")

    def set_status(self, text: str) -> None:
        """覆盖状态文本（例如下载完成后的提示）。"""
        self.status_label.setText(text)

    def _on_cancel_clicked(self) -> None:
        """请求取消下载；真正的中止由调用方执行。"""
        self.cancel_requested.emit()
        self.reject()


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "NOTES_AREA_HEIGHT",
    "PROGRESS_CARD_HEIGHT",
    "PROGRESS_CARD_WIDTH",
    "UpdateAvailableDialog",
    "UpdateProgressDialog",
    "format_size",
]
