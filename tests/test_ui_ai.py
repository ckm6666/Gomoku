"""人机模式与异步 AI 的离屏 UI 回归测试。

运行方式：
    python -m unittest tests.test_ui_ai -v

测试在 Qt 的 offscreen 平台运行，不弹出真实窗口；AI 线程、QTest 事件处理和
模态确认框内容仍会真实执行。为避免机器速度差异导致 flaky，等待均带超时。

注意：Qt 的 QApplication 必须在整个进程内保持单例，因此 setUpClass 创建一次，
各测试只创建/销毁主窗口。
"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

# 必须在导入 PySide6 之前设置，保证 CI/无桌面环境也能构造控件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication,
    QDialog,
    QMessageBox,
    QPushButton,
)

from gomoku.client.ai_engine import AIDifficulty  # noqa: E402
from gomoku.client.audio import get_audio_manager  # noqa: E402
from gomoku.client.constants import APP_TITLE  # noqa: E402
from gomoku.client.main_window import GameMode, MainWindow  # noqa: E402
from gomoku.client.result_dialog import ResultDialog  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.shared.rules import Player  # noqa: E402


def wait_until(
    predicate,
    timeout: float = 6.0,
    app: QApplication | None = None,
) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    application = app or QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    application.processEvents()
    return bool(predicate())


def _slow_find_move(
    self,
    *,
    board_snapshot,
    player,
    difficulty,
    cancel_event,
):
    """测试注入：可取消的慢速搜索，确定性地制造“AI 思考中”状态。

    使用 Event.wait 主动让出 GIL；真实搜索长时间占用解释器时，
    离屏 Qt 的事件处理会被明显拖慢，使“线程是否正在运行”的观察偶发超时。
    """
    del self, board_snapshot, player, difficulty
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if cancel_event.wait(0.01):
            return None
    return None


class UIAndAITests(unittest.TestCase):
    """覆盖双人回归、AI 自动落子、AI 先手、悔棋、设置锁定和关窗清理。"""

    def test_audio_controls_exist_but_disabled_offscreen(self) -> None:
        """离屏环境不创建真实音频设备，但两页的声音控件仍可构建。"""
        self.assertTrue(hasattr(self.window, "sound_button"))
        self.assertTrue(hasattr(self.window, "menu_sound_button"))
        self.assertFalse(get_audio_manager().available)
        self.assertFalse(self.window.sound_button.isEnabled())
        self.assertFalse(self.window.menu_sound_button.isEnabled())

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance() or QApplication([])
        cls.app = app
        app.setStyle("Fusion")
        apply_app_theme(app)

    def setUp(self) -> None:
        self.window = MainWindow()
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.window.show()
        self.app.processEvents()

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()

    def _click_cell(self, row: int, col: int) -> None:
        """通过棋盘点击落子，真实覆盖坐标映射与输入开关。"""
        point = self.window.board_widget._point_for(row, col).toPoint()
        QTest.mouseClick(
            self.window.board_widget,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
            point,
        )
        self.app.processEvents()

    def _start_local(self) -> None:
        """通过主菜单进入本地双人对局。"""
        self.window.local_button.click()
        self.app.processEvents()
        self.assertIs(self.window._game_mode, GameMode.LOCAL)
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )

    def _start_ai(
        self, *, difficulty_index: int = 0, ai_first: bool = False
    ) -> None:
        """通过主菜单进入人机对局，再按需调整难度/先手。"""
        self.window.ai_button.click()
        self.app.processEvents()
        self.assertIs(self.window._game_mode, GameMode.AI)
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.window.difficulty_combo.setCurrentIndex(difficulty_index)
        if ai_first:
            self.window.first_combo.setCurrentIndex(1)
        self.app.processEvents()

    def _click_modal_button(self, text: str, delay_ms: int = 250) -> dict:
        """安排 QTimer 点击当前模态框中的指定按钮。"""
        seen = {"count": 0, "text": ""}

        def click_button() -> None:
            box = self.app.activeModalWidget()
            if isinstance(box, QMessageBox):
                seen["count"] += 1
                seen["text"] = box.text()
                for button in box.buttons():
                    if button.text() == text:
                        button.click()
                        return
                box.close()
            elif isinstance(box, QDialog):
                seen["count"] += 1
                message_label = getattr(box, "message_label", None)
                seen["text"] = (
                    message_label.text()
                    if message_label is not None
                    else box.windowTitle()
                )
                for button in box.findChildren(QPushButton):
                    if button.text() == text:
                        button.click()
                        return
                box.reject()

        QTimer.singleShot(delay_ms, click_button)
        return seen

    def _arm_result_dialog_button(
        self, text: str, timeout_ms: int = 5000
    ) -> dict:
        """轮询等待终局弹窗，并点击指定按钮。"""
        state = {"count": 0, "text": ""}
        deadline = time.monotonic() + timeout_ms / 1000.0

        def attempt() -> None:
            box = self.app.activeModalWidget()
            if isinstance(box, ResultDialog):
                state["count"] += 1
                state["text"] = box.title_label.text()
                for button in (box.restart_button, box.view_button):
                    if button.text() == text:
                        button.click()
                        return
            if time.monotonic() < deadline:
                QTimer.singleShot(50, attempt)

        QTimer.singleShot(50, attempt)
        return state

    def _play_local_win(self) -> None:
        """在双人模式落出黑棋五连（9 手，白棋只有四连）。"""
        pairs = (
            ((7, 3), (0, 0)),
            ((7, 4), (0, 1)),
            ((7, 5), (0, 2)),
            ((7, 6), (0, 3)),
        )
        for black, white in pairs:
            self._click_cell(*black)
            self._click_cell(*white)
        self._click_cell(7, 7)

    def _return_to_menu(self, *, confirm: bool = True) -> dict:
        """点击返回主菜单，并自动处理确认框。"""
        seen = self._click_modal_button(
            "返回主菜单" if confirm else "取消"
        )
        self.window.menu_back_button.click()
        self.app.processEvents()
        return seen

    def _populate_midgame(self) -> None:
        """通过模型直接摆出一个无立即胜负的 16 子中盘，轮到黑方。"""
        black = [
            (8, 2),
            (8, 14),
            (6, 11),
            (13, 11),
            (7, 1),
            (6, 4),
            (13, 12),
            (5, 1),
        ]
        white = [
            (0, 7),
            (1, 14),
            (3, 10),
            (14, 13),
            (7, 5),
            (10, 7),
            (10, 10),
            (1, 5),
        ]
        for index, black_cell in enumerate(black):
            self.assertTrue(self.window.game.place_stone(*black_cell))
            self.assertTrue(self.window.game.place_stone(*white[index]))
        self.window._sync_ui()
        self.app.processEvents()

    def test_local_mode_still_works(self) -> None:
        """从主菜单进入双人模式后仍能落子、轮换和悔棋。"""
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self._start_local()
        self.assertFalse(self.window.difficulty_combo.isEnabled())
        self.assertFalse(self.window.first_combo.isEnabled())
        self.assertFalse(self.window.difficulty_group.isVisible())
        self.assertFalse(self.window.first_group.isVisible())
        self.assertTrue(self.window.local_mode_label.isVisible())
        self.assertTrue(self.window.menu_back_button.isEnabled())

        self._click_cell(7, 7)
        self._click_cell(7, 8)
        self.assertEqual(self.window.game.move_count, 2)
        self.assertEqual(self.window.turn_pill.text(), "黑棋回合")

        self.window.undo_button.click()
        self.app.processEvents()
        self.assertEqual(self.window.game.move_count, 1)
        self.assertEqual(self.window.turn_pill.text(), "白棋回合")

    def test_online_button_opens_auth_window(self) -> None:
        """离线主窗口可打开独立联机窗口，不需要真实服务器。"""
        self.window.online_button.click()
        self.app.processEvents()
        online = self.window._online_window
        self.assertIsNotNone(online)
        if online is None:
            return
        self.assertIs(online.stack.currentWidget(), online.auth_page)
        online.close()
        self.app.processEvents()

    def test_online_lobby_shows_ranked_controls_and_leaderboard(self) -> None:
        """联机大厅包含排位匹配入口和排行榜表格，可离屏构建。"""
        self.window.online_button.click()
        self.app.processEvents()
        online = self.window._online_window
        self.assertIsNotNone(online)
        if online is None:
            return
        online._show_lobby_page("")
        self.app.processEvents()
        self.assertIs(online.stack.currentWidget(), online.lobby_page)
        self.assertEqual(online.quick_match_button.text(), "开始排位匹配")
        self.assertEqual(online.rating_badge_label.text(), "未定级")
        self.assertEqual(online.leaderboard_table.columnCount(), 5)

        online._fill_leaderboard(
            [
                {
                    "position": 1,
                    "user_id": "u1",
                    "username": "冠军",
                    "rank_name": "大师",
                    "rating": 1720,
                    "wins": 12,
                    "losses": 3,
                    "draws": 1,
                },
                {
                    "position": 2,
                    "user_id": "u2",
                    "username": "亚军",
                    "rank_name": "钻石",
                    "rating": 1602,
                    "wins": 8,
                    "losses": 4,
                    "draws": 0,
                },
            ],
            2,
        )
        self.app.processEvents()
        self.assertEqual(online.leaderboard_table.rowCount(), 2)
        self.assertEqual(
            online.leaderboard_table.item(0, 1).text(), "冠军"
        )
        self.assertEqual(
            online.leaderboard_table.item(0, 2).text(), "大师"
        )
        self.assertIn("共 2 名", online.leaderboard_status_label.text())
        online.close()
        self.app.processEvents()

    def test_ai_mode_plays_automatically(self) -> None:
        """人机玩家先手：玩家落子后 AI 自动走一手合法棋。"""
        self._start_ai(difficulty_index=0)  # 简单档，测试更快
        self.assertEqual(self.window.turn_pill.text(), "你的回合")

        self._click_cell(7, 7)
        self.assertEqual(self.window.game.move_count, 1)
        self.assertFalse(self.window.board_widget._interaction_enabled)

        self.assertTrue(
            wait_until(
                lambda: self.window.game.move_count == 2
                and self.window._ai_thread is None,
                5.0,
                self.app,
            )
        )
        self.assertEqual(self.window.game.last_move.player.name, "WHITE")
        self.assertIs(
            self.window.game.current_player, self.window._human_player
        )
        self.assertTrue(self.window.board_widget._interaction_enabled)
        self.assertEqual(self.window.turn_pill.text(), "你的回合")

    def test_ai_first_and_undo_round(self) -> None:
        """AI 先手会自动开局；悔棋撤回到玩家决策点。"""
        self._start_ai(ai_first=True)

        self.assertTrue(
            wait_until(
                lambda: self.window.game.move_count == 1
                and self.window._ai_thread is None,
                5.0,
                self.app,
            )
        )
        self.assertEqual(self.window.game.last_move.player.name, "BLACK")
        self.assertIs(
            self.window.game.current_player, self.window._human_player
        )
        self.assertFalse(self.window.undo_button.isEnabled())

        self._click_cell(0, 0)
        self.assertTrue(
            wait_until(
                lambda: self.window.game.move_count == 3
                and self.window._ai_thread is None,
                5.0,
                self.app,
            )
        )
        self.window.undo_button.click()
        self.app.processEvents()
        # 撤 AI + 玩家两手，只剩 AI 开局；玩家尚无棋可悔。
        self.assertEqual(self.window.game.move_count, 1)
        self.assertIs(
            self.window.game.current_player, self.window._human_player
        )
        self.assertFalse(self.window.undo_button.isEnabled())

    def test_settings_locked_and_thinking_undo_cancels_ai(self) -> None:
        """困难 AI 思考时输入/设置锁定，悔棋可取消任务且不产生多余落子。"""
        self._start_ai(difficulty_index=2)  # 困难
        self._populate_midgame()
        self.assertEqual(self.window.game.move_count, 16)

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_slow_find_move,
        ):
            self._click_cell(0, 0)
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is not None
                    and self.window._ai_thread.isRunning(),
                    2.0,
                    self.app,
                )
            )
            self.assertFalse(self.window.difficulty_combo.isEnabled())
            self.assertFalse(self.window.first_combo.isEnabled())
            self.assertFalse(self.window.board_widget._interaction_enabled)
            self.assertTrue(self.window.menu_back_button.isEnabled())

            self.window.undo_button.click()
            self.app.processEvents()
            self.assertEqual(self.window.game.move_count, 16)
            self.assertIs(
                self.window.game.current_player, self.window._human_player
            )
            self.assertTrue(self.window.board_widget._interaction_enabled)
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is None,
                    5.0,
                    self.app,
                )
            )
            # 多等一小段时间，确认取消的旧任务不会迟到落子。
            wait_until(lambda: False, 0.25, self.app)
            self.assertEqual(self.window.game.move_count, 16)

    def test_return_to_menu_confirmation_cancel_and_accept(self) -> None:
        """返回主菜单：取消保留棋局，确认回到菜单，再次进入为新局。"""
        self._start_ai(ai_first=True)
        self.assertTrue(
            wait_until(
                lambda: self.window.game.move_count == 1
                and self.window._ai_thread is None,
                5.0,
                self.app,
            )
        )
        session_before = self.window.session
        moves_before = self.window.game.move_count

        seen_cancel = self._return_to_menu(confirm=False)
        self.assertEqual(seen_cancel["count"], 1)
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.assertIs(self.window.session, session_before)
        self.assertEqual(self.window.game.move_count, moves_before)
        self.assertIs(self.window._game_mode, GameMode.AI)
        self.assertTrue(self.window.board_widget._interaction_enabled)

        seen_accept = self._return_to_menu(confirm=True)
        self.assertEqual(seen_accept["count"], 1)
        self.assertIn("返回主菜单", seen_accept["text"])
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self.assertIs(self.window.session, session_before)
        self.assertFalse(self.window.session.accepts_human_input)
        self.assertIsNone(self.window._ai_thread)

        self.window.local_button.click()
        self.app.processEvents()
        self.assertIsNot(self.window.session, session_before)
        self.assertIs(self.window._game_mode, GameMode.LOCAL)
        self.assertEqual(self.window.game.move_count, 0)
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.assertFalse(self.window.difficulty_combo.isEnabled())
        self.assertFalse(self.window.first_combo.isEnabled())

    def test_menu_is_initial_page_with_five_entries(self) -> None:
        """启动默认显示主菜单，五个入口顺序固定。"""
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self.assertEqual(self.window.stack.count(), 2)
        buttons = self.window.menu_page.findChildren(QPushButton)
        self.assertEqual(
            [button.text() for button in buttons],
            ["联机对战", "双人对战", "人机对战", "残局挑战", "退出游戏"],
        )
        self.assertTrue(all(button.isEnabled() for button in buttons))
        self.assertFalse(hasattr(self.window, "mode_combo"))
        self.assertTrue(hasattr(self.window, "menu_sound_button"))
        self.assertEqual(self.window.windowTitle(), APP_TITLE)

    def test_ai_menu_entry_uses_medium_and_human_first(self) -> None:
        """菜单入口重置为人机默认：中等难度、玩家先手。"""
        self.window._difficulty = AIDifficulty.HARD
        self.window._human_player = Player.WHITE
        self.window.ai_button.click()
        self.app.processEvents()
        self.assertIs(self.window._difficulty, AIDifficulty.MEDIUM)
        self.assertIs(self.window._human_player, Player.BLACK)
        self.assertIs(self.window._game_mode, GameMode.AI)
        self.assertEqual(self.window.difficulty_combo.currentIndex(), 1)
        self.assertEqual(self.window.first_combo.currentIndex(), 0)
        self.assertTrue(self.window.difficulty_group.isVisible())
        self.assertTrue(self.window.first_group.isVisible())
        self.assertFalse(self.window.local_mode_label.isVisible())
        self.assertEqual(self.window.turn_pill.text(), "你的回合")

    def test_return_to_menu_without_moves_switches_immediately(self) -> None:
        """空棋盘返回主菜单不应弹确认框。"""
        self._start_local()
        probes: list[object] = []
        QTimer.singleShot(
            100, lambda: probes.append(self.app.activeModalWidget())
        )
        self.window.menu_back_button.click()
        self.app.processEvents()
        self.assertTrue(wait_until(lambda: bool(probes), 1.0, self.app))
        self.assertIsNone(probes[0])
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self.assertFalse(self.window.session.accepts_human_input)
        self.assertFalse(self.window.board_widget._interaction_enabled)

    def test_return_to_menu_while_ai_is_thinking(self) -> None:
        """AI 思考中返回菜单：取消恢复搜索，确认后不产生迟到落子。"""
        self._start_ai(difficulty_index=2)
        self._populate_midgame()

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_slow_find_move,
        ):
            self._click_cell(0, 0)
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is not None
                    and self.window._ai_thread.isRunning(),
                    2.0,
                    self.app,
                )
            )
            seen_cancel = self._return_to_menu(confirm=False)
            self.assertEqual(seen_cancel["count"], 1)
            self.assertIs(
                self.window.stack.currentWidget(), self.window.game_page
            )
            # 取消后恢复 AI 搜索，等待新的工作线程。
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is not None
                    and self.window._ai_thread.isRunning(),
                    2.0,
                    self.app,
                )
            )
            moves_before = self.window.game.move_count
            seen_accept = self._return_to_menu(confirm=True)
            self.assertEqual(seen_accept["count"], 1)
            self.assertIs(
                self.window.stack.currentWidget(), self.window.menu_page
            )
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is None,
                    5.0,
                    self.app,
                )
            )
            wait_until(lambda: False, 0.25, self.app)
            self.assertEqual(self.window.game.move_count, moves_before)

    def test_puzzle_placeholder_shows_coming_soon_message(self) -> None:
        """残局挑战入口显示即将上线提示，且不改变对局状态。"""
        seen = self._click_modal_button("知道了")
        self.window.puzzle_button.click()
        self.app.processEvents()
        self.assertEqual(seen["count"], 1)
        self.assertIn("即将上线", seen["text"])
        self.assertIn("敬请期待", seen["text"])
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self.assertEqual(self.window.game.move_count, 0)

    def test_shortcuts_disabled_on_menu(self) -> None:
        """菜单页 Ctrl+Z/Ctrl+N 不生效，进入对局后恢复。"""
        self.assertFalse(self.window._undo_shortcut.isEnabled())
        self.assertFalse(self.window._restart_shortcut.isEnabled())
        self.window._undo_move()
        self.window._request_restart()
        self.app.processEvents()
        self.assertIs(
            self.window.stack.currentWidget(), self.window.menu_page
        )
        self.assertEqual(self.window.game.move_count, 0)

        self._start_local()
        self.assertTrue(self.window._undo_shortcut.isEnabled())
        self.assertTrue(self.window._restart_shortcut.isEnabled())

    def test_exit_button_closes_window(self) -> None:
        """退出游戏按钮关闭主窗口。"""
        self.assertTrue(self.window.isVisible())
        self.window.exit_button.click()
        self.app.processEvents()
        self.assertTrue(self.window._closing)
        self.assertFalse(self.window.isVisible())

    def test_restart_confirmation_uses_themed_dialog(self) -> None:
        """重新开局确认框使用古风弹窗，取消/确认语义正确。"""
        self._start_local()
        self._click_cell(7, 7)
        moves_before = self.window.game.move_count
        session_before = self.window.session

        seen_cancel = self._click_modal_button("取消")
        self.window.restart_button.click()
        self.app.processEvents()
        self.assertEqual(seen_cancel["count"], 1)
        self.assertIn("重新开局", seen_cancel["text"])
        self.assertIs(self.window.session, session_before)
        self.assertEqual(self.window.game.move_count, moves_before)

        seen_accept = self._click_modal_button("开始新对局")
        self.window.restart_button.click()
        self.app.processEvents()
        self.assertEqual(seen_accept["count"], 1)
        self.assertTrue(
            wait_until(
                lambda: self.window.session is not session_before
                and self.window.game.move_count == 0,
                5.0,
                self.app,
            )
        )

    def test_result_dialog_view_board_keeps_game(self) -> None:
        """终局弹窗“查看棋盘”保留胜负与最后一局。"""
        self._start_local()
        session_before = self.window.session
        state = self._arm_result_dialog_button("查看棋盘")
        self._play_local_win()
        self.assertTrue(
            wait_until(lambda: state["count"] == 1, 8.0, self.app)
        )
        self.assertTrue(
            wait_until(
                lambda: not self.window._result_dialog_open
                and not self.window._modal_dialog_open,
                5.0,
                self.app,
            )
        )
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.assertIs(self.window.session, session_before)
        self.assertTrue(self.window.game.is_game_over)
        self.assertEqual(self.window.game.move_count, 9)
        self.assertIs(self.window.game.winner, Player.BLACK)

    def test_result_dialog_restart_starts_new_game(self) -> None:
        """终局弹窗“再来一局”创建全新会话。"""
        self._start_local()
        session_before = self.window.session
        state = self._arm_result_dialog_button("再来一局")
        self._play_local_win()
        self.assertTrue(
            wait_until(lambda: state["count"] == 1, 8.0, self.app)
        )
        self.assertTrue(
            wait_until(
                lambda: self.window.session is not session_before
                and self.window.game.move_count == 0,
                5.0,
                self.app,
            )
        )
        self.assertTrue(
            wait_until(
                lambda: not self.window._result_dialog_open
                and not self.window._modal_dialog_open,
                5.0,
                self.app,
            )
        )
        self.assertIs(
            self.window.stack.currentWidget(), self.window.game_page
        )
        self.assertTrue(self.window.board_widget._interaction_enabled)

    def test_close_while_ai_is_thinking(self) -> None:
        """AI 搜索期间关闭窗口应取消并等待线程退出，不崩溃、不卡死。

        用可取消的慢速搜索替代真实搜索，避免“线程瞬间算完”或 GIL 竞争
        让该用例偶发超时；本用例关注的是关闭时的取消与线程清理。
        """
        self._start_ai(difficulty_index=2)
        self._populate_midgame()

        with mock.patch(
            "gomoku.client.ai_worker.GomokuAI.find_move",
            new=_slow_find_move,
        ):
            self._click_cell(0, 0)
            self.assertTrue(
                wait_until(
                    lambda: self.window._ai_thread is not None
                    and self.window._ai_thread.isRunning(),
                    2.0,
                    self.app,
                )
            )
            thread = self.window._ai_thread
            self.assertIsNotNone(thread)
            started = time.monotonic()
            self.window.close()
            elapsed = time.monotonic() - started
            self.app.processEvents()
            self.assertLess(elapsed, 4.0)
            self.assertTrue(thread.isFinished())


if __name__ == "__main__":
    unittest.main(verbosity=2)
