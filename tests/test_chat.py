"""聊天协议（gomoku.shared.chat）与联机会话表情/评论接口的测试。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.sessions.online import OnlineGameSession  # noqa: E402
from gomoku.shared.chat import (  # noqa: E402
    CHAT_BURST,
    CHAT_INTERVAL_SECONDS,
    CHAT_KIND_EMOJI,
    CHAT_KIND_TEXT,
    CHAT_KINDS,
    COMMENT_PRESETS,
    EMOJI_PRESETS,
    MAX_COMMENT_LENGTH,
    MAX_EMOJI_LENGTH,
    MAX_SENDER_NAME_LENGTH,
    ChatMessage,
    build_chat_payload,
    is_emoji_preset,
    normalize_chat_content,
    parse_chat_message,
)
from gomoku.shared.errors import ProtocolError  # noqa: E402
from gomoku.shared.rules import BOARD_SIZE  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_CHAT,
    SERVER_AUTHENTICATED,
    SERVER_CHAT,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    make_message,
)
from tests.fakes import FakeTransport  # noqa: E402

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def authenticated_session() -> tuple[FakeTransport, OnlineGameSession]:
    """构造一个已完成认证的联机会话（尚未进入对局）。"""
    transport = FakeTransport()
    session = OnlineGameSession(transport)
    transport.receive(
        make_message(
            SERVER_AUTHENTICATED,
            {"user": {"id": "u-self", "username": "tester"}},
        )
    )
    return transport, session


def in_game_session() -> tuple[FakeTransport, OnlineGameSession]:
    """构造一个已经收到权威快照（进入对局）的联机会话。

    只有进入对局之后才允许发言，服务器在等待对手时也会拒绝发言。
    """
    transport, session = authenticated_session()
    board = [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    transport.receive(
        make_message(
            SERVER_GAME_STATE,
            {
                "game_id": "g-1",
                "board": board,
                "current_player": "black",
                "your_color": "black",
                "move_no": 0,
                "status": "playing",
            },
        )
    )
    return transport, session


class ChatContentValidationTests(unittest.TestCase):
    """normalize_chat_content 的校验规则。"""

    def test_unknown_kind_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            normalize_chat_content("sticker", "😀")

    def test_non_string_content_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_TEXT, 123)
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_EMOJI, None)

    def test_non_preset_emoji_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_EMOJI, "😀")

    def test_empty_comment_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_TEXT, "")
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_TEXT, "   \n\t ")

    def test_overlong_comment_is_rejected(self) -> None:
        too_long = "评" * (MAX_COMMENT_LENGTH + 1)
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_TEXT, too_long)

    def test_valid_emoji_is_accepted(self) -> None:
        for emoji in EMOJI_PRESETS:
            self.assertEqual(
                normalize_chat_content(CHAT_KIND_EMOJI, emoji), emoji
            )

    def test_valid_comment_is_accepted_and_stripped(self) -> None:
        self.assertEqual(
            normalize_chat_content(CHAT_KIND_TEXT, "  好棋！  "), "好棋！"
        )

    def test_build_chat_payload_returns_kind_and_content(self) -> None:
        payload = build_chat_payload(CHAT_KIND_TEXT, " 你好 ")
        self.assertEqual(payload, {"kind": "text", "content": "你好"})
        with self.assertRaises(ProtocolError):
            build_chat_payload(CHAT_KIND_TEXT, " ")

    def test_is_emoji_preset_recognizes_supported_emoji(self) -> None:
        self.assertTrue(is_emoji_preset(EMOJI_PRESETS[0]))
        self.assertFalse(is_emoji_preset("😀"))

    def test_module_constants_keep_expected_values(self) -> None:
        self.assertEqual(CHAT_KINDS, (CHAT_KIND_EMOJI, CHAT_KIND_TEXT))
        self.assertEqual(len(COMMENT_PRESETS), 8)
        self.assertEqual(len(EMOJI_PRESETS), 12)
        self.assertEqual(MAX_EMOJI_LENGTH, 8)
        self.assertEqual(CHAT_BURST, 3)
        self.assertAlmostEqual(CHAT_INTERVAL_SECONDS, 0.8)


class ChatMessageParsingTests(unittest.TestCase):
    """parse_chat_message 对服务端下发消息的校验。"""

    def test_missing_sender_id_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_chat_message(
                {"kind": "text", "content": "好棋！", "sender_name": "甲"}
            )

    def test_bad_kind_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_chat_message(
                {
                    "kind": "sticker",
                    "content": "好棋！",
                    "sender_id": "u1",
                }
            )

    def test_empty_content_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_chat_message(
                {"kind": "text", "content": "", "sender_id": "u1"}
            )

    def test_non_mapping_payload_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_chat_message(["not", "a", "mapping"])

    def test_valid_payload_is_parsed(self) -> None:
        message = parse_chat_message(
            {
                "kind": "text",
                "content": "好棋！",
                "sender_id": "u1",
                "sender_name": "甲",
                "sent_at_ms": 1234,
            }
        )
        self.assertIsInstance(message, ChatMessage)
        self.assertEqual(message.kind, CHAT_KIND_TEXT)
        self.assertEqual(message.content, "好棋！")
        self.assertEqual(message.sender_id, "u1")
        self.assertEqual(message.sender_name, "甲")
        self.assertEqual(message.sent_at_ms, 1234)

    def test_invalid_sent_at_ms_falls_back_to_zero(self) -> None:
        message = parse_chat_message(
            {
                "kind": "text",
                "content": "好棋！",
                "sender_id": "u1",
                "sent_at_ms": True,
            }
        )
        self.assertEqual(message.sent_at_ms, 0)
        self.assertEqual(message.sender_name, "")


class OnlineSessionChatTests(unittest.TestCase):
    """OnlineGameSession 的表情/评论发送与接收接口。"""

    def test_send_chat_sends_client_chat_envelope(self) -> None:
        transport, session = in_game_session()
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertTrue(session.send_chat(CHAT_KIND_TEXT, "  好棋！ "))
        self.assertEqual(len(transport.sent_messages), 1)
        message = transport.sent_messages[0]
        self.assertEqual(message["type"], CLIENT_CHAT)
        self.assertEqual(
            message["payload"], {"kind": "text", "content": "好棋！"}
        )

    def test_send_emoji_wraps_send_chat(self) -> None:
        transport, session = in_game_session()
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        emoji = EMOJI_PRESETS[0]
        self.assertTrue(session.send_emoji(emoji))
        message = transport.sent_messages[-1]
        self.assertEqual(message["type"], CLIENT_CHAT)
        self.assertEqual(
            message["payload"], {"kind": "emoji", "content": emoji}
        )

    def test_send_comment_wraps_send_chat(self) -> None:
        transport, session = in_game_session()
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertTrue(session.send_comment("承让了"))
        message = transport.sent_messages[-1]
        self.assertEqual(message["type"], CLIENT_CHAT)
        self.assertEqual(
            message["payload"], {"kind": "text", "content": "承让了"}
        )

    def test_invalid_comment_is_rejected_and_emits_chat_failed(self) -> None:
        transport, session = in_game_session()
        failures: list[str] = []
        errors: list[str] = []
        session.chat_failed.connect(failures.append)
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertFalse(session.send_comment("   "))
        self.assertTrue(failures)
        # 发言问题只走 chat_failed，不污染对局的错误通道。
        self.assertEqual(errors, [])
        self.assertEqual(transport.sent_messages, [])

    def test_invalid_emoji_is_rejected_and_emits_chat_failed(self) -> None:
        transport, session = in_game_session()
        failures: list[str] = []
        errors: list[str] = []
        session.chat_failed.connect(failures.append)
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertFalse(session.send_emoji("😀"))
        self.assertTrue(failures)
        self.assertEqual(errors, [])
        self.assertEqual(transport.sent_messages, [])

    def test_chat_received_emits_parsed_message(self) -> None:
        transport, session = authenticated_session()
        received: list[ChatMessage] = []
        session.chat_received.connect(received.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        transport.receive(
            make_message(
                SERVER_CHAT,
                {
                    "kind": "emoji",
                    "content": EMOJI_PRESETS[1],
                    "sender_id": "u-other",
                    "sender_name": "对手",
                    "sent_at_ms": 42,
                },
            )
        )
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].kind, CHAT_KIND_EMOJI)
        self.assertEqual(received[0].content, EMOJI_PRESETS[1])
        self.assertEqual(received[0].sender_id, "u-other")
        self.assertEqual(received[0].sender_name, "对手")
        self.assertEqual(received[0].sent_at_ms, 42)

    def test_malformed_chat_frame_is_ignored(self) -> None:
        transport, session = authenticated_session()
        received: list[ChatMessage] = []
        errors: list[str] = []
        session.chat_received.connect(received.append)
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        # 缺少 sender_id 的非法帧：不能抛出异常，也不能触发信号。
        transport.receive(
            make_message(
                SERVER_CHAT,
                {"kind": "text", "content": "好棋！"},
            )
        )
        # kind 非法。
        transport.receive(
            make_message(
                SERVER_CHAT,
                {
                    "kind": "sticker",
                    "content": "好棋！",
                    "sender_id": "u-other",
                },
            )
        )
        # content 为空。
        transport.receive(
            make_message(
                SERVER_CHAT,
                {"kind": "text", "content": "", "sender_id": "u-other"},
            )
        )
        self.assertEqual(received, [])
        self.assertEqual(errors, [])

    def test_cooldown_blocks_second_send_inside_interval(self) -> None:
        transport, session = in_game_session()
        errors: list[str] = []
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertTrue(session.send_comment("第一条"))
        self.assertFalse(session.send_comment("第二条"))
        self.assertEqual(len(transport.sent_messages), 1)
        # 冷却期间静默拒绝，不弹错误提示。
        self.assertEqual(errors, [])

    def test_cooldown_expires_after_interval(self) -> None:
        transport, session = in_game_session()
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertTrue(session.send_comment("第一条"))
        # 把上次发言时间挪到窗口之前，模拟冷却已经过去。
        session._last_chat_at = time.monotonic() - CHAT_INTERVAL_SECONDS - 0.1
        self.assertTrue(session.send_comment("第二条"))
        self.assertEqual(len(transport.sent_messages), 2)

    def test_can_send_chat_requires_authenticated_session(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        self.assertFalse(session.can_send_chat)
        self.assertFalse(session.send_comment("未认证不能发言"))
        self.assertEqual(transport.sent_messages, [])

    def test_can_send_chat_is_false_while_waiting_for_opponent(self) -> None:
        transport, session = authenticated_session()
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        # 尚未收到任何权威快照，说明还没进入对局。
        self.assertIsNone(session.snapshot)
        self.assertFalse(session.can_send_chat)


class ChatSanitizationTests(unittest.TestCase):
    """评论文本的清洗规则：去掉控制/格式字符，折叠空白。"""

    def test_control_characters_are_removed(self) -> None:
        result = normalize_chat_content(CHAT_KIND_TEXT, "好棋\n\r\t！")
        self.assertEqual(result, "好棋 ！")

    def test_zero_width_and_bidi_characters_are_removed(self) -> None:
        # U+200B 零宽空格、U+202E 方向覆写：不可见，但能伪造日志。
        result = normalize_chat_content(CHAT_KIND_TEXT, "你好‮")
        self.assertEqual(result, "你好")

    def test_whitespace_runs_are_collapsed(self) -> None:
        result = normalize_chat_content(CHAT_KIND_TEXT, "好    棋")
        self.assertEqual(result, "好 棋")

    def test_comment_of_only_invisible_characters_is_rejected(self) -> None:
        # 真的用零宽/方向覆写这类不可见字符，而不是空串。
        invisible = chr(0x200B) + chr(0x202E)
        with self.assertRaises(ProtocolError):
            normalize_chat_content(CHAT_KIND_TEXT, invisible)

    def test_sender_name_is_trimmed_and_capped(self) -> None:
        message = parse_chat_message(
            {
                "kind": CHAT_KIND_TEXT,
                "content": "好棋",
                "sender_id": "u1",
                "sender_name": "  " + "名" * 80 + "  ",
            }
        )
        self.assertEqual(len(message.sender_name), MAX_SENDER_NAME_LENGTH)

    def test_emoji_presets_are_single_codepoint_without_variation_selector(
        self,
    ) -> None:
        for emoji in EMOJI_PRESETS:
            with self.subTest(emoji=emoji):
                self.assertEqual(len(emoji), 1)
                self.assertNotIn("️", emoji)


class ChatErrorRoutingTests(unittest.TestCase):
    """服务器拒绝发言时的错误归属。"""

    def test_server_rejection_routes_to_chat_failed(self) -> None:
        transport, session = authenticated_session()
        failures: list[str] = []
        errors: list[str] = []
        session.chat_failed.connect(failures.append)
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        transport.receive(
            make_message(
                SERVER_ERROR,
                {"reason": "发言太快，请稍候"},
                request_id="chat-42",
            )
        )
        # request_id 未登记在发言队列里时，仍然要提示用户。
        self.assertEqual(failures, [])
        self.assertEqual(errors, ["发言太快，请稍候"])

    def test_registered_chat_request_rejection_routes_to_chat_failed(self) -> None:
        transport, session = in_game_session()
        failures: list[str] = []
        errors: list[str] = []
        session.chat_failed.connect(failures.append)
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        # 先发出一次发言，记录它的 request_id。
        session._last_chat_at = None
        session.send_comment("好棋")
        request_id = transport.sent_messages[-1]["request_id"]

        transport.receive(
            make_message(
                SERVER_ERROR,
                {"reason": "发言太快，请稍候"},
                request_id=request_id,
            )
        )
        self.assertEqual(failures, ["发言太快，请稍候"])
        # 不应该混进对局的错误通道。
        self.assertEqual(errors, [])


class ProtocolForwardCompatibilityTests(unittest.TestCase):
    """未知的服务端消息类型应当被安静忽略。"""

    def test_unknown_server_message_type_is_ignored(self) -> None:
        transport, session = authenticated_session()
        errors: list[str] = []
        received: list[str] = []
        session.error.connect(errors.append)
        session.chat_received.connect(received.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        # 服务端新增了一种本客户端还不认识的消息类型。
        transport.receive(
            {
                "protocol": 1,
                "type": "room_reaction",
                "payload": {"emoji": "👋"},
            }
        )
        # 既不能当成协议错误惊动玩家，也不能当成聊天消息处理。
        self.assertEqual(errors, [])
        self.assertEqual(received, [])

    def test_malformed_known_message_still_reports_an_error(self) -> None:
        """真正畸形的消息仍然要报错，不能被"忽略未知类型"顺手放过。"""
        transport, session = authenticated_session()
        errors: list[str] = []
        session.error.connect(errors.append)
        self.addCleanup(session.shutdown)
        self.addCleanup(transport.close)

        transport.receive(
            {
                "protocol": 1,
                "type": "game_state",
                "payload": "这不是一个对象",
            }
        )
        self.assertTrue(errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
