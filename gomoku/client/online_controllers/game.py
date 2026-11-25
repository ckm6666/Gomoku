"""对局页控制器门面。

``GameController`` 只负责持有各领域子控制器并转发调用，保持
``OnlinePanel`` 与既有测试使用的方法名不变。具体实现分别位于：

* :mod:`gomoku.client.online_controllers.game_clock` — 时钟与倒计时；
* :mod:`gomoku.client.online_controllers.game_sync` — 页面渲染同步；
* :mod:`gomoku.client.online_controllers.game_chat` — 对局内聊天；
* :mod:`gomoku.client.online_controllers.game_sandbox` — 本地沙盘；
* :mod:`gomoku.client.online_controllers.game_actions` — 悔棋/认输/再战；
* :mod:`gomoku.client.online_controllers.game_notices` — 错误横幅与终局提示。
"""

from __future__ import annotations

from typing import Any, Optional

from gomoku.client.online_controllers.common import PanelController
from gomoku.client.online_controllers.game_actions import (
    GameActionsController,
)
from gomoku.client.online_controllers.game_chat import GameChatController
from gomoku.client.online_controllers.game_clock import GameClockController
from gomoku.client.online_controllers.game_notices import (
    GameNoticesController,
)
from gomoku.client.online_controllers.game_sandbox import (
    GameSandboxController,
)
from gomoku.client.online_controllers.game_sync import GameSyncController
from gomoku.client.sessions.online import OnlineGameSession
from gomoku.shared.chat import ChatMessage
from gomoku.shared.protocol import GameSnapshot


class GameController(PanelController):
    """联机对局页的显示与操作门面。

    各子控制器只弱引用面板；门面强引用它们，形成单向的“面板 → 门面
    → 子控制器”结构，不会产生引用环。
    """

    def __init__(self, panel: Any) -> None:
        super().__init__(panel)
        self.clock = GameClockController(panel)
        self.sync = GameSyncController(panel)
        self.chat = GameChatController(panel)
        self.sandbox = GameSandboxController(panel)
        self.actions = GameActionsController(panel)
        self.notices = GameNoticesController(panel)

    # ------------------------------------------------------------------
    # 状态提示与错误横幅
    # ------------------------------------------------------------------

    def show_status(self, text: str, error: bool = False) -> None:
        self.notices.show_status(text, error)

    def set_game_error(self, text: str) -> None:
        self.notices.set_game_error(text)

    def clear_game_error(self) -> None:
        self.notices.clear_game_error()

    def clear_stale_game_error(self) -> None:
        self.notices.clear_stale_game_error()

    def on_online_error(self, message: str) -> None:
        self.notices.on_online_error(message)

    # ------------------------------------------------------------------
    # 对局内聊天
    # ------------------------------------------------------------------

    def log_game_notice(self, text: str) -> None:
        self.chat.log_game_notice(text)

    def on_chat_received(self, message: ChatMessage) -> None:
        self.chat.on_chat_received(message)

    def on_emoji_picked(self, emoji: str) -> None:
        self.chat.on_emoji_picked(emoji)

    def on_chat_failed(self, reason: str) -> None:
        self.chat.on_chat_failed(reason)

    def on_spectator_chat_received(self, message: ChatMessage) -> None:
        self.chat.on_spectator_chat_received(message)

    def on_spectator_chat_failed(self, reason: str) -> None:
        self.chat.on_spectator_chat_failed(reason)

    def apply_spectator_chat_mode(self, available: bool) -> None:
        self.chat.apply_spectator_chat_mode(available)

    def flash_chat_hint(self, text: str) -> None:
        self.chat.flash_chat_hint(text)

    def restore_chat_hint(self) -> None:
        self.chat.restore_chat_hint()

    def on_chat_log_toggled(self, checked: bool) -> None:
        self.chat.on_chat_log_toggled(checked)

    def apply_chat_log_visibility(self, visible: bool) -> None:
        self.chat.apply_chat_log_visibility(visible)

    def load_chat_log_visible(self) -> bool:
        return self.chat.load_chat_log_visible()

    def save_chat_log_visible(self, visible: bool) -> None:
        self.chat.save_chat_log_visible(visible)

    def on_preset_comment_picked(self, text: str) -> None:
        self.chat.on_preset_comment_picked(text)

    def on_send_comment(self) -> None:
        self.chat.on_send_comment()

    def refresh_chat_controls(self) -> None:
        self.chat.refresh_chat_controls()

    def clear_chat_area(self) -> None:
        self.chat.clear_chat_area()

    def set_chat_visibility(self, visible: bool) -> None:
        self.chat.set_chat_visibility(visible)

    # ------------------------------------------------------------------
    # 落子、沙盘、悔棋、认输与再战
    # ------------------------------------------------------------------

    def on_board_clicked(self, row: int, col: int) -> None:
        self.actions.on_board_clicked(row, col)

    def on_sandbox_toggled(self) -> None:
        self.sandbox.on_sandbox_toggled()

    def open_sandbox(self) -> bool:
        return self.sandbox.open_sandbox()

    def close_sandbox(self) -> None:
        self.sandbox.close_sandbox()

    def hide_sandbox(self) -> None:
        self.sandbox.hide_sandbox()

    def sync_sandbox_button(
        self, session: Optional[OnlineGameSession]
    ) -> None:
        self.sandbox.sync_sandbox_button(session)

    def on_undo_clicked(self) -> None:
        self.actions.on_undo_clicked()

    def on_undo_declined(self) -> None:
        self.actions.on_undo_declined()

    def on_undo_request_received(self, name: str) -> None:
        self.actions.on_undo_request_received(name)

    def on_undo_status_changed(self, state: str, reason: str) -> None:
        self.actions.on_undo_status_changed(state, reason)

    def show_undo_notice(self, text: str) -> None:
        self.actions.show_undo_notice(text)

    def clear_undo_notice(self) -> None:
        self.actions.clear_undo_notice()

    def set_undo_text(self, text: str) -> None:
        self.actions.set_undo_text(text)

    def refresh_undo_controls(self) -> None:
        self.actions.refresh_undo_controls()

    def on_resign(self) -> None:
        self.actions.on_resign()

    def on_rematch(self) -> None:
        self.actions.on_rematch()

    # ------------------------------------------------------------------
    # 服务器事件与终局提示
    # ------------------------------------------------------------------

    def on_online_state_changed(self) -> None:
        self.sync.on_online_state_changed()

    def on_online_stone_placed(self) -> None:
        self.notices.on_online_stone_placed()

    def on_online_game_over(self) -> None:
        self.notices.on_online_game_over()

    def show_online_victory_notice(self) -> None:
        self.notices.show_online_victory_notice()

    def show_online_timeout_notice(self) -> None:
        self.notices.show_online_timeout_notice()

    def play_online_result_sound(self) -> None:
        self.notices.play_online_result_sound()

    def maybe_play_online_start(self, snapshot: GameSnapshot) -> None:
        self.notices.maybe_play_online_start(snapshot)

    # ------------------------------------------------------------------
    # 页面渲染同步
    # ------------------------------------------------------------------

    def sync_online_ui(self) -> None:
        self.sync.sync_online_ui()

    def sync_game_page(self) -> None:
        self.sync.sync_game_page()

    def sync_spectator_page(self, snapshot: GameSnapshot) -> None:
        self.sync.sync_spectator_page(snapshot)

    def update_achievement_notice(
        self, snapshot: Optional[GameSnapshot]
    ) -> None:
        self.sync.update_achievement_notice(snapshot)

    def set_game_pill(self, text: str, result: bool) -> None:
        self.sync.set_game_pill(text, result)

    def set_action_visibility(
        self,
        resign: bool,
        rematch: bool,
        leave: bool,
        *,
        undo: bool,
        reject_undo: bool,
        report: bool = False,
    ) -> None:
        self.sync.set_action_visibility(
            resign,
            rematch,
            leave,
            undo=undo,
            reject_undo=reject_undo,
            report=report,
        )

    # ------------------------------------------------------------------
    # 时钟与倒计时
    # ------------------------------------------------------------------

    def apply_snapshot_deadline(self, snapshot: GameSnapshot) -> None:
        self.clock.apply_snapshot_deadline(snapshot)

    def update_clock_label(self) -> None:
        self.clock.update_clock_label()

    def hide_countdown(self) -> None:
        self.clock.hide_countdown()

    def set_countdown_urgent(self, urgent: bool) -> None:
        self.clock.set_countdown_urgent(urgent)

    def show_countdown(self, seconds: int, snapshot: GameSnapshot) -> None:
        self.clock.show_countdown(seconds, snapshot)

    def maybe_beep_countdown(
        self, seconds: int, snapshot: GameSnapshot
    ) -> None:
        self.clock.maybe_beep_countdown(seconds, snapshot)

    def countdown_is_mine(self, snapshot: GameSnapshot) -> bool:
        return self.clock.countdown_is_mine(snapshot)

    def fischer_clock_text(self, snapshot: GameSnapshot) -> str:
        return self.clock.fischer_clock_text(snapshot)
