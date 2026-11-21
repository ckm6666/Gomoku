"""有界置换表。

搜索节点用 Zobrist 哈希索引，保存深度、得分、边界类型与最佳着法。
表大小硬上限，超过容量时优先淘汰上一代的条目，避免长时间运行时内存
无限增长。当前引擎每手创建一次搜索上下文，因此表主要服务单次搜索内
的迭代加深；代际接口为未来跨手复用保留。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Optional

Move = tuple[int, int]


class Bound(IntEnum):
    """置换表条目对应的 alpha-beta 边界类型。"""

    EXACT = 0
    LOWER = 1
    UPPER = 2


@dataclass(slots=True)
class TTEntry:
    """一条置换表记录。"""

    key: int
    depth: int
    score: int
    bound: Bound
    best_move: Optional[Move]
    generation: int


class TranspositionTable:
    """以 Zobrist 哈希为键的有界置换表。"""

    def __init__(self, max_entries: int = 100_000) -> None:
        self.max_entries = max(1, int(max_entries))
        self.generation = 0
        self._entries: dict[int, TTEntry] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def clear(self) -> None:
        """清空全部条目。"""
        self._entries.clear()

    def new_generation(self) -> None:
        """进入新一代；旧代条目在容量压力下优先淘汰。"""
        self.generation += 1

    def probe(self, key: int) -> Optional[TTEntry]:
        """按哈希读取条目；不存在返回 None。"""
        return self._entries.get(key)

    def store(
        self,
        key: int,
        depth: int,
        score: int,
        bound: Bound,
        best_move: Optional[Move],
    ) -> None:
        """写入/替换条目。

        同一局面已有更深的结果时保留旧条目；否则用当前结果覆盖。
        容量不足时先清理旧代条目，仍不足则整体清空。
        """
        existing = self._entries.get(key)
        if (
            existing is not None
            and existing.generation == self.generation
            and existing.depth > depth
        ):
            return

        if (
            existing is None
            and len(self._entries) >= self.max_entries
        ):
            stale = [
                entry_key
                for entry_key, entry in self._entries.items()
                if entry.generation < self.generation
            ]
            if stale:
                for entry_key in stale:
                    del self._entries[entry_key]
            else:
                self._entries.clear()

        self._entries[key] = TTEntry(
            key=key,
            depth=depth,
            score=score,
            bound=bound,
            best_move=best_move,
            generation=self.generation,
        )


__all__ = ["Bound", "TTEntry", "TranspositionTable"]
