"""发现新版本时的提示弹窗。

客户端只负责「发现」与「指引」两件事：真正的安装包始终由用户自己从
GitHub Releases 页面下载。客户端不经过游戏服务端分发新版本，服务端不可用
也不影响更新检查。

弹窗沿用主窗口内的模态遮罩（见 :mod:`gomoku.client.modal_overlay`），
不创建第二个原生窗口。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
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


class UpdateAvailableDialog(ModalOverlay):
    """「发现新版本」弹窗。

    展示最新版本号与更新说明，并提供一个「前往下载」按钮：点击后发出
    ``download_requested`` 信号，由调用方打开 GitHub 发布页。弹窗本身不
    下载任何文件，也不直接操作浏览器。
    """

    download_requested = Signal(str)  # 下载地址（GitHub 发布页）

    def __init__(
        self,
        *,
        version: str,
        current_version: str,
        notes: str,
        download_url: str,
        asset_name: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, object_name="updateDialog")

        self._download_url = download_url

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

        # 安装包文件名：告诉用户发布页上该找哪个文件，避免下载错产物。
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

        layout.addStretch(1)

        buttons = QHBoxLayout()
        buttons.setSpacing(14)
        buttons.addStretch(1)
        self.download_button = QPushButton("前往下载", card)
        self.download_button.setObjectName("resultPrimaryButton")
        self.download_button.setAutoDefault(False)
        self.download_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.download_button.setMinimumWidth(112)
        self.download_button.setMinimumHeight(38)
        self.later_button = QPushButton("稍后再说", card)
        self.later_button.setObjectName("resultSecondaryButton")
        self.later_button.setDefault(True)
        self.later_button.setAutoDefault(True)
        self.later_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.later_button.setMinimumWidth(112)
        self.later_button.setMinimumHeight(38)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.later_button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self.download_button.clicked.connect(self._on_download_clicked)
        self.later_button.clicked.connect(self.reject)
        self.set_default_action(self.reject)
        self.later_button.setFocus()

    def _on_download_clicked(self) -> None:
        """把下载意图交给调用方去打开浏览器。"""
        self.download_requested.emit(self._download_url)
        self.accept()


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "NOTES_AREA_HEIGHT",
    "UpdateAvailableDialog",
]
