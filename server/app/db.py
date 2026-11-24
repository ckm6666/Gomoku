"""异步数据库引擎与建表辅助。

本机好友内测默认 SQLite + aiosqlite；SQLAlchemy URL 保持可切换到
PostgreSQL（生产配置见 README）。正常启动由
:mod:`server.app.migrations` 执行 Alembic 迁移；``create_tables`` 仅保留
给引入 Alembic 之前创建的旧库做一次性兼容登记。

SQLite 连接会统一打开 WAL 日志模式、5 秒忙等待与 NORMAL 同步级别，
避免多人同时落子时读写互斥导致的 ``database is locked``；PostgreSQL
不受这些 PRAGMA 影响。
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

from server.app.config import (
    Settings,
    normalize_database_url,
    sqlite_path_from_url as _sqlite_path_from_url,
)

logger = logging.getLogger(__name__)

#: SQLite 写锁被占用时的最长等待时间（毫秒）。
#:
#: 服务端每手棋都写穿数据库，读写并发时容易撞上写锁；没有忙等待的话
#: SQLite 会立刻抛 "database is locked"，有了它则最多等这么久。
SQLITE_BUSY_TIMEOUT_MS = 5000


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


def apply_sqlite_pragmas(dbapi_connection: Any) -> None:
    """为每条 SQLite 连接设置忙等待、WAL 日志模式与同步级别。

    * ``busy_timeout=5000``：写锁被占用时最多等 5 秒，而不是立刻抛
      ``database is locked``。这是**每连接**设置，必须每次建连都设。
    * ``journal_mode=WAL``：读写不再互斥，读者可以与写者同时存在。这是
      **库级**设置（写在数据库文件里），每次连接都设无害，但必须检查
      返回值：内存库不支持 WAL（返回 ``memory``），部分网络文件系统也
      切不过去（返回原模式）。
    * ``synchronous=NORMAL``：WAL 下兼顾安全与速度，也是每连接设置。

    顺序说明：先设 ``busy_timeout`` 再切 WAL——切换日志模式本身要拿库级
    锁，没有忙等待时并发建连会直接失败。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA journal_mode=WAL")
        row = cursor.fetchone()
        mode = str(row[0]).lower() if row else ""
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()
    if mode == "memory":
        # 内存数据库不支持 WAL，属于预期情况，不报警。
        return
    if mode != "wal":
        logger.warning(
            "SQLite 未启用 WAL 日志模式（当前 %s），"
            "并发写入可能报 database is locked",
            mode or "未知",
        )


def create_engine(settings: Settings) -> AsyncEngine:
    """创建异步引擎；SQLite 连接统一打开 WAL、忙等待与外键约束。"""
    url = normalize_database_url(settings.database_url)
    sqlite_path = sqlite_path_from_url(url)
    if sqlite_path is not None:
        raw_path = str(sqlite_path)
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(url, future=True)
    if sqlite_path is not None:

        @event.listens_for(engine.sync_engine, "connect")
        def _configure_sqlite(dbapi_connection, _record) -> None:
            apply_sqlite_pragmas(dbapi_connection)

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
    """测试/部署辅助：从 SQLite URL 提取文件路径。

    实现放在 :mod:`server.app.config`（无第三方依赖），备份工具也复用
    同一份解析逻辑，这里只做转发以兼容既有导入路径。
    """
    return _sqlite_path_from_url(url)


__all__ = [
    "Base",
    "SQLITE_BUSY_TIMEOUT_MS",
    "apply_sqlite_pragmas",
    "create_engine",
    "create_session_factory",
    "create_tables",
    "mark_unfinished_games_abandoned",
    "rowcount_of",
    "sqlite_path_from_url",
    "utcnow_naive",
]
