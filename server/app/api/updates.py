"""客户端更新清单接口。

服务端有两种提供更新的方式：

1. 配置了 `GOMOKU_UPDATE_MANIFEST_PATH` 时，读取本地 JSON 更新清单文件
   （兼容 GitHub Release 或手工维护的清单）；
2. 只配置了 `GOMOKU_UPDATE_ARTIFACTS_DIR` 时，服务端扫描该目录下的
   `Gomoku-<版本号>.exe` 安装包，自行生成清单并直接托管下载。

两种方式都不需要登录：客户端在启动阶段自动检查更新时未必已经拿到 token。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

router = APIRouter(tags=["updates"])

logger = logging.getLogger(__name__)

#: 允许的下载协议，只放行 http/https，避免清单指向 file:// 等本地路径。
_ALLOWED_URL_SCHEMES = frozenset({"http", "https"})

#: 安装包命名规则：`Gomoku-<版本号>.exe`，版本号是最多三段的点分十进制数字
#: （与客户端 `parse_version` 接受的段数保持一致，避免下发客户端解析不了的版本）。
_ARTIFACT_NAME_PATTERN = re.compile(r"^Gomoku-(\d+(?:\.\d+){0,2})\.exe$")

#: 可选的同目录说明文件，用来补充更新说明与发布时间。
_SIDECAR_NAME = "update.json"

#: 计算摘要时的分块大小，避免把整个安装包一次性读入内存。
_CHUNK_SIZE = 1024 * 1024

#: 安装包摘要缓存，键为 (绝对路径, 文件大小, 修改时间)。
#: 50MB 级别的安装包每次请求都重新计算摘要会拖慢事件循环，
#: 因此按文件内容特征缓存，文件被替换后会自动失效。
_SHA256_CACHE: dict[tuple[str, int, int], str] = {}

#: 摘要缓存的最大条目数；超出后丢弃最旧的条目，避免长期运行时无限增长。
_SHA256_CACHE_MAX_ENTRIES = 64


class UpdateManifestResponse(BaseModel):
    """更新清单内容。"""

    version: str
    url: str
    sha256: Optional[str] = None
    size: Optional[int] = None
    notes: Optional[str] = None
    published_at: Optional[str] = None


def _manifest_error(
    status_code: int, detail: str
) -> HTTPException:
    """构造带中文提示的 HTTP 异常。"""
    return HTTPException(status_code=status_code, detail=detail)


def _read_manifest(path: Path) -> dict[str, Any]:
    """读取并解析清单文件；读取或解析失败时抛出 500。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "更新清单读取失败 path=%s error=%s", path, exc.__class__.__name__
        )
        raise _manifest_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "更新清单文件无法读取"
        ) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("更新清单不是合法 JSON path=%s", path)
        raise _manifest_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "更新清单文件无法读取"
        ) from exc
    if not isinstance(data, dict):
        raise _manifest_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "更新清单格式不正确"
        )
    return data


def _text_field(data: dict[str, Any], name: str) -> Optional[str]:
    """取出一个可选的字符串字段；类型不符时返回 None。"""
    value = data.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _size_field(data: dict[str, Any]) -> Optional[int]:
    """取出一个可选的文件大小字段；类型不符时返回 None。"""
    value = data.get("size")
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_utc(timestamp: float) -> str:
    """把 POSIX 时间戳格式化成 UTC 的 ISO-8601 字符串。"""
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _absolute_path(path: Path) -> str:
    """取绝对路径；解析失败时退回到不解析符号链接的绝对路径。"""
    try:
        return str(path.resolve())
    except OSError:  # pragma: no cover - 极少见的文件系统异常
        return str(path.absolute())


def _sha256_of(path: Path) -> str:
    """计算文件的 SHA-256（小写十六进制）。

    结果按 (绝对路径, 文件大小, 修改时间) 缓存，避免同一份安装包被反复
    读取计算；文件一旦被替换（大小或修改时间变化）缓存自动失效。
    """
    stat = path.stat()
    key = (_absolute_path(path), stat.st_size, stat.st_mtime_ns)
    cached = _SHA256_CACHE.get(key)
    if cached is not None:
        return cached

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    value = digest.hexdigest()
    _SHA256_CACHE[key] = value
    # 缓存不能无限增长：超出上限时丢弃最旧的条目。
    while len(_SHA256_CACHE) > _SHA256_CACHE_MAX_ENTRIES:
        _SHA256_CACHE.pop(next(iter(_SHA256_CACHE)))
    return value


def _find_latest_artifact(directory: Path) -> Optional[tuple[str, Path]]:
    """在目录中找出版本号最高的安装包。

    只匹配 `Gomoku-<版本号>.exe`（版本号为点分十进制数字），目录不存在
    或没有符合条件的文件时返回 None。
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        logger.warning("更新包目录不可读 dir=%s", directory)
        return None

    best: Optional[tuple[tuple[int, ...], str, Path]] = None
    for entry in entries:
        match = _ARTIFACT_NAME_PATTERN.match(entry.name)
        if match is None:
            continue
        try:
            if not entry.is_file():
                continue
        except OSError:
            continue
        version_text = match.group(1)
        parts = tuple(int(item) for item in version_text.split("."))
        if best is None or parts > best[0]:
            best = (parts, version_text, entry)
    if best is None:
        return None
    return best[1], best[2]


def _read_sidecar(directory: Path) -> tuple[str, Optional[str]]:
    """读取同目录下可选的 `update.json`。

    返回 (更新说明, 发布时间)；文件缺失或内容非法时分别返回 ("", None)，
    不影响主流程。
    """
    path = directory / _SIDECAR_NAME
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("更新说明文件不是合法 JSON path=%s", path)
        return "", None
    if not isinstance(data, dict):
        return "", None
    return _text_field(data, "notes") or "", _text_field(data, "published_at")


def _is_safe_filename(filename: str) -> bool:
    """判断文件名是否安全：不含路径分隔符、`..`、盘符或空字节。"""
    if not filename or filename in {".", ".."}:
        return False
    if "\x00" in filename:
        return False
    if ".." in filename:
        return False
    if "/" in filename or "\\" in filename:
        return False
    if ":" in filename:
        return False
    if os.sep in filename or (os.altsep and os.altsep in filename):
        return False
    if filename != Path(filename).name:
        return False
    return True


def _manifest_from_file(path: Path) -> UpdateManifestResponse:
    """从本地 JSON 清单文件生成响应。"""
    try:
        exists = path.is_file()
    except OSError:
        exists = False
    if not exists:
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "更新清单文件不存在"
        )

    data = _read_manifest(path)

    version = _text_field(data, "version")
    url = _text_field(data, "url")
    if not version or not url:
        raise _manifest_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "更新清单格式不正确"
        )
    scheme = urlparse(url).scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise _manifest_error(
            status.HTTP_503_SERVICE_UNAVAILABLE, "更新清单格式不正确"
        )

    return UpdateManifestResponse(
        version=version,
        url=url,
        sha256=_text_field(data, "sha256"),
        size=_size_field(data),
        notes=_text_field(data, "notes"),
        published_at=_text_field(data, "published_at"),
    )


async def _manifest_from_artifacts(
    request: Request, directory: Path
) -> UpdateManifestResponse:
    """根据服务端托管的安装包生成响应。"""
    found = _find_latest_artifact(directory)
    if found is None:
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "尚未上传任何更新包"
        )
    version_text, path = found

    try:
        stat = path.stat()
    except OSError as exc:
        raise _manifest_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "更新包文件无法读取"
        ) from exc
    try:
        # 摘要计算放到工作线程里，避免阻塞事件循环。
        sha256 = await run_in_threadpool(_sha256_of, path)
    except OSError as exc:
        logger.warning("更新包摘要计算失败 path=%s", path)
        raise _manifest_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR, "更新包文件无法读取"
        ) from exc

    notes, published_at = _read_sidecar(directory)
    if not published_at:
        published_at = _iso_utc(stat.st_mtime)

    filename = path.name
    base_url = str(request.base_url).rstrip("/")
    url = f"{base_url}/api/v1/updates/download/{quote(filename)}"

    return UpdateManifestResponse(
        version=version_text,
        url=url,
        sha256=sha256,
        size=stat.st_size,
        notes=notes,
        published_at=published_at,
    )


@router.get("/updates/latest", response_model=UpdateManifestResponse)
async def latest_update(request: Request) -> UpdateManifestResponse:
    """返回当前可用的客户端更新清单。

    优先使用配置的清单文件；没有配置清单文件、但配置了安装包目录时，
    由服务端根据自己托管的安装包生成清单。
    """
    settings = request.app.state.settings
    raw_path = getattr(settings, "update_manifest_path", None)
    if raw_path:
        return _manifest_from_file(Path(raw_path))

    raw_dir = getattr(settings, "update_artifacts_dir", None)
    if raw_dir:
        return await _manifest_from_artifacts(request, Path(raw_dir))

    raise _manifest_error(
        status.HTTP_503_SERVICE_UNAVAILABLE, "服务端未配置更新清单"
    )


@router.get("/updates/download/{filename}")
async def download_update(filename: str, request: Request) -> FileResponse:
    """下载服务端托管的客户端安装包。"""
    settings = request.app.state.settings
    raw_dir = getattr(settings, "update_artifacts_dir", None)
    if not raw_dir:
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "服务端未配置更新包目录"
        )

    if not _is_safe_filename(filename):
        logger.warning("拒绝非法的更新包文件名 filename=%r", filename)
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "更新包不存在"
        )

    base = Path(raw_dir)
    try:
        resolved_base = base.resolve()
        target = (base / filename).resolve()
    except OSError as exc:
        logger.warning("更新包目录不可读 dir=%s", base)
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "更新包不存在"
        ) from exc

    # 只允许下载安装包目录下的文件，防止越权访问其它路径。
    if target.parent != resolved_base:
        logger.warning("拒绝越界的更新包请求 filename=%r", filename)
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "更新包不存在"
        )

    try:
        if not target.is_file():
            raise _manifest_error(
                status.HTTP_404_NOT_FOUND, "更新包不存在"
            )
    except OSError as exc:
        raise _manifest_error(
            status.HTTP_404_NOT_FOUND, "更新包不存在"
        ) from exc

    media_type = (
        "application/vnd.microsoft.portable-executable"
        if filename.lower().endswith(".exe")
        else "application/octet-stream"
    )
    return FileResponse(
        target, media_type=media_type, filename=filename
    )


__all__ = ["router", "UpdateManifestResponse"]
