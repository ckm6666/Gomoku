"""联机控制器共享的纯函数与延迟导入入口。

``host_module`` 延迟导入 :mod:`gomoku.client.online_panel`，既避免
包导入时的循环依赖，也让测试对面板模块级名字（例如
``get_audio_manager``、``save_login``）的 ``mock.patch`` 在控制器里
依然生效。
"""

from __future__ import annotations

import weakref
from datetime import datetime
from typing import Any, Mapping, Optional


class PanelController:
    """控制器基类：弱引用面板，避免与面板形成引用环。

    面板强引用控制器；控制器若再强引用面板，就会形成环。面板被关闭
    （但循环垃圾回收尚未运行）时，界面定时器可能继续触发并访问已失效
    的测试替身。弱引用让面板的生命周期与改造前保持一致。
    """

    def __init__(self, panel: Any) -> None:
        self._panel_ref = weakref.ref(panel)

    @property
    def panel(self) -> Any:
        """返回所属面板；面板已销毁时抛出 ``RuntimeError``。"""
        panel = self._panel_ref()
        if panel is None:
            raise RuntimeError("OnlinePanel 已销毁")
        return panel


def host_module() -> Any:
    """返回 ``online_panel`` 模块对象（延迟导入）。"""
    from gomoku.client import online_panel

    return online_panel


def format_game_time(value: object) -> str:
    """把服务器返回的 ISO 时间格式化为本地 ``月-日 时:分``。"""
    if not isinstance(value, str) or not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:16]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%m-%d %H:%M")


def result_label(value: object) -> str:
    """把 ``win/loss/draw`` 映射为中文结果。"""
    if not isinstance(value, str):
        return "—"
    return {
        "win": "胜",
        "loss": "负",
        "draw": "和",
    }.get(value, "—")


def format_clock_ms(value: Optional[int]) -> str:
    """把 Fischer 余量格式化为 M:SS（小时以上显示 H:MM:SS）。"""
    if value is None:
        return "—"
    total = max(0, int(value) // 1000)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def entry_avatar_version(entry: Mapping) -> int:
    """从资料字典里安全读取头像版本号。"""
    value = entry.get("avatar_version", 0)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return 0
