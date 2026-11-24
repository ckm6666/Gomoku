"""关闭/清理路径的异常必须留下痕迹，不能被静默吞掉。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server.app import security
from server.app.config import Settings
from server.app.main import create_app
from server.app.ws import endpoint
from server.app.ws.entities import ClientConnection

_SECRET = "test-secret-" + "x" * 48


class _FailingWebSocket:
    """close() 必定抛异常的假 WebSocket。"""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        raise RuntimeError("WebSocket 已断开")


class _FakePool:
    """假的线程池：记录 shutdown 调用，可以按需抛异常。"""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[bool] = []

    def shutdown(self, wait: bool = False) -> None:
        self.calls.append(wait)
        if self.error is not None:
            raise self.error


class PasswordPoolShutdownTests(unittest.TestCase):
    """密码线程池关闭失败要写日志。"""

    def test_shutdown_failure_is_logged(self) -> None:
        original_verify = security._verify_pool
        original_hash = security._hash_pool
        verify_pool = _FakePool(RuntimeError("关闭失败"))
        hash_pool = _FakePool()
        security._verify_pool = verify_pool
        security._hash_pool = hash_pool
        try:
            with self.assertLogs(
                "server.app.security", level="DEBUG"
            ) as captured:
                security.shutdown_password_pools()
        finally:
            security._verify_pool = original_verify
            security._hash_pool = original_hash
        self.assertTrue(
            any("关闭密码线程池失败" in line for line in captured.output),
            captured.output,
        )
        # 失败的池也要被调用过，不能直接跳过。
        self.assertEqual(len(verify_pool.calls), 1)
        self.assertEqual(len(hash_pool.calls), 1)

    def test_successful_shutdown_is_silent(self) -> None:
        original_verify = security._verify_pool
        original_hash = security._hash_pool
        verify_pool = _FakePool()
        hash_pool = _FakePool()
        security._verify_pool = verify_pool
        security._hash_pool = hash_pool
        try:
            with self.assertNoLogs(
                "server.app.security", level="DEBUG"
            ):
                security.shutdown_password_pools()
        finally:
            security._verify_pool = original_verify
            security._hash_pool = original_hash
        self.assertEqual(len(verify_pool.calls), 1)
        self.assertEqual(len(hash_pool.calls), 1)


class AppShutdownLoggingTests(unittest.TestCase):
    """应用退出时的清理失败要写日志，且不能影响后续清理步骤。"""

    def test_room_manager_shutdown_failure_is_logged(self) -> None:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "shutdown.db"
            settings = Settings(
                environment="test",
                database_url=(
                    f"sqlite+aiosqlite:///{db_path.as_posix()}"
                ),
                jwt_secret=_SECRET,
                log_level="WARNING",
            )
            app = create_app(settings)
            with self.assertLogs(
                "server.app.main", level="WARNING"
            ) as captured:
                with TestClient(app) as client:
                    response = client.get("/api/v1/health")
                    self.assertEqual(response.status_code, 200)

                    async def boom() -> None:
                        raise RuntimeError("关闭房间管理器失败")

                    app.state.room_manager.shutdown = boom
            # 退出 TestClient 会触发 lifespan 的 finally。
            self.assertTrue(
                any(
                    "关闭房间管理器失败" in line
                    for line in captured.output
                ),
                captured.output,
            )


class WebSocketCloseLoggingTests(unittest.IsolatedAsyncioTestCase):
    """WebSocket 关闭失败必须写调试日志，不能静默吞掉。"""

    async def test_safe_close_failure_is_logged(self) -> None:
        with self.assertLogs(
            "server.app.ws.endpoint", level="DEBUG"
        ) as captured:
            await endpoint._safe_close(_FailingWebSocket(), 1011, "关闭")
        self.assertTrue(
            any("关闭 WebSocket 失败" in line for line in captured.output),
            captured.output,
        )

    async def test_connection_close_failure_is_logged(self) -> None:
        conn = ClientConnection(
            connection_id="c-close",
            user_id="u-close",
            username="关闭测试",
            websocket=_FailingWebSocket(),
        )
        with self.assertLogs(
            "server.app.ws.entities", level="DEBUG"
        ) as captured:
            await conn.close(1000, "再见")
        self.assertFalse(conn.connected)
        self.assertTrue(
            any("关闭 WebSocket 连接失败" in line for line in captured.output),
            captured.output,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
