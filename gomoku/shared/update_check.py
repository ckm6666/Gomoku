"""更新检查的数据模型与版本比较规则。

本模块是纯 Python（仅标准库），刻意不引入任何 Qt 依赖：HTTP 客户端层
负责请求 GitHub Releases API，界面层负责展示，两者都复用这里的数据模型
与版本比较逻辑，避免出现“两套判断规则”导致客户端误报更新。

版本元数据一律来自 GitHub Releases API；这里只负责把接口返回的 JSON
变成可比较、可展示的数据结构。发布产物自带的 ``digest`` 摘要也在这一层
解析：客户端下载完成后要用它校验安装包，规则放在这里才不会散落两套。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Optional

#: 版本号中的数字部分，允许缺省次版本号与修订号（如 ``1``/``1.13``）。
#: 只从字符串开头匹配：若允许在任意位置搜索，"nightly-20241120" 这类标签
#: 会被读成 20241120.0.0，从而误报“有新版本”。
_VERSION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[vV]?(\d+)(?:\.(\d+))?(?:\.(\d+))?"
)

#: 发布说明摘要的默认最大字符数。
NOTES_SUMMARY_LIMIT: Final[int] = 400

#: 摘要被截断时追加的省略号。
ELLIPSIS: Final[str] = "…"

#: Windows x64 客户端压缩包的文件名后缀。
WINDOWS_ASSET_SUFFIX: Final[str] = "-windows-x64.zip"

#: Windows x64 客户端压缩包的文件名模板。
WINDOWS_ASSET_NAME_TEMPLATE: Final[str] = "Gomoku-{version}-windows-x64.zip"

#: GitHub 资产摘要的写法：``sha256:<64 位十六进制>``。
_DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^sha256:([0-9a-fA-F]{64})$"
)


@dataclass(frozen=True)
class ReleaseAsset:
    """一个可下载的发布产物。"""

    name: str
    download_url: str
    size: int
    #: GitHub 给出的产物摘要，形如 ``sha256:<64 位十六进制>``；可能为空串。
    digest: str = ""


@dataclass(frozen=True)
class ReleaseInfo:
    """一次 GitHub Release 的结构化信息。"""

    tag: str
    version: str
    title: str
    url: str
    notes: str
    published_at: str
    assets: tuple[ReleaseAsset, ...]
    prerelease: bool


def _scan_version(text: str) -> Optional[tuple[tuple[int, int, int], str]]:
    """扫描文本开头的版本号。

    返回 ``((major, minor, patch), 余下文本)``；余下文本用于判断预发布
    后缀（例如 ``1.13.0-rc.1`` 中的 ``-rc.1``）。开头不是版本号时返回
    ``None``——标签可能是 ``nightly`` 这类与版本无关的字符串。
    """
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    match = _VERSION_PATTERN.match(stripped)
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2)) if match.group(2) else 0
    patch = int(match.group(3)) if match.group(3) else 0
    return (major, minor, patch), stripped[match.end() :]


def _strip_leading_v(text: str) -> str:
    """去掉版本号前缀的单个 ``v``（大小写均可）。"""
    if text[:1] in ("v", "V"):
        return text[1:]
    return text


def _is_prerelease(remainder: str) -> bool:
    """判断版本号数字部分之后是否带有 ``-``/``+`` 后缀。

    预发布版本（``1.13.0-rc.1``）排在同版本正式版（``1.13.0``）之前，
    否则 RC 版会被误判成“有新版本”。
    """
    return remainder[:1] in ("-", "+")


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """解析版本号文本，返回 ``(major, minor, patch)``。

    支持 ``1.13.0``、``v1.13.0``、``1.13``（修订号补 0）以及带预发布后缀
    的写法。任何无法解析的输入都返回 ``None``，本函数不会抛异常。
    """
    scanned = _scan_version(text)
    if scanned is None:
        return None
    return scanned[0]


def compare_versions(left: str, right: str) -> int:
    """比较两个版本号，返回 ``-1``/``0``/``1``（left 相对 right）。

    前导 ``v`` 会被忽略。带预发布后缀的版本排在同版本正式版之前；
    不同后缀之间视为相等（客户端只需区分“有/无预发布”）。
    任一侧无法解析时抛 ``ValueError``。
    """
    left_scanned = _scan_version(left)
    right_scanned = _scan_version(right)
    if left_scanned is None or right_scanned is None:
        raise ValueError(f"无法解析版本号：{left!r} / {right!r}")

    left_number, left_remainder = left_scanned
    right_number, right_remainder = right_scanned
    if left_number != right_number:
        return -1 if left_number < right_number else 1

    left_pre = _is_prerelease(left_remainder)
    right_pre = _is_prerelease(right_remainder)
    if left_pre == right_pre:
        return 0
    return -1 if left_pre else 1


def is_newer_version(latest: str, current: str) -> bool:
    """判断 ``latest`` 是否比 ``current`` 新。

    任一侧无法解析时返回 ``False``：宁可漏报更新，也不要因为版本号格式
    异常而打扰玩家。
    """
    try:
        return compare_versions(latest, current) > 0
    except ValueError:
        return False


def _clean_string(value: Any) -> str:
    """把任意值转成字符串；非字符串一律返回空串。"""
    return value if isinstance(value, str) else ""


def _parse_assets(payload: Mapping[str, Any]) -> tuple[ReleaseAsset, ...]:
    """从发布信息里挑出可用的下载产物。

    GitHub 的 ``assets`` 数组可能混入非对象或缺字段的条目，这些条目对
    下载毫无意义，直接过滤掉，避免界面层拿到半截数据。
    """
    raw_assets = payload.get("assets")
    if not isinstance(raw_assets, (list, tuple)):
        return ()

    assets: list[ReleaseAsset] = []
    for item in raw_assets:
        if not isinstance(item, Mapping):
            continue
        name = item.get("name")
        download_url = item.get("browser_download_url")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(download_url, str) or not download_url.strip():
            continue
        raw_size = item.get("size")
        if isinstance(raw_size, bool) or not isinstance(raw_size, int):
            size = 0
        else:
            size = max(0, raw_size)
        raw_digest = item.get("digest")
        digest = raw_digest if isinstance(raw_digest, str) else ""
        assets.append(
            ReleaseAsset(
                name=name,
                download_url=download_url,
                size=size,
                digest=digest,
            )
        )
    return tuple(assets)


def parse_release_payload(payload: Mapping[str, Any]) -> ReleaseInfo:
    """把 GitHub ``/releases/latest`` 返回的 JSON 解析成 ``ReleaseInfo``。

    缺少 ``tag_name``（或不是非空字符串）时抛 ``ValueError``：没有标签就
    无法判断版本，任何“猜测”都可能让客户端下载错误的安装包。
    """
    raw_tag = payload.get("tag_name")
    if not isinstance(raw_tag, str) or not raw_tag.strip():
        raise ValueError("发布信息缺少有效的 tag_name")
    tag = raw_tag.strip()

    scanned = _scan_version(tag)
    if scanned is None:
        # 标签形如 “nightly” 时无从比较版本，退化为去掉前缀的标签原文。
        version = _strip_leading_v(tag)
    else:
        major, minor, patch = scanned[0]
        suffix = scanned[1].strip()
        # 预发布后缀必须保留：``v1.13.0-rc.1`` 若被截成 ``1.13.0``，
        # 客户端会报出一个并不存在的正式版，并指向 RC 并不存在的压缩包。
        version = f"{major}.{minor}.{patch}"
        if suffix[:1] in ("-", "+"):
            version = f"{version}{suffix}"

    name = payload.get("name")
    title = name if isinstance(name, str) and name.strip() else tag

    return ReleaseInfo(
        tag=tag,
        version=version,
        title=title,
        url=_clean_string(payload.get("html_url")),
        notes=_clean_string(payload.get("body")),
        published_at=_clean_string(payload.get("published_at")),
        assets=_parse_assets(payload),
        prerelease=bool(payload.get("prerelease")),
    )


def select_windows_asset(release: ReleaseInfo) -> Optional[ReleaseAsset]:
    """从发布信息里挑出 Windows x64 客户端压缩包。

    优先匹配发布流水线约定的规范文件名；其次匹配任意 ``-windows-x64.zip``
    后缀；再次退而求其次匹配名字里带 ``windows`` 的 ``.zip``。都没有时
    返回 ``None``，由界面层提示“该版本未提供 Windows 安装包”。
    """
    exact_name = WINDOWS_ASSET_NAME_TEMPLATE.format(version=release.version)
    for asset in release.assets:
        if asset.name == exact_name:
            return asset

    suffix = WINDOWS_ASSET_SUFFIX.lower()
    for asset in release.assets:
        if asset.name.lower().endswith(suffix):
            return asset

    for asset in release.assets:
        lowered = asset.name.lower()
        if lowered.endswith(".zip") and "windows" in lowered:
            return asset
    return None


def normalize_digest(digest: str) -> Optional[str]:
    """把 GitHub 的资产摘要归一成裸的小写十六进制 SHA-256。

    只认 ``sha256:<64 位十六进制>`` 这一种写法（GitHub 目前只给 sha256）；
    空串、别的算法或位数不对都返回 ``None``，调用方据此决定「无法校验」
    而不是「校验通过」。
    """
    if not isinstance(digest, str):
        return None
    match = _DIGEST_PATTERN.match(digest.strip())
    if match is None:
        return None
    return match.group(1).lower()


def summarize_notes(notes: str, limit: int = NOTES_SUMMARY_LIMIT) -> str:
    """把发布说明压缩成单行摘要。

    GitHub 的 ``body`` 往往是带换行与列表的 Markdown，直接塞进气泡对话框
    会撑破布局；这里把所有空白折叠成单个空格，超长时截断并追加省略号。
    """
    if not isinstance(notes, str):
        return ""
    collapsed = " ".join(notes.split())
    cut = max(0, limit)
    if len(collapsed) > cut:
        return collapsed[:cut] + ELLIPSIS
    return collapsed


__all__ = [
    "ELLIPSIS",
    "NOTES_SUMMARY_LIMIT",
    "WINDOWS_ASSET_NAME_TEMPLATE",
    "WINDOWS_ASSET_SUFFIX",
    "ReleaseAsset",
    "ReleaseInfo",
    "compare_versions",
    "is_newer_version",
    "normalize_digest",
    "parse_release_payload",
    "parse_version",
    "select_windows_asset",
    "summarize_notes",
]
