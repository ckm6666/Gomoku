"""生成 GitHub Release 的发布说明。

发布流水线（.github/workflows/release.yml）调用本脚本，把 README 的「当前版本：」
那一行转成 Release 正文。

README 的那一行是滚动写法：本版说明在最前，历史上其它版本用分号依次跟在后面，
例如::

    当前版本：**1.14.0**（本版说明；1.13.0 的说明；1.12.0 的说明）

Release 页面只需要本版内容，所以本脚本会：

- 取出圆括号里的正文；
- 在遇到下一个版本号时截断，丢掉后面所有历史版本的部分；
- 按分号拆成 Markdown 列表，让每条说明单独成行；
- 限制总长度，避免出现几百字没有换行的一整段。

用法::

    VERSION=1.14.0 TAG=v1.14.0 REPO=ckm6666/Gomoku python tools/compose_release_notes.py
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from string import Template

#: 发布说明正文的长度上限。超过时按行截断，避免 Release 页面被长文本撑开。
MAX_NOTES_CHARS = 1200

#: README 中标注当前版本的那一行前缀。
CURRENT_VERSION_PREFIX = "当前版本："

#: 历史版本号的起点。版本号总是紧跟分号出现，这样像 ``Windows 11.2.1`` 这样的
#: 普通数字不会被误判成历史版本。
VERSION_MARKER = re.compile(r"(?:^|[；;])\s*(?<![\d.])\d+\.\d+\.\d+(?![\d.])")

#: 行首的版本号，例如 ``**1.14.0**``。
LEADING_VERSION = re.compile(r"^\**\s*v?\d+(?:\.\d+)+\**\s*[（(]?")

#: Release 正文的固定结构。``${highlight}`` 是本次版本的更新说明。
NOTES_TEMPLATE = Template(
    """**版本 ${version}** — 由 GitHub Actions 自动发布。

${highlight}

## 发布产物

| 产物 | 说明 |
| --- | --- |
| `Gomoku-${version}-windows-x64.zip` | Windows x64 客户端（`Gomoku.exe`），压缩包内含 `THIRD_PARTY_NOTICES.md` 与字体 OFL 文本 |
| `ghcr.io/${repo_image}/gomoku-server:${version}` | 服务端镜像（同时推送 `:${tag}` 与 `:latest` 标签） |

## 使用提示

- 解压后双击 `Gomoku.exe` 即可运行离线双人与人机模式；联机模式需要另行部署服务端。
- 拉取服务端镜像：`docker pull ghcr.io/${repo_image}/gomoku-server:${version}`
- 打包版默认连接内置的固定公网入口；若要接入自建服务端，请设置
  `GOMOKU_API_BASE_URL` / `GOMOKU_WS_BASE_URL` 环境变量（详见 README）。
- 分发时请保留压缩包内的 `THIRD_PARTY_NOTICES.md` 与 `fonts/OFL-*.txt`，
  以满足 OFL 字体再分发要求。
- 版本说明见 README 顶部的「当前版本」段落。
"""
)


def summary_line(markdown: str) -> str:
    """返回「当前版本：」那一行；没有时返回空串。"""
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith(CURRENT_VERSION_PREFIX):
            return stripped
    return ""


def extract_bracket(text: str) -> str:
    """取出圆括号里的内容，同时支持中文与英文括号。"""
    starts = [pos for pos in (text.find("（"), text.find("(")) if pos >= 0]
    if not starts:
        return ""
    start = min(starts)
    ends = [pos for pos in (text.rfind("）"), text.rfind(")")) if pos > start]
    if not ends:
        return ""
    return text[start + 1 : max(ends)].strip()


def strip_leading_version(text: str) -> str:
    """去掉行首的版本号，例如 ``**1.14.0**``。"""
    return LEADING_VERSION.sub("", text, count=1).strip()


def cut_at_next_version(text: str) -> str:
    """在下一个版本号之前截断，丢掉其后的历史版本说明。

    README 的「当前版本」行把历史版本依次跟在后面，所以本版内容一定是第一个
    版本号之后、第二个版本号之前的那一段。
    """
    match = VERSION_MARKER.search(text)
    if match is None:
        return text.strip()
    return text[: match.start()].strip().rstrip("；;，,、")


def split_sentences(text: str) -> list[str]:
    """按分号把长句拆成短句。"""
    parts = re.split(r"[；;]\s*", text)
    cleaned = []
    for part in parts:
        item = part.strip().strip(" 　，,。.、")
        if item:
            cleaned.append(item)
    return cleaned


def as_bullets(text: str) -> str:
    """把一段长文本转成 Markdown 列表，保证每条单独一行。"""
    stripped = text.strip()
    if not stripped:
        return ""
    if "\n" in stripped:
        return stripped
    items = split_sentences(stripped)
    if not items:
        return ""
    if len(items) == 1:
        return f"- {items[0]}"
    return "\n".join(f"- {item}" for item in items)


def trim_notes(text: str) -> str:
    """按行截断过长的说明，保证 Release 正文不会超出长度上限。"""
    stripped = text.strip()
    if len(stripped) <= MAX_NOTES_CHARS:
        return stripped
    kept: list[str] = []
    total = 0
    for line in stripped.splitlines():
        remaining = MAX_NOTES_CHARS - total
        if remaining <= 1:
            break
        if len(line) + 1 > remaining:
            kept.append(line[:remaining].rstrip())
            break
        kept.append(line)
        total += len(line) + 1
    trimmed = "\n".join(kept).strip()
    if not trimmed:
        trimmed = stripped[:MAX_NOTES_CHARS].rstrip()
    return trimmed + "\n- （说明过长，已截断）"


def version_notes(markdown: str, version: str) -> str:
    """生成本次版本的更新说明正文。"""
    line = summary_line(markdown)
    if not line:
        return ""
    rest = line[len(CURRENT_VERSION_PREFIX) :].strip()
    content = extract_bracket(rest)
    if not content:
        content = strip_leading_version(rest)
    if not content:
        return ""
    return trim_notes(as_bullets(cut_at_next_version(content)))


def build_release_notes(version: str, tag: str, repo_image: str, markdown: str) -> str:
    """组装完整的 Release 正文。"""
    notes = version_notes(markdown, version)
    highlight = notes if notes else "本次更新未附带说明。"
    # 用 substitute 而非 safe_substitute：占位符写错时直接失败，
    # 避免把未替换的 ${...} 原样发布到 Release 说明里。
    return NOTES_TEMPLATE.substitute(
        version=version,
        tag=tag,
        repo_image=repo_image,
        highlight=highlight,
    )


def main() -> int:
    """命令行入口：读取环境变量，生成 release-notes.md。"""
    version = os.environ.get("VERSION", "").strip()
    if not version:
        print("[!] 缺少 VERSION 环境变量", file=sys.stderr)
        return 1
    tag = os.environ.get("TAG", "").strip() or f"v{version}"
    repo = os.environ.get("REPO", "").strip()
    if not repo:
        print("[!] 缺少 REPO 环境变量", file=sys.stderr)
        return 1
    readme_path = Path(os.environ.get("README_PATH", "README.md"))
    notes_path = Path(os.environ.get("NOTES_PATH", "release-notes.md"))
    markdown = readme_path.read_text(encoding="utf-8")
    # GHCR 的镜像路径强制全小写（docker/metadata-action 也会转小写），
    # 仓库名原样（如 ckm6666/Gomoku）会让 docker pull 报
    # "invalid reference format: repository name must be lowercase"。
    notes = build_release_notes(version, tag, repo.lower(), markdown)
    notes_path.write_text(notes, encoding="utf-8")
    print(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
