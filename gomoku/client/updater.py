"""更新检查的纯逻辑核心：版本号比较、清单解析与文件校验。

本模块只依赖标准库，不引入 PySide6，因此可以在没有 Qt 事件循环的环境下
直接进行单元测试。职责边界：

1. 版本号解析与比较（支持 ``v`` 前缀与 ``-beta.1`` 之类的预发布后缀）；
2. 解析自家服务端下发的更新清单 JSON；
3. 计算 / 校验文件的 SHA-256 摘要；
4. 把字节数格式化成中文用户熟悉的体积字符串；
5. 更新地址的安全策略：只放行 HTTPS（本机回环地址除外）。

真正的网络下载与替换安装逻辑分别由调用方与
:mod:`gomoku.client.update_installer` 负责。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

#: 服务端更新清单接口路径（相对于 API 基地址）。
SERVER_UPDATE_PATH: str = "/api/v1/updates/latest"

#: 计算摘要时的分块大小，避免把整个安装包一次性读入内存。
_CHUNK_SIZE = 1024 * 1024

#: 允许走明文 HTTP 的本机地址：更新功能在开发自测时经常连本机服务，
#: 对回环地址不强制 HTTPS；其余地址一律要求 HTTPS。
_LOOPBACK_HOSTS = frozenset(
    {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
)

__all__ = [
    "SERVER_UPDATE_PATH",
    "InsecureUrlError",
    "UpdateInfo",
    "compare_versions",
    "default_manifest_url",
    "format_size",
    "is_loopback_url",
    "is_newer_version",
    "is_secure_url",
    "parse_manifest",
    "parse_version",
    "sha256_of_file",
    "verify_file",
]


class InsecureUrlError(ValueError):
    """更新地址不满足安全策略（必须是 HTTPS，或本机回环地址）。"""


@dataclass(frozen=True)
class UpdateInfo:
    """一条可用的更新信息。"""

    version: str
    url: str
    sha256: str = ""
    size_bytes: int = 0
    notes: str = ""
    published_at: str = ""


def parse_version(text: str) -> tuple[int, int, int, str]:
    """把版本号字符串解析成 ``(major, minor, patch, prerelease)``。

    支持 ``"1"`` / ``"1.2"`` / ``"1.2.3"`` / ``"v1.2.3"``，
    预发布后缀写成 ``"-beta.1"``（正式版本为空字符串）。
    缺失的数值段按 0 处理；空串、非数字段或超过三段数值都抛出 ``ValueError``。
    """
    if not isinstance(text, str):
        raise ValueError("版本号必须是字符串")

    raw = text.strip()
    if raw[:1] in ("v", "V"):
        raw = raw[1:].strip()
    if not raw:
        raise ValueError("版本号不能为空")

    core, separator, prerelease = raw.partition("-")
    core = core.strip()
    if not core:
        raise ValueError("版本号不能为空")
    if separator and not prerelease.strip():
        # 形如 "1.2.3-" 的悬空分隔符属于非法版本号。
        raise ValueError(f"版本号的预发布后缀不能为空：{text!r}")

    parts = core.split(".")
    if len(parts) > 3:
        raise ValueError(f"版本号最多支持三段数值：{text!r}")

    numbers = [0, 0, 0]
    for index, part in enumerate(parts):
        chunk = part.strip()
        if not chunk or not chunk.isdigit():
            raise ValueError(f"版本号包含非法数值段：{text!r}")
        numbers[index] = int(chunk)

    return (numbers[0], numbers[1], numbers[2], prerelease.strip())


def _compare_prerelease(left: str, right: str) -> int:
    """比较两个预发布后缀：逐段比较，数字段按数值、其余按字典序。"""
    if left == right:
        return 0
    # 正式版本排在同版本的预发布版本之后。
    if not left:
        return 1
    if not right:
        return -1

    left_parts = left.split(".")
    right_parts = right.split(".")
    for left_part, right_part in zip(left_parts, right_parts):
        left_num = _maybe_int(left_part)
        right_num = _maybe_int(right_part)
        if left_num is not None and right_num is not None:
            if left_num != right_num:
                return -1 if left_num < right_num else 1
        elif left_num is not None:
            # 数字段优先级低于非数字段（与 semver 一致）。
            return -1
        elif right_num is not None:
            return 1
        elif left_part != right_part:
            return -1 if left_part < right_part else 1

    if len(left_parts) != len(right_parts):
        return -1 if len(left_parts) < len(right_parts) else 1
    # 最后再按整串字典序兜底比较。
    return -1 if left < right else 1


def _maybe_int(text: str) -> Optional[int]:
    """尝试把字符串转成整数，失败时返回 ``None``。"""
    chunk = text.strip()
    if not chunk or not chunk.isdigit():
        return None
    return int(chunk)


def compare_versions(left: str, right: str) -> int:
    """比较两个版本号，返回 ``-1`` / ``0`` / ``1``。

    预发布版本排在同号正式版本之前，例如
    ``compare_versions("1.2.0-beta.1", "1.2.0") == -1``。
    """
    left_parts = parse_version(left)
    right_parts = parse_version(right)

    left_core = left_parts[:3]
    right_core = right_parts[:3]
    if left_core != right_core:
        return -1 if left_core < right_core else 1

    return _compare_prerelease(left_parts[3], right_parts[3])


def is_newer_version(candidate: str, current: str) -> bool:
    """判断 ``candidate`` 是否比 ``current`` 更新。"""
    return compare_versions(candidate, current) > 0


def _first_str(payload: Mapping[str, Any], keys: tuple[str, ...]) -> str:
    """按顺序取出第一个存在且非空的字符串字段。"""
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _coerce_int(value: Any) -> int:
    """把体积字段安全地转换成整数，非法值按 0 处理。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        return _maybe_int(value) or 0
    return 0


def _normalize_sha256(value: str) -> str:
    """规范化摘要字符串：去掉 ``sha256:`` 前缀并转成小写。"""
    text = value.strip().lower()
    if text.startswith("sha256:"):
        text = text[len("sha256:") :].strip()
    return text


def default_manifest_url(api_base_url: Optional[str]) -> Optional[str]:
    """由联机服务地址推导更新清单地址；未配置服务器时返回 ``None``。

    ``"https://s.example"`` 与 ``"https://s.example/"`` 都会得到
    ``"https://s.example/api/v1/updates/latest"``；空串、纯空白或 ``None``
    都表示没有配置服务器，此时返回 ``None``。
    """
    if not isinstance(api_base_url, str):
        return None
    base = api_base_url.strip()
    if not base:
        return None
    return base.rstrip("/") + SERVER_UPDATE_PATH


def is_loopback_url(url: str) -> bool:
    """判断 URL 是否指向本机回环地址。

    只有本机地址才允许走明文 HTTP：开发自测常常连本机服务，而明文传输到
    其它主机意味着清单和安装包都可能被中间人替换。
    """
    if not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return False
    if host in _LOOPBACK_HOSTS:
        return True
    # 127.0.0.0/8 整个网段都是回环地址。
    return host.startswith("127.")


def is_secure_url(url: str) -> bool:
    """判断 URL 是否可以安全地用于更新下载。

    HTTPS 一律通过；HTTP 只有在本机回环地址时才通过（开发自测用）。
    """
    if not isinstance(url, str):
        return False
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return True
    if scheme == "http":
        return is_loopback_url(url)
    return False


def parse_manifest(payload: Any) -> UpdateInfo:
    """解析服务端下发的更新清单，返回 :class:`UpdateInfo`。

    只支持服务端清单结构：``version`` / ``url`` / ``sha256`` / ``size`` /
    ``notes`` / ``published_at``。部分字段允许使用别名：

    * 版本号：``version``；
    * 下载地址：``url`` 或 ``download_url``；
    * 摘要：``sha256``（允许带 ``sha256:`` 前缀）；
    * 体积：``size`` 或 ``size_bytes``；
    * 更新说明：``notes`` 或 ``release_notes``。

    下载地址必须满足安全策略：HTTPS，或本机回环地址（开发自测）。
    不满足时抛出 :class:`InsecureUrlError`；其余无法解析的情况抛出
    ``ValueError``。
    """
    if not isinstance(payload, Mapping):
        raise ValueError("更新清单必须是 JSON 对象")

    raw_size = payload.get("size")
    if raw_size is None:
        raw_size = payload.get("size_bytes")

    info = UpdateInfo(
        version=_first_str(payload, ("version",)),
        url=_first_str(payload, ("url", "download_url")),
        sha256=_normalize_sha256(_first_str(payload, ("sha256",))),
        size_bytes=_coerce_int(raw_size),
        notes=_first_str(payload, ("notes", "release_notes")),
        published_at=_first_str(payload, ("published_at",)),
    )

    if not info.version:
        raise ValueError("更新清单缺少版本号")
    if not info.url:
        raise ValueError("更新清单缺少下载地址")

    parsed = urlparse(info.url)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"更新下载地址必须是 http/https URL：{info.url!r}")
    if not is_secure_url(info.url):
        # 明文 HTTP 下载安装包意味着中间人可以替换二进制；只有本机
        # 回环地址（开发自测）才允许走 HTTP。
        raise InsecureUrlError(
            f"更新下载地址必须使用 HTTPS（本机回环地址除外）：{info.url!r}"
        )

    return info


def sha256_of_file(path: str | Path) -> str:
    """计算文件的 SHA-256 摘要（流式读取，返回小写十六进制）。"""
    digest = hashlib.sha256()
    with open(Path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_file(path: str | Path, expected_sha256: str) -> bool:
    """校验文件摘要是否与预期一致。

    ``expected_sha256`` 为空表示“不做校验”，此时只要文件存在即返回 ``True``；
    文件不存在一律返回 ``False``。
    """
    target = Path(path)
    if not target.is_file():
        return False

    expected = _normalize_sha256(expected_sha256 or "")
    if not expected:
        return True

    try:
        actual = sha256_of_file(target)
    except OSError:
        return False
    return actual == expected


def format_size(num_bytes: int) -> str:
    """把字节数格式化成便于阅读的字符串，例如 ``860 KB`` / ``12.3 MB``。"""
    try:
        size = int(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    if size <= 0:
        return "0 B"

    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.0f} KB"
    if size < 1024**3:
        return f"{size / 1024**2:.1f} MB"
    if size < 1024**4:
        return f"{size / 1024**3:.1f} GB"
    return f"{size / 1024**4:.1f} TB"
