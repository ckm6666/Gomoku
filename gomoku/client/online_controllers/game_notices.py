"""联机对局的提示与音效：错误横幅、终局弹窗和结果音。

错误提示需要停留几秒，不能被紧接着的状态同步清掉；终局弹窗与音效
同一局只触发一次，面板在后台时延后到回到前台。
"""

from __future__ import annotations

from PySide6.QtCore import QTimer

from gomoku.client.online_controllers.common import (
    PanelController,
    host_module,
)
from gomoku.shared.protocol import GameSnapshot


class GameNoticesController(PanelController):
    """对局页的状态提示、错误横幅、终局弹窗与音效。"""

    def show_status(self, text: str, error: bool = False) -> None:
        """在当前页面的状态标签上显示一条提示。"""
        if self.panel.stack.currentWidget() is self.panel.game_page:
            if error:
                self.panel._set_game_error(text)
            else:
                # 普通提示别塞进红色的错误标签里。
                self.panel._clear_game_error()
                self.panel._set_lobby_status(text)
        else:
            self.panel._set_lobby_status(text, error=error)

    def set_game_error(self, text: str) -> None:
        """在对局页显示一条错误提示，并让它停留几秒。

        服务端拒绝落子时会先发 error、紧接着推一次状态同步，状态同步
        不能把这条提示清掉，否则玩家根本看不到“该位置已有棋子”这类
        失败原因。
        """
        if not text:
            self.panel._clear_game_error()
            return
        self.panel.game_error_label.setText(text)
        self.panel._game_error_timer.start(
            int(host_module().GAME_ERROR_HOLD_SECONDS * 1000)
        )

    def clear_game_error(self) -> None:
        """清除对局页的错误提示。"""
        self.panel._game_error_timer.stop()
        if not self.panel.game_error_label.text():
            return
        self.panel.game_error_label.setText("")

    def clear_stale_game_error(self) -> None:
        """状态同步时只清理已经过期的错误提示。

        停留时间内的提示必须保留：落子被拒后紧接着的这次状态同步不能
        把失败原因抹掉。
        """
        if self.panel._game_error_timer.isActive():
            return
        self.panel.game_error_label.setText("")

    def on_online_error(self, message: str) -> None:
        """服务器错误：按当前页面放到认证/大厅/对局错误横幅上。"""
        if self.panel.sender() is not self.panel._session:
            return
        self.panel._room_action_pending = False
        if self.panel.stack.currentWidget() is self.panel.auth_page:
            self.panel._set_auth_status(message, error=True)
        elif self.panel.stack.currentWidget() is self.panel.lobby_page:
            self.panel._set_lobby_status(message, error=True)
            self.panel._refresh_lobby_controls()
        else:
            # 落子被拒等原因要停留几秒，不能被紧接着的状态同步清掉。
            self.panel._set_game_error(message)
            # 原因同时进对话日志，日志收起时也能回看。
            self.panel._log_game_notice(message)

    def on_online_stone_placed(self) -> None:
        """服务器确认落子后播放音效；仅发送意图的本地点击不会触发。"""
        if self.panel.sender() is not self.panel._session or self.panel._closing:
            return
        host_module().get_audio_manager().play_move()

    def on_online_game_over(self) -> None:
        """终局：刷新界面，并在前台时播放终局音、弹终局提示。"""
        if self.panel.sender() is not self.panel._session:
            return
        self.panel._sync_online_ui()
        if not self.panel._closing:
            if self.panel._visible_paused:
                # 面板在后台：不放终局音、不弹终局窗，等用户回来再补。
                self.panel._pending_end_notice = True
            else:
                self.panel._play_online_result_sound()
                QTimer.singleShot(0, self.panel._show_online_victory_notice)
                QTimer.singleShot(0, self.panel._show_online_timeout_notice)
        if self.panel._session is not None and not self.panel._session.is_spectator:
            # 观战不会改变本人的段位，无需重新拉取资料。
            QTimer.singleShot(0, self.panel._refresh_profile_data)

    def show_online_victory_notice(self) -> None:
        """对手认输/逃跑后的胜利弹窗；同一局只弹一次。"""
        if self.panel._closing or self.panel._session is None:
            return
        snapshot = self.panel._session.snapshot
        notice = host_module().online_victory_notice(snapshot)
        if notice is None or snapshot is None:
            return
        if self.panel._victory_notice_game_id == snapshot.game_id:
            return
        if (
            not self.panel.isVisible()
            or self.panel.stack.currentWidget() is not self.panel.game_page
        ):
            return
        self.panel._victory_notice_game_id = snapshot.game_id
        title, message = notice
        overlay = host_module().NoticeOverlay(
            title, message, parent=self.panel, seal_text="胜"
        )
        try:
            overlay.exec()
        finally:
            overlay.deleteLater()

    def show_online_timeout_notice(self) -> None:
        """超时判负后的提示弹窗：胜方与负方各弹一条。

        超时的一方判负、对手获胜，双方都会看到弹窗，只是内容不同。
        同一局只弹一次。
        """
        if self.panel._closing or self.panel._session is None:
            return
        snapshot = self.panel._session.snapshot
        notice = host_module().online_timeout_notice(snapshot)
        if notice is None or snapshot is None:
            return
        if self.panel._timeout_notice_game_id == snapshot.game_id:
            return
        if (
            not self.panel.isVisible()
            or self.panel.stack.currentWidget() is not self.panel.game_page
        ):
            return
        self.panel._timeout_notice_game_id = snapshot.game_id
        title, message = notice
        seal = (
            "胜" if snapshot.winner is snapshot.your_color else "负"
        )
        overlay = host_module().NoticeOverlay(
            title, message, parent=self.panel, seal_text=seal
        )
        try:
            overlay.exec()
        finally:
            overlay.deleteLater()

    def play_online_result_sound(self) -> None:
        """按服务器快照判断胜负，播放对应的终局音效。"""
        if self.panel._visible_paused:
            # 面板隐藏时不放终局音，别打扰正在看别的页面的用户。
            return
        snapshot = (
            self.panel._session.snapshot
            if self.panel._session is not None
            else None
        )
        if snapshot is None or not snapshot.is_game_over:
            return
        if snapshot.is_spectator:
            # 观战者不是参与者，不该听到自己“赢了”或“输了”的音效。
            return
        audio = host_module().get_audio_manager()
        if snapshot.winner is None:
            audio.play_draw()
        elif snapshot.winner is snapshot.your_color:
            audio.play_win()
        else:
            audio.play_lose()

    def maybe_play_online_start(self, snapshot: GameSnapshot) -> None:
        """每个新 game_id 的第一份权威快照播放一次开局音。"""
        if self.panel._audio_game_id == snapshot.game_id:
            return
        self.panel._audio_game_id = snapshot.game_id
        if not self.panel._closing:
            host_module().get_audio_manager().play_start()
