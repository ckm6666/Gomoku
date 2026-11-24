"""联机对局历史/棋谱的异步 HTTP 客户端。

地址拼接、Bearer 头、超时、JSON 解析与 reply 生命周期都由
:class:`~gomoku.client.net.json_api.JsonApiClient` 统一处理，这里只保留
对局历史相关的 endpoint、信号与响应映射。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, Optional
from urllib.parse import quote

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QNetworkReply

from gomoku.client.config import api_path
from gomoku.client.net.json_api import JsonApiClient, ReplyOutcome

logger = logging.getLogger(__name__)


class GameHistoryHTTPClient(JsonApiClient):
    """读取最近对局与单局棋谱。"""

    recent_ready = Signal(object)  # {"entries": [...], "total": n}
    game_ready = Signal(object)  # {"game_id": ..., "moves": [...]}
    live_ready = Signal(object)  # {"entries": [...], "total": n}
    failed = Signal(str)
    unauthorized = Signal()
    busy_changed = Signal(bool)

    missing_base_url_message = "未配置联机服务器"
    invalid_url_message = "联机服务器地址无效"
    malformed_payload_message = "服务器返回的对局数据格式异常"
    default_error_message = "读取对局数据失败"

    def __init__(
        self,
        api_base_url: Optional[str],
        timeout_seconds: float = 10.0,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(api_base_url, timeout_seconds, parent=parent)
        self._replies: dict[str, QNetworkReply] = {}

    @property
    def busy(self) -> bool:
        return bool(self._replies)

    # ------------------------------------------------------------------
    # 公开请求
    # ------------------------------------------------------------------
    def fetch_recent(self, access_token: str, limit: int = 20) -> bool:
        bounded = max(1, min(100, int(limit)))
        return self._start(
            "recent",
            api_path("games", "recent") + f"?limit={bounded}",
            access_token,
        )

    def fetch_game(self, access_token: str, game_id: str) -> bool:
        if not game_id:
            self.failed.emit("对局编号为空")
            return False
        kind = f"game:{game_id}"
        return self._start(
            kind,
            api_path("games", quote(game_id, safe="")),
            access_token,
        )

    def fetch_live(self, access_token: str) -> bool:
        """读取当前进行中的对局列表（观战入口）。"""
        return self._start("live", api_path("games", "live"), access_token)

    def abort_all(self) -> None:
        for kind in list(self._replies):
            reply = self._replies.pop(kind, None)
            if reply is not None:
                reply.abort()
                self.discard(reply)
        self.busy_changed.emit(False)

    # ------------------------------------------------------------------
    # 请求/响应
    # ------------------------------------------------------------------
    def _start(self, kind: str, path: str, access_token: str) -> bool:
        if not access_token:
            self.failed.emit("登录状态失效，请重新登录")
            return False

        url, error = self.prepare_url(path)
        if url is None:
            self.failed.emit(error)
            return False

        old_reply = self._replies.pop(kind, None)
        if old_reply is not None:
            old_reply.abort()
            self.discard(old_reply)

        reply = self.send_request("GET", url, token=access_token)
        if reply is None:
            self.failed.emit(self.unsupported_method_message)
            return False

        self._replies[kind] = reply
        reply.finished.connect(
            lambda reply=reply, kind=kind: self._on_finished(reply, kind)
        )
        self.busy_changed.emit(True)
        return True

    def _on_finished(self, reply: QNetworkReply, kind: str) -> None:
        if reply is not self._replies.get(kind):
            self.discard(reply)
            return

        self._replies.pop(kind, None)
        self.busy_changed.emit(bool(self._replies))

        self.handle_reply(
            reply,
            on_success=lambda data: self._on_success(kind, data),
            on_error=self._on_error,
        )

    def _on_success(self, kind: str, data: Mapping[str, Any]) -> None:
        if kind == "recent":
            self.recent_ready.emit(dict(data))
        elif kind == "live":
            self.live_ready.emit(dict(data))
        else:
            self.game_ready.emit(dict(data))

    def _on_error(self, outcome: ReplyOutcome) -> None:
        if outcome.status == 401:
            self.unauthorized.emit()
        self.failed.emit(
            outcome.error_message or self.default_error_message
        )


__all__ = ["GameHistoryHTTPClient"]
