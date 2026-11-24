"""服务端配置项测试：房间码长度/字母表等环境变量解析。"""

from __future__ import annotations

import unittest

from gomoku.shared.errors import ConfigurationError
from server.app.config import DEFAULT_ROOM_CODE_ALPHABET, Settings


class RoomCodeSettingsTests(unittest.TestCase):
    """room_code_length / room_code_alphabet 必须真正可配置。"""

    def _env(self, **extra: str) -> dict[str, str]:
        env = {"GOMOKU_SERVER_ENV": "test"}
        env.update(extra)
        return env

    def test_defaults(self) -> None:
        settings = Settings.from_env(self._env())
        self.assertEqual(settings.room_code_length, 8)
        self.assertEqual(
            settings.room_code_alphabet, DEFAULT_ROOM_CODE_ALPHABET
        )

    def test_room_code_length_from_env(self) -> None:
        settings = Settings.from_env(
            self._env(GOMOKU_ROOM_CODE_LENGTH="12")
        )
        self.assertEqual(settings.room_code_length, 12)

    def test_invalid_room_code_length_is_rejected(self) -> None:
        for value in ("3", "17", "0", "abc", ""):
            with self.subTest(value=value):
                with self.assertRaises(ConfigurationError):
                    Settings.from_env(
                        self._env(GOMOKU_ROOM_CODE_LENGTH=value)
                    )

    def test_room_code_alphabet_from_env(self) -> None:
        settings = Settings.from_env(
            self._env(GOMOKU_ROOM_CODE_ALPHABET="ABCDEFGH")
        )
        self.assertEqual(settings.room_code_alphabet, "ABCDEFGH")

    def test_room_code_alphabet_and_length_together(self) -> None:
        settings = Settings.from_env(
            self._env(
                GOMOKU_ROOM_CODE_ALPHABET="ABCDE",
                GOMOKU_ROOM_CODE_LENGTH="5",
            )
        )
        self.assertEqual(settings.room_code_alphabet, "ABCDE")
        self.assertEqual(settings.room_code_length, 5)

    def test_invalid_room_code_alphabet_is_rejected(self) -> None:
        # 空字母表。
        with self.assertRaises(ConfigurationError):
            Settings.from_env(self._env(GOMOKU_ROOM_CODE_ALPHABET="  "))
        # 重复字符。
        with self.assertRaises(ConfigurationError):
            Settings.from_env(self._env(GOMOKU_ROOM_CODE_ALPHABET="AABA"))
        # 字符数少于房间码长度。
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                self._env(
                    GOMOKU_ROOM_CODE_ALPHABET="ABCD",
                    GOMOKU_ROOM_CODE_LENGTH="8",
                )
            )

    def test_default_alphabet_supports_default_length(self) -> None:
        self.assertGreaterEqual(
            len(DEFAULT_ROOM_CODE_ALPHABET),
            Settings.from_env(self._env()).room_code_length,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
