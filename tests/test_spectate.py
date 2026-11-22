"""观战（spectate）功能的客户端测试。

覆盖三件事：
* 会话层能发出 ``watch_game``，并在收到旁观者快照后关闭一切玩家操作；
* 普通玩家的行为不被观战改动（回归保护）；
* 联机面板在观战时渲染成只读视图。
"""

from __future__ import annotations

import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtNetwork import QNetworkReply  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QLabel,
    QPushButton,
    QWidget,
)

from gomoku.client.auth import AuthSession, AuthUser  # noqa: E402
from gomoku.client.net.game_history_api import (  # noqa: E402
    GameHistoryHTTPClient,
)
from gomoku.client.online_panel import (  # noqa: E402
    OnlinePanel,
    online_victory_notice,
)
from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    SERVER_LIVE_GAMES_CHANGED,
    make_message,
)
from gomoku.shared.rules import BOARD_SIZE, Player  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def blank_board() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def state_message(
    *,
    your_color: str | None = "black",
    is_spectator: bool = False,
    current: str = "black",
    move_no: int = 0,
    status: str = "playing",
    game_id: str = "game-1",
    black_name: str = "张三",
    white_name: str = "李四",
    rated: bool = False,
) -> dict:
    """构造一个符合协议的服务器局面消息。

    旁观者的 ``your_color`` 为 null，并且必须带上 ``is_spectator``。
    """
    return make_message(
        SERVER_GAME_STATE,
        {
            "game_id": game_id,
            "board": blank_board(),
            "current_player": current,
            "your_color": your_color,
            "move_no": move_no,
            "status": status,
            "last_move": None,
            "winning_line": [],
            "rated": rated,
            "is_spectator": is_spectator,
            "black_name": black_name,
            "white_name": white_name,
            "black_user_id": "u-black",
            "white_user_id": "u-white",
        },
    )


def authenticate(transport: FakeTransport, session: OnlineGameSession) -> None:
    """让会话进入已认证状态。"""
    transport.receive(
        make_message(
            SERVER_AUTHENTICATED,
            {"user": {"id": "u3", "username": "观察者"}},
        )
    )


class WatchGameSessionTests(unittest.TestCase):
    """观战请求与旁观者身份。"""

    def _session(self) -> tuple[FakeTransport, OnlineGameSession]:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        return transport, session

    def test_watch_game_sends_watch_message(self) -> None:
        """观战请求要带上对局编号。"""
        transport, session = self._session()
        try:
            self.assertTrue(session.watch_game("game-42"))
            self.assertEqual(len(transport.sent_messages), 1)
            message = transport.sent_messages[0]
            self.assertEqual(message["type"], CLIENT_WATCH_GAME)
            self.assertEqual(message["payload"], {"game_id": "game-42"})
            self.assertTrue(session.is_spectator)
        finally:
            session.shutdown()
            transport.close()

    def test_watch_game_rejects_empty_id(self) -> None:
        """空对局编号不能发出请求，要给出中文提示。"""
        transport, session = self._session()
        errors: list[str] = []
        session.error.connect(errors.append)
        try:
            self.assertFalse(session.watch_game("   "))
            self.assertEqual(transport.sent_messages, [])
            self.assertIn("对局编号无效", errors)
            self.assertFalse(session.is_spectator)
        finally:
            session.shutdown()
            transport.close()

    def test_watch_game_requires_authentication(self) -> None:
        """没有登录就不能观战。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        try:
            self.assertFalse(session.watch_game("game-42"))
            self.assertEqual(transport.sent_messages, [])
        finally:
            session.shutdown()
            transport.close()

    def test_spectator_snapshot_blocks_player_actions(self) -> None:
        """旁观者快照下所有玩家操作都必须被拒绝。"""
        transport, session = self._session()
        try:
            transport.receive(
                state_message(your_color=None, is_spectator=True)
            )
            self.assertTrue(session.is_spectator)
            self.assertFalse(session.accepts_human_input)
            self.assertFalse(session.can_undo)
            self.assertFalse(session.chat_available)

            sent_before = len(transport.sent_messages)
            self.assertFalse(session.request_move(7, 7))
            self.assertFalse(session.resign())
            self.assertFalse(session.request_rematch())
            self.assertFalse(session.request_undo())
            self.assertFalse(session.respond_undo(True))
            self.assertFalse(session.send_chat("emoji", "😀"))
            self.assertEqual(len(transport.sent_messages), sent_before)

            # 退出观战必须可用：观战者靠它离开。
            self.assertTrue(session.leave_room())
        finally:
            session.shutdown()
            transport.close()

    def test_player_snapshot_keeps_normal_behaviour(self) -> None:
        """普通玩家的快照不受观战改动影响（回归保护）。"""
        transport, session = self._session()
        try:
            transport.receive(state_message(your_color="black"))
            self.assertFalse(session.is_spectator)
            self.assertTrue(session.accepts_human_input)
            self.assertTrue(session.chat_available)
            self.assertTrue(session.request_move(7, 7))
            self.assertEqual(
                transport.sent_messages[-1]["type"], "move_intent"
            )
        finally:
            session.shutdown()
            transport.close()

    def test_live_games_changed_is_forwarded(self) -> None:
        """服务器推送“对局列表变了”，会话要转发出去。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        seen: list[bool] = []
        session.live_games_changed.connect(lambda: seen.append(True))
        transport.receive(make_message(SERVER_LIVE_GAMES_CHANGED, {}))
        self.assertEqual(seen, [True])

    def test_watch_failure_resets_spectator_flag(self) -> None:
        """观战被服务器拒绝后，要回到普通玩家身份。"""
        transport, session = self._session()
        try:
            self.assertTrue(session.watch_game("game-42"))
            transport.receive(
                make_message("server_error", {"reason": "对局不存在或已结束"})
            )
            self.assertFalse(session.is_spectator)
        finally:
            session.shutdown()
            transport.close()


class SpectatorPanelTests(unittest.TestCase):
    """观战页面必须只读：不能落子、不能操作、看不到聊天。"""

    def _panel(self, current: str = "white") -> tuple[
        OnlinePanel, OnlineGameSession, FakeTransport
    ]:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        transport.receive(
            state_message(
                your_color=None,
                is_spectator=True,
                current=current,
                move_no=12,
            )
        )
        panel = OnlinePanel()
        panel._session = session
        panel._ws_connected = True
        panel._show_game_page()
        panel._sync_game_page()
        return panel, session, transport

    def test_spectator_page_is_read_only(self) -> None:
        """观战页面隐藏一切玩家专属控件。"""
        panel, session, transport = self._panel()
        try:
            self.assertTrue(session.is_spectator)
            # 棋盘只读。
            self.assertFalse(panel.board_widget._interaction_enabled)
            sent_before = len(transport.sent_messages)
            panel._on_board_clicked(7, 7)
            self.assertEqual(len(transport.sent_messages), sent_before)

            # 玩家操作按钮全部隐藏。
            self.assertTrue(panel.undo_button.isHidden())
            self.assertTrue(panel.undo_reject_button.isHidden())
            self.assertTrue(panel.resign_button.isHidden())
            self.assertTrue(panel.rematch_button.isHidden())
            # 只保留“退出观战”。
            self.assertFalse(panel.leave_button.isHidden())
            self.assertEqual(panel.leave_button.text(), "退出观战")
            # 邀请好友对旁观者没有意义。
            self.assertTrue(panel.invite_friend_button.isHidden())

            # 聊天输入栏与对话日志都要藏起来。
            self.assertTrue(panel.chat_input.isHidden())
            self.assertTrue(panel.chat_send_button.isHidden())
            self.assertTrue(panel.emoji_button.isHidden())
            self.assertTrue(panel.preset_comment_button.isHidden())
            self.assertTrue(panel.chat_log.isHidden())

            # 积分与成就不属于旁观者。
            self.assertTrue(panel.game_rating_label.isHidden())
            self.assertTrue(panel.game_achievement_label.isHidden())

            # 标题与身份卡显示的是对局双方。
            self.assertIn("观战", panel.room_title_label.text())
            self.assertEqual(panel.you_card._name.text(), "张三")
            self.assertEqual(panel.opponent_card._name.text(), "李四")
        finally:
            panel.close()

    def test_spectator_status_pill_names_the_side_to_move(self) -> None:
        """状态标签说黑方/白方，不能出现“你”。"""
        panel, _session, _transport = self._panel(current="white")
        try:
            self.assertIn("白方", panel.game_status_pill.text())
            self.assertNotIn("你", panel.game_status_pill.text())
        finally:
            panel.close()

        panel, _session, _transport = self._panel(current="black")
        try:
            self.assertIn("黑方", panel.game_status_pill.text())
            self.assertNotIn("你", panel.game_status_pill.text())
        finally:
            panel.close()

    def test_spectator_never_gets_victory_notice(self) -> None:
        """旁观者不是参与者，不该收到“你获胜”的弹窗。"""
        panel, session, _transport = self._panel()
        try:
            snapshot = session.snapshot
            self.assertIsNotNone(snapshot)
            finished = replace(
                snapshot,
                winner=Player.BLACK,
                is_game_over=True,
                result_reason="resign",
            )
            self.assertIsNone(online_victory_notice(finished))
        finally:
            panel.close()


class LiveGamesHttpTests(unittest.TestCase):
    """进行中对局列表的 HTTP 客户端。"""

    def test_fetch_live_uses_live_endpoint(self) -> None:
        client = GameHistoryHTTPClient("http://127.0.0.1:8000")
        calls: list[tuple[str, str, str]] = []

        def fake_start(kind: str, path: str, token: str) -> bool:
            calls.append((kind, path, token))
            return True

        client._start = fake_start  # type: ignore[method-assign]
        try:
            self.assertTrue(client.fetch_live("token"))
            self.assertEqual(
                calls, [("live", "/api/v1/games/live", "token")]
            )
        finally:
            client.abort_all()

    def test_live_response_emits_live_ready(self) -> None:
        """live 请求的结果必须走 live_ready，不能混进最近对局。"""
        client = GameHistoryHTTPClient("http://127.0.0.1:8000")
        live: list[dict] = []
        recent: list[dict] = []
        client.live_ready.connect(live.append)
        client.recent_ready.connect(recent.append)

        class FakeReply:
            def attribute(self, _attr):
                return 200

            def readAll(self) -> bytes:
                return b'{"entries": [{"game_id": "g1"}], "total": 1}'

            def error(self):
                return QNetworkReply.NetworkError.NoError

            def deleteLater(self) -> None:
                pass

        reply = FakeReply()
        client._replies["live"] = reply  # type: ignore[assignment]
        try:
            client._on_finished(reply, "live")  # type: ignore[arg-type]
            self.assertEqual(
                live, [{"entries": [{"game_id": "g1"}], "total": 1}]
            )
            self.assertEqual(recent, [])
        finally:
            client.abort_all()


class LiveGamesLobbyTests(unittest.TestCase):
    """大厅里的“进行中对局”列表是观战的入口。"""

    def _panel(self) -> OnlinePanel:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        panel = OnlinePanel()
        panel._session = session
        panel._auth_session = AuthSession(
            access_token="token",
            refresh_token="refresh",
            user=AuthUser(id="u3", username="观察者"),
            expires_in=3600,
        )
        panel._ws_connected = True
        return panel

    def test_lobby_lists_live_games_with_watch_buttons(self) -> None:
        """列表要显示双方昵称，并且每行都有一个能发起观战的按钮。"""
        panel = self._panel()
        try:
            panel._on_live_ready(
                {
                    "entries": [
                        {
                            "game_id": "g-1",
                            "rated": True,
                            "black_username": "张三",
                            "white_username": "李四",
                            "black_user_id": "u1",
                            "white_user_id": "u2",
                            "move_no": 12,
                            "status": "playing",
                        }
                    ],
                    "total": 1,
                }
            )
            rows = [
                panel.live_games_layout.itemAt(i).widget()
                for i in range(panel.live_games_layout.count())
            ]
            self.assertEqual(len(rows), 1)
            texts = [
                label.text()
                for row in rows
                for label in row.findChildren(QLabel)
            ]
            self.assertTrue(any("张三" in text for text in texts))
            self.assertTrue(any("李四" in text for text in texts))
            buttons = [
                button
                for row in rows
                for button in row.findChildren(QPushButton)
            ]
            self.assertEqual(len(buttons), 1)
            self.assertEqual(buttons[0].text(), "观战")
        finally:
            panel.close()

    def test_watch_button_starts_watching(self) -> None:
        """点击“观战”必须发出 watch_game 消息。"""
        panel = self._panel()
        try:
            panel._on_watch_game("g-1")
            sent = [
                message
                for message in panel._session._transport.sent_messages
                if message.get("type") == CLIENT_WATCH_GAME
            ]
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0]["payload"]["game_id"], "g-1")
            self.assertTrue(panel._session.is_spectator)
        finally:
            panel.close()

    def test_empty_live_list_shows_placeholder(self) -> None:
        """没有进行中的对局时给出提示，而不是空白。"""
        panel = self._panel()
        try:
            panel._on_live_ready({"entries": [], "total": 0})
            self.assertEqual(
                panel.live_status_label.text(), "当前没有进行中的对局"
            )
        finally:
            panel.close()


class PlazaPageTests(unittest.TestCase):
    """对战广场是独立页面：大厅只留入口，列表动态跟随对局开关。"""

    def _panel(self) -> OnlinePanel:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        authenticate(transport, session)
        panel = OnlinePanel()
        panel._session = session
        panel._auth_session = AuthSession(
            access_token="token",
            refresh_token="refresh",
            user=AuthUser(id="u3", username="观察者"),
            expires_in=3600,
        )
        panel._ws_connected = True
        return panel

    def test_lobby_only_keeps_the_plaza_entry(self) -> None:
        """大厅不放观战列表，只留一个“对战广场”入口按钮。"""
        panel = self._panel()
        try:
            lobby_buttons = [
                button.text()
                for button in panel.lobby_page.findChildren(QPushButton)
            ]
            self.assertIn("对战广场", lobby_buttons)
            self.assertNotIn("观战", lobby_buttons)
            # 列表控件挂在广场页上，不在大厅页上。
            self.assertNotIn(
                panel.live_games_container,
                panel.lobby_page.findChildren(QWidget),
            )
        finally:
            panel.close()

    def test_plaza_button_opens_plaza_page(self) -> None:
        """点“对战广场”切到广场页，列表和返回按钮都在那一页。"""
        panel = self._panel()
        try:
            panel.plaza_button.click()
            self.assertIs(panel.stack.currentWidget(), panel.plaza_page)
            plaza_buttons = [
                button.text()
                for button in panel.plaza_page.findChildren(QPushButton)
            ]
            self.assertIn("刷新", plaza_buttons)
            self.assertIn("返回大厅", plaza_buttons)
        finally:
            panel.close()

    def test_plaza_back_returns_to_lobby(self) -> None:
        panel = self._panel()
        try:
            panel._show_plaza_page()
            panel.plaza_back_button.click()
            self.assertIs(panel.stack.currentWidget(), panel.lobby_page)
        finally:
            panel.close()

    def test_push_refreshes_plaza_when_visible(self) -> None:
        """服务器推送“对局列表变了”，广场要重新拉一次。"""
        panel = self._panel()
        calls: list[str] = []
        panel._live_client.fetch_live = (  # type: ignore[method-assign]
            lambda token: calls.append(token) or True
        )
        try:
            panel._show_plaza_page()
            calls.clear()
            panel._on_live_games_changed()
            self.assertEqual(len(calls), 1)
        finally:
            panel.close()

    def test_push_is_ignored_when_plaza_hidden(self) -> None:
        """广场不可见时不刷新，避免无谓的请求。"""
        panel = self._panel()
        calls: list[str] = []
        panel._live_client.fetch_live = (  # type: ignore[method-assign]
            lambda token: calls.append(token) or True
        )
        try:
            panel._show_lobby_page()
            panel._on_live_games_changed()
            self.assertEqual(calls, [])
        finally:
            panel.close()

    def test_spectate_exits_back_to_plaza(self) -> None:
        """从广场进去观战，退出后回到广场而不是大厅。"""
        panel = self._panel()
        try:
            panel._show_plaza_page()
            panel._on_watch_game("g-1")
            self.assertTrue(panel._watching_from_plaza)
            panel._leave_game_page("已退出观战")
            self.assertIs(panel.stack.currentWidget(), panel.plaza_page)
        finally:
            panel.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
