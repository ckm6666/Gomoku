"""共享时间工具：统一“存储 UTC、展示本地”的时间口径。

客户端与服务端各自散落着 ``datetime.now()`` / ``astimezone()`` /
``strftime()`` 的写法，同一个时刻可能在复盘页与排行榜显示成两个时间。
这里提供一套最小工具，供各端逐步收敛：

* :func:`utc_now_naive` —— 落盘/写库用的 UTC 无时区时间；
* :func:`parse_utc_timestamp` —— 把服务端返回的 ISO 时间串解析成 UTC
  无时区时间；
* :func:`format_local_time` / :func:`format_local_time_iso` —— 展示用的
  本地时间。

约定：**无时区的时间一律按 UTC 解释**，展示时再换算成本地时间。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

#: 时间缺失或无法解析时使用的固定占位值（Unix 纪元）。
EPOCH_NAIVE = datetime(1970, 1, 1)


def utc_now_naive() -> datetime:
    """返回当前 UTC 时间的无时区版本（用于写库/落盘）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def parse_utc_timestamp(text: object) -> Optional[datetime]:
    """把 ISO 时间串解析成 UTC 无时区时间；无法解析时返回 ``None``。

    带时区的时间串先换算成 UTC；不带时区的时间串按 UTC 解释。传入
    ``datetime`` 实例时按同一规则处理。
    """
    if isinstance(text, datetime):
        parsed = text
    elif isinstance(text, str):
        candidate = text.strip()
        if not candidate:
            return None
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _to_local(value: datetime) -> datetime:
    """把 UTC 时间换算成本地时间（无时区输入按 UTC 解释）。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone()


def format_local_time(value: datetime) -> str:
    """把时间格式成本地时间的 ``YYYY-MM-DD HH:MM:SS`` 文本。"""
    return _to_local(value).strftime("%Y-%m-%d %H:%M:%S")


def format_local_time_iso(value: datetime) -> str:
    """把时间格式成本地时间的 ISO 文本（带时区偏移）。"""
    return _to_local(value).isoformat(timespec="seconds")


__all__ = [
    "EPOCH_NAIVE",
    "format_local_time",
    "format_local_time_iso",
    "parse_utc_timestamp",
    "utc_now_naive",
]
