"""服务端测试夹具：临时 SQLite 和 TestClient。"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path

# 从项目根目录运行测试；这里再显式加入一次，兼容直接执行测试文件。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from server.app.config import Settings  # noqa: E402
from server.app.main import create_app  # noqa: E402
from server.app.security import LoginBackoff  # noqa: E402


class ServerTestBase(unittest.TestCase):
    """每个测试拥有独立临时数据库、独立 FastAPI app 和 TestClient。"""

    #: 掉线判负宽限；需要验证“逃跑”的用例可以调短。
    disconnect_forfeit_seconds: float = 20.0
    #: 公平性/资源限制；具体用例可覆盖。
    undo_limit_per_game: int = 3
    undo_cooldown_seconds: float = 0.0
    max_spectators_per_room: int = 50
    max_connections_per_ip: int = 200
    ws_command_burst: int = 40
    ws_command_window_seconds: float = 10.0

    def setUp(self) -> None:
        # Windows 上 SQLite 文件句柄的释放有延迟，临时目录偶尔会删不掉
        # （WinError 32），ignore_cleanup_errors 让清理失败不影响用例结果。
        self._tmpdir = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        db_path = Path(self._tmpdir.name) / "test.db"
        self.settings = Settings(
            environment="test",
            database_url=f"sqlite+aiosqlite:///{db_path.as_posix()}",
            jwt_secret="test-secret-" + "x" * 48,
            access_token_minutes=15,
            refresh_token_days=30,
            turn_seconds=30,
            ws_auth_timeout_seconds=2.0,
            disconnect_forfeit_seconds=self.disconnect_forfeit_seconds,
            log_level="WARNING",
            undo_limit_per_game=self.undo_limit_per_game,
            undo_cooldown_seconds=self.undo_cooldown_seconds,
            max_spectators_per_room=self.max_spectators_per_room,
            max_connections_per_ip=self.max_connections_per_ip,
            ws_command_burst=self.ws_command_burst,
            ws_command_window_seconds=self.ws_command_window_seconds,
        )
        self.app = create_app(self.settings)
        self._client_cm = TestClient(self.app)
        self.client = self._client_cm.__enter__()
        # 测试里把登录退避的等待时间清零：退避的逻辑由单独的单元测试
        # 覆盖，接口测试不应该真的睡上好几秒。
        self.app.state.login_backoff = LoginBackoff(
            base_delay_seconds=0.0, max_delay_seconds=0.0
        )

    def tearDown(self) -> None:
        try:
            self._client_cm.__exit__(None, None, None)
        finally:
            self._release_database()
            self._cleanup_tmpdir()

    def _release_database(self) -> None:
        """释放数据库连接。

        Windows 上 SQLite 文件在被占用时无法删除，TestClient 退出后连接
        池里的句柄可能还没完全释放。这里再显式回收一次引擎，降低
        临时目录清理失败的概率。
        """
        engine = getattr(self.app.state, "engine", None)
        if engine is None:
            return
        try:
            asyncio.run(engine.dispose())
        except Exception:  # pragma: no cover - 清理失败不应影响用例结果
            pass

    def _cleanup_tmpdir(self) -> None:
        """删除临时目录；文件仍被占用时稍作重试。"""
        for attempt in range(10):
            try:
                self._tmpdir.cleanup()
                return
            except PermissionError:
                if attempt == 9:
                    # ignore_cleanup_errors 已经让清理失败不再是硬错误，
                    # 到这里直接放弃，避免整个用例失败。
                    return
                time.sleep(0.1)

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
