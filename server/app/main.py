"""FastAPI 应用工厂与启动入口。

从项目根目录启动（开发）：
    server/.venv/Scripts/python.exe -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST
from sqlalchemy import text

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
    mark_unfinished_games_abandoned,
)
from server.app.metrics import ServerMetrics
from server.app.migrations import prepare_database
from server.app.observability import (
    bind_request_id,
    configure_logging,
    reset_request_id,
    sanitize_request_id,
)
from server.app.security import LoginBackoff
from server.app.ws.endpoint import router as ws_router
from server.app.ws.rooms import RoomManager

logger = logging.getLogger(__name__)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """创建可测试的 FastAPI 应用；每次调用拥有独立数据库/房间状态。"""
    service_settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(service_settings)
        engine = create_engine(service_settings)
        session_factory = create_session_factory(engine)
        await prepare_database(engine, service_settings)
        await mark_unfinished_games_abandoned(session_factory)

        app.state.settings = service_settings
        app.state.engine = engine
        app.state.session_factory = session_factory
        app.state.registration_lock = asyncio.Lock()
        app.state.login_backoff = LoginBackoff()
        metrics = ServerMetrics(
            room_manager_getter=lambda: getattr(
                app.state, "room_manager", None
            )
        )
        app.state.metrics = metrics
        app.state.room_manager = RoomManager(
            service_settings, session_factory, metrics
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
        version="0.4.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_observability(request: Request, call_next):
        request_id = sanitize_request_id(
            request.headers.get("X-Request-ID")
        )
        token = bind_request_id(request_id)
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            logger.exception(
                "http_unhandled_error",
                extra={
                    "event": "http_unhandled_error",
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            raise
        finally:
            duration = time.perf_counter() - started
            route = request.scope.get("route")
            route_path = str(
                getattr(route, "path", None) or "unmatched"
            )
            metrics = getattr(request.app.state, "metrics", None)
            if metrics is not None:
                metrics.record_http(
                    request.method, status_code, route_path, duration
                )
            fields = {
                "event": "http_request",
                "method": request.method,
                "path": route_path,
                "status": status_code,
                "duration_ms": round(duration * 1000, 2),
            }
            if route_path in {
                "/api/v1/health",
                "/api/v1/health/live",
                "/api/v1/health/ready",
                "/metrics",
            }:
                logger.debug("http_request", extra=fields)
            elif status_code >= 500:
                logger.error("http_request", extra=fields)
            else:
                logger.info("http_request", extra=fields)
            reset_request_id(token)

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(ranked_router, prefix="/api/v1")
    app.include_router(games_router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(friends_router, prefix="/api/v1")
    app.include_router(messages_router, prefix="/api/v1")
    app.include_router(ws_router, prefix="/api/v1")

    async def _database_ready() -> bool:
        session_factory = getattr(app.state, "session_factory", None)
        if session_factory is None:
            return False

        async def check() -> None:
            async with session_factory() as session:
                await session.execute(text("SELECT 1"))

        try:
            await asyncio.wait_for(check(), timeout=2.0)
        except Exception:
            logger.exception(
                "health_database_check_failed",
                extra={"event": "health_database_check_failed"},
            )
            return False
        return True

    @app.get("/api/v1/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "environment": request.app.state.settings.environment,
        }

    @app.get("/api/v1/health/live")
    async def health_live(request: Request) -> dict:
        return {
            "status": "ok",
            "environment": request.app.state.settings.environment,
        }

    @app.get("/api/v1/health/ready")
    async def health_ready(request: Request) -> Response:
        ready = await _database_ready()
        payload = {
            "status": "ok" if ready else "error",
            "environment": request.app.state.settings.environment,
            "checks": {"database": "ok" if ready else "error"},
        }
        return JSONResponse(
            status_code=200 if ready else 503,
            content=payload,
        )

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(request: Request) -> Response:
        metrics = getattr(request.app.state, "metrics", None)
        if metrics is None:
            return Response(status_code=503)
        return Response(
            content=metrics.render(),
            media_type=CONTENT_TYPE_LATEST,
        )

    return app


app = create_app()
