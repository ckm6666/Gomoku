"""联机对局的表情 / 评论相关控件。

包含三部分：

* :class:`ChatLogPanel` —— 只读的对话日志面板，只展示双方发过的评论与表情，
  面板内没有任何输入框或按钮，因此无法在此处发言；
* :class:`EmojiPickerButton` —— 点击后弹出表情网格的按钮；
* :class:`PresetCommentButton` —— 点击后弹出预设评论的按钮。
"""

from __future__ import annotations

import html
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QLabel,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from gomoku.client.danmaku import (
    _resolve_emoji_font,
    resolve_emoji_font_family,
)
from gomoku.client.font_settings import current_font_scale
from gomoku.shared.chat import (
    CHAT_KIND_EMOJI,
    COMMENT_PRESETS,
    EMOJI_PRESETS,
    ChatMessage,
)

#: 对话日志最多保留的条数，超出后丢弃最早的记录。
MAX_LOG_ENTRIES = 200


class ChatLogPanel(QFrame):
    """只读对话日志面板：显示自己与对手发送的评论和表情，不能在此发送。"""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatLogPanel")
        self.setFrameShape(QFrame.Shape.NoFrame)

        # 日志条目与其纯文本一一对应，便于测试断言。
        self._entries: list[str] = []
        self._labels: list[QLabel] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self._title = QLabel("对话日志", self)
        self._title.setObjectName("chatLogTitle")
        layout.addWidget(self._title)

        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("chatLogScroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        layout.addWidget(self._scroll, 1)

        self._body = QWidget(self._scroll)
        self._body.setObjectName("chatLogBody")
        self._body_layout = QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(2, 2, 2, 2)
        self._body_layout.setSpacing(4)
        self._body_layout.addStretch(1)
        self._scroll.setWidget(self._body)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def append_message(self, message: ChatMessage, is_self: bool) -> None:
        """追加一条日志；自己的消息右对齐暖色，对手的左对齐冷色。"""
        name = "你" if is_self else (message.sender_name or "对手")
        plain = f"{name}：{message.content}"

        label = QLabel(self._body)
        label.setObjectName("chatLogEntry")
        label.setProperty("side", "self" if is_self else "opponent")
        label.setWordWrap(True)
        label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        # 只允许选中复制，不允许编辑。
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        if message.kind == CHAT_KIND_EMOJI:
            self._apply_emoji_text(label, plain, message.content)
        else:
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setText(plain)

        if is_self:
            label.setAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
            )
        else:
            label.setAlignment(
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
            )

        self._body_layout.insertWidget(
            self._body_layout.count() - 1, label
        )
        self._entries.append(plain)
        self._labels.append(label)

        # 动态属性变更后需要重新抛光样式。
        label.style().unpolish(label)
        label.style().polish(label)

        self._trim_entries()
        self._scroll_to_bottom()

    def append_notice(self, text: str) -> None:
        """追加一条系统提示（例如"发言太快"）。

        聊天相关的提示放在日志面板里，而不是在棋盘上方单独加一行：
        对局页的垂直空间本来就紧张，多一行控件会把棋盘挤扁、裁掉
        棋盘下沿。
        """
        label = QLabel(self._body)
        label.setObjectName("chatLogNotice")
        label.setWordWrap(True)
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        label.setText(text or "")
        label.setAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignTop
        )
        self._body_layout.insertWidget(
            self._body_layout.count() - 1, label
        )
        # 提示也要记进 _entries：_labels 与 _entries 一一对应，
        # _trim_entries 才会裁掉正确的行。
        self._entries.append(text or "")
        self._labels.append(label)
        self._trim_entries()
        self._scroll_to_bottom()

    def clear_log(self) -> None:
        """清空全部日志。"""
        for label in self._labels:
            self._body_layout.removeWidget(label)
            label.setParent(None)
            label.deleteLater()
        self._labels.clear()
        self._entries.clear()
        self._scroll_to_bottom()

    def entry_texts(self) -> list[str]:
        """按顺序返回每条日志的纯文本（供测试断言）。"""
        return list(self._entries)

    def entry_count(self) -> int:
        """当前保留的日志条数。"""
        return len(self._entries)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------
    def _apply_emoji_text(
        self, label: QLabel, plain: str, emoji: str
    ) -> None:
        """用富文本渲染表情，让表情字形使用解析出的彩色字体。"""
        prefix = plain[: len(plain) - len(emoji)] if emoji else plain
        family = resolve_emoji_font_family()
        emoji_size = max(8, int(round(16 * current_font_scale())))
        if family is None:
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setText(plain)
            label.setFont(_resolve_emoji_font(emoji_size))
            return

        style = (
            f"font-family:'{family}'; font-size:{emoji_size}px;"
        )
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setText(
            f"{html.escape(prefix)}"
            f'<span style="{style}">{html.escape(emoji)}</span>'
        )

    def _trim_entries(self) -> None:
        """超过上限时丢弃最早的日志。"""
        while len(self._entries) > MAX_LOG_ENTRIES:
            label = self._labels.pop(0)
            self._entries.pop(0)
            self._body_layout.removeWidget(label)
            label.setParent(None)
            label.deleteLater()

    def _scroll_to_bottom(self) -> None:
        """滚动到最新一条日志。"""
        bar = self._scroll.verticalScrollBar()
        bar.setValue(bar.maximum())


class EmojiPickerButton(QPushButton):
    """表情按钮：点击后弹出表情网格菜单（Clash Royale 风格）。"""

    #: 用户选中一个表情时发出，参数为表情字符。
    emoji_picked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("emojiPickerButton")
        self.setText("表情")

        self._menu = QMenu(self)
        # 命名后才会套用 APP_STYLE 里的菜单样式，否则是平台默认的
        # 深色菜单，和水墨宣纸的整体风格冲突。
        self._menu.setObjectName("chatMenu")
        self._grid_host = QWidget(self._menu)
        self._grid = QGridLayout(self._grid_host)
        self._grid.setContentsMargins(6, 6, 6, 6)
        self._grid.setSpacing(4)

        emoji_font = _resolve_emoji_font(19)
        for index, emoji in enumerate(EMOJI_PRESETS):
            button = QPushButton(emoji, self._grid_host)
            # 必须带上自己的 objectName：全局 QPushButton 规则给按钮留了
            # 9px 17px 的内边距，在 36x36 的小方块里内容宽度会被挤成 0，
            # 表情就整个看不见了。详见 styles.py 的 #emojiGridButton。
            button.setObjectName("emojiGridButton")
            button.setFixedSize(36, 36)
            button.setFont(emoji_font)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip("发送这个表情")
            button.clicked.connect(
                lambda _checked=False, item=emoji: self.emoji_picked.emit(item)
            )
            self._grid.addWidget(button, index // 4, index % 4)

        # PySide6 没有 QMenu.addWidget，必须使用 QWidgetAction。
        action = QWidgetAction(self._menu)
        action.setDefaultWidget(self._grid_host)
        self._menu.addAction(action)

        self.setMenu(self._menu)

    def set_emoji_enabled(self, enabled: bool) -> None:
        """启用或禁用表情发送（不可用时整颗按钮变灰）。"""
        self.setEnabled(bool(enabled))


class PresetCommentButton(QPushButton):
    """快捷评论按钮：点击后弹出预设评论菜单。"""

    #: 用户选中一条预设评论时发出，参数为评论文本。
    comment_picked = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("presetCommentButton")
        self.setText("快捷评论")

        self._menu = QMenu(self)
        self._menu.setObjectName("chatMenu")
        for text in COMMENT_PRESETS:
            action = QAction(text, self._menu)
            action.triggered.connect(
                lambda _checked=False, item=text: (
                    self.comment_picked.emit(item)
                )
            )
            self._menu.addAction(action)

        self.setMenu(self._menu)


__all__ = [
    "MAX_LOG_ENTRIES",
    "ChatLogPanel",
    "EmojiPickerButton",
    "PresetCommentButton",
]
