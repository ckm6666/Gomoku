"""联机对局的聊天：表情/评论收发、对话日志与聊天控件状态。

观战者不参与对局内聊天，因此收到广播、渲染日志与控件可用性都要
先判断旁观身份。方法由 ``GameController`` 门面转发，行为不变。
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QTimer

from gomoku.client.online_controllers.common import (
    PanelController,
    host_module,
)
from gomoku.client.online_pages.common import CHAT_INPUT_HINT
from gomoku.shared.chat import CHAT_KIND_EMOJI, CHAT_KIND_TEXT, ChatMessage


class GameChatController(PanelController):
    """对局页聊天的发送、接收、日志与输入控件管理。"""

    def __init__(self, panel: Any) -> None:
        super().__init__(panel)
        # 是否处于"观友模式"：复用同一套聊天控件，但语义变成观战席
        # 之间的悄悄话——只来自观众，棋手看不到，也不飞弹幕挡住棋盘。
        self._spectator_mode = False
        # 进入观友模式前的日志展开状态，离开观战席后还原。
        self._log_expanded_before_spectator: Optional[bool] = None

    def log_game_notice(self, text: str) -> None:
        """把一条对局内的提示写进对话日志，方便回看失败原因。"""
        if not text:
            return
        if self.panel._session is not None and self.panel._session.is_spectator:
            # 观战者看不到对局内的聊天，不必记。
            return
        try:
            self.panel.chat_log.append_notice(f"（{text}）")
        except RuntimeError:  # pragma: no cover - 面板已销毁的竞态
            return

    def on_chat_received(self, message: ChatMessage) -> None:
        """收到服务端广播的表情/评论：飞弹幕并写入对话日志。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        if self.panel._session is not None and self.panel._session.is_spectator:
            # 观战者看不到对局内的聊天（弹幕与对话日志都不显示）。
            return
        is_self = bool(
            self.panel._session is not None
            and message.sender_id == self.panel._session.user_id
        )
        if message.kind == CHAT_KIND_EMOJI:
            self.panel.danmaku_overlay.push_emoji(message.content, is_self)
        elif message.kind == CHAT_KIND_TEXT:
            self.panel.danmaku_overlay.push_text(message.content, is_self)
        self.panel.chat_log.append_message(message, is_self)

    def on_emoji_picked(self, emoji: str) -> None:
        """发送用户选中的预设表情。

        观战者发到同观频道，其他人对局玩家聊天。
        """
        if self.panel._session is not None:
            self._send_from_widgets(CHAT_KIND_EMOJI, emoji)

    # ------------------------------------------------------------------
    # 同观频道：观战者之间的小角落
    # ------------------------------------------------------------------
    def apply_spectator_chat_mode(self, available: bool) -> None:
        """切换聊天控件到"观友模式"。

        观友模式复用同一套控件，但语义完全不同：消息只来自观战席，
        棋手既收不到也看不到。这里同时把留言区默认展开——
        默认收起的日志会让观友消息彻底看不见。
        """
        available = bool(available)
        if available and not self._spectator_mode:
            # 记下玩家原本的展开状态，离开观战席后还原。
            self._log_expanded_before_spectator = self.panel._chat_log_expanded
        self._spectator_mode = available
        if available:
            self.panel._apply_chat_log_visibility(True)
            self.panel._set_chat_visibility(True)
        else:
            self.panel._set_chat_visibility(False)
            if self._log_expanded_before_spectator is not None:
                self.panel._apply_chat_log_visibility(
                    self._log_expanded_before_spectator
                )
                self._log_expanded_before_spectator = None

    def on_spectator_chat_received(self, message: ChatMessage) -> None:
        """收到同观频道的一条消息：只写进观友留言区。

        刻意不飞弹幕：观战者正在看棋盘，浮字会挡住他要看的棋局。
        """
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        session = self.panel._session
        if session is None or not session.is_spectator:
            return
        is_self = message.sender_id == session.user_id
        self.panel.chat_log.append_message(message, is_self)

    def on_spectator_chat_failed(self, reason: str) -> None:
        """观战席发言失败：提示在观友留言区，不打断观战。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        text = reason or "发言未发送"
        self.panel.chat_log.append_notice(f"（{text}）")
        if not self.panel._chat_log_expanded:
            self.panel._flash_chat_hint(text)

    def _send_from_widgets(self, kind: str, content: str) -> bool:
        """按当前身份把一条发言送到对应对的通道。

        观战者走同观频道，其他人对局玩家聊天。
        """
        session = self.panel._session
        if session is None:
            return False
        if session.is_spectator:
            return session.send_spectator_chat(kind, content)
        return session.send_chat(kind, content)

    def on_chat_failed(self, reason: str) -> None:
        """发言失败：提示在聊天区域，不打断对局。

        提示同时出现在两处：对话日志里留一条记录，日志收起时则改用
        输入框的占位提示——对话日志默认是收起的，不能让失败提示跟着
        一起消失。两种提示都不会在对局页上多占一行垂直空间，避免把
        棋盘挤小、裁掉棋盘下沿。
        """
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        text = reason or "发言未发送"
        self.panel.chat_log.append_notice(f"（{text}）")
        if not self.panel._chat_log_expanded:
            self.panel._flash_chat_hint(text)

    def flash_chat_hint(self, text: str) -> None:
        """把提示临时显示在输入框的占位文字上。"""
        self.panel.chat_input.setPlaceholderText(text)
        QTimer.singleShot(3000, self.panel._restore_chat_hint)

    def restore_chat_hint(self) -> None:
        """恢复输入框的常规占位提示。"""
        if not self.panel._closing:
            self.panel.chat_input.setPlaceholderText(CHAT_INPUT_HINT)

    def on_chat_log_toggled(self, checked: bool) -> None:
        """对话日志的展开/收起。"""
        self.panel._apply_chat_log_visibility(checked)
        self.panel._save_chat_log_visible(checked)

    def apply_chat_log_visibility(self, visible: bool) -> None:
        """显示或隐藏对话日志，并同步按钮状态。"""
        expanded = bool(visible)
        # 记录逻辑状态：chat_log.isVisible() 还会受父级页面是否显示
        # 的影响，对局页不在前台时它恒为 False，不能拿来判断"用户
        # 是否展开了日志"。
        self.panel._chat_log_expanded = expanded
        self.panel.chat_log.setVisible(expanded)
        button = self.panel.chat_log_toggle_button
        # 程序化设置勾选状态时不要触发 toggled，否则会绕一圈回来。
        button.blockSignals(True)
        button.setChecked(expanded)
        button.blockSignals(False)
        button.setText("隐藏日志" if expanded else "对话日志")
        button.setToolTip(
            "收起对话日志，让棋盘占满整行"
            if expanded
            else "展开对话日志，查看双方的发言"
        )
        if self._spectator_mode:
            # 观友模式下这块面板不是"对局发言记录"，而是观战席上的
            # 悄悄话，语义不同，措辞也要跟着变。
            button.setText("隐藏观友" if expanded else "观友留言")
            button.setToolTip(
                "收起观友留言"
                if expanded
                else "展开观友留言，看观战席上的悄悄话"
            )

    def load_chat_log_visible(self) -> bool:
        """读取上次的对话日志展开状态；没有记录时默认收起。"""
        value = self.panel._settings.value(
            host_module().SETTINGS_CHAT_LOG_VISIBLE, False
        )
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes")
        return False

    def save_chat_log_visible(self, visible: bool) -> None:
        """记住对话日志的展开状态。"""
        self.panel._settings.setValue(
            host_module().SETTINGS_CHAT_LOG_VISIBLE, bool(visible)
        )
        self.panel._settings.sync()

    def on_preset_comment_picked(self, text: str) -> None:
        """发送用户选中的预设评论。"""
        if self.panel._session is not None:
            self._send_from_widgets(CHAT_KIND_TEXT, text)

    def on_send_comment(self) -> None:
        """发送手动输入的评论；发送成功后清空输入框。"""
        if self.panel._session is None:
            return
        text = self.panel.chat_input.text().strip()
        if not text:
            return
        if self._send_from_widgets(CHAT_KIND_TEXT, text):
            self.panel.chat_input.clear()

    def refresh_chat_controls(self) -> None:
        """按当前会话状态启用或禁用聊天输入控件。

        这里用的是 chat_available 而不是 can_send_chat：后者还包含
        发言冷却，如果拿它去禁用输入框，Qt 会把焦点从被禁用的控件
        上摘走，用户每发一条消息都得重新点回输入框。
        """
        chat_input = getattr(self.panel, "chat_input", None)
        if chat_input is None:
            return
        session = self.panel._session
        if session is not None and session.is_spectator:
            # 观战者只发同观频道：段位赛不开放，自建房与休闲局开放。
            enabled = (
                self.panel._ws_connected
                and session.spectator_chat_available
            )
        else:
            enabled = (
                session is not None
                and self.panel._ws_connected
                and session.chat_available
                # 观战者不发言。
                and not session.is_spectator
            )
        chat_input.setEnabled(enabled)
        self.panel.chat_send_button.setEnabled(enabled)
        self.panel.emoji_button.set_emoji_enabled(enabled)
        self.panel.preset_comment_button.setEnabled(enabled)

    def clear_chat_area(self) -> None:
        """清空对话日志与在飞的弹幕。"""
        self.panel.chat_log.clear_log()
        self.panel.danmaku_overlay.clear()

    def set_chat_visibility(self, visible: bool) -> None:
        """整体显示或隐藏聊天控件。

        旁观者不参与对局内的聊天，也看不到双方的发言，因此聊天输入栏与
        对话日志要一起藏起来。
        """
        for widget in (
            self.panel.emoji_button,
            self.panel.preset_comment_button,
            self.panel.chat_input,
            self.panel.chat_send_button,
            self.panel.chat_log_toggle_button,
        ):
            widget.setVisible(visible)
        if visible:
            self.panel._apply_chat_log_visibility(self.panel._chat_log_expanded)
        else:
            self.panel.chat_log.setVisible(False)
