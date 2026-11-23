"""“记住密码”凭据保存：DPAPI 加密与 QSettings 读写。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings  # noqa: E402

from gomoku.client.credential_store import (  # noqa: E402
    clear_login,
    load_login,
    protect_password,
    save_login,
    unprotect_password,
)


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.settings = QSettings(
            os.path.join(self._tmpdir.name, "settings.ini"),
            QSettings.Format.IniFormat,
        )

    def tearDown(self) -> None:
        self.settings.clear()
        self.settings.sync()
        self._tmpdir.cleanup()

    @unittest.skipUnless(
        sys.platform == "win32", "DPAPI 仅在 Windows 上可用"
    )
    def test_password_roundtrip_is_encrypted(self) -> None:
        token = protect_password("correct horse battery")
        self.assertIsNotNone(token)
        assert token is not None
        self.assertTrue(token.startswith("dpapi:"))
        self.assertNotIn("correct horse battery", token)
        self.assertEqual(
            unprotect_password(token), "correct horse battery"
        )

    def test_save_and_load_login(self) -> None:
        saved = save_login(self.settings, "alice", "s3cret-pass")
        remember, username, password = load_login(self.settings)
        self.assertTrue(remember)
        self.assertEqual(username, "alice")
        if sys.platform == "win32":
            self.assertTrue(saved)
            self.assertEqual(password, "s3cret-pass")
        else:
            self.assertFalse(saved)
            self.assertEqual(password, "")

    def test_clear_removes_saved_login(self) -> None:
        save_login(self.settings, "alice", "s3cret-pass")
        clear_login(self.settings)
        remember, username, password = load_login(self.settings)
        self.assertFalse(remember)
        self.assertEqual(username, "")
        self.assertEqual(password, "")
