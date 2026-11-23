"""构建期固化的配置（服务地址、更新清单地址、启动自动检查开关）。

打包脚本 :mod:`build` 会在调用 PyInstaller 之前把配置写入
``build/build_info.json``，本模块在运行时读取它。这样分发的 exe 不需要
用户手工设置环境变量也能正常联网，并且用户可以从日志里看到程序到底去
哪里检查更新。

读取失败（文件缺失、无法读取、JSON 非法、格式不对）时一律退化为默认值，
绝不抛出异常，避免因配置问题阻止程序启动。
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: 构建期配置文件名（打包后位于 exe 解包目录根部）。
BUILD_INFO_FILENAME: str = "build_info.json"


@dataclass(frozen=True)
class BuildInfo:
    """构建期固化的非敏感配置。"""

    api_base_url: str = ""
    update_manifest_url: str = ""
    auto_update_check: Optional[bool] = None


def build_info_path() -> Path:
    """返回 ``build_info.json`` 的路径。

    打包后（PyInstaller onefile）文件位于解包目录根部；开发时位于本模块
    所在目录。返回值总是绝对路径。

    开发时**不**去读仓库根目录的 ``build/``：那里正是 ``build.py`` 写盘的
    位置，一旦开发者在本地打过包，测试与开发启动就会读到上一次的固化
    配置，行为随工作区状态漂移。开发时用环境变量
    ``GOMOKU_API_BASE_URL`` / ``GOMOKU_UPDATE_MANIFEST_URL`` 覆盖即可。
    """
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = Path(__file__).parent
    return base / BUILD_INFO_FILENAME


def _text(value: Any) -> str:
    """把 JSON 里的字符串字段规范化成字符串；其他类型视为未提供。"""
    if isinstance(value, str):
        return value.strip()
    return ""


def load_build_info() -> BuildInfo:
    """读取构建期固化的配置；任何异常都退化为默认值。"""
    try:
        path = build_info_path()
        if not path.is_file():
            return BuildInfo()
        raw = path.read_bytes()
    except OSError:
        return BuildInfo()

    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return BuildInfo()
    if not isinstance(payload, dict):
        return BuildInfo()

    auto = payload.get("auto_update_check")
    if not isinstance(auto, bool):
        # 非布尔值一律视为未提供，避免把 "false" 之类的字符串当成真值。
        auto = None

    return BuildInfo(
        api_base_url=_text(payload.get("api_base_url")),
        update_manifest_url=_text(payload.get("update_manifest_url")),
        auto_update_check=auto,
    )


__all__ = ["BUILD_INFO_FILENAME", "BuildInfo", "build_info_path", "load_build_info"]
