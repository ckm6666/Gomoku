"""服务端 Prometheus 指标。

指标端点由 :mod:`server.app.main` 挂载在 ``/metrics``。房间/连接数等
瞬时状态通过自定义 collector 在抓取时从 ``RoomManager`` 读取，避免在每个
房间生命周期分支里手动维护 Gauge；落子、对局、重连等事件用 Counter /
Histogram 在发生时记录。
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, Optional

from prometheus_client import CollectorRegistry, Counter, Histogram
from prometheus_client import generate_latest
from prometheus_client.core import GaugeMetricFamily

#: 延迟直方图桶（秒）。
_LATENCY_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)


class _RoomStatsCollector:
    """抓取时读取 RoomManager 当前内存状态。"""

    def __init__(self, manager_getter: Callable[[], Any]) -> None:
        self._manager_getter = manager_getter

    def collect(self) -> Iterator[GaugeMetricFamily]:
        manager = self._manager_getter()
        stats: dict[str, int] = {}
        if manager is not None:
            try:
                stats = manager.metrics_snapshot()
            except Exception:
                stats = {}
        yield GaugeMetricFamily(
            "gomoku_ws_connections",
            "当前已认证的 WebSocket 连接数",
            value=stats.get("connections", 0),
        )
        yield GaugeMetricFamily(
            "gomoku_rooms_active",
            "当前内存中的房间总数",
            value=stats.get("rooms_active", 0),
        )
        yield GaugeMetricFamily(
            "gomoku_rooms_playing",
            "正在进行中的对局房间数",
            value=stats.get("rooms_playing", 0),
        )
        yield GaugeMetricFamily(
            "gomoku_rooms_waiting",
            "等待对手加入的房间数",
            value=stats.get("rooms_waiting", 0),
        )
        yield GaugeMetricFamily(
            "gomoku_spectators",
            "当前观战者总数",
            value=stats.get("spectators", 0),
        )
        yield GaugeMetricFamily(
            "gomoku_match_queue",
            "当前排位匹配队列人数",
            value=stats.get("match_queue", 0),
        )


class ServerMetrics:
    """一个应用实例独立的指标注册表。

    每个 ``create_app`` 都有自己的 registry，测试中重复创建应用不会触发
    Prometheus 全局重复注册错误。
    """

    def __init__(
        self, room_manager_getter: Optional[Callable[[], Any]] = None
    ) -> None:
        self.registry = CollectorRegistry()
        self.registry.register(
            _RoomStatsCollector(room_manager_getter or (lambda: None))
        )

        self.http_requests = Counter(
            "gomoku_http_requests_total",
            "HTTP 请求总数",
            ["method", "status"],
            registry=self.registry,
        )
        self.http_request_seconds = Histogram(
            "gomoku_http_request_seconds",
            "HTTP 请求耗时",
            ["method", "route"],
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.moves_confirmed = Counter(
            "gomoku_moves_confirmed_total",
            "服务器确认并写入数据库的落子数",
            registry=self.registry,
        )
        self.move_db_seconds = Histogram(
            "gomoku_move_db_seconds",
            "落子数据库事务耗时",
            buckets=_LATENCY_BUCKETS,
            registry=self.registry,
        )
        self.games_started = Counter(
            "gomoku_games_started_total",
            "开始的联机对局数",
            registry=self.registry,
        )
        self.games_finished = Counter(
            "gomoku_games_finished_total",
            "结束的联机对局数（按终局原因）",
            ["reason"],
            registry=self.registry,
        )
        self.reconnects = Counter(
            "gomoku_reconnects_total",
            "断线后恢复房间连接的次数",
            registry=self.registry,
        )
        self.ws_errors = Counter(
            "gomoku_ws_errors_total",
            "WebSocket 消息处理异常次数",
            registry=self.registry,
        )
        self.ws_rate_limited = Counter(
            "gomoku_ws_rate_limited_total",
            "因命令频率过高被拒绝的 WebSocket 消息数",
            registry=self.registry,
        )
        self.connection_limited = Counter(
            "gomoku_connection_limited_total",
            "因单 IP 连接数超限被拒绝的认证连接数",
            registry=self.registry,
        )

    def record_http(
        self, method: str, status: int, route: str, duration_seconds: float
    ) -> None:
        self.http_requests.labels(
            method=method, status=str(status)
        ).inc()
        self.http_request_seconds.labels(
            method=method, route=route
        ).observe(duration_seconds)

    def record_move(self, db_seconds: float) -> None:
        self.moves_confirmed.inc()
        self.move_db_seconds.observe(db_seconds)

    def render(self) -> bytes:
        return generate_latest(self.registry)


__all__ = ["ServerMetrics"]
