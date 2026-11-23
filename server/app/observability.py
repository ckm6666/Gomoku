"""结构化日志与请求上下文。

日志只记录请求元数据（方法、路由、状态码、耗时、请求 ID、用户 ID、
对局 ID），不记录请求体、Authorization 头、密码或 token。生产环境可用
``GOMOKU_LOG_FORMAT=json`` 输出单行 JSON，便于日志采集。
"""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar, Token
from datetime import datetime, timezone
from typing import Any, Optional

from server.app.config import Settings

request_id_var: ContextVar[str] = ContextVar("request_id", default="")

_REQUEST_ID_MAX_LENGTH = 128
_REQUEST_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789._-"
)

_STANDARD_RECORD_FIELDS = frozenset(
    {
        "name",
        "msg",
        "args",
        "levelname",
        "levelno",
        "pathname",
        "filename",
        "module",
        "exc_info",
        "exc_text",
        "stack_info",
        "lineno",
        "funcName",
        "created",
        "msecs",
        "relativeCreated",
        "thread",
        "threadName",
        "processName",
        "process",
        "taskName",
    }
)


def sanitize_request_id(value: Optional[str]) -> str:
    """使用客户端请求 ID，非法值则生成新的 UUID。"""
    if (
        isinstance(value, str)
        and value
        and len(value) <= _REQUEST_ID_MAX_LENGTH
        and all(ch in _REQUEST_ID_CHARS for ch in value)
    ):
        return value
    return uuid.uuid4().hex


def bind_request_id(request_id: str) -> Token:
    return request_id_var.set(request_id)


def reset_request_id(token: Token) -> None:
    request_id_var.reset(token)


def _context_fields() -> dict[str, str]:
    fields: dict[str, str] = {}
    request_id = request_id_var.get()
    if request_id:
        fields["request_id"] = request_id
    return fields


class JsonFormatter(logging.Formatter):
    """单行 JSON 日志；自动带上请求上下文与 ``extra`` 字段。"""

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        data.update(_context_fields())
        for key, value in record.__dict__.items():
            if key in _STANDARD_RECORD_FIELDS or key.startswith("_"):
                continue
            if key in {"message", "ts", "level", "logger"}:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                data[key] = value
            else:
                data[key] = str(value)
        if record.exc_info:
            data["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(data, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """开发环境可读日志，附上请求上下文。"""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = _context_fields()
        for key in (
            "event",
            "method",
            "path",
            "status",
            "duration_ms",
            "user_id",
            "game_id",
            "connection_id",
            "message_type",
            "reason",
            "move_no",
        ):
            value = getattr(record, key, None)
            if value is not None and value != "":
                fields[key] = str(value)
        if not fields:
            return base
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        return f"{base} [{suffix}]"


def configure_logging(settings: Settings) -> None:
    """按配置安装根日志处理器；重复调用不会累积 handler。"""
    level = getattr(logging, settings.log_level, logging.INFO)
    stream = sys.stderr
    # Windows 控制台默认可能是 GBK；JSON 日志必须保持 UTF-8，否则中文会
    # 以非 UTF-8 字节落盘，采集端无法解析。
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        JsonFormatter()
        if settings.log_format == "json"
        else TextFormatter()
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    # SQLAlchemy 在 INFO 级别会打印每条 SQL，默认压到 WARNING。
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    # 我们自己的 HTTP 访问日志已带耗时/请求 ID；关闭 Uvicorn 的重复访问日志。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # 测试/HTTP 客户端日志通常没有运维价值。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


__all__ = [
    "JsonFormatter",
    "TextFormatter",
    "bind_request_id",
    "configure_logging",
    "request_id_var",
    "reset_request_id",
    "sanitize_request_id",
]
