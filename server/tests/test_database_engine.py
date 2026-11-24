"""SQLite 引擎的 WAL/忙等待设置与数据库 URL 解析测试。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import text

from server.app.config import (
    Settings,
    is_sqlite_url,
    normalize_database_url,
    sqlite_path_from_url,
)
from server.app.db import SQLITE_BUSY_TIMEOUT_MS, create_engine

_SECRET = "test-secret-" + "x" * 48


def _settings(url: str) -> Settings:
    return Settings(
        environment="test",
        database_url=url,
        jwt_secret=_SECRET,
        log_level="WARNING",
    )


class SqlitePragmaTests(unittest.TestCase):
    """SQLite 连接必须打开 WAL 与忙等待，否则并发写会直接失败。"""

    def test_file_database_uses_wal_and_busy_timeout(self) -> None:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "wal.db"
            settings = _settings(
                f"sqlite+aiosqlite:///{db_path.as_posix()}"
            )

            async def read_pragmas() -> tuple[object, object, object]:
                engine = create_engine(settings)
                try:
                    async with engine.connect() as conn:
                        mode = (
                            await conn.execute(text("PRAGMA journal_mode"))
                        ).scalar()
                        timeout = (
                            await conn.execute(text("PRAGMA busy_timeout"))
                        ).scalar()
                        synchronous = (
                            await conn.execute(text("PRAGMA synchronous"))
                        ).scalar()
                finally:
                    await engine.dispose()
                return mode, timeout, synchronous

            mode, timeout, synchronous = asyncio.run(read_pragmas())
            self.assertEqual(str(mode).lower(), "wal")
            self.assertEqual(timeout, SQLITE_BUSY_TIMEOUT_MS)
            # SQLite 的 synchronous 取值：0=OFF 1=NORMAL 2=FULL 3=EXTRA。
            self.assertEqual(synchronous, 1)

            # WAL 是库级设置，落盘后仍然生效。
            connection = sqlite3.connect(db_path)
            try:
                stored = connection.execute(
                    "PRAGMA journal_mode"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(stored).lower(), "wal")

    def test_in_memory_database_still_works(self) -> None:
        # 内存库不支持 WAL（PRAGMA 返回 memory），但不能因此报错。
        settings = _settings("sqlite+aiosqlite:///:memory:")

        async def exercise() -> tuple[object, int]:
            engine = create_engine(settings)
            try:
                async with engine.connect() as conn:
                    mode = (
                        await conn.execute(text("PRAGMA journal_mode"))
                    ).scalar()
                    await conn.execute(text("CREATE TABLE t (x INTEGER)"))
                    await conn.execute(text("INSERT INTO t VALUES (1)"))
                    count = (
                        await conn.execute(text("SELECT count(*) FROM t"))
                    ).scalar()
            finally:
                await engine.dispose()
            return mode, int(count or 0)

        mode, count = asyncio.run(exercise())
        self.assertEqual(str(mode).lower(), "memory")
        self.assertEqual(count, 1)

    def test_concurrent_write_waits_instead_of_failing(self) -> None:
        # 一个事务持有写锁时，另一个写者应该等待 busy_timeout 而不是
        # 立刻抛 "database is locked"。
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "concurrent.db"
            settings = _settings(
                f"sqlite+aiosqlite:///{db_path.as_posix()}"
            )

            async def scenario() -> tuple[bool, int]:
                engine = create_engine(settings)
                try:
                    async with engine.begin() as conn:
                        await conn.execute(
                            text("CREATE TABLE t (x INTEGER)")
                        )
                    async with engine.connect() as first:
                        await first.execute(
                            text("INSERT INTO t VALUES (1)")
                        )

                        async def second_write() -> None:
                            async with engine.connect() as conn:
                                await conn.execute(
                                    text("INSERT INTO t VALUES (2)")
                                )
                                await conn.commit()

                        task = asyncio.create_task(second_write())
                        await asyncio.sleep(0.3)
                        blocked = not task.done()
                        await first.commit()
                        await asyncio.wait_for(task, timeout=15)
                    async with engine.connect() as conn:
                        total = (
                            await conn.execute(text("SELECT count(*) FROM t"))
                        ).scalar()
                finally:
                    await engine.dispose()
                return blocked, int(total or 0)

            blocked, total = asyncio.run(scenario())
            self.assertTrue(blocked, "第二个写者应当等待写锁释放")
            self.assertEqual(total, 2)


class DatabaseUrlTests(unittest.TestCase):
    """SQLite URL 只有一处解析入口，两种前缀指向同一个文件。"""

    def test_both_prefixes_resolve_to_same_file(self) -> None:
        self.assertEqual(
            sqlite_path_from_url("sqlite+aiosqlite:///C:/data/gomoku.db"),
            Path("C:/data/gomoku.db"),
        )
        self.assertEqual(
            sqlite_path_from_url("sqlite:///C:/data/gomoku.db"),
            Path("C:/data/gomoku.db"),
        )

    def test_non_sqlite_url_is_not_sqlite(self) -> None:
        url = "postgresql+asyncpg://u:p@db/gomoku"
        self.assertFalse(is_sqlite_url(url))
        self.assertIsNone(sqlite_path_from_url(url))
        # 非 SQLite URL 不做任何改写，PostgreSQL 行为保持不变。
        self.assertEqual(normalize_database_url(url), url)

    def test_sync_sqlite_url_is_normalized_to_async_driver(self) -> None:
        self.assertEqual(
            normalize_database_url("sqlite:///data/gomoku.db"),
            "sqlite+aiosqlite:///data/gomoku.db",
        )
        self.assertEqual(
            normalize_database_url("sqlite+aiosqlite:///data/gomoku.db"),
            "sqlite+aiosqlite:///data/gomoku.db",
        )

    def test_engine_accepts_sync_sqlite_url(self) -> None:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "sync-form.db"
            settings = _settings(f"sqlite:///{db_path.as_posix()}")

            async def roundtrip() -> int:
                engine = create_engine(settings)
                try:
                    async with engine.begin() as conn:
                        await conn.execute(
                            text("CREATE TABLE t (x INTEGER)")
                        )
                        await conn.execute(
                            text("INSERT INTO t VALUES (7)")
                        )
                    async with engine.connect() as conn:
                        value = (
                            await conn.execute(text("SELECT x FROM t"))
                        ).scalar()
                finally:
                    await engine.dispose()
                return int(value or 0)

            self.assertEqual(asyncio.run(roundtrip()), 7)
            self.assertTrue(db_path.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
