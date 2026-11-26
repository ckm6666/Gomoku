"""联机对局页的渲染同步：玩家视角与观战视角。

服务器权威快照驱动棋盘、身份卡、状态胶囊、积分与成就显示，并据此
决定操作按钮的可见性。聊天可见性与沙盘基线等跨模块动作仍通过
``OnlinePanel`` 上的门面方法转发，保持既有行为。
"""

from __future__ import annotations

from typing import Optional

from gomoku.client.achievement_widgets import achievement_names
from gomoku.client.online_controllers.common import PanelController
from gomoku.client.sessions.online_state import OnlineSessionPhase
from gomoku.shared.protocol import GameSnapshot
from gomoku.shared.rules import Player, RuleMode

#: 连珠规则的界面标签文案；自由规则不显示标签。
RENJU_RULE_TEXT = "规则：连珠（黑棋禁手）"


class GameSyncController(PanelController):
    """对局页与观战页的快照驱动渲染。"""

    def on_online_state_changed(self) -> None:
        """服务器推送状态变化：刷新页面。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        if (
            self.panel._session.snapshot is not None
            or self.panel._session.waiting_for_opponent
        ):
            self.panel._room_action_pending = False
        self.panel._sync_online_ui()

    def sync_online_ui(self) -> None:
        """按会话阶段切换对局页/大厅的显示。"""
        self.panel._refresh_chat_controls()
        if self.panel._session is None:
            return
        session = self.panel._session
        if not session.is_authenticated:
            return
        if session.phase in {
            OnlineSessionPhase.WAITING,
            OnlineSessionPhase.PLAYING,
            OnlineSessionPhase.SPECTATING,
        }:
            self.panel._show_game_page()
            self.panel._sync_game_page()
        elif self.panel.stack.currentWidget() is self.panel.game_page:
            self.panel._leave_game_page()
        else:
            self.panel._update_rating_summary()
            self.panel._refresh_lobby_controls()

    def sync_game_page(self) -> None:
        """把最新快照渲染到对局页（玩家视角）。"""
        self.panel._refresh_chat_controls()
        session = self.panel._session
        # 没有会话时按钮同样要禁用。
        self.panel._sync_sandbox_button(session)
        if session is None:
            self._update_rule_label(RuleMode.FREESTYLE)
            return
        snapshot = session.snapshot
        # 规则以权威快照为准；等待房间还没有快照时用 room_created 下发的值。
        self._update_rule_label(
            snapshot.rule_mode if snapshot is not None else session.rule_mode
        )
        if snapshot is not None and (
            session.is_spectator or snapshot.your_color is None
        ):
            # 观战者不是对局双方：走独立的只读分支，不能按玩家视角渲染。
            # 快照里没有执子颜色同样说明这是旁观者视角。
            self.panel._sync_spectator_page(snapshot)
            return
        # 玩家视角：聊天控件重新可见（观战时是被隐藏的），
        # 同时退出观友模式，免得沿用观战席的措辞。
        self.panel._apply_spectator_chat_mode(False)
        self.panel._set_chat_visibility(True)
        waiting = session.waiting_for_opponent
        rated = session.rated or (
            snapshot.rated if snapshot is not None else False
        )
        your_color = (
            snapshot.your_color
            if snapshot is not None
            else (session.human_player or Player.BLACK)
        )
        opponent_color = your_color.opponent
        you_name = f"你，{session.your_username}"
        opponent_name = (
            "对手，等待好友加入"
            if waiting
            else f"对手，{session.opponent_username}"
        )
        if rated:
            room_title = "排位赛，积分对局"
        elif waiting:
            room_title = "自建房，等待好友"
        else:
            room_title = "自建房，休闲对局"
        time_control = session.time_control
        if time_control is not None:
            # 把服务端实际采用的时限显示出来，选项未生效时能一眼看出。
            room_title = f"{room_title}，{time_control.label()}"
        self.panel.room_title_label.setText(room_title)
        self.panel.invite_friend_button.setVisible(waiting)
        self.panel.invite_friend_button.setEnabled(
            waiting and self.panel._ws_connected
        )
        self.panel.connection_label.setText(
            "已连接" if self.panel._ws_connected else "连接中断"
        )
        # 只清理已经过期的错误提示：落子被拒后紧接着的状态同步不能
        # 把失败原因抹掉。
        self.panel._clear_stale_game_error()

        your_rating = (
            snapshot.your_rating_before
            if snapshot is not None
            else session.your_rating
        )
        opponent_rating = (
            snapshot.opponent_rating_before
            if snapshot is not None
            else session.opponent_rating
        )

        self_id = self.panel._auth_session.user.id if self.panel._auth_session else ""
        self_avatar = (
            self.panel._auth_session.user.avatar if self.panel._auth_session else ""
        )
        self_avatar_version = (
            self.panel._auth_session.user.avatar_version
            if self.panel._auth_session
            else 0
        )
        self_achievements = (
            self.panel._auth_session.user.achievement_keys
            if self.panel._auth_session
            else ()
        )
        if snapshot is not None:
            black_id = snapshot.black_user_id
            white_id = snapshot.white_user_id
            black_avatar = snapshot.black_avatar
            white_avatar = snapshot.white_avatar
            black_avatar_version = snapshot.black_avatar_version
            white_avatar_version = snapshot.white_avatar_version
            black_achievements = snapshot.black_achievements
            white_achievements = snapshot.white_achievements
        else:
            black_id = self_id if your_color is Player.BLACK else ""
            white_id = self_id if your_color is Player.WHITE else ""
            black_avatar = (
                self_avatar if your_color is Player.BLACK else ""
            )
            white_avatar = (
                self_avatar if your_color is Player.WHITE else ""
            )
            black_avatar_version = (
                self_avatar_version
                if your_color is Player.BLACK
                else 0
            )
            white_avatar_version = (
                self_avatar_version
                if your_color is Player.WHITE
                else 0
            )
            black_achievements = (
                self_achievements if your_color is Player.BLACK else ()
            )
            white_achievements = (
                self_achievements if your_color is Player.WHITE else ()
            )
        you_id = black_id if your_color is Player.BLACK else white_id
        opponent_id = (
            white_id if your_color is Player.BLACK else black_id
        )
        you_avatar = (
            black_avatar
            if your_color is Player.BLACK
            else white_avatar
        )
        opponent_avatar = (
            white_avatar
            if your_color is Player.BLACK
            else black_avatar
        )
        you_avatar_version = (
            black_avatar_version
            if your_color is Player.BLACK
            else white_avatar_version
        )
        opponent_avatar_version = (
            white_avatar_version
            if your_color is Player.BLACK
            else black_avatar_version
        )
        you_achievements = (
            black_achievements
            if your_color is Player.BLACK
            else white_achievements
        )
        opponent_achievements = (
            white_achievements
            if your_color is Player.BLACK
            else black_achievements
        )

        if waiting:
            self.panel.you_card.set_content(
                you_name,
                your_color,
                False,
                "等待对手",
                your_rating,
                avatar=you_avatar,
                user_id=you_id,
                avatar_version=you_avatar_version,
                achievements=you_achievements,
            )
            self.panel.opponent_card.set_content(
                opponent_name,
                opponent_color,
                False,
                "等待对手加入",
                opponent_rating,
                avatar="",
                user_id="",
            )
            self.panel._set_game_pill("等待对手", result=False)
            self.panel.clock_label.setText("等待好友加入…")
            self.panel._hide_countdown()
            self.panel.move_label.setText("第 0 手")
            self.panel.game_rating_label.clear()
            self.panel.game_rating_label.setVisible(False)
            self.panel._update_achievement_notice(None)
            self.panel._clear_undo_notice()
            self.panel.board_widget.set_view(session.view)
            self.panel.board_widget.set_interaction_enabled(False)
            self.panel._set_action_visibility(
                resign=False,
                rematch=False,
                leave=True,
                undo=False,
                reject_undo=False,
            )
            self.panel.rematch_button.setEnabled(False)
            self.panel.leave_button.setText("离开房间")
            self.panel.leave_button.setEnabled(self.panel._ws_connected)
            return

        self.panel.you_card.set_content(
            you_name,
            your_color,
            False,
            "等待",
            your_rating,
            avatar=you_avatar,
            user_id=you_id,
            avatar_version=you_avatar_version,
            achievements=you_achievements,
        )
        self.panel.opponent_card.set_content(
            opponent_name,
            opponent_color,
            False,
            "等待" if session.opponent_online else "已离线",
            opponent_rating,
            avatar=opponent_avatar,
            user_id=opponent_id,
            avatar_version=opponent_avatar_version,
            achievements=opponent_achievements,
        )
        if snapshot is None:
            self.panel.clock_label.setText("—")
            self.panel._hide_countdown()
            self.panel.move_label.setText("第 0 手")
            self.panel.game_rating_label.clear()
            self.panel.game_rating_label.setVisible(False)
            self.panel._update_achievement_notice(None)
            self.panel.board_widget.set_interaction_enabled(False)
            return

        self.panel._last_snapshot = snapshot
        self.panel._maybe_play_online_start(snapshot)
        # 依据服务器时间计算倒计时，不在客户端自行判定超时胜负。
        self.panel._apply_snapshot_deadline(snapshot)

        current = snapshot.current_player
        over = snapshot.is_game_over
        winner = snapshot.winner
        if over:
            self.panel._clear_undo_notice()
            if snapshot.your_rating_after is not None:
                your_rating = snapshot.your_rating_after
            if snapshot.opponent_rating_after is not None:
                opponent_rating = snapshot.opponent_rating_after
            self.panel.you_card.set_content(
                you_name,
                your_color,
                winner is your_color,
                "获胜"
                if winner is your_color
                else ("平局" if winner is None else "本局结束"),
                your_rating,
                avatar=you_avatar,
                user_id=you_id,
                avatar_version=you_avatar_version,
                achievements=you_achievements,
            )
            self.panel.opponent_card.set_content(
                opponent_name,
                opponent_color,
                winner is opponent_color,
                "获胜"
                if winner is opponent_color
                else ("平局" if winner is None else "本局结束"),
                opponent_rating,
                avatar=opponent_avatar,
                user_id=opponent_id,
                avatar_version=opponent_avatar_version,
                achievements=opponent_achievements,
            )
            if winner is None:
                self.panel._set_game_pill("本局平局", result=True)
            elif winner is your_color:
                self.panel._set_game_pill("你获胜", result=True)
            else:
                self.panel._set_game_pill("对手获胜", result=True)
            self.panel.clock_label.setText("对局结束")

            if rated and snapshot.your_rating_after is not None:
                delta = snapshot.rating_delta
                before = snapshot.your_rating_before
                if delta is not None and before is not None:
                    sign = "+" if delta > 0 else ""
                    self.panel.game_rating_label.setText(
                        "排位积分："
                        f"{before} → {snapshot.your_rating_after}"
                        f"（{sign}{delta}）"
                    )
                elif delta is not None:
                    sign = "+" if delta > 0 else ""
                    self.panel.game_rating_label.setText(
                        f"当前排位积分 {snapshot.your_rating_after}"
                        f"（{sign}{delta}）"
                    )
                else:
                    self.panel.game_rating_label.setText(
                        f"当前排位积分 {snapshot.your_rating_after}"
                    )
                self.panel.game_rating_label.setVisible(True)
            else:
                self.panel.game_rating_label.clear()
                self.panel.game_rating_label.setVisible(False)
            self.panel._update_achievement_notice(snapshot)
        else:
            self.panel.game_rating_label.clear()
            self.panel.game_rating_label.setVisible(False)
            self.panel._update_achievement_notice(None)
            self.panel.you_card.set_content(
                you_name,
                your_color,
                current is your_color,
                "等待服务器确认"
                if session.busy and current is your_color
                else ("落子中" if current is your_color else "等待"),
                your_rating,
                you_avatar,
                you_id,
                you_avatar_version,
                you_achievements,
            )
            self.panel.opponent_card.set_content(
                opponent_name,
                opponent_color,
                current is opponent_color,
                "已离线"
                if not session.opponent_online
                else ("落子中" if current is opponent_color else "等待"),
                opponent_rating,
                opponent_avatar,
                opponent_id,
                opponent_avatar_version,
                opponent_achievements,
            )
            if not self.panel._ws_connected:
                self.panel._set_game_pill("连接中断", result=False)
            elif not session.opponent_online:
                self.panel._set_game_pill("对手离线", result=False)
            elif session.busy:
                self.panel._set_game_pill("等待服务器", result=False)
            elif current is your_color:
                self.panel._set_game_pill("你的回合", result=False)
            else:
                self.panel._set_game_pill("对手回合", result=False)

        self.panel.move_label.setText(f"第 {snapshot.move_no} 手")
        if not self.panel._ws_connected:
            self.panel._set_game_error("网络连接中断，正在尝试恢复")
        self.panel.board_widget.set_view(session.view)
        if self.panel._sandbox_open:
            # 真实对局推进了，把新局面同步给沙盘基线。
            self.panel.sandbox_panel.sync_base(session.view)
        self.panel.board_widget.set_interaction_enabled(
            self.panel._ws_connected
            and session.accepts_human_input
            and not self.panel._sandbox_open
        )
        self.panel.board_widget.update()
        self.panel._update_clock_label()

        undo_pending = session.undo_pending
        undo_mine = session.undo_requested_by_you
        reject_undo = not over and undo_pending and not undo_mine
        self.panel._set_action_visibility(
            resign=not over,
            rematch=over,
            leave=over,
            undo=not over,
            reject_undo=reject_undo,
            report=over,
            # 观战者没有自己的对局，拓不出"我的这一局"。
            colophon=over and not snapshot.is_spectator,
        )
        self.panel.resign_button.setEnabled(not over and self.panel._ws_connected)
        self.panel.report_button.setEnabled(over and self.panel._ws_connected)
        self.panel.leave_button.setEnabled(over and self.panel._ws_connected)
        self.panel.undo_reject_button.setEnabled(
            reject_undo and self.panel._ws_connected
        )
        if not over:
            self.panel._refresh_undo_controls()
            self.panel.leave_button.setText("离开房间")
            return
        if rated:
            self.panel.leave_button.setText("返回大厅")
            self.panel.rematch_button.setText("继续排位")
            self.panel.rematch_button.setEnabled(self.panel._ws_connected)
            return

        self.panel.leave_button.setText("离开房间")
        yours_requested, opponent_requested = session.rematch_state
        if yours_requested:
            self.panel.rematch_button.setText("等待对手同意")
            self.panel.rematch_button.setEnabled(False)
        else:
            self.panel.rematch_button.setText(
                "接受对手再战" if opponent_requested else "申请再战"
            )
            self.panel.rematch_button.setEnabled(self.panel._ws_connected)

    def _update_rule_label(self, mode: RuleMode) -> None:
        """按当前规则显示或隐藏房间标题后的规则标签。

        自由规则是默认行为，不再额外占位置；连珠规则对黑棋有禁手，
        必须让玩家和旁观者都能一眼看到。
        """
        if mode is RuleMode.RENJU:
            self.panel.game_rule_label.setText(RENJU_RULE_TEXT)
            self.panel.game_rule_label.setVisible(True)
        else:
            self.panel.game_rule_label.clear()
            self.panel.game_rule_label.setVisible(False)

    def sync_spectator_page(self, snapshot: GameSnapshot) -> None:
        """观战页：只读展示棋盘与双方信息，隐藏一切玩家专属的操作。"""
        session = self.panel._session
        self.panel._sync_sandbox_button(session)
        # 旁观的规则标签同样以权威快照为准。
        self._update_rule_label(snapshot.rule_mode)
        rated = snapshot.rated
        spectator_title = "观战 · 排位赛" if rated else "观战 · 自建房"
        if snapshot.time_control is not None:
            spectator_title = (
                f"{spectator_title}，{snapshot.time_control.label()}"
            )
        self.panel.room_title_label.setText(spectator_title)
        # 旁观者不能邀请好友进自己的房间。
        self.panel.invite_friend_button.setVisible(False)
        self.panel.connection_label.setText(
            "已连接" if self.panel._ws_connected else "连接中断"
        )
        self.panel._clear_game_error()

        current = snapshot.current_player
        over = snapshot.is_game_over
        winner = snapshot.winner
        if over:
            if winner is None:
                black_status = white_status = "本局平局"
            elif winner is Player.BLACK:
                black_status, white_status = "获胜", "本局结束"
            else:
                black_status, white_status = "本局结束", "获胜"
        else:
            black_status = "落子中" if current is Player.BLACK else "等待"
            white_status = "落子中" if current is Player.WHITE else "等待"

        # 两张身份卡展示的是对局中的两名玩家，而不是旁观者自己。
        self.panel.you_card.set_content(
            snapshot.black_name or "黑方",
            Player.BLACK,
            current is Player.BLACK,
            black_status,
            avatar=snapshot.black_avatar,
            user_id=snapshot.black_user_id,
            avatar_version=snapshot.black_avatar_version,
            achievements=snapshot.black_achievements,
        )
        self.panel.opponent_card.set_content(
            snapshot.white_name or "白方",
            Player.WHITE,
            current is Player.WHITE,
            white_status,
            avatar=snapshot.white_avatar,
            user_id=snapshot.white_user_id,
            avatar_version=snapshot.white_avatar_version,
            achievements=snapshot.white_achievements,
        )

        if over:
            if winner is None:
                self.panel._set_game_pill("本局平局", result=True)
            elif winner is Player.BLACK:
                self.panel._set_game_pill("黑方获胜", result=True)
            else:
                self.panel._set_game_pill("白方获胜", result=True)
            self.panel.clock_label.setText("对局结束")
        elif not self.panel._ws_connected:
            self.panel._set_game_pill("连接中断", result=False)
        elif current is Player.BLACK:
            self.panel._set_game_pill("黑方回合", result=False)
        else:
            self.panel._set_game_pill("白方回合", result=False)

        self.panel.move_label.setText(f"第 {snapshot.move_no} 手")
        self.panel._last_snapshot = snapshot
        self.panel._maybe_play_online_start(snapshot)
        self.panel._apply_snapshot_deadline(snapshot)
        # 观战者只读：棋盘不接受任何点击。
        if session is not None:
            self.panel.board_widget.set_view(session.view)
            if self.panel._sandbox_open:
                # 观战也是本地工具：真实局面推进时同步沙盘基线。
                self.panel.sandbox_panel.sync_base(session.view)
        self.panel.board_widget.set_interaction_enabled(False)
        self.panel.board_widget.update()
        self.panel._update_clock_label()

        # 观战者不参与积分与成就结算，这两个标签始终隐藏。
        self.panel.game_rating_label.clear()
        self.panel.game_rating_label.setVisible(False)
        self.panel.game_achievement_label.clear()
        self.panel.game_achievement_label.setVisible(False)
        self.panel._clear_undo_notice()

        # 只有“退出观战”可用，其余玩家操作全部隐藏。
        self.panel._set_action_visibility(
            resign=False,
            rematch=False,
            leave=True,
            undo=False,
            reject_undo=False,
        )
        self.panel.leave_button.setText("退出观战")
        self.panel.leave_button.setEnabled(self.panel._ws_connected)
        # 旁观者不参与对局内的聊天，也看不到双方的发言；
        # 但自建房与休闲局的观战席之间有自己的小频道。
        self.panel._apply_spectator_chat_mode(
            session.spectator_chat_available
            if session is not None
            else False
        )

    def update_achievement_notice(
        self, snapshot: Optional[GameSnapshot]
    ) -> None:
        """终局后展示本局新解锁的成就；同一局只弹一次。"""
        if snapshot is None or not snapshot.is_game_over:
            self.panel._achievement_notice_game_id = ""
            self.panel.game_achievement_label.clear()
            self.panel.game_achievement_label.setVisible(False)
            return
        keys = snapshot.your_new_achievements
        if not keys:
            self.panel.game_achievement_label.clear()
            self.panel.game_achievement_label.setVisible(False)
            return
        if self.panel._achievement_notice_game_id == snapshot.game_id:
            return
        self.panel._achievement_notice_game_id = snapshot.game_id
        names = achievement_names(keys)
        self.panel.game_achievement_label.setText(
            f"新成就解锁：{names}" if names else ""
        )
        self.panel.game_achievement_label.setVisible(bool(names))

    def set_game_pill(self, text: str, result: bool) -> None:
        """更新对局状态胶囊的文字与结果样式。"""
        self.panel.game_status_pill.setText(text)
        value = "true" if result else "false"
        if self.panel.game_status_pill.property("result") != value:
            self.panel.game_status_pill.setProperty("result", value)
            self.panel.game_status_pill.style().unpolish(self.panel.game_status_pill)
            self.panel.game_status_pill.style().polish(self.panel.game_status_pill)
            self.panel.game_status_pill.update()

    def set_action_visibility(
        self,
        resign: bool,
        rematch: bool,
        leave: bool,
        *,
        undo: bool,
        reject_undo: bool,
        report: bool = False,
        colophon: bool = False,
    ) -> None:
        """按对局阶段显示或隐藏操作按钮。"""
        self.panel.undo_button.setVisible(undo)
        self.panel.undo_reject_button.setVisible(reject_undo)
        self.panel.resign_button.setVisible(resign)
        self.panel.rematch_button.setVisible(rematch)
        self.panel.report_button.setVisible(report)
        self.panel.colophon_button.setVisible(colophon)
        self.panel.leave_button.setVisible(leave)
