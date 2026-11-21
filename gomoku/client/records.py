"""本地棋谱存储（JSON Lines）。

默认写入 ``%LOCALAPPDATA%/Gomoku/Gomoku/records/games.jsonl``。每局一行，
读取时跳过损坏行；超过上限后按时间裁剪，避免文件无限增长。联机棋谱由
服务端保存，不写入本地。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QStandardPaths

from gomoku.shared.record import GameRecord, RecordError

logger = logging.getLogger(__name__)

_RECORDS_FILENAME = "games.jsonl"


def default_records_directory() -> Path:
    """返回应用本地数据目录下的 records 目录。"""
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppLocalDataLocation
    )
    if not base:
        base = str(Path.home() / ".gomoku" / "Gomoku")
    return Path(base) / "records"


class LocalRecordStore:
    """追加式本地棋谱仓库。"""

    def __init__(
        self,
        directory: Optional[Path] = None,
        max_records: int = 1000,
    ) -> None:
        self._directory = (
            Path(directory) if directory is not None else default_records_directory()
        )
        self._max_records = max(1, int(max_records))

    @property
    def directory(self) -> Path:
        return self._directory

    @property
    def path(self) -> Path:
        return self._directory / _RECORDS_FILENAME

    def append(self, record: GameRecord) -> None:
        """追加一条棋谱；写入失败时记录日志但不向 UI 抛异常。"""
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                record.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            logger.exception("写入本地棋谱失败 path=%s", self.path)
            return
        self._prune_if_needed()

    def list_recent(self, limit: int = 50) -> tuple[GameRecord, ...]:
        """按创建时间倒序返回最近棋谱。"""
        if limit <= 0:
            return ()
        records = self._read_all()
        records.sort(key=lambda record: record.created_at, reverse=True)
        return tuple(records[:limit])

    def load(self, record_id: str) -> Optional[GameRecord]:
        for record in self._read_all():
            if record.record_id == record_id:
                return record
        return None

    def count(self) -> int:
        return len(self._read_all())

    # ------------------------------------------------------------------
    # 内部读写
    # ------------------------------------------------------------------
    def _read_all(self) -> list[GameRecord]:
        if not self.path.is_file():
            return []

        records: list[GameRecord] = []
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        data = json.loads(text)
                        records.append(GameRecord.from_dict(data))
                    except (
                        json.JSONDecodeError,
                        RecordError,
                        TypeError,
                        ValueError,
                    ) as exc:
                        logger.warning(
                            "跳过损坏棋谱 %s:%s：%s",
                            self.path,
                            line_number,
                            exc,
                        )
        except OSError:
            logger.exception("读取本地棋谱失败 path=%s", self.path)
        return records

    def _prune_if_needed(self) -> None:
        records = self._read_all()
        if len(records) <= self._max_records:
            return

        records.sort(key=lambda record: record.created_at)
        keep = records[-self._max_records :]
        temp_path = self.path.with_suffix(".jsonl.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                for record in keep:
                    handle.write(
                        json.dumps(
                            record.to_dict(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                        + "\n"
                    )
            temp_path.replace(self.path)
        except OSError:
            logger.exception("裁剪本地棋谱失败 path=%s", self.path)


__all__ = ["LocalRecordStore", "default_records_directory"]
