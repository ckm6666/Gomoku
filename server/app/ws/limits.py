"""WebSocket 命令限流与连接配额。

这些类不依赖 FastAPI/房间状态，便于单独测试；RoomManager 在自己的
``_state_lock`` 内调用 ``IpConnectionLimiter``，endpoint 为每条连接创建
一个 ``CommandRateLimiter``。
"""

from __future__ import annotations

from collections import deque
from typing import Optional


class CommandRateLimiter:
    """滑动窗口令牌桶：window 秒内最多允许 burst 条命令。"""

    def __init__(self, burst: int, window_seconds: float) -> None:
        self._burst = max(1, int(burst))
        self._window = max(0.001, float(window_seconds))
        self._times: deque[float] = deque()

    def allow(self, now: float) -> bool:
        while self._times and now - self._times[0] > self._window:
            self._times.popleft()
        if len(self._times) >= self._burst:
            return False
        self._times.append(now)
        return True

    def reset(self) -> None:
        self._times.clear()


class IpConnectionLimiter:
    """按 IP 限制已认证连接数；同一连接重复 reserve 幂等。"""

    def __init__(self, max_per_ip: int) -> None:
        self._max_per_ip = max(1, int(max_per_ip))
        self._connection_ips: dict[str, str] = {}
        self._ip_connections: dict[str, set[str]] = {}

    def reserve(
        self,
        ip: str,
        connection_id: str,
        replacing_id: Optional[str] = None,
    ) -> bool:
        if connection_id in self._connection_ips:
            return True
        if replacing_id is not None and replacing_id != connection_id:
            self.release(replacing_id)
        key = ip or "unknown"
        connections = self._ip_connections.setdefault(key, set())
        if len(connections) >= self._max_per_ip:
            return False
        connections.add(connection_id)
        self._connection_ips[connection_id] = key
        return True

    def release(self, connection_id: str) -> None:
        ip = self._connection_ips.pop(connection_id, None)
        if ip is None:
            return
        connections = self._ip_connections.get(ip)
        if connections is not None:
            connections.discard(connection_id)
            if not connections:
                self._ip_connections.pop(ip, None)

    def clear(self) -> None:
        self._connection_ips.clear()
        self._ip_connections.clear()


__all__ = ["CommandRateLimiter", "IpConnectionLimiter"]
