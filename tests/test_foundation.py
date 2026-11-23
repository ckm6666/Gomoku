"""配置、错误与联机协议草稿的纯 Python 测试（不依赖 Qt 事件循环）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client.auth import AuthSession  # noqa: E402
from gomoku.client.config import (  # noqa: E402
    DEFAULT_API_BASE_URL,
    DEFAULT_WS_BASE_URL,
    AppConfig,
)
from gomoku.shared.capabilities import (  # noqa: E402
    MAX_CAPABILITIES,
    normalize_capabilities,
)
from gomoku.shared.errors import ConfigurationError, ProtocolError  # noqa: E402
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_MOVE_INTENT,
    PROTOCOL_VERSION,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_QUEUE_STATUS,
    make_message,
    make_move_intent,
    parse_game_state,
    parse_message,
)
from gomoku.shared.rules import BOARD_SIZE, Player  # noqa: E402
from gomoku.shared.time_control import (  # noqa: E402
    MODE_FISCHER,
    MODE_PER_MOVE,
    TimeControl,
)


def blank_values() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


class ConfigTests(unittest.TestCase):
    """配置只接受非敏感环境变量并校验基本格式。"""

    def test_defaults_are_offline(self) -> None:
        config = AppConfig.from_env({})
        self.assertEqual(config.environment, "development")
        self.assertFalse(config.online_configured)
        self.assertIsNone(config.api_base_url)
        self.assertIsNone(config.ws_base_url)

    def test_valid_environment(self) -> None:
        config = AppConfig.from_env(
            {
                "GOMOKU_ENV": "staging",
                "GOMOKU_API_BASE_URL": "https://api.example.test/",
                "GOMOKU_WS_BASE_URL": "wss://api.example.test",
                "GOMOKU_REQUEST_TIMEOUT": "7.5",
                "GOMOKU_LOG_LEVEL": "debug",
            }
        )
        self.assertEqual(config.environment, "staging")
        self.assertEqual(config.api_base_url, "https://api.example.test")
        self.assertEqual(config.ws_base_url, "wss://api.example.test")
        self.assertEqual(config.request_timeout_seconds, 7.5)
        self.assertEqual(config.log_level, "DEBUG")
        self.assertTrue(config.online_configured)

    def test_frozen_build_defaults_to_public_server(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            config = AppConfig.from_env({})
        self.assertEqual(config.api_base_url, DEFAULT_API_BASE_URL)
        self.assertEqual(config.ws_base_url, DEFAULT_WS_BASE_URL)
        self.assertTrue(config.online_configured)

    def test_frozen_build_allows_env_override(self) -> None:
        with mock.patch.object(sys, "frozen", True, create=True):
            config = AppConfig.from_env(
                {
                    "GOMOKU_API_BASE_URL": "https://api.example.test",
                    "GOMOKU_WS_BASE_URL": "wss://api.example.test",
                }
            )
        self.assertEqual(config.api_base_url, "https://api.example.test")
        self.assertEqual(config.ws_base_url, "wss://api.example.test")

    def test_invalid_values_raise(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"GOMOKU_API_BASE_URL": "ftp://bad"})
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"GOMOKU_LOG_LEVEL": "VERBOSE"})
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"GOMOKU_REQUEST_TIMEOUT": "0"})


class AuthSessionTests(unittest.TestCase):
    def test_parse_auth_session(self) -> None:
        session = AuthSession.from_response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 900,
                "user": {"id": "u1", "username": "tester"},
            }
        )
        self.assertEqual(session.user.username, "tester")
        self.assertEqual(session.expires_in, 900)
        self.assertFalse(session.is_access_expired())
        # 旧服务器不带积分字段时使用安全默认值。
        self.assertEqual(session.user.rating, 1200)
        self.assertEqual(session.user.rated_games, 0)

    def test_parse_rating_fields(self) -> None:
        session = AuthSession.from_response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 900,
                "user": {
                    "id": "u1",
                    "username": "tester",
                    "rating": 1345,
                    "peak_rating": 1400,
                    "rated_games": 12,
                    "wins": 7,
                    "losses": 4,
                    "draws": 1,
                    "rank_name": "黄金",
                    "rank_key": "gold",
                    "next_rank_rating": 1350,
                    "rank_progress": 63,
                    "win_rate": 58.3,
                },
            }
        )
        self.assertEqual(session.user.rating, 1345)
        self.assertEqual(session.user.peak_rating, 1400)
        self.assertEqual(session.user.wins, 7)
        self.assertEqual(session.user.rank_name, "黄金")
        self.assertEqual(session.user.next_rank_rating, 1350)
        self.assertAlmostEqual(session.user.win_rate, 58.3)


class ProtocolTests(unittest.TestCase):
    """协议 envelope 与服务器快照解析必须有严格边界。"""

    def test_envelope_roundtrip(self) -> None:
        message = make_message(
            CLIENT_MOVE_INTENT,
            make_move_intent(7, 8),
            request_id="req-1",
        )
        self.assertEqual(message["protocol"], PROTOCOL_VERSION)
        parsed = parse_message(message)
        self.assertEqual(parsed["type"], CLIENT_MOVE_INTENT)
        self.assertEqual(parsed["request_id"], "req-1")
        self.assertEqual(parsed["payload"], {"row": 7, "col": 8})

    def test_bad_envelopes_are_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_message({"protocol": 99, "type": "x", "payload": {}})
        with self.assertRaises(ProtocolError):
            parse_message({"protocol": 1, "type": "", "payload": {}})
        with self.assertRaises(ProtocolError):
            parse_message({"protocol": 1, "type": "unknown", "payload": {}})
        with self.assertRaises(ProtocolError):
            make_move_intent(-1, 0)

    def test_game_state_parse(self) -> None:
        board = blank_values()
        board[7][7] = "black"
        snapshot = parse_game_state(
            {
                "game_id": "g-1",
                "board": board,
                "current_player": "white",
                "your_color": "black",
                "move_no": 1,
                "status": "playing",
                "last_move": {"row": 7, "col": 7, "player": "black"},
                "winning_line": [],
            }
        )
        self.assertEqual(snapshot.board[7][7], Player.BLACK)
        self.assertEqual(snapshot.current_player, Player.WHITE)
        self.assertEqual(snapshot.your_color, Player.BLACK)
        self.assertFalse(snapshot.is_game_over)

    def test_game_state_undo_metadata_parse(self) -> None:
        payload = {
            "game_id": "g-undo",
            "board": blank_values(),
            "current_player": "black",
            "your_color": "black",
            "move_no": 2,
            "status": "playing",
            "state_version": 5,
            "result_reason": "escape",
            "undo_requester_id": "u2",
            "undo_remaining": 2,
            "undo_limit": 3,
            "undo_cooldown_until_ms": 123456,
            "undo_cooldown_seconds": 10,
        }
        snapshot = parse_game_state(payload)
        self.assertEqual(snapshot.state_version, 5)
        self.assertEqual(snapshot.result_reason, "escape")
        self.assertEqual(snapshot.undo_requester_id, "u2")
        self.assertEqual(snapshot.undo_remaining, 2)
        self.assertEqual(snapshot.undo_limit, 3)
        self.assertEqual(snapshot.undo_cooldown_until_ms, 123456)
        self.assertEqual(snapshot.undo_cooldown_seconds, 10)

        bad_version = dict(payload)
        bad_version["state_version"] = -1
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_version)

        bad_reason = dict(payload)
        bad_reason["result_reason"] = 5
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_reason)

    def test_game_state_rejects_bad_board_and_status(self) -> None:
        payload = {
            "game_id": "g-1",
            "board": blank_values(),
            "current_player": "black",
            "your_color": "black",
            "move_no": 0,
            "status": "playing",
        }
        bad_board = dict(payload)
        bad_board["board"] = blank_values()[:-1]
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_board)

        bad_status = dict(payload)
        bad_status["status"] = "unknown"
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_status)

    def test_time_control_payload_roundtrip(self) -> None:
        per_move = TimeControl.per_move(30)
        self.assertEqual(per_move.mode, MODE_PER_MOVE)
        self.assertEqual(
            TimeControl.from_payload(per_move.to_payload()), per_move
        )
        fischer = TimeControl.fischer(300, 3)
        self.assertEqual(fischer.mode, MODE_FISCHER)
        self.assertEqual(
            TimeControl.from_payload(fischer.to_payload()), fischer
        )
        self.assertEqual(fischer.initial_remaining_ms(), 300_000)
        self.assertIn("Fischer", fischer.label())
        with self.assertRaises(ProtocolError):
            TimeControl.from_payload({"mode": "unknown"})
        with self.assertRaises(ProtocolError):
            TimeControl.from_payload(
                {"mode": "fischer", "main_seconds": 10}
            )
        with self.assertRaises(ProtocolError):
            TimeControl.per_move(1)

    def test_game_state_parses_fischer_clocks(self) -> None:
        snapshot = parse_game_state(
            {
                "game_id": "g-clocks",
                "board": blank_values(),
                "current_player": "white",
                "your_color": "black",
                "move_no": 1,
                "status": "playing",
                "time_control": {
                    "mode": "fischer",
                    "main_seconds": 300,
                    "increment_seconds": 3,
                },
                "black_remaining_ms": 301_500,
                "white_remaining_ms": 299_000,
                "turn_deadline_ms": 1_000_000,
                "server_now_ms": 900_000,
            }
        )
        self.assertIsNotNone(snapshot.time_control)
        self.assertTrue(snapshot.time_control.is_fischer)
        self.assertEqual(snapshot.black_remaining_ms, 301_500)
        self.assertEqual(snapshot.white_remaining_ms, 299_000)
        self.assertIsNone(snapshot.move_timeout_seconds)

        legacy = parse_game_state(
            {
                "game_id": "g-legacy",
                "board": blank_values(),
                "current_player": "black",
                "your_color": "black",
                "move_no": 0,
                "status": "playing",
                "move_timeout_seconds": 45,
            }
        )
        self.assertIsNotNone(legacy.time_control)
        self.assertEqual(legacy.time_control.turn_seconds, 45)

    def test_rated_game_state_parse(self) -> None:
        snapshot = parse_game_state(
            {
                "game_id": "g-rated",
                "board": blank_values(),
                "current_player": "black",
                "your_color": "white",
                "move_no": 6,
                "status": "black_won",
                "rated": True,
                "room_code": "",
                "your_rating_before": 1190,
                "your_rating_after": 1178,
                "opponent_rating_before": 1230,
                "opponent_rating_after": 1242,
                "rating_delta": -12,
                "black_avatar_version": 3,
                "white_avatar_version": 2,
                "black_achievements": [
                    "rank_gold",
                    "first_win",
                    "unknown_key",
                    "rank_gold",
                ],
                "white_achievements": ["streak_3"],
                "your_new_achievements": ["streak_3", "nope"],
            }
        )
        self.assertTrue(snapshot.rated)
        self.assertEqual(snapshot.room_code, "")
        self.assertEqual(snapshot.your_rating_before, 1190)
        self.assertEqual(snapshot.your_rating_after, 1178)
        self.assertEqual(snapshot.rating_delta, -12)
        self.assertEqual(snapshot.black_avatar_version, 3)
        self.assertEqual(snapshot.white_avatar_version, 2)
        self.assertEqual(
            snapshot.black_achievements, ("first_win", "rank_gold")
        )
        self.assertEqual(snapshot.white_achievements, ("streak_3",))
        self.assertEqual(
            snapshot.your_new_achievements, ("streak_3",)
        )

    def test_rated_game_state_rejects_bad_fields(self) -> None:
        payload = {
            "game_id": "g-rated",
            "board": blank_values(),
            "current_player": "black",
            "your_color": "white",
            "move_no": 0,
            "status": "playing",
        }
        bad_rated = dict(payload)
        bad_rated["rated"] = "yes"
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_rated)

        bad_rating = dict(payload)
        bad_rating["your_rating_before"] = -5
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_rating)

        bad_delta = dict(payload)
        bad_delta["rating_delta"] = "plus"
        with self.assertRaises(ProtocolError):
            parse_game_state(bad_delta)

    def test_capabilities_are_normalized(self) -> None:
        self.assertEqual(
            normalize_capabilities(
                ["spectate", " spectate ", "undo_quota"]
            ),
            ("spectate", "undo_quota"),
        )
        self.assertEqual(normalize_capabilities(None), ())
        with self.assertRaises(ProtocolError):
            normalize_capabilities("spectate")
        with self.assertRaises(ProtocolError):
            normalize_capabilities([1])
        with self.assertRaises(ProtocolError):
            normalize_capabilities(["x"] * (MAX_CAPABILITIES + 1))

    def test_message_type_constant_is_exported(self) -> None:
        self.assertEqual(SERVER_GAME_STATE, "game_state")
        self.assertEqual(SERVER_QUEUE_STATUS, "queue_status")
        self.assertEqual(SERVER_MATCH_FOUND, "match_found")


if __name__ == "__main__":
    unittest.main(verbosity=2)
