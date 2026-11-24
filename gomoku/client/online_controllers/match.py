"""建房、排位匹配与房间关闭流程控制器。"""

from __future__ import annotations

import logging
from PySide6.QtCore import QTimer

from gomoku.client.online_controllers.common import PanelController
from gomoku.shared.rules import RuleMode
from gomoku.shared.time_control import TimeControl

logger = logging.getLogger(__name__)


class MatchController(PanelController):
    """创建房间、快速匹配以及房间生命周期。"""

    def on_create_room(self) -> None:
        if self.panel._session is None:
            return
        if self.panel._room_action_pending:
            return
        self.panel._room_action_pending = True
        self.panel._set_lobby_status("正在创建自建房…")
        self.panel._refresh_lobby_controls()
        time_control = self.panel.turn_seconds_combo.currentData()
        if not isinstance(time_control, TimeControl):
            time_control = None
        rule_mode = self.panel.rule_mode_combo.currentData()
        if not isinstance(rule_mode, RuleMode):
            # 下拉框数据异常时退回自由规则，不能把非法值发给服务器。
            rule_mode = RuleMode.FREESTYLE
        logger.info(
            "请求创建自建房：允许观战=%s，时限=%s，规则=%s",
            self.panel.allow_spectators_checkbox.isChecked(),
            time_control.label() if time_control is not None else "服务器默认",
            rule_mode.value,
        )
        if not self.panel._session.create_room(
            self.panel.allow_spectators_checkbox.isChecked(),
            time_control=time_control,
            rule_mode=rule_mode,
        ):
            self.panel._room_action_pending = False
            self.panel._set_lobby_status("无法创建房间，请检查连接", error=True)
            self.panel._refresh_lobby_controls()

    def on_quick_match(self) -> None:
        if self.panel._session is None or not self.panel._ws_connected:
            return
        if self.panel._room_action_pending:
            return
        if self.panel._session.queued:
            self.panel._room_action_pending = True
            self.panel._set_lobby_status("")
            self.panel._set_match_status("正在取消匹配…")
            self.panel._refresh_lobby_controls()
            if not self.panel._session.cancel_match():
                self.panel._room_action_pending = False
                self.panel._set_match_status("无法取消匹配，请检查连接", error=True)
                self.panel._refresh_lobby_controls()
            return

        self.panel._room_action_pending = True
        self.panel._auto_requeue = False
        self.panel._set_lobby_status("")
        self.panel._set_match_status("正在进入排位匹配队列…")
        self.panel._refresh_lobby_controls()
        # 排位固定每手 30 秒，不需要向服务器提交时限设置。
        if not self.panel._session.quick_match():
            self.panel._room_action_pending = False
            self.panel._set_match_status("无法开始匹配，请检查连接", error=True)
            self.panel._refresh_lobby_controls()

    def on_queue_status_changed(self, queued: bool, size: int) -> None:
        if self.panel.sender() is not self.panel._session:
            return
        self.panel._room_action_pending = False
        if queued:
            self.panel._set_lobby_status("")
            self.panel._set_match_status(
                f"正在匹配排位对手… 当前队列 {size} 人，请稍候"
            )
        else:
            if self.panel._session is not None and self.panel._session.snapshot is None:
                self.panel._set_match_status("已退出排位匹配")
        self.panel._refresh_lobby_controls()

    def on_match_found(self, _game_id: str, opponent_name: str) -> None:
        if self.panel.sender() is not self.panel._session:
            return
        self.panel._room_action_pending = False
        # 新的排位对局开始，旧房间的聊天记录不再保留。
        self.panel._clear_chat_area()
        self.panel._set_lobby_status("")
        self.panel._set_match_status(
            f"已匹配到 {opponent_name}，正在载入棋局…"
        )
        self.panel._refresh_lobby_controls()

    def on_room_created(self, code: str) -> None:
        self.panel._room_action_pending = False
        # 新房间 = 新的对话上下文，旧房间的聊天记录不再保留。
        self.panel._clear_chat_area()
        self.panel._set_match_status("")
        self.panel._show_game_page()

    def on_room_closed(self, reason: str) -> None:
        self.panel._room_action_pending = False
        self.panel._audio_game_id = None
        self.panel.clock_label.setText("—")
        self.panel._deadline_monotonic = None
        self.panel._countdown_turn_key = None
        self.panel._countdown_beep_second = None
        self.panel._hide_countdown()
        self.panel.game_rating_label.clear()
        self.panel.game_rating_label.setVisible(False)
        self.panel._set_match_status("")
        # 房间关闭后清空对话日志与在飞弹幕。
        self.panel._clear_chat_area()
        self.panel._leave_game_page(reason)
        if self.panel._auto_requeue:
            self.panel._auto_requeue = False
            QTimer.singleShot(0, self.panel._on_quick_match)

    def on_leave_room(self) -> None:
        if self.panel._session is not None:
            self.panel._auto_requeue = False
            self.panel._session.leave_room()

    def on_invite_friend_clicked(self) -> None:
        self.panel._open_friends_panel(invite_mode=True)

    def invite_friend(self, user_id: str, username: str) -> None:
        if self.panel._session is None or not user_id:
            return
        if not self.panel._session.waiting_for_opponent:
            self.panel._show_status("请先创建自建房", error=True)
            return
        if self.panel._session.invite_to_room(user_id):
            self.panel._show_status(f"已向 {username} 发送对局邀请")
