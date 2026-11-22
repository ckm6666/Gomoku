"""Alembic 异步迁移环境。

不在 env.py 里调用 logging.config.fileConfig，避免迁移时覆盖应用自身
的日志配置。数据库地址按以下顺序读取：

1. Alembic Config 的 ``sqlalchemy.url``（应用启动时由代码设置）；
2. ``GOMOKU_DATABASE_URL`` 环境变量（命令行 autogenerate / stamp 用）。
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# 导入模型模块，把全部表注册到 Base.metadata，供 autogenerate 使用。
import server.app.models  # noqa: F401
from server.app.db import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """返回未转义的数据库 URL。"""
    url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if not url:
        url = os.environ.get("GOMOKU_DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "Alembic 未配置数据库地址：请设置 sqlalchemy.url 或 GOMOKU_DATABASE_URL"
        )
    return url


def run_migrations_offline() -> None:
    """离线模式：只输出 SQL，不连接数据库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """用异步引擎创建连接并执行迁移。"""
    # ConfigParser 使用 % 插值，URL 中的 % 需要转义。
    config.set_main_option("sqlalchemy.url", _database_url().replace("%", "%%"))
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
