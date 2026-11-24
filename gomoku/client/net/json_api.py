"""统一的 HTTP JSON 客户端基类。

所有 REST 客户端共用同一条请求流水线：

1. 拼接并校验地址（只接受 http/https，且必须带主机名）；
2. 带上 ``Authorization: Bearer …``；
3. 设置统一的传输超时；
4. 序列化请求体、**严格**反序列化响应体；
5. 用统一的 :func:`format_api_error` 提取错误文案；
6. 无论成败都在 ``finally`` 里释放 reply。

严格解析是关键：正文不是合法 JSON（或不是 JSON 对象）时必须当成**失败**，
绝不能退化成空字典当成「没有好友/没有消息」的成功结果——那会让用户以为
服务端真的返回了空列表。

业务客户端只保留自己的 endpoint、信号，以及「响应 → 信号」的映射。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from PySide6.QtCore import QByteArray, QObject, QUrl
from PySide6.QtNetwork import (
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
)

logger = logging.getLogger(__name__)

#: 错误详情最多展示多少个字符（含末尾的省略号）。
ERROR_DETAIL_LIMIT = 300

#: 列表形态的 detail 各项之间用这个分隔符连接。
_DETAIL_SEPARATOR = "；"


def parse_json_body(raw: bytes) -> tuple[bool, Any]:
    """严格解析响应正文。

    返回 ``(是否成功, 解析结果)``：

    * 正文为空：成功，结果是空对象 ``{}``。服务端在 204 No Content 时
      不带正文（例如删除好友），这属于正常响应；
    * 正文不是合法 JSON：失败，结果为 ``None``。调用方必须把它当成失败，
      绝不能当成空的成功结果；
    * 正文是合法 JSON：成功，结果是解析出来的值（不一定是对象）。
    """
    if not raw:
        return True, {}
    try:
        return True, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False, None


def read_reply_body(reply: QNetworkReply) -> bytes:
    """读取响应正文。

    真实 reply 返回 ``QByteArray``，测试里的假 reply 有时直接返回
    ``bytes``，这里两种都兼容。
    """
    raw = reply.readAll()
    if isinstance(raw, QByteArray):
        return bytes(raw.data())
    return bytes(raw)


def format_api_error(data: Any) -> str:
    """从服务端响应里提取可展示的错误文案。

    兼容 FastAPI 的两种 ``detail`` 形态：字符串，以及 ``[{"msg": ...}]``
    列表（列表项统一用「；」连接）。结果截断到
    :data:`ERROR_DETAIL_LIMIT` 个字符，避免超长详情把界面撑坏；没有可用
    详情时返回空串，由调用方决定兜底文案。
    """
    if not isinstance(data, Mapping):
        return ""
    detail = data.get("detail")
    if isinstance(detail, str):
        text = detail.strip()
    elif isinstance(detail, (list, tuple)):
        parts: list[str] = []
        for item in detail:
            message: Any = None
            if isinstance(item, Mapping):
                message = item.get("msg")
            elif isinstance(item, str):
                message = item
            if isinstance(message, str) and message.strip():
                parts.append(message.strip())
        text = _DETAIL_SEPARATOR.join(parts)
    else:
        text = ""
    if not text:
        return ""
    if len(text) > ERROR_DETAIL_LIMIT:
        return text[: ERROR_DETAIL_LIMIT - 1] + "…"
    return text


@dataclass(frozen=True)
class ReplyOutcome:
    """一次 HTTP 响应的统一解析结果。"""

    #: HTTP 状态码；拿不到时为 None。
    status: Optional[int]
    #: 解析出来的正文；解析失败时为 None。
    payload: Any
    #: 正文是否为合法 JSON。
    parsed: bool
    #: Qt 是否报了网络错误。
    network_error: bool
    #: 业务上是否成功。
    success: bool
    #: 2xx 但正文不是合法 JSON 对象。
    malformed: bool
    #: 失败时可展示的文案；没有可用详情时为空串。
    error_message: str


def local_failure(message: str) -> ReplyOutcome:
    """构造一个本地失败结果：没有 HTTP 响应可以依据时使用。"""
    return ReplyOutcome(
        status=None,
        payload=None,
        parsed=False,
        network_error=False,
        success=False,
        malformed=False,
        error_message=message,
    )


class JsonApiClient(QObject):
    """HTTP JSON 客户端的公共基类。

    子类只需要提供 endpoint、信号，并把响应映射到信号上；URL 校验、
    Bearer 头、超时、JSON 序列化/解析、错误提取与 reply 生命周期都由
    这里统一处理。
    """

    #: 没有配置服务器地址时的提示文案。
    missing_base_url_message = "未配置服务器地址"
    #: 服务器地址不合法时的提示文案。
    invalid_url_message = "服务器地址无效"
    #: 2xx 响应正文不是合法 JSON 对象时的提示文案。
    malformed_payload_message = "服务器响应格式异常"
    #: 响应里没有可用错误详情时的兜底文案。
    default_error_message = "请求失败，请稍后再试"
    #: 请求方法不受支持时的提示文案。
    unsupported_method_message = "不支持的请求方法"

    def __init__(
        self,
        base_url: Optional[str],
        timeout_seconds: float = 10.0,
        *,
        parent: Optional[QObject] = None,
        manager: Optional[QNetworkAccessManager] = None,
    ) -> None:
        super().__init__(parent)
        self._base_url = (base_url or "").rstrip("/")
        self._timeout_seconds = timeout_seconds
        # manager 允许注入；不注入时按需创建，方便测试替换成假对象。
        self._manager: QNetworkAccessManager = (
            manager if manager is not None else QNetworkAccessManager(self)
        )

    # ------------------------------------------------------------------
    # 基础属性
    # ------------------------------------------------------------------
    @property
    def base_url(self) -> str:
        """服务器基址（不含末尾斜杠）。"""
        return self._base_url

    @property
    def timeout_seconds(self) -> float:
        """单次请求的传输超时（秒）。"""
        return self._timeout_seconds

    @property
    def manager(self) -> QNetworkAccessManager:
        """网络管理器。"""
        return self._manager

    # ------------------------------------------------------------------
    # 地址与请求
    # ------------------------------------------------------------------
    def prepare_url(self, path: str) -> tuple[Optional[QUrl], str]:
        """拼接并校验请求地址。

        返回 ``(地址, 错误文案)``：地址为 ``None`` 时错误文案非空，
        调用方据此给出明确的中文提示。
        """
        if not self._base_url:
            return None, self.missing_base_url_message
        url = QUrl(f"{self._base_url}{path}")
        if url.scheme() not in ("http", "https") or not url.host():
            return None, self.invalid_url_message
        return url, ""

    def build_request(
        self,
        url: QUrl,
        *,
        token: str = "",
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, bytes]] = None,
    ) -> QNetworkRequest:
        """构造请求：Bearer 头、传输超时、JSON 请求头。"""
        request = QNetworkRequest(url)
        if token:
            request.setRawHeader(
                QByteArray(b"Authorization"),
                QByteArray(f"Bearer {token}".encode("utf-8")),
            )
        request.setTransferTimeout(int(self._timeout_seconds * 1000))
        if payload is not None:
            request.setHeader(
                QNetworkRequest.KnownHeaders.ContentTypeHeader,
                "application/json",
            )
        for name, value in (headers or {}).items():
            request.setRawHeader(
                QByteArray(name.encode("ascii", "ignore")),
                QByteArray(value),
            )
        return request

    @staticmethod
    def encode_body(payload: Optional[Mapping[str, Any]]) -> QByteArray:
        """把请求体序列化成 JSON；没有请求体时返回空字节串。"""
        if payload is None:
            return QByteArray()
        return QByteArray(
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def dispatch(
        self,
        method: str,
        request: QNetworkRequest,
        body: QByteArray,
    ) -> Optional[QNetworkReply]:
        """按 HTTP 方法发出请求；方法不受支持时返回 ``None``。"""
        if method == "GET":
            return self.manager.get(request)
        if method == "POST":
            return self.manager.post(request, body)
        if method in ("PATCH", "PUT"):
            return self.manager.sendCustomRequest(
                request, method.encode("ascii"), body
            )
        if method == "DELETE":
            return self.manager.deleteResource(request)
        return None

    def send_request(
        self,
        method: str,
        url: QUrl,
        *,
        token: str = "",
        payload: Optional[Mapping[str, Any]] = None,
        headers: Optional[Mapping[str, bytes]] = None,
    ) -> Optional[QNetworkReply]:
        """发出一次请求并返回 reply。

        调用方负责在 ``finished`` 时调用 :meth:`handle_reply`，这样业务
        客户端可以在此之前完成自己的在途请求登记。
        """
        request = self.build_request(
            url, token=token, payload=payload, headers=headers
        )
        return self.dispatch(method, request, self.encode_body(payload))

    # ------------------------------------------------------------------
    # 响应
    # ------------------------------------------------------------------
    def is_success(
        self, status_code: Optional[int], network_error: bool
    ) -> bool:
        """默认判定：只有 HTTP 2xx 才算业务成功。

        Qt 收到 4xx/5xx 时一定会给出错误枚举，所以状态码是唯一可信的判据；
        反过来，状态码缺失（连不上、超时）一律按失败处理——不能因为“正文
        恰好能解析”就当成成功。
        """
        return status_code is not None and 200 <= status_code < 300

    def classify_reply(self, reply: QNetworkReply) -> ReplyOutcome:
        """把一次响应归类成统一的结果。"""
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        status_code = status if isinstance(status, int) else None
        network_error = reply.error() != QNetworkReply.NetworkError.NoError
        parsed, payload = parse_json_body(read_reply_body(reply))

        if not self.is_success(status_code, network_error):
            message = format_api_error(payload) if parsed else ""
            return ReplyOutcome(
                status=status_code,
                payload=payload,
                parsed=parsed,
                network_error=network_error,
                success=False,
                malformed=False,
                error_message=message,
            )

        if not parsed or not isinstance(payload, Mapping):
            # 正文不是合法 JSON（或不是 JSON 对象）：这是失败，绝不能当成
            # 空的成功结果报给用户。
            return ReplyOutcome(
                status=status_code,
                payload=payload,
                parsed=parsed,
                network_error=network_error,
                success=False,
                malformed=True,
                error_message=self.malformed_payload_message,
            )

        return ReplyOutcome(
            status=status_code,
            payload=payload,
            parsed=parsed,
            network_error=network_error,
            success=True,
            malformed=False,
            error_message="",
        )

    def handle_reply(
        self,
        reply: QNetworkReply,
        *,
        on_success: Callable[[Mapping[str, Any]], None],
        on_error: Callable[[ReplyOutcome], None],
    ) -> None:
        """统一处理一次响应。

        成功时把解析好的 JSON 对象交给 ``on_success``；失败时把统一结果交给
        ``on_error``。无论成败，reply 一定会在 ``finally`` 里被释放；宿主
        窗口已经销毁时 Qt 会抛 ``RuntimeError``，这里静默收场。
        """
        try:
            outcome = self.classify_reply(reply)
            if outcome.success:
                on_success(outcome.payload)
            else:
                on_error(outcome)
        except RuntimeError:
            # 宿主窗口已经销毁，reply 也跟着没了。槽函数里抛出的异常
            # 会让 PySide 直接中止整个进程，这里必须静默收场。
            return
        except Exception:  # pragma: no cover - 兜底，界面不能被异常打断
            logger.exception("HTTP 响应处理异常")
            try:
                on_error(local_failure(self.default_error_message))
            except Exception:  # pragma: no cover - 连报错都失败就没辙了
                logger.exception("HTTP 失败回调异常")
        finally:
            self.discard(reply)

    def fallback_message(self, outcome: ReplyOutcome) -> str:
        """响应里没有可用错误详情时的兜底文案。"""
        return self.default_error_message

    @staticmethod
    def discard(reply: QNetworkReply) -> None:
        """安全地销毁 reply：它可能已经随宿主窗口一起没了。"""
        try:
            reply.deleteLater()
        except RuntimeError:
            pass


__all__ = [
    "ERROR_DETAIL_LIMIT",
    "JsonApiClient",
    "ReplyOutcome",
    "format_api_error",
    "local_failure",
    "parse_json_body",
    "read_reply_body",
]
