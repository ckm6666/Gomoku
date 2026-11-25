"""更新包下载：把 GitHub Release 的产物异步下载到本地文件。

与 :mod:`gomoku.client.net.update_api` 分工不同：那边只取 JSON 元数据，
这边取的是二进制安装包，因此不走 :class:`JsonApiClient` 的 JSON 流水线，
而是直接把响应体写进文件。

两层分工：
- 本模块（网络层）只发请求、落盘、汇报进度与错误码；
- 界面层负责展示进度，并在下载完成后决定如何替换。

下载地址必须先经过 :func:`gomoku.client.auto_update.asset_download_url`
校验：只允许本仓库在 ``github.com`` 上的发布产物。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from gomoku.client.auto_update import asset_download_url
from gomoku.client.constants import APP_VERSION
from gomoku.client.net.json_api import read_reply_body

logger = logging.getLogger(__name__)

#: 统一的错误文案：界面直接展示，这里不再二次加工。
_ERR_IN_PROGRESS = "更新包正在下载中"
_ERR_BAD_URL = "更新包地址无效"
_ERR_NETWORK = "下载更新包失败，请检查网络"
_ERR_HTTP = "下载更新包失败，请稍后再试"
_ERR_WRITE = "无法写入更新包，请检查磁盘空间与权限"


class UpdateDownloadClient(QObject):
    """下载更新包到本地文件；同一时刻只允许一个在途下载。"""

    #: 进度信号：(已下载字节, 总字节)。总字节为 0 表示大小未知。
    progress = Signal(int, int)
    #: 下载完成：携带本地文件路径。
    finished = Signal(str)
    #: 下载失败：携带中文错误消息。
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(
        self,
        *,
        timeout_seconds: float = 300.0,
        manager: Optional[QNetworkAccessManager] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._manager: QNetworkAccessManager = (
            manager if manager is not None else QNetworkAccessManager(self)
        )
        self._timeout_seconds = timeout_seconds
        self._reply: Optional[QNetworkReply] = None
        self._target: Optional[Path] = None
        # 每个响应各自绑定自己的目标文件（``id(reply)`` → 路径）：
        # 被取消的旧响应可能迟到才送来数据，绝不能写进新下载的文件里。
        self._targets: dict[int, Path] = {}
        self._aborted = False

    @property
    def busy(self) -> bool:
        return self._reply is not None

    @property
    def target(self) -> Optional[Path]:
        """当前下载的目标文件路径。"""
        return self._target

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def start(self, url: str, target: Path) -> bool:
        """开始下载。

        返回 ``True`` 表示请求已发出；返回 ``False`` 表示根本没发出去，
        失败原因通过 ``failed`` 信号告知界面。
        """
        if self.busy:
            self.failed.emit(_ERR_IN_PROGRESS)
            return False

        safe_url = asset_download_url(url)
        if not safe_url:
            self.failed.emit(_ERR_BAD_URL)
            return False

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 目标文件是本次下载专用的：先删掉旧文件，避免残留内容被
            # 追加到新下载的内容后面。
            if target.exists():
                target.unlink()
        except OSError:
            self.failed.emit(_ERR_WRITE)
            return False

        request = QNetworkRequest(QUrl(safe_url))
        request.setRawHeader(
            QByteArray(b"User-Agent"),
            QByteArray(f"Gomoku/{APP_VERSION}".encode("utf-8")),
        )
        # GitHub 会把产物地址 302 到 objects.githubusercontent.com，
        # 这里允许同级别（https → https）的跳转。
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy,
        )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))

        try:
            reply = self._manager.get(request)
        except RuntimeError:
            # 宿主窗口已销毁，事件循环已经退出。
            self.failed.emit(_ERR_NETWORK)
            return False

        self._reply = reply
        resolved_target = Path(target)
        self._target = resolved_target
        self._targets[id(reply)] = resolved_target
        self._aborted = False
        reply.downloadProgress.connect(self._on_progress)
        # 目标文件随响应一起绑定：旧响应迟到的数据只会落到它自己的
        # 目标文件上，不会污染下一次下载。
        reply.readyRead.connect(
            lambda reply=reply, target=resolved_target: self._drain(reply, target)
        )
        reply.finished.connect(lambda reply=reply: self._on_finished(reply))
        self.busy_changed.emit(True)
        return True

    def abort(self) -> None:
        """取消在途下载：不再发出 ``finished``，只复位忙状态。"""
        reply = self._reply
        if reply is None:
            return
        # 先摘掉引用再 abort：真实 Qt 会在 abort 后发出 finished，
        # 那时这次下载已经过期，不能再触发任何结果信号。
        self._reply = None
        self._targets.pop(id(reply), None)
        self._target = None
        self._aborted = True
        try:
            reply.abort()
        except RuntimeError:
            pass
        self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def _on_progress(self, received: int, total: int) -> None:
        """转发下载进度。"""
        self.progress.emit(int(received), int(total) if total > 0 else 0)

    def _drain(self, reply: QNetworkReply, target: Optional[Path]) -> None:
        """把已经收到的数据写入目标文件。

        只处理「当前响应」的数据：被取消的旧响应即使迟到才送来
        ``readyRead``，也直接丢弃——否则它会被写进新下载的文件里，
        把安装包搅成损坏的压缩包。
        """
        if target is None:
            return
        if reply is not self._reply:
            return
        chunk = read_reply_body(reply)
        if not chunk:
            return
        try:
            with open(target, "ab") as handle:
                handle.write(chunk)
        except OSError:
            logger.exception("写入更新包失败")
            self.abort()
            self.failed.emit(_ERR_WRITE)

    def _on_finished(self, reply: QNetworkReply) -> None:
        """处理一次下载的结束。"""
        target = self._targets.pop(id(reply), None)
        if reply is not self._reply:
            # 已经被 abort() 取消的旧响应：静默丢弃，不发任何信号。
            self.discard(reply)
            return

        # finished 之前可能还有数据没被 readyRead 消费掉；这一步要在
        # 摘掉 ``self._reply`` 之前做，否则 ``_drain`` 会把它当成过期
        # 数据丢掉。
        self._drain(reply, target)
        self._reply = None
        self._target = None
        self.busy_changed.emit(False)
        if self._aborted:
            self.discard(reply)
            return

        try:
            self._log_reply(reply)
        except RuntimeError:
            pass

        if not self._check_reply(reply):
            self.discard(reply)
            self.failed.emit(self._error_for(reply))
            return

        self.discard(reply)
        if target is None:
            self.failed.emit(_ERR_NETWORK)
            return
        try:
            if not target.is_file() or target.stat().st_size <= 0:
                self.failed.emit(_ERR_NETWORK)
                return
        except OSError:
            self.failed.emit(_ERR_WRITE)
            return
        self.finished.emit(str(target))

    @staticmethod
    def _check_reply(reply: QNetworkReply) -> bool:
        """判断这次下载是否成功。"""
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        if reply.error() != QNetworkReply.NetworkError.NoError:
            return False
        if isinstance(status, int) and not 200 <= status < 300:
            return False
        return True

    @staticmethod
    def _error_for(reply: QNetworkReply) -> str:
        """把失败原因映射成中文提示。"""
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        if isinstance(status, int) and not 200 <= status < 300:
            if status in (403, 429):
                return "GitHub 请求频率受限，请稍后再试"
            return _ERR_HTTP
        return _ERR_NETWORK

    @staticmethod
    def _log_reply(reply: QNetworkReply) -> None:
        """记录状态码与错误枚举：不落盘响应正文。"""
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        logger.info(
            "更新包下载响应：HTTP %s，错误枚举 %s",
            status if isinstance(status, int) else None,
            reply.error(),
        )

    @staticmethod
    def discard(reply: QNetworkReply) -> None:
        """安全地销毁 reply：它可能已经随宿主窗口一起没了。"""
        try:
            reply.deleteLater()
        except RuntimeError:
            pass


__all__ = ["UpdateDownloadClient"]
