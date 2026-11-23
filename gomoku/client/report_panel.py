"""对局举报浮层：类别、说明与回放审计摘要。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QWidget,
)

from gomoku.client.paper_panel import PaperPanel
from gomoku.client.ui_theme import PaperComboBox

#: 举报类别 → 服务端 category / 显示文案。
REPORT_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("cheating", "作弊或使用外部 AI 辅助"),
    ("abuse", "辱骂或不当言论"),
    ("stalling", "故意拖延、挂机或恶意超时"),
    ("other", "其他违规行为"),
)

MAX_DESCRIPTION_LENGTH = 500


class ReportPanel(PaperPanel):
    """举报一局已经结束的联机对局。

    面板本身不直接发网络请求；由 OnlinePanel 连接 ``submitted`` 后调用
    HTTP 客户端，并在成功/失败时回调 :meth:`set_result`。
    """

    submitted = Signal(str, str)  # category, description

    def __init__(
        self,
        game_id: str,
        opponent_name: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(
            parent,
            seal_text="举",
            window_title="举报对手",
            min_width=520,
            min_height=440,
            modal=True,
        )
        self.game_id = game_id
        # 成功提示后自动关闭；计时器挂在面板上，面板销毁时自动停止，
        # 避免对已删除的控件调用 accept()。
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self.accept)
        self._build(opponent_name)

    def _build(self, opponent_name: str) -> None:
        title = QLabel("举报对手", self.card)
        title.setObjectName("confirmTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel(
            f"对局：{self.game_id[:8]}…　对手：{opponent_name or '未知'}",
            self.card,
        )
        subtitle.setObjectName("confirmMessage")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)

        category_row = QHBoxLayout()
        category_label = QLabel("违规类型", self.card)
        category_label.setObjectName("formLabel")
        self.category_combo = PaperComboBox(self.card)
        for key, label in REPORT_CATEGORIES:
            self.category_combo.addItem(label, key)
        category_row.addWidget(category_label)
        category_row.addWidget(self.category_combo, 1)

        self.description_edit = QPlainTextEdit(self.card)
        self.description_edit.setObjectName("reportDescription")
        self.description_edit.setPlaceholderText(
            "请简要说明发生了什么（可选，最多 500 字）。"
            "管理员会结合回放审计与对局记录处理。"
        )
        self.description_edit.setMaximumBlockCount(20)
        self.description_edit.setFixedHeight(120)

        self.audit_label = QLabel("正在读取回放审计…", self.card)
        self.audit_label.setObjectName("hintLabel")
        self.audit_label.setWordWrap(True)

        self.status_label = QLabel("", self.card)
        self.status_label.setObjectName("onlineStatus")
        self.status_label.setWordWrap(True)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.submit_button = QPushButton("提交举报", self.card)
        self.submit_button.setObjectName("resultPrimaryButton")
        self.submit_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.submit_button.setAutoDefault(False)
        self.cancel_button = QPushButton("取消", self.card)
        self.cancel_button.setObjectName("resultSecondaryButton")
        self.cancel_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.cancel_button.setAutoDefault(True)
        buttons.addWidget(self.submit_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)

        self.content.addWidget(title)
        self.content.addWidget(subtitle)
        self.content.addLayout(category_row)
        self.content.addWidget(self.description_edit)
        self.content.addWidget(self.audit_label)
        self.content.addWidget(self.status_label)
        self.content.addLayout(buttons)

        self.submit_button.clicked.connect(self._on_submit)
        self.cancel_button.clicked.connect(self.reject)

    @property
    def selected_category(self) -> str:
        value = self.category_combo.currentData()
        return value if isinstance(value, str) else "other"

    @property
    def description(self) -> str:
        return self.description_edit.toPlainText().strip()[
            :MAX_DESCRIPTION_LENGTH
        ]

    def _on_submit(self) -> None:
        if not self.submit_button.isEnabled():
            return
        self.submit_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.set_status("正在提交…")
        self.submitted.emit(self.selected_category, self.description)

    def set_status(self, text: str, error: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("error", "true" if error else "false")
        self.status_label.style().unpolish(self.status_label)
        self.status_label.style().polish(self.status_label)

    def set_audit(
        self,
        findings: int,
        *,
        ok: bool,
        details: str = "",
    ) -> None:
        if ok and findings == 0:
            text = "回放审计：落子序列与终局结果一致，未发现异常。"
        elif findings:
            text = f"回放审计：发现 {findings} 项异常，管理员会重点复核。"
            if details:
                text += f"\n{details}"
        else:
            text = "回放审计：暂无结果。"
        self.audit_label.setText(text)

    def set_result(self, message: str, *, success: bool) -> None:
        self.set_status(message, error=not success)
        if success:
            self.cancel_button.setEnabled(True)
            self.cancel_button.setText("关闭")
            self._close_timer.start(900)
        else:
            self.submit_button.setEnabled(True)
            self.cancel_button.setEnabled(True)


__all__ = [
    "MAX_DESCRIPTION_LENGTH",
    "REPORT_CATEGORIES",
    "ReportPanel",
]
