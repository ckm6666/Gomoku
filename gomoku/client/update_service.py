"""客户端更新服务：异步检查新版本并下载更新包。

所有网络访问都通过 QtNetwork 在 Qt 事件循环里完成，GUI 线程不会被阻塞；
这里不引入任何第三方 HTTP 库。版本比较、清单解析与校验复用
:mod:`gomoku.client.updater`，安装脚本相关逻辑复用
:mod:`gomoku.client.update_installer`。

服务只负责"取回信息"，不做任何界面决策：是否提示、是否下载、如何安装
都由 :class:`gomoku.client.main_window.MainWindow` 决定。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional

from PySide6.QtCore import (
    QObject,
    QStandardPaths,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

from gomoku.client.update_installer import staged_download_path
from gomoku.client.updater import (
    UpdateInfo,
    is_newer_version,
    parse_manifest,
    verify_file,
)

logger = logging.getLogger(__name__)


class UpdateService(QObject):
    """检查新版本、下载更新包；同一时刻只允许一个下载任务。"""

    check_started = Signal()  # 开始检查
    update_available = Signal(object)  # UpdateInfo
    up_to_date = Signal(str)  # 当前版本号
    check_failed = Signal(str)  # 中文错误提示
    download_progress = Signal(int, int)  # 已下载字节, 总字节（-1 表示未知）
    download_ready = Signal(object, str)  # UpdateInfo, 本地文件路径
    download_failed = Signal(str)  # 中文错误提示

    def __init__(
        self,
        manifest_url: str,
        current_version: str,
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._manifest_url = (manifest_url or "").strip()
        self._current_version = current_version
        self._timeout_seconds = max(1.0, float(timeout_seconds))
        self._manager = QNetworkAccessManager(self)

        self._check_reply: Optional[QNetworkReply] = None
        self._download_reply: Optional[QNetworkReply] = None
        self._download_info: Optional[UpdateInfo] = None
        self._download_path: Optional[Path] = None
        self._download_file: Optional[BinaryIO] = None
        self._download_canceled = False
        self._check_canceled = False
        # 旧版 Qt 没有 setTransferTimeout 时的定时器兜底。
        self._fallback_timers: dict[QNetworkReply, QTimer] = {}

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def manifest_url(self) -> str:
        """更新清单地址。"""
        return self._manifest_url

    @property
    def current_version(self) -> str:
        """当前运行中的版本号。"""
        return self._current_version

    @property
    def downloading(self) -> bool:
        """是否正在下载更新包。"""
        return self._download_reply is not None

    @staticmethod
    def default_download_dir() -> Path:
        """返回默认的更新包目录（当前用户可写的应用数据目录）。"""
        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppLocalDataLocation
        )
        if not base:
            base = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.AppDataLocation
            )
        if not base:
            base = str(Path.home() / ".gomoku")
        return Path(base) / "updates"

    # ------------------------------------------------------------------
    # 检查更新
    # ------------------------------------------------------------------
    def check_for_update(self) -> None:
        """请求更新清单；结果通过信号通知，方法本身不会抛出异常。"""
        try:
            if self._check_reply is not None:
                logger.info("已有检查请求在进行中，忽略重复请求")
                return

            url = QUrl(self._manifest_url)
            if not self._manifest_url:
                self.check_failed.emit("未配置更新服务器地址")
                return
            if url.scheme() not in ("http", "https") or not url.host():
                self.check_failed.emit("更新地址无效")
                return

            request = QNetworkRequest(url)
            request.setHeader(
                QNetworkRequest.KnownHeaders.UserAgentHeader,
                f"Gomoku/{self._current_version}",
            )
            self.check_started.emit()
            reply = self._manager.get(request)
            self._check_reply = reply
            self._check_canceled = False
            self._arm_timeout(request, reply)
            reply.finished.connect(
                lambda reply=reply: self._on_check_finished(reply)
            )
        except Exception:
            logger.exception("发起更新检查失败")
            self.check_failed.emit("检查更新失败，请稍后再试")

    def _on_check_finished(self, reply: QNetworkReply) -> None:
        """处理清单请求的回调。"""
        if reply is not self._check_reply:
            self._discard_reply(reply)
            return
        self._check_reply = None
        self._clear_fallback_timer(reply)
        try:
            if self._check_canceled:
                # 用户主动取消：不再报错提示。
                self._check_canceled = False
                return
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            error = reply.error()
            raw = bytes(reply.readAll())
            ok = (
                error == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            )
            if not ok:
                if isinstance(status, int) and status == 404:
                    # 服务器上还没有发布任何更新清单。
                    self.check_failed.emit("未找到更新信息，暂时没有新版本")
                    return
                self.check_failed.emit(self._describe_network_error(error))
                return

            try:
                # 用 utf-8-sig 兼容带 BOM 的清单文件。
                payload = json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                logger.warning("更新清单不是合法 JSON")
                self.check_failed.emit("更新信息解析失败")
                return
            if not isinstance(payload, Mapping):
                logger.warning("更新清单格式异常：%s", type(payload).__name__)
                self.check_failed.emit("更新信息解析失败")
                return

            try:
                info = parse_manifest(payload)
            except Exception:
                logger.exception("解析更新清单失败")
                self.check_failed.emit("更新信息解析失败")
                return

            if is_newer_version(info.version, self._current_version):
                self.update_available.emit(info)
            else:
                self.up_to_date.emit(self._current_version)
        except Exception:
            logger.exception("处理更新检查结果失败")
            self.check_failed.emit("检查更新失败，请稍后再试")
        finally:
            reply.deleteLater()

    # ------------------------------------------------------------------
    # 下载更新
    # ------------------------------------------------------------------
    def download(
        self, info: UpdateInfo, target_dir: Optional[Path] = None
    ) -> None:
        """下载更新包到本地；重复调用会被忽略。"""
        if self._download_reply is not None:
            logger.info("已有更新下载在进行中，忽略重复请求")
            return
        try:
            directory = (
                Path(target_dir)
                if target_dir is not None
                else self.default_download_dir()
            )
            directory.mkdir(parents=True, exist_ok=True)
            path = staged_download_path(directory, info.version)

            url = QUrl(info.url)
            if url.scheme() not in ("http", "https") or not url.host():
                self.download_failed.emit("下载地址无效")
                return

            request = QNetworkRequest(url)
            # 先打开文件：打开失败时不会留下在途的下载请求。
            handle = open(path, "wb")
            try:
                reply = self._manager.get(request)
            except Exception:
                handle.close()
                raise

            self._download_reply = reply
            self._download_info = info
            self._download_path = path
            self._download_canceled = False
            self._download_file = handle

            self._arm_timeout(request, reply)
            reply.downloadProgress.connect(self._on_download_progress)
            reply.readyRead.connect(
                lambda reply=reply: self._on_ready_read(reply)
            )
            reply.finished.connect(
                lambda reply=reply: self._on_download_finished(reply)
            )
        except Exception:
            logger.exception("开始下载更新失败")
            reply = self._download_reply
            self._download_reply = None
            if reply is not None:
                try:
                    reply.abort()
                except RuntimeError:  # pragma: no cover - reply 已被释放
                    logger.debug("中止下载请求失败：对象已销毁")
            self._close_download_file()
            self.download_failed.emit("开始下载更新失败，请重试")

    def _on_ready_read(self, reply: QNetworkReply) -> None:
        """把收到的数据块写入磁盘，避免把整个安装包读进内存。"""
        if reply is not self._download_reply:
            return
        try:
            data = bytes(reply.readAll())
        except Exception:
            logger.exception("读取更新数据失败")
            return
        if not data:
            return
        handle = self._download_file
        if handle is None:
            return
        try:
            handle.write(data)
        except OSError:
            logger.exception("写入更新文件失败")

    def _on_download_progress(self, received: int, total: int) -> None:
        """转发下载进度；total 为 -1 表示服务器未提供总长度。"""
        try:
            self.download_progress.emit(int(received), int(total))
        except Exception:
            logger.exception("转发下载进度失败")

    def _on_download_finished(self, reply: QNetworkReply) -> None:
        """收尾：落盘剩余数据、校验 SHA-256 并发出结果信号。"""
        if reply is not self._download_reply:
            self._discard_reply(reply)
            return
        self._download_reply = None
        self._clear_fallback_timer(reply)
        info = self._download_info
        path = self._download_path
        try:
            self._flush_download_file()
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            error = reply.error()
            ok = (
                error == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            )
            if self._download_canceled:
                self._remove_partial_file(path)
                self.download_failed.emit("下载已取消")
                return
            if not ok:
                self._remove_partial_file(path)
                self.download_failed.emit(self._describe_network_error(error))
                return
            if path is None or not path.is_file() or path.stat().st_size == 0:
                self._remove_partial_file(path)
                self.download_failed.emit("下载文件为空，请重试")
                return
            if info is not None and info.sha256:
                if not verify_file(path, info.sha256):
                    self._remove_partial_file(path)
                    self.download_failed.emit("下载文件校验失败，请重试")
                    return
            if info is None:
                self.download_failed.emit("下载更新失败，请重试")
                return
            self.download_ready.emit(info, str(path))
        except Exception:
            logger.exception("处理更新下载结果失败")
            self._remove_partial_file(path)
            self.download_failed.emit("下载更新失败，请重试")
        finally:
            self._download_info = None
            self._download_path = None
            reply.deleteLater()

    # ------------------------------------------------------------------
    # 取消与内部工具
    # ------------------------------------------------------------------
    def cancel(self) -> None:
        """中止正在进行的检查与下载。"""
        if self._check_reply is not None:
            self._check_canceled = True
        if self._download_reply is not None:
            self._download_canceled = True
        for reply in (self._check_reply, self._download_reply):
            if reply is None:
                continue
            try:
                reply.abort()
            except RuntimeError:  # pragma: no cover - reply 已被释放
                logger.debug("中止请求失败：对象已销毁")

    def _flush_download_file(self) -> None:
        """把缓冲区里的剩余数据写完并关闭文件。"""
        handle = self._download_file
        self._download_file = None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except OSError:
            logger.warning("关闭更新文件失败", exc_info=True)

    def _close_download_file(self) -> None:
        """下载未开始成功时关闭可能已打开的文件句柄。"""
        self._flush_download_file()

    def _remove_partial_file(self, path: Optional[Path]) -> None:
        """删除未完成的下载文件。"""
        if path is None:
            return
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            logger.warning("删除未完成的更新文件失败", exc_info=True)

    def _discard_reply(self, reply: QNetworkReply) -> None:
        """回收一个已经不再关心的响应对象。"""
        self._clear_fallback_timer(reply)
        reply.deleteLater()

    def _arm_timeout(
        self, request: QNetworkRequest, reply: QNetworkReply
    ) -> None:
        """设置请求超时：新版 Qt 用 setTransferTimeout，旧版用定时器兜底。"""
        milliseconds = int(self._timeout_seconds * 1000)
        if hasattr(request, "setTransferTimeout"):
            request.setTransferTimeout(milliseconds)
            return
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(reply.abort)
        timer.start(milliseconds)
        self._fallback_timers[reply] = timer

    def _clear_fallback_timer(self, reply: QNetworkReply) -> None:
        """停止并移除超时兜底定时器。"""
        timer = self._fallback_timers.pop(reply, None)
        if timer is None:
            return
        timer.stop()
        timer.deleteLater()

    @staticmethod
    def _describe_network_error(error: Any) -> str:
        """把 Qt 网络错误码翻译成中文提示。"""
        if error == QNetworkReply.NetworkError.TimeoutError:
            return "连接更新服务器超时，请稍后再试"
        if error in (
            QNetworkReply.NetworkError.ConnectionRefusedError,
            QNetworkReply.NetworkError.HostNotFoundError,
            QNetworkReply.NetworkError.RemoteHostClosedError,
        ):
            return "无法连接更新服务器，请检查网络"
        if error == QNetworkReply.NetworkError.SslHandshakeFailedError:
            return "与更新服务器建立安全连接失败"
        return "网络请求失败，无法获取更新信息"


__all__ = ["UpdateService"]
