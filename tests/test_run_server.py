"""run_server 启动脚本的纯 Python 测试（不启动真实服务）。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import run_server  # noqa: E402


class SecretTests(unittest.TestCase):
    def test_secret_is_created_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "jwt_secret"
            first = run_server._read_or_create_secret(path)
            second = run_server._read_or_create_secret(path)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, second)


class BuildEnvTests(unittest.TestCase):
    def _build(
        self, environment: str, *, trust_proxy: bool | None
    ) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch.object(
                    run_server, "PROD_SECRET_FILE", Path(tmp) / "prod"
                ),
                mock.patch.object(
                    run_server, "DEV_SECRET_FILE", Path(tmp) / "dev"
                ),
            ):
                return run_server._build_env(
                    environment, trust_proxy=trust_proxy
                )

    def test_production_defaults_to_trusted_proxy(self) -> None:
        env = self._build("production", trust_proxy=None)
        self.assertEqual(env["GOMOKU_SERVER_ENV"], "production")
        self.assertEqual(env["GOMOKU_TRUST_PROXY_HEADERS"], "1")
        self.assertGreaterEqual(len(env["GOMOKU_JWT_SECRET"]), 32)
        self.assertIn(str(PROJECT_ROOT), env["PYTHONPATH"])

    def test_production_can_disable_trusted_proxy(self) -> None:
        env = self._build("production", trust_proxy=False)
        self.assertEqual(env["GOMOKU_TRUST_PROXY_HEADERS"], "0")

    def test_development_does_not_trust_proxy_by_default(self) -> None:
        env = self._build("development", trust_proxy=None)
        self.assertEqual(env["GOMOKU_SERVER_ENV"], "development")
        self.assertNotIn("GOMOKU_TRUST_PROXY_HEADERS", env)

    def test_existing_secret_is_kept(self) -> None:
        existing = "x" * 32
        with mock.patch.dict(
            os.environ, {"GOMOKU_JWT_SECRET": existing}, clear=True
        ):
            env = run_server._build_env("production", trust_proxy=None)
        self.assertEqual(env["GOMOKU_JWT_SECRET"], existing)
