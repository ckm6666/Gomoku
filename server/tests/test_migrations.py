"""Alembic 启动迁移：全新库、旧库登记基线与重复启动。"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from server.app.config import Settings
from server.app.db import (
    create_engine,
    create_tables,
    sqlite_path_from_url,
)
from server.app.main import create_app
from server.app.migrations import prepare_database
from server.tests.helpers import ServerTestBase

_EXPECTED_TABLES = {
    "users",
    "refresh_sessions",
    "games",
    "moves",
    "rating_events",
    "friendships",
    "direct_messages",
}


def _tables_and_indexes(db_path: Path) -> tuple[set[str], set[str]]:
    connection = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name IS NOT NULL"
            )
        }
    finally:
        connection.close()
    return tables, indexes


class FreshDatabaseMigrationTests(ServerTestBase):
    def test_fresh_database_is_at_baseline_head(self) -> None:
        db_path = sqlite_path_from_url(self.settings.database_url)
        self.assertIsNotNone(db_path)
        assert db_path is not None
        tables, indexes = _tables_and_indexes(db_path)
        self.assertTrue(_EXPECTED_TABLES.issubset(tables))
        self.assertIn("alembic_version", tables)
        self.assertIn("ix_users_rating", indexes)
        self.assertIn("ix_direct_messages_unread", indexes)

        connection = sqlite3.connect(db_path)
        try:
            version = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(version, [("0002_seasons_glicko_reports",)])

    def test_prepare_database_is_idempotent(self) -> None:
        # 在独立数据库上连续迁移两次，确认 upgrade head 是幂等的。
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "idempotent.db"
            settings = Settings(
                environment="test",
                database_url=(
                    f"sqlite+aiosqlite:///{db_path.as_posix()}"
                ),
                jwt_secret="test-secret-" + "x" * 48,
                log_level="WARNING",
            )

            async def migrate_twice() -> None:
                engine = create_engine(settings)
                await prepare_database(engine, settings)
                await prepare_database(engine, settings)
                await engine.dispose()

            asyncio.run(migrate_twice())
            connection = sqlite3.connect(db_path)
            try:
                version = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(version, [("0002_seasons_glicko_reports",)])


class LegacyDatabaseMigrationTests(unittest.TestCase):
    def test_legacy_database_is_stamped_then_upgraded(self) -> None:
        with tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        ) as tmp:
            db_path = Path(tmp) / "legacy.db"
            settings = Settings(
                environment="test",
                database_url=(
                    f"sqlite+aiosqlite:///{db_path.as_posix()}"
                ),
                jwt_secret="test-secret-" + "x" * 48,
                log_level="WARNING",
            )

            # 模拟引入 Alembic 之前的数据库：只有业务表，没有版本表。
            async def create_legacy() -> None:
                engine = create_engine(settings)
                await create_tables(engine)
                await engine.dispose()

            asyncio.run(create_legacy())
            tables, _ = _tables_and_indexes(db_path)
            self.assertNotIn("alembic_version", tables)

            app = create_app(settings)
            with TestClient(app) as client:
                response = client.get("/api/v1/health")
                self.assertEqual(response.status_code, 200)

            tables, _ = _tables_and_indexes(db_path)
            self.assertTrue(_EXPECTED_TABLES.issubset(tables))
            connection = sqlite3.connect(db_path)
            try:
                version = connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(version, [("0002_seasons_glicko_reports",)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
