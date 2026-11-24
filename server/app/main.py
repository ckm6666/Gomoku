"""FastAPI 应用工厂与启动入口。

从项目根目录启动（开发）：
    server/.venv/Scripts/python.exe -m uvicorn server.app.main:app --host 127.0.0.1 --port 8000

**部署约束：必须单 worker 运行。**
房间、匹配队列、断线重连宽限等状态全部保存在进程内存里，多 worker
（或多实例）之间互不可见：玩家会被匹配到别的进程上的队列、建好的
房间在另一个进程里查不到、观战与重连会找不到房间。要横向扩容必须先把
这些状态外置（Redis 等），当前只能用 ``--workers 1``。
"""

from __future__ import annotations

import asyncio
import logging
import os
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
from server.app.api.reports import router as reports_router
from server.app.api.seasons import router as seasons_router
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
from server.app.seasons import ensure_current_season
from server.app.security import LoginBackoff
from server.app.ws.endpoint import router as ws_router
from server.app.ws.rooms import RoomManager

logger = logging.getLogger(__name__)

#: REST/WS 接口版本号；升级到 v2 时只需要改这一处。
API_VERSION = "v1"
#: 所有接口的统一前缀，由版本号拼出来，避免各处硬编码 "/api/v1"。
API_PREFIX = f"/api/{API_VERSION}"
#: 健康检查路径（挂在 API_PREFIX 下）。
HEALTH_PATHS = (
    f"{API_PREFIX}/health",
    f"{API_PREFIX}/health/live",
    f"{API_PREFIX}/health/ready",
)
#: Prometheus 指标路径（不在版本前缀下）。
METRICS_PATH = "/metrics"


def _warn_if_multiple_workers() -> None:
    """多 worker 启动时给出告警：房间状态在进程内存中，必须单 worker。"""
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if not raw.isdigit() or int(raw) <= 1:
        return
    logger.warning(
        "检测到 WEB_CONCURRENCY=%s：房间/匹配队列保存在进程内存中，"
        "多 worker 之间互不可见，请改用 --workers 1；"
        "需要横向扩容请先把状态外置（如 Redis）",
        raw,
    )


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
        await ensure_current_season(session_factory, service_settings)

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
        # 启动扫描：补结算"已终局但 rating_applied 仍为假"的排位对局。
        # 落子终局路径的写库与结算仍是两次提交，进程在中间崩溃会留下
        # 这种对局；启动时补一遍，积分不会永久丢失。
        await app.state.room_manager.repair_unsettled_games_once()

        logger.info(
            "服务启动 env=%s db=%s",
            service_settings.environment,
            service_settings.database_url.split(":", 1)[0],
        )
        _warn_if_multiple_workers()
        try:
            yield
        finally:
            room_manager = getattr(app.state, "room_manager", None)
            if room_manager is not None:
                try:
                    await room_manager.shutdown()
                except Exception:
                    # 关闭失败不能把后续清理步骤一起带崩，至少留个痕迹。
                    logger.warning(
                        "关闭房间管理器失败", exc_info=True
                    )
            try:
                await engine.dispose()
            except Exception:
                logger.warning("释放数据库连接池失败", exc_info=True)
            logger.info("服务已停止")

    app = FastAPI(
        title="Gomoku Online Service",
        version="0.5.0",
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
            if route_path in HEALTH_PATHS or route_path == METRICS_PATH:
                logger.debug("http_request", extra=fields)
            elif status_code >= 500:
                logger.error("http_request", extra=fields)
            else:
                logger.info("http_request", extra=fields)
            reset_request_id(token)

    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(ranked_router, prefix=API_PREFIX)
    app.include_router(seasons_router, prefix=API_PREFIX)
    app.include_router(games_router, prefix=API_PREFIX)
    app.include_router(reports_router, prefix=API_PREFIX)
    app.include_router(users_router, prefix=API_PREFIX)
    app.include_router(friends_router, prefix=API_PREFIX)
    app.include_router(messages_router, prefix=API_PREFIX)
    app.include_router(ws_router, prefix=API_PREFIX)

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

    @app.get(f"{API_PREFIX}/health")
    async def health(request: Request) -> dict:
        return {
            "status": "ok",
            "environment": request.app.state.settings.environment,
        }

    @app.get(f"{API_PREFIX}/health/live")
    async def health_live(request: Request) -> dict:
        return {
            "status": "ok",
            "environment": request.app.state.settings.environment,
        }

    @app.get(f"{API_PREFIX}/health/ready")
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

    @app.get(METRICS_PATH, include_in_schema=False)
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

__all__ = [
    "API_PREFIX",
    "API_VERSION",
    "HEALTH_PATHS",
    "METRICS_PATH",
    "app",
    "create_app",
]
