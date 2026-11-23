"""“记住密码”凭据保存：DPAPI 加密与 QSettings 读写。"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QSettings  # noqa: E402

from gomoku.client.credential_store import (  # noqa: E402
    clear_login,
    clear_refresh_token,
    load_login,
    load_refresh_token,
    protect_password,
    protect_refresh_token,
    save_login,
    save_refresh_token,
    unprotect_password,
    unprotect_refresh_token,
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

    @unittest.skipUnless(
        sys.platform == "win32", "DPAPI 仅在 Windows 上可用"
    )
    def test_refresh_token_roundtrip_is_encrypted(self) -> None:
        token = protect_refresh_token("refresh-token-value")
        self.assertIsNotNone(token)
        assert token is not None
        self.assertTrue(token.startswith("dpapi:"))
        self.assertNotIn("refresh-token-value", token)
        self.assertEqual(
            unprotect_refresh_token(token), "refresh-token-value"
        )

    def test_save_and_load_refresh_token(self) -> None:
        saved = save_refresh_token(self.settings, "refresh-pass")
        loaded = load_refresh_token(self.settings)
        if sys.platform == "win32":
            self.assertTrue(saved)
            self.assertEqual(loaded, "refresh-pass")
        else:
            self.assertFalse(saved)
            self.assertEqual(loaded, "")

    def test_clear_refresh_token_removes_value(self) -> None:
        save_refresh_token(self.settings, "refresh-pass")
        clear_refresh_token(self.settings)
        self.assertEqual(load_refresh_token(self.settings), "")

    def test_undecryptable_refresh_token_is_cleared(self) -> None:
        self.settings.setValue("online/refresh_token", "dpapi:bm90LWRwYXBp")
        self.settings.sync()
        self.assertEqual(load_refresh_token(self.settings), "")
        self.assertEqual(
            self.settings.value("online/refresh_token", "", type=str), ""
        )

    def test_clear_login_removes_saved_refresh_token(self) -> None:
        save_refresh_token(self.settings, "refresh-pass")
        clear_login(self.settings)
        self.assertEqual(load_refresh_token(self.settings), "")

    def test_clear_removes_saved_login(self) -> None:
        save_login(self.settings, "alice", "s3cret-pass")
        clear_login(self.settings)
        remember, username, password = load_login(self.settings)
        self.assertFalse(remember)
        self.assertEqual(username, "")
        self.assertEqual(password, "")

    def test_undecryptable_password_is_treated_as_not_remembered(self) -> None:
        """密文失效时不能显示"已记住"却拿不出密码。

        例如 Windows 账户密钥轮换后，原来的 DPAPI 密文再也解不开。
        """
        self.settings.setValue("online/remember_password", True)
        self.settings.setValue("online/saved_username", "alice")
        self.settings.setValue("online/saved_password", "dpapi:bm90LWRwYXBp")
        self.settings.sync()

        remember, username, password = load_login(self.settings)
        self.assertFalse(remember)
        self.assertEqual(username, "alice")
        self.assertEqual(password, "")
        # 失效的密文要被删掉，别让界面下次继续误判。
        self.assertEqual(
            self.settings.value("online/saved_password", "", type=str), ""
        )

    def test_non_windows_platform_does_not_save_password(self) -> None:
        """非 Windows 平台不保存密码，只记住用户名。

        模块必须在非 Windows 上也能导入（客户端在 Linux/macOS 上仍要能
        启动），只是不能保存密码。
        """
        code = textwrap.dedent(
            """
            import os
            import sys
            import tempfile

            sys.path.insert(0, {root!r})
            from PySide6.QtCore import QSettings

            sys.platform = "linux"

            from gomoku.client.credential_store import (
                load_login,
                protect_password,
                save_login,
                unprotect_password,
            )

            assert protect_password("secret") is None
            assert unprotect_password("dpapi:AAAA") is None

            path = os.path.join(tempfile.mkdtemp(), "settings.ini")
            settings = QSettings(path, QSettings.Format.IniFormat)
            assert save_login(settings, "alice", "secret") is False
            remember, username, password = load_login(settings)
            assert remember is True
            assert username == "alice"
            assert password == ""
            print("ok")
            """
        ).format(root=str(PROJECT_ROOT))
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)
