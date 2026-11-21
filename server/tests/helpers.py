"""服务端测试夹具：临时 SQLite 和 TestClient。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

# 从项目根目录运行测试；这里再显式加入一次，兼容直接执行测试文件。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient

from server.app.config import Settings
from server.app.main import create_app


class ServerTestBase(unittest.TestCase):
    """每个测试拥有独立临时数据库、独立 FastAPI app 和 TestClient。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = Path(self._tmpdir.name) / "test.db"
        self.settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
            jwt_secret="test-secret-" + "x" * 48,
            access_token_minutes=15,
            refresh_token_days=30,
            turn_seconds=30,
            ws_auth_timeout_seconds=2.0,
        )
        self.app = create_app(self.settings)
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()

    def tearDown(self) -> None:
        try:
            self._client_cm.__exit__(None, None, None)
        finally:
            self._tmpdir.cleanup()

    def register(
        self,
        username: str,
        password: str = "correct horse battery",
    ):
        return self.client.post(
            "/api/v1/auth/register",
            json={
                "username": username,
                "password": password,
            },
        )

    def auth_headers(self, access_token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {access_token}"}
