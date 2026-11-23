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

    def test_module_imports_without_wintypes(self) -> None:
        """非 Windows 上 ctypes.wintypes 不可用，模块必须仍能导入。

        ctypes.wintypes 在非 Windows 上会因为 VARIANT_BOOL 的 'v' 类型
        直接抛 ValueError；如果在模块级无条件导入它，整个客户端在非
        Windows 平台上都会启动失败。
        """
        code = textwrap.dedent(
            """
            import sys

            sys.path.insert(0, {root!r})
            from PySide6.QtCore import QSettings  # 先加载 Qt，避免受平台影响

            sys.platform = "linux"

            class Blocker:
                \"\"\"模拟非 Windows 上 import ctypes.wintypes 直接失败。\"\"\"

                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "ctypes.wintypes":
                        raise ValueError("_type_ 'v' not supported")
                    return None

            sys.meta_path.insert(0, Blocker())

            from gomoku.client.credential_store import (
                protect_password,
                unprotect_password,
            )

            assert protect_password("secret") is None
            assert unprotect_password("dpapi:AAAA") is None
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
