"""更新检查 GitHub Releases 客户端测试（离屏）。

用假的 reply/manager 驱动客户端，验证状态码映射、JSON 解析、并发保护
与请求头行为；不发起任何真实网络请求。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QByteArray, QCoreApplication, QObject, Signal  # noqa: E402
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest  # noqa: E402

from gomoku.client.net.update_api import UpdateHTTPClient  # noqa: E402

#: 一份合法的 GitHub ``/releases/latest`` 响应。
VALID_PAYLOAD: dict[str, Any] = {
    "tag_name": "v1.13.0",
    "name": "v1.13.0 发布",
    "html_url": "https://github.com/ckm6666/Gomoku/releases/tag/v1.13.0",
    "body": "修复若干问题",
    "published_at": "2026-01-02T03:04:05Z",
    "prerelease": False,
    "draft": False,
    "assets": [
        {
            "name": "Gomoku-1.13.0-windows-x64.zip",
            "browser_download_url": (
                "https://github.com/ckm6666/Gomoku/releases/download/"
                "v1.13.0/Gomoku-1.13.0-windows-x64.zip"
            ),
            "size": 1024,
        }
    ],
}


class FakeReply(QObject):
    """假的网络响应：按需返回状态码、错误枚举与响应正文。"""

    finished = Signal()

    def __init__(
        self,
        status: Optional[int] = None,
        body: bytes = b"",
        error: QNetworkReply.NetworkError = QNetworkReply.NetworkError.NoError,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._status = status
        self._body = QByteArray(body)
        self._error = error
        self.aborted = False

    def error(self) -> QNetworkReply.NetworkError:
        return self._error

    def attribute(self, key: Any) -> Optional[int]:
        return self._status

    def readAll(self) -> QByteArray:
        return self._body

    def abort(self) -> None:
        # 真实 Qt 在 abort 后也会发出 finished，这里保持一致。
        self.aborted = True
        self.finished.emit()


class FakeManager:
    """假的 ``QNetworkAccessManager``：记录请求并返回预置的 reply。"""

    def __init__(self, reply: FakeReply) -> None:
        self.reply = reply
        self.requests: list[QNetworkRequest] = []

    def get(self, request: QNetworkRequest) -> FakeReply:
        self.requests.append(request)
        return self.reply


class UpdateHTTPClientTests(unittest.TestCase):
    """覆盖 ``UpdateHTTPClient`` 的成功、失败与边界行为。

    本模块刻意不创建任何 ``QApplication``/``QCoreApplication`` 实例：
    假 reply 与假 manager 都是普通 ``QObject``，信号在无事件循环时也能
    直接连接。若在这里创建一个 ``QCoreApplication``，同进程里后续的
    ``QApplication.instance()`` 会拿到非 GUI 实例，导致所有 QWidget 测试
    崩溃——测试顺序一变就会炸。
    """

    def setUp(self) -> None:
        self.releases: list[Any] = []
        self.errors: list[str] = []
        self.busy_states: list[bool] = []

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _make_client(
        self,
        reply: FakeReply,
        api_url: str = "https://api.github.com/repos/ckm6666/Gomoku/releases/latest",
        token: str = "",
    ) -> tuple[UpdateHTTPClient, FakeManager]:
        manager = FakeManager(reply)
        client = UpdateHTTPClient(api_url, 10.0, token, manager=manager)
        client.release_ready.connect(self.releases.append)
        client.failed.connect(self.errors.append)
        client.busy_changed.connect(self.busy_states.append)
        return client, manager

    def _drain(self) -> None:
        QCoreApplication.processEvents()

    # ------------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------------
    def test_success_emits_release_info(self) -> None:
        reply = FakeReply(
            status=200,
            body=json.dumps(VALID_PAYLOAD).encode("utf-8"),
        )
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        self.assertTrue(client.busy)
        self.assertEqual(self.busy_states, [True])

        reply.finished.emit()
        self._drain()

        self.assertEqual(len(self.releases), 1)
        release = self.releases[0]
        self.assertEqual(release.tag, "v1.13.0")
        self.assertEqual(release.version, "1.13.0")
        self.assertEqual(self.errors, [])
        self.assertEqual(self.busy_states, [True, False])
        self.assertFalse(client.busy)

    def test_rate_limit_status(self) -> None:
        reply = FakeReply(status=403, body=b'{"message": "rate limit"}')
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["GitHub 请求频率受限，请稍后再试"])

    def test_too_many_requests_status(self) -> None:
        reply = FakeReply(status=429, body=b"{}")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.errors, ["GitHub 请求频率受限，请稍后再试"])

    def test_not_found_status(self) -> None:
        reply = FakeReply(status=404, body=b'{"message": "Not Found"}')
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["未找到发布版本"])

    def test_server_error_status(self) -> None:
        reply = FakeReply(status=500, body=b"oops")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["更新检查失败（HTTP 500）"])

    def test_network_error_without_status(self) -> None:
        reply = FakeReply(
            status=None,
            body=b"",
            error=QNetworkReply.NetworkError.HostNotFoundError,
        )
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["无法连接 GitHub，请检查网络"])
        self.assertEqual(self.busy_states, [True, False])

    def test_invalid_json_body(self) -> None:
        reply = FakeReply(status=200, body=b"not a json")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["更新信息格式异常，请稍后再试"])

    def test_payload_without_tag_name(self) -> None:
        body = json.dumps({"name": "没有标签"}).encode("utf-8")
        reply = FakeReply(status=200, body=body)
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["更新信息格式异常，请稍后再试"])

    def test_second_check_while_in_flight(self) -> None:
        reply = FakeReply(status=200, body=json.dumps(VALID_PAYLOAD).encode())
        client, manager = self._make_client(reply)

        self.assertTrue(client.check())
        self.assertFalse(client.check())

        self.assertEqual(len(manager.requests), 1)
        self.assertEqual(self.errors, ["更新检查正在进行中"])

        reply.finished.emit()
        self._drain()
        self.assertEqual(len(self.releases), 1)

    def test_abort_suppresses_release_ready(self) -> None:
        reply = FakeReply(status=200, body=json.dumps(VALID_PAYLOAD).encode())
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        client.abort()

        self.assertTrue(reply.aborted)
        self.assertFalse(client.busy)
        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, [])

        # abort 之后到达的响应不能再发任何结果信号。
        reply.finished.emit()
        self._drain()
        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, [])

    def test_empty_api_url_is_rejected(self) -> None:
        reply = FakeReply(status=200, body=b"{}")
        client, manager = self._make_client(reply, api_url="")

        self.assertFalse(client.check())
        self.assertEqual(self.errors, ["更新地址无效"])
        self.assertEqual(manager.requests, [])
        self.assertFalse(client.busy)

    def test_invalid_api_url_is_rejected(self) -> None:
        reply = FakeReply(status=200, body=b"{}")
        client, manager = self._make_client(reply, api_url="ftp://example.com/x")

        self.assertFalse(client.check())
        self.assertEqual(self.errors, ["更新地址无效"])
        self.assertEqual(manager.requests, [])

    def test_authorization_header_only_with_token(self) -> None:
        reply = FakeReply(status=200, body=b"{}")
        client, manager = self._make_client(reply)

        self.assertTrue(client.check())
        request = manager.requests[0]
        self.assertEqual(bytes(request.rawHeader("Authorization")), b"")
        self.assertEqual(
            bytes(request.rawHeader("Accept")), b"application/vnd.github+json"
        )
        self.assertEqual(
            bytes(request.rawHeader("X-GitHub-Api-Version")), b"2022-11-28"
        )
        self.assertTrue(
            bytes(request.rawHeader("User-Agent")).startswith(b"Gomoku/")
        )

    def test_authorization_header_with_token(self) -> None:
        reply = FakeReply(status=200, body=b"{}")
        client, manager = self._make_client(reply, token="secret-token")

        self.assertTrue(client.check())
        request = manager.requests[0]
        self.assertEqual(
            bytes(request.rawHeader("Authorization")), b"Bearer secret-token"
        )

    def test_token_is_not_sent_to_foreign_host(self) -> None:
        """令牌只发给 api.github.com，避免被自定义地址骗走。"""
        reply = FakeReply(status=200, body=b"{}")
        client, manager = self._make_client(
            reply,
            api_url="https://example.com/releases/latest",
            token="secret-token",
        )

        self.assertTrue(client.check())
        request = manager.requests[0]
        self.assertEqual(bytes(request.rawHeader("Authorization")), b"")

    def test_token_never_appears_in_logs(self) -> None:
        """令牌只出现在请求头里，不许进日志。"""
        reply = FakeReply(status=200, body=b"{}")
        client, _manager = self._make_client(reply, token="secret-token")

        with self.assertLogs(
            "gomoku.client.net.update_api", level="DEBUG"
        ) as captured:
            self.assertTrue(client.check())
            reply.finished.emit()
            self._drain()

        joined = "\n".join(captured.output)
        self.assertNotIn("secret-token", joined)

    def test_no_content_status_is_reported_as_bad_payload(self) -> None:
        """2xx 但正文为空（204）也算格式异常。"""
        reply = FakeReply(status=204, body=b"")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["更新信息格式异常，请稍后再试"])

    def test_success_status_with_error_enum_is_network_failure(self) -> None:
        """状态码是 200 但 Qt 报了错，按网络故障处理。"""
        reply = FakeReply(
            status=200,
            body=b"{}",
            error=QNetworkReply.NetworkError.UnknownNetworkError,
        )
        client, _manager = self._make_client(reply)

        self.assertTrue(client.check())
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.releases, [])
        self.assertEqual(self.errors, ["无法连接 GitHub，请检查网络"])


if __name__ == "__main__":
    unittest.main()
