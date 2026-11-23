"""对战广场页面：进行中对局列表与观战入口。

页面只负责渲染和发出意图信号，不持有网络客户端或联机会话；
拉取列表、开始观战由 OnlinePanel 编排。
"""

from __future__ import annotations

from typing import Mapping, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

#: 观战列表最多展示的对局条数，避免页面被拉得过长。
MAX_LIVE_GAME_ROWS = 10


class PlazaPage(QWidget):
    """只读的对战广场：刷新、返回大厅、请求观战。"""

    refresh_requested = Signal()
    back_requested = Signal()
    watch_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("onlinePage")
        page_layout = QVBoxLayout(self)
        page_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea(self)
        scroll.setObjectName("onlineScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        viewport = scroll.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )

        container = QWidget(scroll)
        container.setObjectName("onlineScrollBody")
        container.setAutoFillBackground(False)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(18, 10, 18, 10)
        outer.setSpacing(12)
        outer.addStretch(1)

        card = QFrame(container)
        card.setObjectName("onlineCard")
        card.setMaximumWidth(660)
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        layout = QVBoxLayout(card)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(9)

        header = QHBoxLayout()
        title = QLabel("对战广场", card)
        title.setObjectName("onlineTitle")
        self.refresh_live_button = QPushButton("刷新", card)
        self.refresh_live_button.setObjectName("smallButton")
        self.refresh_live_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        self.refresh_live_button.setAutoDefault(False)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self.refresh_live_button)
        layout.addLayout(header)

        hint = QLabel(
            "这里列出所有允许观战的进行中对局，点“观战”即可进去旁观；"
            "观战者只能看，不能落子或发言。对局开始会自动出现，结束会自动消失。",
            card,
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # 每行一局：一个说明标签 + 一个“观战”按钮。
        self.live_games_container = QWidget(card)
        self.live_games_layout = QVBoxLayout(self.live_games_container)
        self.live_games_layout.setContentsMargins(0, 0, 0, 0)
        self.live_games_layout.setSpacing(6)
        layout.addWidget(self.live_games_container)

        self.live_status_label = QLabel(
            "正在加载进行中的对局…", card
        )
        self.live_status_label.setObjectName("hintLabel")
        self.live_status_label.setWordWrap(True)
        layout.addWidget(self.live_status_label)

        footer = QHBoxLayout()
        self.plaza_status_label = QLabel(card)
        self.plaza_status_label.setObjectName("onlineStatus")
        self.plaza_status_label.setWordWrap(True)
        self.plaza_status_label.setMinimumHeight(22)
        self.plaza_back_button = QPushButton("返回大厅", card)
        self.plaza_back_button.setObjectName("undoButton")
        self.plaza_back_button.setCursor(
            Qt.CursorShape.PointingHandCursor
        )
        footer.addWidget(self.plaza_status_label, 1)
        footer.addWidget(self.plaza_back_button)
        layout.addSpacing(2)
        layout.addLayout(footer)

        card_row = QHBoxLayout()
        card_row.setContentsMargins(0, 0, 0, 0)
        card_row.addStretch(1)
        card_row.addWidget(card, 20)
        card_row.addStretch(1)
        outer.addLayout(card_row)
        outer.addStretch(1)
        scroll.setWidget(container)
        page_layout.addWidget(scroll)

        self.refresh_live_button.clicked.connect(
            lambda _checked=False: self.refresh_requested.emit()
        )
        self.plaza_back_button.clicked.connect(
            lambda _checked=False: self.back_requested.emit()
        )

    def set_status(self, text: str) -> None:
        self.plaza_status_label.setText(text)

    def set_entries(self, entries: list[Mapping], total: int) -> None:
        """重建进行中对局列表。"""
        while self.live_games_layout.count():
            item = self.live_games_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        shown = entries[:MAX_LIVE_GAME_ROWS]
        for entry in shown:
            self.live_games_layout.addWidget(self._make_row(entry))
        if not entries:
            self.live_status_label.setText("当前没有进行中的对局")
            return
        if len(shown) < total:
            self.live_status_label.setText(
                f"共 {total} 局进行中，最多显示前 {len(shown)} 局"
            )
        else:
            self.live_status_label.setText(f"共 {total} 局进行中")

    def _make_row(self, entry: Mapping) -> QWidget:
        """构造一行“进行中对局”的说明与观战按钮。"""
        row = QWidget(self.live_games_container)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(8)
        label = QLabel(self._format_entry(entry), row)
        label.setObjectName("rankSummary")
        label.setWordWrap(True)
        row_layout.addWidget(label, 1)
        button = QPushButton("观战", row)
        button.setObjectName("undoButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        game_id = entry.get("game_id")
        if isinstance(game_id, str) and game_id:
            button.clicked.connect(
                lambda _checked=False, gid=game_id: (
                    self.watch_requested.emit(gid)
                )
            )
        else:
            button.setEnabled(False)
        row_layout.addWidget(button, 0)
        return row

    @staticmethod
    def _format_entry(entry: Mapping) -> str:
        """把一条进行中的对局格式化成一行中文说明。"""
        black = str(entry.get("black_username") or "黑方")
        white = str(entry.get("white_username") or "白方")
        mode = "排位" if entry.get("rated") else "自建房"
        move_no = entry.get("move_no", 0)
        if not isinstance(move_no, int) or isinstance(move_no, bool):
            move_no = 0
        return f"{black}（黑）对 {white}（白）· {mode} · 第 {move_no} 手"


__all__ = ["MAX_LIVE_GAME_ROWS", "PlazaPage"]
