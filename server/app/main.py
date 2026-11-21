"""FastAPI 应用工厂与启动入口。

从项目根目录启动（开发）：
    server/.venv/Scripts/python.exe -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request

from server.app.api.auth import router as auth_router
from server.app.api.friends import router as friends_router
from server.app.api.games import router as games_router
from server.app.api.messages import router as messages_router
from server.app.api.ranked import router as ranked_router
from server.app.api.users import router as users_router
from server.app.config import Settings
from server.app.db import (
    create_engine,
    create_session_factory,
    create_tables,
    mark_unfinished_games_abandoned,
)
from server.app.ws.endpoint import router as ws_router
from server.app.ws.rooms import RoomManager

logger = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """创建可测试的 FastAPI 应用；每次调用拥有独立数据库/房间状态。"""
    service_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(service_settings)
        session_factory = create_session_factory(engine)
        await create_tables(engine)
        await mark_unfinished_games_abandoned(session_factory)

        app.state.settings = service_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.registration_lock = asyncio.Lock()
        app.state.room_manager = RoomManager(
            service_settings, session_factory
        )

        logger.info(
            "服务启动 env=%s db=%s",
            service_settings.environment,
            service_settings.database_url.split(":", 1)[0],
        )
        try:
            yield
        finally:
            room_manager = getattr(app.state, "room_manager", None)
            if room_manager is not None:
                await room_manager.shutdown()
            await engine.dispose()
            logger.info("服务已停止")

    app = FastAPI(
        title="Gomoku Online Service",
        version="0.3.0",
        lifespan=lifespan,
    )
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(ranked_router, prefix="/api/v1")
    app.include_router(games_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(friends_router, prefix="/api/v1")
    app.include_router(messages_router, prefix="/api/v1")
    app.include_router(ws_router, prefix="/api/v1")

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "environment": request.app.state.settings.environment,
        }

    return app


app = create_app()
