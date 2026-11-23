"""连接数与 WebSocket 命令频率限制测试。"""

from __future__ import annotations

import unittest

from starlette.websockets import WebSocketDisconnect

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_PING,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_PONG,
    SERVER_QUEUE_STATUS,
    make_message,
)
from server.app.ws.limits import CommandRateLimiter, IpConnectionLimiter
from server.tests.helpers import ServerTestBase


class _LimitTestBase(ServerTestBase):
    def _register_access(self, username: str) -> str:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"]

    def _authenticate(self, ws, token: str) -> None:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)


class ConnectionLimitTests(_LimitTestBase):
    max_connections_per_ip = 1

    def test_second_connection_from_same_ip_is_rejected(self) -> None:
        first_token = self._register_access("ip_first")
        second_token = self._register_access("ip_second")

        with self.client.websocket_connect("/api/v1/ws") as first:
            self._authenticate(first, first_token)
            with self.client.websocket_connect(
                "/api/v1/ws"
            ) as second:
                second.send_json(
                    make_message(
                        CLIENT_AUTH, {"access_token": second_token}
                    )
                )
                with self.assertRaises(WebSocketDisconnect):
                    second.receive_json()


class CommandRateLimitTests(_LimitTestBase):
    ws_command_burst = 2
    ws_command_window_seconds = 60.0

    def test_command_burst_is_limited_but_ping_is_exempt(self) -> None:
        token = self._register_access("burst_player")
        with self.client.websocket_connect("/api/v1/ws") as ws:
            self._authenticate(ws, token)
            for index in range(2):
                ws.send_json(
                    make_message(
                        CLIENT_CANCEL_MATCH,
                        {},
                        request_id=f"burst-{index}",
                    )
                )
                message = ws.receive_json()
                self.assertEqual(
                    message["type"], SERVER_QUEUE_STATUS
                )

            ws.send_json(
                make_message(
                    CLIENT_CANCEL_MATCH,
                    {},
                    request_id="burst-over",
                )
            )
            limited = ws.receive_json()
            self.assertEqual(limited["type"], SERVER_ERROR)
            self.assertIn("频繁", limited["payload"]["reason"])
            self.assertEqual(limited["request_id"], "burst-over")

            # 心跳不计入命令配额，避免空闲连接被误伤。
            for _ in range(3):
                ws.send_json(make_message(CLIENT_PING, {}))
                pong = ws.receive_json()
                self.assertEqual(pong["type"], SERVER_PONG)


class CommandRateLimiterUnitTests(unittest.TestCase):
    def test_sliding_window(self) -> None:
        limiter = CommandRateLimiter(burst=2, window_seconds=10)
        self.assertTrue(limiter.allow(0.0))
        self.assertTrue(limiter.allow(1.0))
        self.assertFalse(limiter.allow(2.0))
        self.assertTrue(limiter.allow(11.0))


class IpConnectionLimiterUnitTests(unittest.TestCase):
    def test_reserve_release_replace_and_clear(self) -> None:
        limiter = IpConnectionLimiter(max_per_ip=2)
        self.assertTrue(limiter.reserve("1.1.1.1", "c1"))
        self.assertTrue(limiter.reserve("1.1.1.1", "c2"))
        self.assertFalse(limiter.reserve("1.1.1.1", "c3"))
        # 幂等：同一连接重复登记不占新配额。
        self.assertTrue(limiter.reserve("1.1.1.1", "c2"))

        limiter.release("c1")
        self.assertTrue(limiter.reserve("1.1.1.1", "c3"))
        # 同账号重连可替换旧连接。
        self.assertTrue(
            limiter.reserve("1.1.1.1", "c4", replacing_id="c3")
        )
        limiter.clear()
        self.assertTrue(limiter.reserve("1.1.1.1", "c5"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
