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
        """登记连接占用的 IP 配额。

        替换旧连接（重连）时先判断"净增量"是否放得下：替换同一 IP 下的
        旧连接不增加占用，否则净增 1。只有确认新连接放得下之后才释放旧
        连接的配额——否则旧配额已经释放、新连接又被拒绝，用户会两头
        落空，等于完全没有连接。
        """
        if connection_id in self._connection_ips:
            return True
        key = ip or "unknown"
        connections = self._ip_connections.get(key)
        current = len(connections) if connections is not None else 0
        replacing = (
            replacing_id is not None
            and replacing_id != connection_id
            and connections is not None
            and replacing_id in connections
        )
        net_increment = 0 if replacing else 1
        if current + net_increment > self._max_per_ip:
            # 放不下就保持旧连接的原状：它继续占用配额，用户至少还在线。
            return False
        if replacing_id is not None and replacing_id != connection_id:
            self.release(replacing_id)
        connections = self._ip_connections.setdefault(key, set())
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
