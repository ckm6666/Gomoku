"""好友私聊面板：历史消息与实时收发。"""

from __future__ import annotations

from datetime import datetime
from typing import Mapping, Optional, Sequence

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarView
from gomoku.client.chat_bubble_list import ChatBubbleList
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
            min_width=520,
            min_height=560,
        )
        self.friend_id = friend_id
        # 对方最新的头像：消息里没带头像时用它兜底。
        self._friend_avatar = avatar
        self._friend_avatar_version = max(0, int(avatar_version))
        # 自己的头像：消息里没带头像时用它兜底。
        self._self_avatar = ""
        self._self_avatar_version = 0
        self._seen_ids: set[str] = set()
        # 对方发来、已经显示在窗口里、但还没被服务端销账的消息。
        # 实时推送过来的消息服务端并不知道用户已经读过，得由客户端报上去，
        # 否则未读数会一直挂着。
        self._pending_read: list[str] = []
        self._last_sent = ""
        # 私聊浮层不是原生窗口，没有系统标题栏上的关闭按钮；面板自己
        # 必须能被关闭，因此显式接受键盘焦点，Esc 才有地方可去。
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

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

        self.messages_view = ChatBubbleList(self.card)
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

        # 关闭按钮：私聊是浮在联机页面上的子控件，没有系统标题栏，
        # 不给一个明确的关闭入口，用户就只能看着这个窗口干瞪眼。
        self.close_button = QPushButton("关闭", self.card)
        self.close_button.setObjectName("socialSecondaryButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.setAutoDefault(False)
        self.close_button.setToolTip("关闭聊天窗口（Esc）")
        self.close_button.clicked.connect(self.close)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        footer.addWidget(self.status_label, 1)
        footer.addWidget(
            self.close_button, 0, Qt.AlignmentFlag.AlignBottom
        )
        self.content.addLayout(footer)

        self.send_button.clicked.connect(self._on_send)
        self.input.returnPressed.connect(self._on_send)
        # 输入框吃掉焦点时 Esc 不会冒泡到面板，这里单独拦一道。
        self.input.installEventFilter(self)

    def set_friend(
        self,
        username: str,
        avatar: str = "",
        online: Optional[bool] = None,
        avatar_version: int = 0,
    ) -> None:
        self.name_label.setText(username)
        self._friend_avatar = avatar
        self._friend_avatar_version = max(0, int(avatar_version))
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

    def set_self_avatar(
        self,
        avatar: str = "",
        avatar_version: int = 0,
    ) -> None:
        """记住自己的头像：自己发的消息旁边也要挂头像。

        服务端通常会在消息里带回发送者的头像，但没有时用它兜底，
        免得自己那一侧的气泡全顶着默认印章。
        """
        self._self_avatar = avatar
        self._self_avatar_version = max(0, int(avatar_version))

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
        if not self._seen_ids and self.messages_view.is_empty():
            # 空状态由消息列表自己负责，这里只保证历史会重画。
            self.messages_view.clear()
        added = False
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            if self._append(
                message, self_id, scroll=False, read_server_side=True
            ):
                added = True
            # 历史接口返回的消息，服务端已经顺手标成已读——哪怕这条
            # 是实时推送先到的（已经进了待销账队列），也要一并销掉。
            message_id = str(message.get("id", ""))
            if message_id:
                self._pending_read = [
                    pending
                    for pending in self._pending_read
                    if pending != message_id
                ]
        if added:
            self._scroll_to_bottom()

    def append_message(self, message: Mapping, self_id: str) -> None:
        if self._append(message, self_id, scroll=True):
            self._scroll_to_bottom()

    def pending_read_ids(self) -> list[str]:
        """已经显示、但还没报给服务端销账的消息 id。"""
        return list(self._pending_read)

    def mark_reported(self, message_ids: Sequence[str]) -> None:
        """这些消息已经报给服务端，从待销账队列里拿掉。"""
        reported = set(message_ids)
        self._pending_read = [
            message_id
            for message_id in self._pending_read
            if message_id not in reported
        ]

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
        read_server_side: bool = False,
    ) -> bool:
        """把一条消息画进记录里；已经显示过就跳过。

        返回 True 表示这次确实新增了一条。

        ``read_server_side`` 表示服务端已经把这批消息标成已读（历史接口
        的行为），这时不用再排队等客户端销账。
        """
        message_id = str(message.get("id", ""))
        if message_id:
            if message_id in self._seen_ids:
                return False
            self._seen_ids.add(message_id)
        sender_id = str(message.get("sender_id", ""))
        is_self = sender_id == self_id
        if message_id and not is_self and not read_server_side:
            # 对方发来的消息：先记在待销账队列里，等窗口确实显示着
            # 它的时侯再报给服务端。
            self._pending_read.append(message_id)
        sender_name = (
            "我"
            if is_self
            else str(message.get("sender_name", "好友"))
        )
        content = str(message.get("content", ""))
        time_text = self._format_time(message)
        avatar, avatar_version = self._message_avatar(message, is_self)
        self.messages_view.append_message(
            sender_name=sender_name,
            content=content,
            time_text=time_text,
            is_self=is_self,
            avatar=avatar,
            user_id=sender_id,
            avatar_version=avatar_version,
        )
        if scroll:
            self._scroll_to_bottom()
        return True

    def _message_avatar(
        self, message: Mapping, is_self: bool
    ) -> tuple[str, int]:
        """消息气泡旁头像用的头像 key 与版本号。

        消息里带了发送者的头像就用它；没带时回落到好友头像或自己的
        头像，免得整排消息都顶着默认印章。
        """
        raw = message.get("sender_avatar", "")
        avatar = raw if isinstance(raw, str) else ""
        version = message.get("sender_avatar_version", 0)
        if not isinstance(version, int) or isinstance(version, bool):
            version = 0
        if avatar:
            return avatar, max(0, version)
        if is_self:
            return self._self_avatar, self._self_avatar_version
        return self._friend_avatar, self._friend_avatar_version

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
        self.messages_view.scroll_to_bottom()

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt 接口
        """输入框里按 Esc 直接关窗。"""
        if (
            obj is self.input
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.close()
            return True
        return super().eventFilter(obj, event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        """Esc 关闭聊天窗。"""
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def _on_send(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        # 记住这次发送的内容：万一服务端拒绝（限流、对方已不是好友），
        # 还能把文本放回输入框。
        self._last_sent = text
        self.send_requested.emit(text)


__all__ = ["PrivateChatPanel"]
