"""健康检查、请求 ID、Prometheus 指标与日志配置测试。"""

from __future__ import annotations

import io
import logging
import unittest

from gomoku.shared.errors import ConfigurationError
from server.app.config import Settings
from server.app.observability import (
    JsonFormatter,
    TextFormatter,
    configure_logging,
    sanitize_request_id,
)
from server.tests.helpers import ServerTestBase


class HealthAndMetricsTests(ServerTestBase):
    def test_liveness_and_readiness(self) -> None:
        live = self.client.get("/api/v1/health/live")
        self.assertEqual(live.status_code, 200)
        self.assertEqual(live.json()["status"], "ok")

        ready = self.client.get("/api/v1/health/ready")
        self.assertEqual(ready.status_code, 200, ready.text)
        payload = ready.json()
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["checks"]["database"], "ok")

    def test_request_id_is_echoed_or_generated(self) -> None:
        forwarded = self.client.get(
            "/api/v1/health",
            headers={"X-Request-ID": "test-request-123"},
        )
        self.assertEqual(
            forwarded.headers.get("X-Request-ID"), "test-request-123"
        )

        generated = self.client.get("/api/v1/health")
        request_id = generated.headers.get("X-Request-ID", "")
        self.assertTrue(request_id)
        self.assertNotEqual(request_id, "test-request-123")

    def test_request_id_rejects_unsafe_values(self) -> None:
        self.assertEqual(sanitize_request_id("abc-123_ok"), "abc-123_ok")
        unsafe = sanitize_request_id("bad value\n")
        self.assertNotIn(" ", unsafe)
        self.assertNotIn("\n", unsafe)

    def test_metrics_endpoint_exposes_core_metrics(self) -> None:
        # 先产生一个 HTTP 样本，确保计数器出现在输出中。
        self.client.get("/api/v1/health")
        response = self.client.get("/metrics")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.text
        for name in (
            "gomoku_http_requests_total",
            "gomoku_http_request_seconds",
            "gomoku_ws_connections",
            "gomoku_rooms_active",
            "gomoku_rooms_playing",
            "gomoku_spectators",
            "gomoku_match_queue",
            "gomoku_moves_confirmed_total",
            "gomoku_move_db_seconds",
            "gomoku_games_started_total",
            "gomoku_games_finished_total",
            "gomoku_reconnects_total",
            "gomoku_ws_errors_total",
            "gomoku_ws_rate_limited_total",
            "gomoku_connection_limited_total",
        ):
            self.assertIn(name, body)


class LoggingConfigTests(unittest.TestCase):
    def test_log_level_and_format_are_parsed(self) -> None:
        settings = Settings.from_env(
            {
                "GOMOKU_SERVER_ENV": "test",
                "GOMOKU_LOG_LEVEL": "debug",
                "GOMOKU_LOG_FORMAT": "json",
            }
        )
        self.assertEqual(settings.log_level, "DEBUG")
        self.assertEqual(settings.log_format, "json")

    def test_invalid_log_settings_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "GOMOKU_SERVER_ENV": "test",
                    "GOMOKU_LOG_LEVEL": "verbose",
                }
            )
        with self.assertRaises(ConfigurationError):
            Settings.from_env(
                {
                    "GOMOKU_SERVER_ENV": "test",
                    "GOMOKU_LOG_FORMAT": "xml",
                }
            )


class FormatterTests(unittest.TestCase):
    """文本日志不能再用硬编码字段白名单，extra 字段必须全部可见。"""

    def _record(self, **extra: object) -> logging.LogRecord:
        record = logging.LogRecord(
            name="gomoku.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="测试消息",
            args=None,
            exc_info=None,
        )
        for key, value in extra.items():
            setattr(record, key, value)
        return record

    def test_text_formatter_includes_custom_extra_fields(self) -> None:
        record = self._record(custom_field="value", event="custom_event")
        output = TextFormatter().format(record)
        self.assertIn("custom_field=value", output)
        self.assertIn("event=custom_event", output)

    def test_text_formatter_skips_standard_record_fields(self) -> None:
        record = self._record(custom_field="value")
        output = TextFormatter().format(record)
        # 正文里已经有消息和时间，不能再重复一份。
        self.assertNotIn("message=", output)
        self.assertNotIn("asctime=", output)
        self.assertNotIn("levelname=", output)

    def test_json_formatter_includes_custom_extra_fields(self) -> None:
        record = self._record(custom_field="value")
        output = JsonFormatter().format(record)
        self.assertIn('"custom_field": "value"', output)


class ConfigureLoggingTests(unittest.TestCase):
    """configure_logging 只管理自己装的 handler。"""

    def setUp(self) -> None:
        self.root = logging.getLogger()
        self.original_handlers = list(self.root.handlers)
        self.original_level = self.root.level

    def tearDown(self) -> None:
        for handler in list(self.root.handlers):
            if handler not in self.original_handlers:
                self.root.removeHandler(handler)
        for handler in self.original_handlers:
            if handler not in self.root.handlers:
                self.root.addHandler(handler)
        self.root.setLevel(self.original_level)

    def _settings(self) -> Settings:
        return Settings(
            environment="test",
            database_url="sqlite+aiosqlite:///:memory:",
            jwt_secret="test-secret-" + "x" * 48,
            log_level="WARNING",
            log_format="text",
        )

    def test_repeated_call_does_not_accumulate_handlers(self) -> None:
        marker = logging.StreamHandler(io.StringIO())
        self.root.addHandler(marker)
        try:
            configure_logging(self._settings())
            first = len(self.root.handlers)
            configure_logging(self._settings())
            second = len(self.root.handlers)
            # 别的组件装上的 handler 不能被清掉。
            self.assertIn(marker, self.root.handlers)
        finally:
            self.root.removeHandler(marker)
        self.assertEqual(first, second)

    def test_foreign_handlers_survive_configuration(self) -> None:
        marker = logging.StreamHandler(io.StringIO())
        self.root.addHandler(marker)
        try:
            configure_logging(self._settings())
            self.assertIn(marker, self.root.handlers)
        finally:
            self.root.removeHandler(marker)


if __name__ == "__main__":
    unittest.main(verbosity=2)
