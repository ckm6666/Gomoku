"""异步数据库引擎与建表辅助。

本机好友内测默认 SQLite + aiosqlite；SQLAlchemy URL 保持可切换到
PostgreSQL（生产配置见 README）。正常启动由
:mod:`server.app.migrations` 执行 Alembic 迁移；``create_tables`` 仅保留
给引入 Alembic 之前创建的旧库做一次性兼容登记。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, cast

from sqlalchemy import event, inspect, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from server.app.config import Settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy 声明基类。"""


def utcnow_naive() -> datetime:
    """SQLite/跨数据库更简单的 UTC 无时区时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def rowcount_of(result: Any) -> int:
    """返回 UPDATE/DELETE 的影响行数。

    ``AsyncSession.execute`` 的静态类型是通用 ``Result``，但 DML 实际返回
    ``CursorResult``；这里统一收窄，避免每个调用点重复 cast。
    """
    return int(cast(CursorResult[Any], result).rowcount or 0)


def create_engine(settings: Settings) -> AsyncEngine:
    """创建异步引擎，并为 SQLite 打开外键约束。"""
    url = settings.database_url
    if url.startswith("sqlite+aiosqlite:///"):
        db_path = url.split("sqlite+aiosqlite:///", 1)[1]
        if db_path and db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url, future=True)
    if url.startswith("sqlite+aiosqlite:///"):

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


# Stage 2 新增列。``create_all`` 不会修改已存在的表，因此为旧的
# Stage 1 数据库补列；新增表（rating_events）仍由 create_all 创建。
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "users": (
        ("rating", "INTEGER NOT NULL DEFAULT 1200"),
        ("peak_rating", "INTEGER NOT NULL DEFAULT 1200"),
        ("rated_games", "INTEGER NOT NULL DEFAULT 0"),
        ("wins", "INTEGER NOT NULL DEFAULT 0"),
        ("losses", "INTEGER NOT NULL DEFAULT 0"),
        ("draws", "INTEGER NOT NULL DEFAULT 0"),
        ("avatar", "VARCHAR(16) NOT NULL DEFAULT 'yun'"),
        ("avatar_image", "TEXT"),
        ("has_avatar_image", "BOOLEAN NOT NULL DEFAULT {false}"),
        ("avatar_version", "INTEGER NOT NULL DEFAULT 0"),
        ("current_win_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("best_win_streak", "INTEGER NOT NULL DEFAULT 0"),
        ("rating_rd", "FLOAT NOT NULL DEFAULT 100.0"),
        ("rating_volatility", "FLOAT NOT NULL DEFAULT 0.06"),
    ),
    "games": (
        ("rating_applied", "BOOLEAN NOT NULL DEFAULT {false}"),
        ("time_mode", "VARCHAR(16) NOT NULL DEFAULT 'per_move'"),
        ("main_seconds", "INTEGER NOT NULL DEFAULT 0"),
        ("increment_seconds", "INTEGER NOT NULL DEFAULT 0"),
    ),
    "rating_events": (
        ("rating_rd_before", "FLOAT"),
        ("rating_rd_after", "FLOAT"),
        ("rating_volatility_before", "FLOAT"),
        ("rating_volatility_after", "FLOAT"),
        ("season_id", "VARCHAR(36)"),
    ),
}


def _apply_additive_schema(connection) -> None:
    """为已存在的旧表增加新列/索引（SQLite 与 PostgreSQL 均适用）。"""
    dialect = connection.dialect.name
    false_literal = "0" if dialect == "sqlite" else "FALSE"
    inspector = inspect(connection)
    table_names = set(inspector.get_table_names())

    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in table_names:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in columns:
            if name in existing:
                continue
            statement = ddl.format(false=false_literal)
            connection.execute(
                text(f"ALTER TABLE {table} ADD COLUMN {name} {statement}")
            )
            logger.info("数据库迁移：%s 表新增列 %s", table, name)

    if "users" in table_names:
        inspector = inspect(connection)
        index_names = {
            index["name"] for index in inspector.get_indexes("users")
        }
        if "ix_users_rating" not in index_names:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_users_rating "
                    "ON users (rating)"
                )
            )
            logger.info("数据库迁移：users.rating 索引已创建")

    if "direct_messages" in table_names:
        inspector = inspect(connection)
        index_names = {
            index["name"]
            for index in inspector.get_indexes("direct_messages")
        }
        if "ix_direct_messages_unread" not in index_names:
            connection.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_direct_messages_unread "
                    "ON direct_messages (recipient_id, read_at, sender_id)"
                )
            )
            logger.info("数据库迁移：direct_messages 未读计数索引已创建")


async def create_tables(engine: AsyncEngine) -> None:
    """旧库兼容建表：仅由 ``prepare_database`` 的 legacy 分支调用。"""
    # 延迟导入，避免 Base 与模型模块的循环导入。
    from server.app import models  # noqa: F401

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(_apply_additive_schema)


async def mark_unfinished_games_abandoned(
    session_factory: async_sessionmaker[AsyncSession],
) -> int:
    """服务重启时把等待中/进行中的对局作废，避免客户端以为房间仍存在。

    本阶段不做完整崩溃恢复；每手落子仍会持久化用于审计，但服务重启后
    客户端会回到大厅，需要重新建房。生产级恢复需要在后续阶段持久化
    完整房间快照、时钟 deadline 和连接会话。
    """
    from server.app.models import Game

    async with session_factory() as session:
        result = await session.execute(
            update(Game)
            .where(Game.status.in_(("waiting", "playing")))
            .values(
                status="abandoned",
                result_reason="server_restart",
                ended_at=utcnow_naive(),
            )
        )
        await session.commit()
        count = rowcount_of(result)
    if count:
        logger.warning("服务启动：作废 %s 个未完成对局", count)
    return count


def sqlite_path_from_url(url: str) -> Optional[Path]:
    """测试/部署辅助：从 SQLite URL 提取文件路径。"""
    prefix = "sqlite+aiosqlite:///"
    if url.startswith(prefix):
        return Path(url[len(prefix):])
    return None


__all__ = [
    "Base",
    "create_engine",
    "create_session_factory",
    "create_tables",
    "mark_unfinished_games_abandoned",
    "rowcount_of",
    "sqlite_path_from_url",
    "utcnow_naive",
]
