"""更新包下载客户端的离屏测试。

用假的 reply/manager 驱动客户端，验证地址白名单、落盘、进度上报与
失败映射；不会发起任何真实网络请求。

运行方式：
    python -m unittest tests.test_update_download -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QByteArray, QObject, Signal  # noqa: E402
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest  # noqa: E402

from gomoku.client.net.update_download import UpdateDownloadClient  # noqa: E402

#: 合法的产物下载地址。
VALID_URL = (
    "https://github.com/ckm6666/Gomoku/releases/download/"
    "v1.14.0/Gomoku-1.14.0-windows-x64.zip"
)


class FakeReply(QObject):
    """假的下载响应：按需返回状态码、错误枚举与响应体。"""

    finished = Signal()
    readyRead = Signal()
    downloadProgress = Signal(int, int)

    def __init__(
        self,
        status: Optional[int] = None,
        payload: bytes = b"",
        error: QNetworkReply.NetworkError = QNetworkReply.NetworkError.NoError,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._status = status
        self._payload = payload
        self._error = error
        self.aborted = False

    def error(self) -> QNetworkReply.NetworkError:
        return self._error

    def attribute(self, key: Any) -> Optional[int]:
        del key
        return self._status

    def readAll(self) -> QByteArray:
        payload = self._payload
        self._payload = b""
        return QByteArray(payload)

    def abort(self) -> None:
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


class UpdateDownloadClientTests(unittest.TestCase):
    """``UpdateDownloadClient`` 的下载、校验与失败处理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.target = self.root / "update.zip"
        self.finished: list[str] = []
        self.errors: list[str] = []
        self.progress: list[tuple[int, int]] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_client(
        self, reply: FakeReply
    ) -> tuple[UpdateDownloadClient, FakeManager]:
        manager = FakeManager(reply)
        client = UpdateDownloadClient(manager=manager)
        client.finished.connect(self.finished.append)
        client.failed.connect(self.errors.append)
        client.progress.connect(
            lambda received, total: self.progress.append((received, total))
        )
        return client, manager

    def _drain(self) -> None:
        # 假 reply 都是普通 QObject，信号在无事件循环时也会直接触发；
        # 这里只需要让事件循环转一圈，保证 deleteLater 之类的收尾完成。
        from PySide6.QtCore import QCoreApplication

        if QCoreApplication.instance() is not None:
            QCoreApplication.processEvents()

    # ------------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------------
    def test_successful_download_writes_file(self) -> None:
        reply = FakeReply(status=200, payload=b"PK\x03\x04payload")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.start(VALID_URL, self.target))
        self.assertTrue(client.busy)

        reply.readyRead.emit()
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.errors, [])
        self.assertEqual(self.finished, [str(self.target)])
        self.assertEqual(self.target.read_bytes(), b"PK\x03\x04payload")
        self.assertFalse(client.busy)

    def test_progress_is_reported(self) -> None:
        reply = FakeReply(status=200, payload=b"data")
        client, _manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        reply.downloadProgress.emit(5, 10)
        reply.finished.emit()
        self._drain()

        self.assertIn((5, 10), self.progress)

    def test_foreign_url_is_rejected(self) -> None:
        reply = FakeReply(status=200, payload=b"x")
        client, manager = self._make_client(reply)

        self.assertFalse(client.start("https://example.com/x.zip", self.target))
        self.assertEqual(self.errors, ["更新包地址无效"])
        self.assertEqual(manager.requests, [])
        self.assertFalse(client.busy)

    def test_insecure_url_is_rejected(self) -> None:
        reply = FakeReply(status=200, payload=b"x")
        client, manager = self._make_client(reply)

        self.assertFalse(
            client.start(
                "http://github.com/ckm6666/Gomoku/releases/download/v1/a.zip",
                self.target,
            )
        )
        self.assertEqual(self.errors, ["更新包地址无效"])
        self.assertEqual(manager.requests, [])

    def test_http_error_status_is_reported(self) -> None:
        reply = FakeReply(status=404, payload=b"not found")
        client, _manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.finished, [])
        self.assertEqual(self.errors, ["下载更新包失败，请稍后再试"])

    def test_rate_limited_status_is_reported(self) -> None:
        reply = FakeReply(status=429, payload=b"slow down")
        client, _manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.errors, ["GitHub 请求频率受限，请稍后再试"])

    def test_network_error_is_reported(self) -> None:
        reply = FakeReply(
            status=None,
            payload=b"",
            error=QNetworkReply.NetworkError.HostNotFoundError,
        )
        client, _manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        reply.finished.emit()
        self._drain()

        self.assertEqual(self.errors, ["下载更新包失败，请检查网络"])

    def test_abort_suppresses_finished(self) -> None:
        reply = FakeReply(status=200, payload=b"x")
        client, _manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        client.abort()

        self.assertTrue(reply.aborted)
        self.assertFalse(client.busy)
        self.assertEqual(self.finished, [])
        self.assertEqual(self.errors, [])

    def test_second_download_while_in_flight_is_refused(self) -> None:
        reply = FakeReply(status=200, payload=b"x")
        client, _manager = self._make_client(reply)

        self.assertTrue(client.start(VALID_URL, self.target))
        self.assertFalse(client.start(VALID_URL, self.target))
        self.assertEqual(self.errors, ["更新包正在下载中"])

    def test_late_data_of_cancelled_reply_does_not_pollute_next(self) -> None:
        """被取消的响应迟到的数据不能写进下一次下载的文件。

        旧响应可能取消之后才收到 ``readyRead``；如果按「当前目标文件」
        写入，这些陈旧字节会混进新安装包，把压缩包搅坏。
        """
        first = FakeReply(status=200, payload=b"stale")
        second = FakeReply(status=200, payload=b"fresh")
        replies = [first, second]

        class QueueManager:
            def __init__(self) -> None:
                self.requests: list[QNetworkRequest] = []

            def get(self, request: QNetworkRequest) -> FakeReply:
                self.requests.append(request)
                return replies.pop(0)

        client = UpdateDownloadClient(manager=QueueManager())
        client.finished.connect(self.finished.append)
        client.failed.connect(self.errors.append)

        first_target = self.root / "first.zip"
        second_target = self.root / "second.zip"

        client.start(VALID_URL, first_target)
        client.abort()

        client.start(VALID_URL, second_target)
        # 旧响应这时才把数据送来。
        first.readyRead.emit()
        first.finished.emit()
        second.readyRead.emit()
        second.finished.emit()
        self._drain()

        self.assertEqual(second_target.read_bytes(), b"fresh")
        self.assertEqual(self.finished, [str(second_target)])
        self.assertEqual(self.errors, [])

    def test_request_carries_user_agent_and_redirect_policy(self) -> None:
        reply = FakeReply(status=200, payload=b"x")
        client, manager = self._make_client(reply)

        client.start(VALID_URL, self.target)
        request = manager.requests[0]
        self.assertTrue(
            bytes(request.rawHeader("User-Agent")).startswith(b"Gomoku/")
        )
        self.assertEqual(
            request.attribute(
                QNetworkRequest.Attribute.RedirectPolicyAttribute
            ),
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )


if __name__ == "__main__":
    unittest.main()
