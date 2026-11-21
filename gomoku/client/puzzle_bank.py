"""残局题库加载。

题库文件位于 ``gomoku/client/assets/puzzles/puzzles.json``，与 UI/音频素材
一起由 PyInstaller 打包。单个关卡损坏时跳过并记录日志，避免整个入口不可用。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from gomoku.client.ui_theme import assets_root
from gomoku.shared.puzzle import Puzzle, PuzzleError

logger = logging.getLogger(__name__)


def puzzles_directory() -> Path:
    """返回残局资源目录；兼容源码与 PyInstaller。"""
    return assets_root() / "puzzles"


def default_puzzle_bank_path() -> Path:
    return puzzles_directory() / "puzzles.json"


def load_puzzle_bank(path: Optional[Path] = None) -> tuple[Puzzle, ...]:
    """读取并校验题库；文件缺失/整体损坏时返回空元组。"""
    bank_path = Path(path) if path is not None else default_puzzle_bank_path()
    try:
        raw: Any = json.loads(bank_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.warning("残局题库加载失败 path=%s：%s", bank_path, exc)
        return ()

    if not isinstance(raw, dict):
        logger.warning("残局题库根节点必须是对象")
        return ()
    items = raw.get("puzzles")
    if not isinstance(items, list):
        logger.warning("残局题库缺少 puzzles 数组")
        return ()

    puzzles: list[Puzzle] = []
    seen: set[str] = set()
    for index, item in enumerate(items):
        try:
            puzzle = Puzzle.from_dict(item)
        except (PuzzleError, TypeError, ValueError) as exc:
            logger.warning("跳过无效残局 #%s：%s", index, exc)
            continue
        if puzzle.id in seen:
            logger.warning("跳过重复残局 id：%s", puzzle.id)
            continue
        seen.add(puzzle.id)
        puzzles.append(puzzle)

    return tuple(puzzles)


__all__ = [
    "default_puzzle_bank_path",
    "load_puzzle_bank",
    "puzzles_directory",
]
