"""服务端数据库备份脚本的测试。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.backup_server_data import (  # noqa: E402
    backup_database,
    prune_backups,
    resolve_database,
    sqlite_path_from_url,
)


class SqliteUrlTests(unittest.TestCase):
    def test_default_url_forms(self) -> None:
        self.assertEqual(
            sqlite_path_from_url("sqlite+aiosqlite:///C:/data/gomoku.db"),
            Path("C:/data/gomoku.db"),
        )
        self.assertEqual(
            sqlite_path_from_url("sqlite:///C:/data/gomoku.db"),
            Path("C:/data/gomoku.db"),
        )

    def test_non_sqlite_url(self) -> None:
        self.assertIsNone(
            sqlite_path_from_url("postgresql+asyncpg://u:p@db/gomoku")
        )

    def test_database_url_env_is_resolved(self) -> None:
        # 备份工具与服务端引擎用同一份解析逻辑：
        # 同一个 GOMOKU_DATABASE_URL 必须指向同一个文件。
        previous = os.environ.get("GOMOKU_DATABASE_URL")
        os.environ["GOMOKU_DATABASE_URL"] = (
            "sqlite:///C:/data/gomoku.db"
        )
        try:
            self.assertEqual(
                resolve_database(None), Path("C:/data/gomoku.db")
            )
        finally:
            if previous is None:
                os.environ.pop("GOMOKU_DATABASE_URL", None)
            else:
                os.environ["GOMOKU_DATABASE_URL"] = previous


class BackupTests(unittest.TestCase):
    def test_backup_copies_data(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "source.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (42)")
            conn.commit()
            conn.close()

            dest = backup_database(db_path, Path(tmp) / "backups")

            self.assertTrue(dest.exists())
            with closing(sqlite3.connect(dest)) as copy:
                self.assertEqual(
                    copy.execute("SELECT x FROM t").fetchall(), [(42,)]
                )

    def test_backup_name_uses_utc_timestamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "source.db"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.commit()
            conn.close()

            dest = backup_database(db_path, Path(tmp) / "backups")

            # 文件名时间戳统一用 UTC，末尾 Z 标明时区。
            self.assertTrue(dest.name.startswith("gomoku-"))
            self.assertTrue(dest.name.endswith("Z.db"), dest.name)

    def test_backup_missing_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                backup_database(
                    Path(tmp) / "missing.db", Path(tmp) / "backups"
                )

    def test_prune_keeps_newest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp)
            names = [
                "gomoku-20260101-000000-000000Z.db",
                "gomoku-20260102-000000-000000Z.db",
                "gomoku-20260103-000000-000000Z.db",
            ]
            for name in names:
                (out_dir / name).write_bytes(b"x")
            removed = prune_backups(out_dir, keep=2)
            left = sorted(p.name for p in out_dir.glob("gomoku-*.db"))
        self.assertEqual([p.name for p in removed], [names[0]])
        self.assertEqual(left, names[1:])


if __name__ == "__main__":
    unittest.main()
