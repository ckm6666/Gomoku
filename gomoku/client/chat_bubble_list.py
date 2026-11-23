"""私聊消息气泡列表：对方在左、自己在右，每条消息旁边带头像。

设计要点
--------
* **左右分明**：对方的消息靠左、自己的消息靠右，扫一眼就知道是谁说的；
* **头像随行**：每条消息旁边挂一个头像，聊到谁一目了然；
* **可读性优先**：正文比普通界面大一号，宣纸底 + 浓墨字对比强烈；
* **古典文雅**：对方用淡宣纸气泡、自己用淡金气泡，样式沿用全局的
  纸/墨/金配色，与好友浮层、个人资料浮层保持一致。

消息正文按纯文本渲染：聊天内容里的 ``<``、``>`` 不会被当成标签解释。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarView

#: 气泡最大宽度占可用宽度的比例：再宽就不像聊天了。
BUBBLE_WIDTH_RATIO = 0.68
#: 气泡最大宽度上限（逻辑像素）。
MAX_BUBBLE_WIDTH = 420
#: 消息旁头像的边长（逻辑像素）。
AVATAR_SIZE = 38


class ChatBubbleList(QWidget):
    """只读的消息气泡列表。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("privateChatScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        self._body = QWidget(self._scroll)
        self._body.setObjectName("privateChatBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(8, 8, 8, 10)
        self._body_layout.setSpacing(12)
        self._scroll.setWidget(self._body)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._scroll)

        # 空状态：还没有任何消息时占位，出现第一条消息后收起。
        self._empty_label = QLabel(
            "还没有聊天记录，打个招呼吧", self._body
        )
        self._empty_label.setObjectName("privateChatEmpty")
        self._empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty_label.setWordWrap(True)
        # 头尾各留一段弹性空白：空状态时提示居中，有消息时消息从顶部排。
        self._body_layout.addStretch(1)
        self._body_layout.addWidget(self._empty_label)
        self._body_layout.addStretch(1)

        self._bubbles: list[QFrame] = []
        self._rows: list[QWidget] = []
        self._texts: list[str] = []

        # 追加消息后要滚到底部，但此时布局还没算完，滚动条的最大值还是
        # 旧的。用单次定时器等布局落定再滚，连续追加也只滚一次。
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom)

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def append_message(
        self,
        *,
        sender_name: str,
        content: str,
        time_text: str = "",
        is_self: bool = False,
        avatar: str = "",
        user_id: str = "",
        avatar_version: int = 0,
    ) -> None:
        """追加一条消息气泡。"""
        row = QWidget(self._body)
        row.setObjectName("privateChatRow")
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        bubble = QFrame(row)
        bubble.setObjectName(
            "chatBubbleSelf" if is_self else "chatBubble"
        )
        bubble.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        # 气泡贴着正文宽度生长，但不超过最大宽度。
        bubble.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )

        inner = QVBoxLayout(bubble)
        inner.setContentsMargins(12, 7, 12, 9)
        inner.setSpacing(2)

        meta = QLabel(self._meta_text(sender_name, time_text), bubble)
        meta.setObjectName("chatBubbleMeta")
        meta.setAlignment(
            Qt.AlignmentFlag.AlignRight
            if is_self
            else Qt.AlignmentFlag.AlignLeft
        )
        inner.addWidget(meta)

        text = QLabel(content, bubble)
        text.setObjectName("chatBubbleText")
        text.setWordWrap(True)
        # 纯文本：聊天内容里的尖括号不会被当成标签。
        text.setTextFormat(Qt.TextFormat.PlainText)
        text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        inner.addWidget(text)

        view = AvatarView(
            avatar,
            user_id,
            AVATAR_SIZE,
            row,
            avatar_version=max(0, int(avatar_version)),
        )

        if is_self:
            layout.addStretch(1)
            layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignTop)
            layout.addWidget(view, 0, Qt.AlignmentFlag.AlignTop)
        else:
            layout.addWidget(view, 0, Qt.AlignmentFlag.AlignTop)
            layout.addWidget(bubble, 0, Qt.AlignmentFlag.AlignTop)
            layout.addStretch(1)

        # 插在末尾的弹性空白之前，消息才会自上而下排列。
        self._body_layout.insertWidget(
            self._body_layout.count() - 1, row
        )
        self._bubbles.append(bubble)
        self._rows.append(row)
        self._texts.append(content)
        if len(self._texts) == 1:
            self._empty_label.setVisible(False)
            # 有了消息，头部不再留白，消息从顶部往下排。
            self._body_layout.setStretch(0, 0)
        self._update_bubble_widths()
        self.scroll_to_bottom()

    @staticmethod
    def _meta_text(sender_name: str, time_text: str) -> str:
        """气泡上方的署名与时间。"""
        name = sender_name or "好友"
        if time_text:
            return f"{name} · {time_text}"
        return name

    def is_empty(self) -> bool:
        """当前是否一条消息都没有。"""
        return not self._texts

    def toPlainText(self) -> str:  # noqa: N802 - 与 QTextBrowser 同名接口
        """所有消息正文的纯文本。"""
        return "\n".join(self._texts)

    def clear(self) -> None:
        """清空所有消息。"""
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()
        self._bubbles.clear()
        self._texts.clear()
        self._empty_label.setVisible(True)
        self._body_layout.setStretch(0, 1)

    # ------------------------------------------------------------------
    # 排版
    # ------------------------------------------------------------------
    def _update_bubble_widths(self) -> None:
        """按当前宽度限制气泡的最大宽度，长消息才会折行。"""
        available = self._scroll.viewport().width() or self.width()
        max_width = int(
            min(
                MAX_BUBBLE_WIDTH,
                max(160, available * BUBBLE_WIDTH_RATIO),
            )
        )
        for bubble in self._bubbles:
            bubble.setMaximumWidth(max_width)

    def scroll_to_bottom(self) -> None:
        """滚动到最新一条消息（等布局算完再滚）。"""
        self._scroll_timer.start(0)

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        # 面板刚显示出来时内容才刚排好版，这时才该滚到底部。
        self.scroll_to_bottom()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._update_bubble_widths()


__all__ = ["AVATAR_SIZE", "BUBBLE_WIDTH_RATIO", "ChatBubbleList"]
