"""备份服务端 SQLite 数据库，并清理过期备份。

用法：
    python tools/backup_server_data.py
    python tools/backup_server_data.py --keep 60
    python tools/backup_server_data.py --db server/data/gomoku.db --out-dir D:\\backups

默认备份 ``server/data/gomoku.db`` 到 ``server/data/backups/``。
如果设置了 ``GOMOKU_DATABASE_URL`` 且指向 PostgreSQL，请改用 ``pg_dump``。
备份使用 SQLite 在线备份 API，服务端运行中也可以安全执行。

**时间戳策略**：备份文件名里的时间戳统一使用 **UTC**（形如
``gomoku-20260101-120000-000000Z.db``，末尾 ``Z`` 表示 UTC），与服务端
日志的 UTC 策略一致。这样同一批备份在任何时区排出来的顺序都相同，
也不会因为夏令时/换时区出现重复或错序的文件名。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    # 让脚本能直接 `python tools/backup_server_data.py` 运行：
    # 从仓库根目录导入服务端的数据库 URL 解析（无第三方依赖）。
    sys.path.insert(0, str(ROOT))

from server.app.config import sqlite_path_from_url  # noqa: E402

DEFAULT_DB = ROOT / "server" / "data" / "gomoku.db"
DEFAULT_OUT_DIR = ROOT / "server" / "data" / "backups"
BACKUP_PREFIX = "gomoku-"


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
    # 统一用 UTC 打戳，末尾 Z 标明时区。
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%fZ")
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
