"""Alembic 启动迁移。

数据库结构按以下规则推进到最新版本：

* 有 ``alembic_version``：正常执行 ``upgrade head``；
* 全新的空数据库：执行基线迁移并推进到 head；
* 引入 Alembic 之前创建、且没有 ``alembic_version`` 的旧库：
  先用既有的 ``create_tables`` 把结构补齐到当前模型，再直接登记 ``head``，
  避免后续迁移对已经存在的列重复执行 ``ADD COLUMN``。

最后一条是**一次性兼容路径**。所有部署完成过基线登记后，应移除
``create_tables`` 调用，只保留 Alembic 迁移；否则未来修改模型时，
旧库可能被 ``create_all`` 提前建成新结构，导致后续迁移重复建表。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncEngine

from server.app.config import Settings
from server.app.db import create_tables

logger = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent.parent
BASELINE_REVISION = "0001_baseline"


def alembic_config(settings: Settings) -> Config:
    """构造 Alembic 配置并注入数据库地址。"""
    config = Config(str(SERVER_DIR / "alembic.ini"))
    # ConfigParser 使用 % 插值，URL 中的 % 需要转义。
    config.set_main_option(
        "sqlalchemy.url", settings.database_url.replace("%", "%%")
    )
    return config


async def _existing_tables(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as connection:
        return set(
            await connection.run_sync(
                lambda sync_connection: inspect(
                    sync_connection
                ).get_table_names()
            )
        )


async def prepare_database(
    engine: AsyncEngine, settings: Settings
) -> None:
    """把数据库结构推进到当前 Alembic head。"""
    if ":memory:" in settings.database_url:
        raise RuntimeError(
            "Alembic 迁移需要文件或网络数据库，不支持 SQLite :memory:"
        )
    config = alembic_config(settings)
    tables = await _existing_tables(engine)

    if "alembic_version" in tables:
        await asyncio.to_thread(command.upgrade, config, "head")
        logger.info("数据库迁移完成（Alembic upgrade head）")
        return

    legacy_tables = tables - {"alembic_version"}
    if legacy_tables:
        logger.warning(
            "检测到未纳入 Alembic 的旧数据库，先补齐当前模型结构并登记 head"
        )
        # create_tables 会把旧库补齐到当前模型（create_all + 增量列），
        # 因此直接登记 head；否则后续迁移会重复给新列执行 ADD COLUMN。
        await create_tables(engine)
        await asyncio.to_thread(command.stamp, config, "head")
        logger.info("数据库迁移完成（旧库登记 head）")
        return

    # 新库直接执行基线迁移并推进到 head。
    await asyncio.to_thread(command.upgrade, config, "head")
    logger.info("数据库迁移完成（Alembic head）")


__all__ = ["BASELINE_REVISION", "alembic_config", "prepare_database"]
