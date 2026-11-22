"""好友私聊面板：历史消息与实时收发。"""

from __future__ import annotations

import html
from datetime import datetime
from typing import Mapping, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarView
from gomoku.client.paper_panel import InkDivider, PaperPanel
from gomoku.shared.social import MAX_DIRECT_MESSAGE_LENGTH


class PrivateChatPanel(PaperPanel):
    """与一位好友的私聊窗口。"""

    send_requested = Signal(str)

    def __init__(
        self,
        friend_id: str,
        username: str,
        avatar: str = "",
        parent: Optional[QWidget] = None,
        avatar_version: int = 0,
    ) -> None:
        super().__init__(
            parent,
            seal_text="语",
            window_title=f"与 {username} 聊天",
            min_width=480,
            min_height=520,
        )
        self.friend_id = friend_id
        self._seen_ids: set[str] = set()
        self._last_sent = ""

        header = QHBoxLayout()
        header.setSpacing(12)
        self.avatar_view = AvatarView(
            avatar,
            friend_id,
            44,
            self.card,
            avatar_version=max(0, int(avatar_version)),
        )
        identity = QVBoxLayout()
        identity.setSpacing(1)
        self.name_label = QLabel(username, self.card)
        self.name_label.setObjectName("socialPageTitle")
        self.online_label = QLabel("", self.card)
        self.online_label.setObjectName("profileOnline")
        subtitle = QLabel("好友私聊 · 消息会保存", self.card)
        subtitle.setObjectName("socialSubtitle")
        identity.addWidget(self.name_label)
        identity.addWidget(self.online_label)
        identity.addWidget(subtitle)
        header.addWidget(self.avatar_view)
        header.addLayout(identity, 1)
        self.content.addLayout(header)
        self.content.addWidget(InkDivider(self.card))

        self.messages_view = QTextBrowser(self.card)
        self.messages_view.setObjectName("chatMessages")
        self.messages_view.setOpenExternalLinks(False)
        self.content.addWidget(self.messages_view, 1)

        input_row = QHBoxLayout()
        input_row.setSpacing(8)
        self.input = QLineEdit(self.card)
        self.input.setObjectName("chatMessageInput")
        self.input.setPlaceholderText("输入消息，回车发送")
        self.input.setMaxLength(MAX_DIRECT_MESSAGE_LENGTH)
        self.send_button = QPushButton("发送", self.card)
        self.send_button.setObjectName("chatSendButton")
        self.send_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.send_button.setAutoDefault(False)
        input_row.addWidget(self.input, 1)
        input_row.addWidget(self.send_button)
        self.content.addLayout(input_row)

        self.status_label = QLabel(
            "私聊仅限好友 · 单条最多 200 字", self.card
        )
        self.status_label.setObjectName("socialStatus")
        self.status_label.setWordWrap(True)
        self.content.addWidget(self.status_label)

        self.send_button.clicked.connect(self._on_send)
        self.input.returnPressed.connect(self._on_send)

    def set_friend(
        self,
        username: str,
        avatar: str = "",
        online: Optional[bool] = None,
        avatar_version: int = 0,
    ) -> None:
        self.name_label.setText(username)
        self.avatar_view.set_avatar(
            avatar, self.friend_id, max(0, int(avatar_version))
        )
        if online is None:
            self.online_label.setText("")
        else:
            self.online_label.setText("在线" if online else "离线")

    def set_send_enabled(self, enabled: bool) -> None:
        self.input.setEnabled(enabled)
        self.send_button.setEnabled(enabled)

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def set_history(
        self,
        messages: Sequence[Mapping],
        self_id: str,
    ) -> None:
        """把服务端历史并进当前记录。

        不能直接清空重画：历史请求和实时推送是两条独立的链路，谁先到
        说不准。清空会把刚收到的实时消息抹掉，不去重又会把同一条消息
        显示两遍。这里按消息 id 合并，只补上还没出现过的。
        """
        if not self._seen_ids and not self.messages_view.toPlainText():
            self.messages_view.setHtml(
                '<div style="color:#7A6A55;text-align:center;'
                'padding:20px;">还没有聊天记录，打个招呼吧</div>'
            )
        added = False
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            if self._append(message, self_id, scroll=False):
                added = True
        if added:
            self._scroll_to_bottom()

    def append_message(self, message: Mapping, self_id: str) -> None:
        if self._append(message, self_id, scroll=True):
            self._scroll_to_bottom()

    @property
    def last_draft(self) -> str:
        """最近一次发送出去、但还没确认的文本。"""
        return self._last_sent

    def restore_draft(self) -> None:
        """发送失败时把文本放回输入框，别让用户白打一遍。"""
        if self._last_sent:
            self.input.setText(self._last_sent)
            self._last_sent = ""

    def clear_draft(self) -> None:
        """发送成功：忘掉待确认的草稿。"""
        self._last_sent = ""

    def _append(
        self,
        message: Mapping,
        self_id: str,
        *,
        scroll: bool,
    ) -> bool:
        """把一条消息画进记录里；已经显示过就跳过。

        返回 True 表示这次确实新增了一条。
        """
        message_id = str(message.get("id", ""))
        if message_id:
            if message_id in self._seen_ids:
                return False
            self._seen_ids.add(message_id)
        sender_id = str(message.get("sender_id", ""))
        is_self = sender_id == self_id
        sender_name = (
            "我"
            if is_self
            else str(message.get("sender_name", "好友"))
        )
        content = html.escape(str(message.get("content", "")))
        time_text = self._format_time(message)
        align = "right" if is_self else "left"
        bubble = "#E8D5B5" if is_self else "#EFE6D5"
        block = (
            f'<div style="margin:6px 0;text-align:{align};">'
            f'<span style="color:#7A6A55;font-size:11px;">'
            f"{html.escape(sender_name)} · {time_text}</span><br/>"
            f'<span style="background:{bubble};padding:4px 8px;'
            f'border-radius:6px;color:#2B2118;">{content}</span>'
            "</div>"
        )
        self.messages_view.append(block)
        if scroll:
            self._scroll_to_bottom()
        return True

    @staticmethod
    def _format_time(message: Mapping) -> str:
        sent_at_ms = message.get("sent_at_ms")
        if isinstance(sent_at_ms, int) and sent_at_ms > 0:
            try:
                return datetime.fromtimestamp(
                    sent_at_ms / 1000.0
                ).strftime("%H:%M")
            except (OSError, ValueError):
                pass
        created = message.get("created_at")
        if isinstance(created, str) and created:
            try:
                return datetime.fromisoformat(created).strftime("%H:%M")
            except ValueError:
                pass
        return ""

    def _scroll_to_bottom(self) -> None:
        bar = self.messages_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        # 记住这次发送的内容：万一服务端拒绝（限流、对方已不是好友），
        # 还能把文本放回输入框。
        self._last_sent = text
        self.send_requested.emit(text)

    def mark_read(self) -> None:
        """占位：历史加载时服务端已标记已读。"""


__all__ = ["PrivateChatPanel"]
