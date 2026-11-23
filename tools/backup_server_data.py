"""备份服务端 SQLite 数据库，并清理过期备份。

用法：
    python tools/backup_server_data.py
    python tools/backup_server_data.py --keep 60
    python tools/backup_server_data.py --db server/data/gomoku.db --out-dir D:\\backups

默认备份 ``server/data/gomoku.db`` 到 ``server/data/backups/``。
如果设置了 ``GOMOKU_DATABASE_URL`` 且指向 PostgreSQL，请改用 ``pg_dump``。
备份使用 SQLite 在线备份 API，服务端运行中也可以安全执行。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "server" / "data" / "gomoku.db"
DEFAULT_OUT_DIR = ROOT / "server" / "data" / "backups"
BACKUP_PREFIX = "gomoku-"


def sqlite_path_from_url(url: str) -> Optional[Path]:
    """把 SQLite 数据库 URL 转成文件路径；非 SQLite 返回 None。"""
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            return Path(url[len(prefix) :]).expanduser()
    return None


def resolve_database(explicit: Optional[str]) -> Path:
    """确定要备份的数据库文件，无法处理时抛出 SystemExit。"""
    if explicit:
        return Path(explicit).expanduser()
    raw = os.environ.get("GOMOKU_DATABASE_URL", "").strip()
    if raw:
        path = sqlite_path_from_url(raw)
        if path is None:
            raise SystemExit(
                "当前 GOMOKU_DATABASE_URL 不是 SQLite；"
                "PostgreSQL 请使用 pg_dump 备份。"
            )
        return path
    return DEFAULT_DB


def backup_database(db_path: Path, out_dir: Path) -> Path:
    """用 SQLite 在线备份 API 生成一致性快照。"""
    if not db_path.exists():
        raise FileNotFoundError(f"数据库不存在：{db_path}")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    dest = out_dir / f"{BACKUP_PREFIX}{stamp}.db"
    source = sqlite3.connect(str(db_path), timeout=15)
    try:
        target = sqlite3.connect(str(dest))
        try:
            source.backup(target)
            check = target.execute("PRAGMA integrity_check").fetchone()
        finally:
            target.close()
    finally:
        source.close()
    if not check or check[0] != "ok":
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"备份完整性检查失败：{check}")
    return dest


def prune_backups(out_dir: Path, keep: int) -> list[Path]:
    """按文件名时间戳保留最近 keep 份备份，返回被删除的文件。"""
    if keep < 1:
        raise ValueError("keep 必须至少为 1")
    backups = sorted(out_dir.glob(f"{BACKUP_PREFIX}*.db"))
    removed: list[Path] = []
    for path in backups[:-keep] if len(backups) > keep else []:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="备份 Gomoku 服务端数据库")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite 数据库路径（默认 server/data/gomoku.db 或 GOMOKU_DATABASE_URL）",
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUT_DIR),
        help=f"备份目录（默认 {DEFAULT_OUT_DIR}）",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=30,
        help="保留最近多少份备份（默认 30）",
    )
    args = parser.parse_args(argv)
    if args.keep < 1:
        print("--keep 必须至少为 1", file=sys.stderr)
        return 2

    db_path = resolve_database(args.db)
    out_dir = Path(args.out_dir).expanduser()
    try:
        dest = backup_database(db_path, out_dir)
        removed = prune_backups(out_dir, args.keep)
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        print(f"[!] 备份失败：{exc}", file=sys.stderr)
        return 1

    size_mb = dest.stat().st_size / 1024 / 1024
    print(f"[OK] 已备份 {db_path} -> {dest}（{size_mb:.2f} MB）")
    if removed:
        print(f"     已清理 {len(removed)} 份过期备份（保留 {args.keep} 份）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
