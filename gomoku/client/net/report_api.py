"""举报提交与对局回放审计的异步 HTTP 客户端。"""

from __future__ import annotations

import json
import logging
from typing import Any, Mapping, Optional
from urllib.parse import quote

from PySide6.QtCore import QByteArray, QObject, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger(__name__)


class ReportHTTPClient(QObject):
    """提交举报、读取自己提交的举报与对局审计。"""

    submitted = Signal(object)  # 服务端返回的举报对象
    audit_ready = Signal(object)  # {"game_id":..., "ok":..., "findings":[...]}
    failed = Signal(str)
    unauthorized = Signal()
    busy_changed = Signal(bool)

    def __init__(
        self,
        api_base_url: Optional[str],
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._api_base_url = (api_base_url or "").rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._manager = QNetworkAccessManager(self)
        self._replies: dict[str, QNetworkReply] = {}

    @property
    def busy(self) -> bool:
        return bool(self._replies)

    def submit_report(
        self,
        access_token: str,
        game_id: str,
        category: str,
        description: str,
    ) -> bool:
        return self._start(
            "report",
            "/api/v1/reports",
            access_token,
            method="POST",
            payload={
                "game_id": game_id,
                "category": category,
                "description": description,
            },
        )

    def fetch_audit(self, access_token: str, game_id: str) -> bool:
        return self._start(
            f"audit:{game_id}",
            f"/api/v1/games/{quote(game_id, safe='')}/audit",
            access_token,
            method="GET",
        )

    def abort_all(self) -> None:
        for kind in list(self._replies):
            reply = self._replies.pop(kind, None)
            if reply is not None:
                reply.abort()
                reply.deleteLater()
        self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def _start(
        self,
        kind: str,
        path: str,
        access_token: str,
        *,
        method: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> bool:
        if not access_token:
            self.failed.emit("登录状态失效，请重新登录")
            return False
        if not self._api_base_url:
            self.failed.emit("未配置联机服务器")
            return False
        url = QUrl(f"{self._api_base_url}{path}")
        if url.scheme() not in ("http", "https") or not url.host():
            self.failed.emit("联机服务器地址无效")
            return False

        old_reply = self._replies.pop(kind, None)
        if old_reply is not None:
            old_reply.abort()
            old_reply.deleteLater()

        request = QNetworkRequest(url)
        request.setRawHeader(
            QByteArray(b"Authorization"),
            QByteArray(f"Bearer {access_token}".encode("utf-8")),
        )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))
        if method == "POST":
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                "application/json",
            )
            body = QByteArray(
                json.dumps(
                    payload or {}, ensure_ascii=False
                ).encode("utf-8")
            )
            reply = self._manager.post(request, body)
        else:
            reply = self._manager.get(request)
        self._replies[kind] = reply
        reply.finished.connect(
            lambda reply=reply, kind=kind: self._on_finished(reply, kind)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, kind: str) -> None:
        if reply is not self._replies.get(kind):
            reply.deleteLater()
            return
        self._replies.pop(kind, None)
        self.busy_changed.emit(bool(self._replies))
        try:
            status = reply.attribute(
                QNetworkRequest.Attribute.HttpStatusCodeAttribute
            )
            raw = bytes(reply.readAll())
            data: Any = None
            if raw:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    data = None
            if (
                reply.error() == QNetworkReply.NetworkError.NoError
                or (isinstance(status, int) and 200 <= status < 300)
            ):
                if not isinstance(data, Mapping):
                    self.failed.emit("服务器返回的数据格式异常")
                    return
                if kind == "report":
                    self.submitted.emit(dict(data))
                else:
                    self.audit_ready.emit(dict(data))
                return
            if status == 401:
                self.unauthorized.emit()
            self.failed.emit(
                self._extract_error(data) or "请求失败，请稍后再试"
            )
        finally:
            reply.deleteLater()

    @staticmethod
    def _extract_error(data: Any) -> str:
        if not isinstance(data, Mapping):
            return ""
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail[:300]
        if isinstance(detail, list) and detail:
            first = detail[0]
            if isinstance(first, Mapping):
                message = first.get("msg")
                if isinstance(message, str):
                    return message[:300]
        return ""


__all__ = ["ReportHTTPClient"]
