"""运行时打包状态探测。

PyInstaller 的 onefile/onedir 产物会在 `sys` 上注入 `frozen` 属性，
源码运行时该属性不存在。客户端多处需要区分「打包产物」与「源码运行」，
集中在这里避免各模块各写一份 `getattr(sys, "frozen", False)`。
"""

from __future__ import annotations

import sys


def is_frozen() -> bool:
    """当前进程是否为 PyInstaller 打包产物。"""
    return bool(getattr(sys, "frozen", False))


__all__ = ["is_frozen"]
