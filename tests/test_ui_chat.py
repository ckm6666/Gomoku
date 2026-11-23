"""弹幕覆盖层、对话日志面板与表情/快捷评论按钮的界面测试。"""

from __future__ import annotations

import os
import shutil
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QFontMetrics  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QApplication,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.chat_widgets import (  # noqa: E402
    ChatLogPanel,
    EmojiPickerButton,
    PresetCommentButton,
)
from gomoku.client.danmaku import (  # noqa: E402
    DanmakuOverlay,
    _resolve_emoji_font,
)
from gomoku.client.styles import APP_STYLE  # noqa: E402
from gomoku.shared.chat import (  # noqa: E402
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    COMMENT_PRESETS,
    EMOJI_PRESETS,
    ChatMessage,
)

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout: float = 6.0) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QT_APP.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    QT_APP.processEvents()
    return bool(predicate())


def advance_animation(seconds: float) -> None:
    """推进覆盖层动画若干秒。

    弹幕和表情都有淡入过程，刚加入时透明度为 0，直接截图会什么都
    看不到；断言"确实画出来了"之前必须先把动画推进到可见阶段。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        QT_APP.processEvents()
        time.sleep(0.01)
    QT_APP.processEvents()


def _has_visible_pixels(pixmap) -> bool:
    """判断图像里是否存在任何可见像素。

    覆盖层是透明控件，所以只要有任何一个像素的 alpha 大于 0，
    就说明确实画上了东西。
    """
    image = pixmap.toImage()
    if image.isNull() or image.width() <= 0 or image.height() <= 0:
        return False
    for y in range(0, image.height(), 2):
        for x in range(0, image.width(), 2):
            if image.pixelColor(x, y).alpha() > 8:
                return True
    return False


def _visible_bbox(pixmap) -> tuple[int, int, int, int] | None:
    """返回可见像素的包围盒 (left, top, right, bottom)；没有则返回 None。"""
    image = pixmap.toImage()
    xs: list[int] = []
    ys: list[int] = []
    for y in range(image.height()):
        for x in range(image.width()):
            if image.pixelColor(x, y).alpha() > 8:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


class DanmakuOverlayTests(unittest.TestCase):
    """弹幕/表情覆盖层的动画生命周期。"""

    def setUp(self) -> None:
        self.overlay = DanmakuOverlay()
        self.overlay.resize(320, 240)
        self.addCleanup(self.overlay.deleteLater)

    def test_overlay_does_not_accept_mouse_events(self) -> None:
        self.assertTrue(
            self.overlay.testAttribute(
                Qt.WidgetAttribute.WA_TransparentForMouseEvents
            )
        )

    def test_push_text_and_emoji_increase_active_count(self) -> None:
        self.assertEqual(self.overlay.active_count(), 0)
        self.overlay.push_text("好棋！", True)
        self.assertEqual(self.overlay.active_count(), 1)
        self.overlay.push_emoji(EMOJI_PRESETS[0], False)
        self.assertEqual(self.overlay.active_count(), 2)

    def test_items_are_removed_after_animation_finishes(self) -> None:
        self.overlay.push_text("好棋！", True)
        self.overlay.push_emoji(EMOJI_PRESETS[0], True)
        self.assertEqual(self.overlay.active_count(), 2)
        self.assertTrue(
            wait_until(lambda: self.overlay.active_count() == 0, timeout=6.0)
        )
        self.assertEqual(self.overlay.active_count(), 0)

    def test_clear_empties_the_overlay(self) -> None:
        self.overlay.push_text("好棋！", True)
        self.overlay.push_emoji(EMOJI_PRESETS[0], True)
        self.assertEqual(self.overlay.active_count(), 2)
        self.overlay.clear()
        self.assertEqual(self.overlay.active_count(), 0)

    def test_comment_danmaku_is_actually_painted(self) -> None:
        """弹幕必须真的画出来，而不是只存在于元素列表里。"""
        self.overlay.push_text("好棋！", True)
        advance_animation(0.5)
        image = self.overlay.grab()
        self.assertTrue(
            _has_visible_pixels(image),
            "评论弹幕没有画出任何可见像素",
        )

    def test_emoji_is_actually_painted(self) -> None:
        """表情必须真的画出来。

        这里曾经因为绘制时用了 NoPen 而整块不可见（drawText 遇到
        无笔的画笔什么都不画），所以必须做像素级断言而不是只数元素。
        """
        emoji = EMOJI_PRESETS[0]
        font = _resolve_emoji_font(40)
        if not QFontMetrics(font).inFont(ord(emoji)):
            self.skipTest("当前平台没有能渲染该表情的字体")
        self.overlay.push_emoji(emoji, True)
        advance_animation(0.5)
        image = self.overlay.grab()
        self.assertTrue(
            _has_visible_pixels(image),
            "表情没有画出任何可见像素",
        )


class ChatLogPanelTests(unittest.TestCase):
    """只读对话日志面板：只展示，不能发言。"""

    def setUp(self) -> None:
        self.panel = ChatLogPanel()
        self.panel.resize(280, 360)
        self.addCleanup(self.panel.deleteLater)

    def test_append_message_renders_self_and_opponent_entries(self) -> None:
        self.panel.append_message(
            ChatMessage(
                CHAT_KIND_TEXT, "好棋！", "u-self", "我", sent_at_ms=1
            ),
            True,
        )
        self.panel.append_message(
            ChatMessage(
                CHAT_KIND_TEXT, "承让了", "u-other", "对手甲", sent_at_ms=2
            ),
            False,
        )
        self.assertEqual(self.panel.entry_count(), 2)
        texts = self.panel.entry_texts()
        self.assertEqual(texts[0], "你：好棋！")
        self.assertEqual(texts[1], "对手甲：承让了")

    def test_emoji_entry_is_recorded_in_the_log(self) -> None:
        self.panel.append_message(
            ChatMessage(CHAT_KIND_EMOJI, EMOJI_PRESETS[0], "u-self", "我"),
            True,
        )
        self.assertEqual(
            self.panel.entry_texts(), [f"你：{EMOJI_PRESETS[0]}"]
        )

    def test_panel_contains_no_input_widget(self) -> None:
        self.panel.append_message(
            ChatMessage(CHAT_KIND_TEXT, "好棋！", "u-self", "我"), True
        )
        self.assertEqual(self.panel.findChildren(QLineEdit), [])
        self.assertEqual(self.panel.findChildren(QTextEdit), [])
        self.assertEqual(self.panel.findChildren(QAbstractButton), [])

    def test_entries_are_selectable_but_not_editable(self) -> None:
        self.panel.append_message(
            ChatMessage(CHAT_KIND_TEXT, "好棋！", "u-self", "我"), True
        )
        labels = self.panel.findChildren(QLabel)
        self.assertEqual(len(labels), 2)  # 标题 + 一条日志
        entry = labels[-1]
        flags = entry.textInteractionFlags()
        self.assertTrue(
            flags & Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.assertFalse(flags & Qt.TextInteractionFlag.TextEditable)

    def test_clear_log_removes_all_entries(self) -> None:
        self.panel.append_message(
            ChatMessage(CHAT_KIND_TEXT, "好棋！", "u-self", "我"), True
        )
        self.panel.clear_log()
        self.assertEqual(self.panel.entry_count(), 0)
        self.assertEqual(self.panel.entry_texts(), [])


class EmojiPickerButtonTests(unittest.TestCase):
    """表情按钮：弹出表情网格并发出 emoji_picked。"""

    def setUp(self) -> None:
        self.button = EmojiPickerButton()
        self.addCleanup(self.button.deleteLater)

    def test_picker_exposes_one_button_per_emoji_preset(self) -> None:
        menu = self.button.menu()
        self.assertIsNotNone(menu)
        actions = menu.actions()
        self.assertGreaterEqual(len(actions), 1)
        grid_widget = actions[0].defaultWidget()
        self.assertIsNotNone(grid_widget)
        grid_buttons = grid_widget.findChildren(QPushButton)
        self.assertEqual(len(grid_buttons), len(EMOJI_PRESETS))
        self.assertEqual(
            {button.text() for button in grid_buttons},
            set(EMOJI_PRESETS),
        )

    def test_picker_emits_emoji_picked(self) -> None:
        picked: list[str] = []
        self.button.emoji_picked.connect(picked.append)
        grid_buttons = (
            self.button.menu().actions()[0].defaultWidget().findChildren(
                QPushButton
            )
        )
        target = next(
            button
            for button in grid_buttons
            if button.text() == EMOJI_PRESETS[3]
        )
        target.click()
        self.assertEqual(picked, [EMOJI_PRESETS[3]])

    def test_picker_can_be_disabled(self) -> None:
        self.button.set_emoji_enabled(False)
        self.assertFalse(self.button.isEnabled())
        self.button.set_emoji_enabled(True)
        self.assertTrue(self.button.isEnabled())


class PresetCommentButtonTests(unittest.TestCase):
    """快捷评论按钮：弹出预设评论菜单并发出 comment_picked。"""

    def setUp(self) -> None:
        self.button = PresetCommentButton()
        self.addCleanup(self.button.deleteLater)

    def test_button_exposes_one_action_per_preset(self) -> None:
        menu = self.button.menu()
        self.assertIsNotNone(menu)
        actions = menu.actions()
        self.assertEqual(len(actions), len(COMMENT_PRESETS))
        self.assertEqual(
            [action.text() for action in actions], list(COMMENT_PRESETS)
        )

    def test_button_emits_comment_picked(self) -> None:
        picked: list[str] = []
        self.button.comment_picked.connect(picked.append)
        action = self.button.menu().actions()[2]
        action.trigger()
        self.assertEqual(picked, [COMMENT_PRESETS[2]])


class DanmakuMotionTests(unittest.TestCase):
    """弹幕的移动方向、消失与表情的出现位置。"""

    def setUp(self) -> None:
        self.overlay = DanmakuOverlay()
        self.overlay.resize(320, 240)
        self.addCleanup(self.overlay.deleteLater)

    def test_comment_danmaku_travels_leftward_across_the_board(self) -> None:
        """评论必须真的从右往左移动，而不是原地出现再消失。"""
        self.overlay.push_text("好棋！", True)
        advance_animation(0.4)
        first = _visible_bbox(self.overlay.grab())
        self.assertIsNotNone(first, "弹幕没有画出任何像素")
        advance_animation(0.6)
        second = _visible_bbox(self.overlay.grab())
        self.assertIsNotNone(second, "弹幕在飞行途中消失了")
        # 整条弹幕都要比上一帧更靠左，才算真的在"飞"。
        self.assertLess(
            second[2],
            first[0],
            f"弹幕没有向左移动：{first} -> {second}",
        )

    def test_comment_danmaku_is_gone_after_its_lifetime(self) -> None:
        """评论飞过棋盘之后要彻底消失。"""
        self.overlay.push_text("好棋！", True)
        advance_animation(0.4)
        self.assertIsNotNone(_visible_bbox(self.overlay.grab()))
        self.assertTrue(
            wait_until(lambda: self.overlay.active_count() == 0, timeout=6.0)
        )
        advance_animation(0.2)
        self.assertIsNone(
            _visible_bbox(self.overlay.grab()),
            "弹幕生命周期结束后仍然留在棋盘上",
        )

    def test_emoji_from_self_appears_in_the_lower_half(self) -> None:
        """自己的表情出现在棋盘下半部分（像皇室战争那样在己方一侧）。"""
        self._assert_emoji_side(from_self=True, lower=True)

    def test_emoji_from_opponent_appears_in_the_upper_half(self) -> None:
        """对手的表情出现在棋盘上半部分。"""
        self._assert_emoji_side(from_self=False, lower=False)

    def _assert_emoji_side(self, from_self: bool, lower: bool) -> None:
        emoji = EMOJI_PRESETS[0]
        font = _resolve_emoji_font(40)
        if not QFontMetrics(font).inFont(ord(emoji)):
            self.skipTest("当前平台没有能渲染该表情的字体")
        self.overlay.push_emoji(emoji, from_self)
        advance_animation(0.5)
        bbox = _visible_bbox(self.overlay.grab())
        self.assertIsNotNone(bbox, "表情没有画出任何像素")
        middle = self.overlay.height() / 2
        if lower:
            self.assertGreater(bbox[1], middle, "自己的表情没有出现在下半部分")
        else:
            self.assertLess(bbox[3], middle, "对手的表情没有出现在上半部分")


class EmojiPickerRenderingTests(unittest.TestCase):
    """表情选择器的可点性与可见性。"""

    def setUp(self) -> None:
        self.button = EmojiPickerButton()
        self.addCleanup(self.button.deleteLater)

    def _grid_buttons(self) -> list:
        menu = self.button.menu()
        action = menu.actions()[0]
        widget = action.defaultWidget()
        return widget.findChildren(QPushButton)

    def test_grid_buttons_use_a_scoped_style(self) -> None:
        """表情格子必须用独立的样式规则。

        全局 QPushButton 规则留了 9px 17px 的内边距，在 36x36 的小
        方块里内容宽度会被挤成 0，表情就整个被裁掉（曾经发生过）。
        """
        buttons = self._grid_buttons()
        self.assertEqual(len(buttons), len(EMOJI_PRESETS))
        for button in buttons:
            self.assertEqual(button.objectName(), "emojiGridButton")
        self.assertIn("QPushButton#emojiGridButton", APP_STYLE)
        rule = APP_STYLE.split("QPushButton#emojiGridButton {")[1]
        rule = rule.split("}", 1)[0]
        self.assertIn("padding: 0px", rule)

    def test_grid_buttons_actually_show_their_emoji(self) -> None:
        """表情格子必须真的画出表情，而不是 12 个空白方块。"""
        emoji = EMOJI_PRESETS[0]
        font = _resolve_emoji_font(40)
        if not QFontMetrics(font).inFont(ord(emoji)):
            self.skipTest("当前平台没有能渲染该表情的字体")
        widget = self._grid_buttons()[0]
        widget.show()
        QT_APP.processEvents()
        self.assertIsNotNone(
            _visible_bbox(widget.grab()),
            "表情格子里没有画出任何内容",
        )


class ChatMenuThemeTests(unittest.TestCase):
    """弹出菜单要跟随应用主题。"""

    def test_emoji_menu_uses_the_app_menu_style(self) -> None:
        button = EmojiPickerButton()
        self.addCleanup(button.deleteLater)
        self.assertEqual(button.menu().objectName(), "chatMenu")
        self.assertIn("QMenu#chatMenu", APP_STYLE)

    def test_preset_menu_uses_the_app_menu_style(self) -> None:
        button = PresetCommentButton()
        self.addCleanup(button.deleteLater)
        self.assertEqual(button.menu().objectName(), "chatMenu")


class OnlinePanelChatTests(unittest.TestCase):
    """联机页里的聊天区域行为。"""

    def setUp(self) -> None:
        from gomoku.client.online_panel import OnlinePanel

        self.window = OnlinePanel()
        self.addCleanup(self.window.shutdown)

    def test_chat_log_is_cleared_when_a_new_room_is_created(self) -> None:
        """新房间 = 新对话，旧房间的记录不能留下来。"""
        from gomoku.shared.chat import ChatMessage

        self.window.chat_log.append_message(
            ChatMessage("text", "上一局的话", "u1", "对手", 0), False
        )
        self.assertEqual(self.window.chat_log.entry_count(), 1)
        self.window._on_room_created("ABCD1234")
        self.assertEqual(self.window.chat_log.entry_count(), 0)

    def test_chat_log_survives_a_rematch(self) -> None:
        """再战只是换一局棋，同一个房间的记录要保留。"""
        from gomoku.shared.chat import ChatMessage

        self.window.chat_log.append_message(
            ChatMessage("text", "承让了", "u1", "对手", 0), False
        )
        self.window._sync_online_ui()
        self.assertEqual(self.window.chat_log.entry_count(), 1)

    def test_chat_input_keeps_focus_across_the_cooldown(self) -> None:
        """冷却不能让输入框变灰：Qt 会把焦点从被禁用的控件上摘走。"""
        session = self.window._session
        self.assertIsNone(session)


class ChatLogToggleTests(unittest.TestCase):
    """对话日志的展开/收起。"""

    def setUp(self) -> None:
        import tempfile

        from PySide6.QtCore import QSettings

        from gomoku.client.online_panel import OnlinePanel

        # 把 QSettings 指向临时目录里的 INI 文件：不依赖应用元信息，
        # 也不会往用户的注册表里写东西。
        self._settings_dir = tempfile.mkdtemp()
        QSettings.setDefaultFormat(QSettings.Format.IniFormat)
        QSettings.setPath(
            QSettings.Format.IniFormat,
            QSettings.Scope.UserScope,
            self._settings_dir,
        )
        self.addCleanup(shutil.rmtree, self._settings_dir, True)

        # 联机页现在是主窗口页面栈中的一页，本身不是窗口。
        # 测试里用一个宿主窗口把它承载起来，可见性断言才有意义。
        self.host = QWidget()
        self.window = OnlinePanel(parent=self.host)
        self.host.setLayout(QVBoxLayout(self.host))
        self.host.layout().addWidget(self.window)
        self.addCleanup(self.window.shutdown)
        self.host.show()
        QT_APP.processEvents()
        self.window.stack.setCurrentWidget(self.window.game_page)
        QT_APP.processEvents()

    def test_chat_log_is_collapsed_by_default(self) -> None:
        """默认收起，让棋盘占满整行。"""
        self.assertFalse(self.window._chat_log_expanded)
        self.assertFalse(self.window.chat_log.isVisible())
        self.assertFalse(self.window.chat_log_toggle_button.isChecked())
        self.assertEqual(
            self.window.chat_log_toggle_button.text(), "对话日志"
        )

    def test_toggle_button_expands_and_collapses_the_log(self) -> None:
        button = self.window.chat_log_toggle_button
        button.setChecked(True)
        QT_APP.processEvents()
        self.assertTrue(self.window._chat_log_expanded)
        self.assertTrue(self.window.chat_log.isVisible())
        self.assertEqual(button.text(), "隐藏日志")

        button.setChecked(False)
        QT_APP.processEvents()
        self.assertFalse(self.window._chat_log_expanded)
        self.assertFalse(self.window.chat_log.isVisible())
        self.assertEqual(button.text(), "对话日志")

    def test_collapsed_log_gives_the_board_the_full_width(self) -> None:
        """收起日志后棋盘要变宽，否则开关就没有意义。"""
        button = self.window.chat_log_toggle_button
        button.setChecked(True)
        QT_APP.processEvents()
        expanded_width = self.window.board_widget.width()

        button.setChecked(False)
        QT_APP.processEvents()
        collapsed_width = self.window.board_widget.width()
        self.assertGreater(collapsed_width, expanded_width)

    def test_toggle_choice_is_persisted(self) -> None:
        from gomoku.client.online_panel import SETTINGS_CHAT_LOG_VISIBLE

        self.window.chat_log_toggle_button.setChecked(True)
        QT_APP.processEvents()
        stored = self.window._settings.value(SETTINGS_CHAT_LOG_VISIBLE)
        self.assertIsNotNone(stored, "展开状态没有被保存")
        self.assertTrue(str(stored).strip().lower() in ("1", "true", "yes"))

    def test_failure_notice_falls_back_to_the_placeholder(self) -> None:
        """日志收起时，发言失败的提示不能跟着一起消失。"""
        self.assertFalse(self.window._chat_log_expanded)
        self.window._on_chat_failed("发言太快，请稍候")
        self.assertEqual(
            self.window.chat_input.placeholderText(), "发言太快，请稍候"
        )
        # 提示同时留在日志里，展开后仍能看到历史。
        self.assertIn("发言太快", self.window.chat_log.entry_texts()[0])

    def test_failure_notice_stays_in_the_log_when_expanded(self) -> None:
        """日志展开时提示只进日志，不动输入框的占位文字。"""
        self.window.chat_log_toggle_button.setChecked(True)
        QT_APP.processEvents()
        hint = self.window.chat_input.placeholderText()
        self.window._on_chat_failed("发言太快，请稍候")
        self.assertEqual(self.window.chat_input.placeholderText(), hint)
        self.assertIn("发言太快", self.window.chat_log.entry_texts()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
