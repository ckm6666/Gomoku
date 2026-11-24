"""统一 JSON HTTP 客户端基类的测试（离屏）。

覆盖四件事：

* 错误文案提取（``format_api_error``）在各种 ``detail`` 形态下都稳定；
* **畸形响应是失败**，不会再被当成「没有好友/没有消息」的空成功
  （``social_api.py`` 的历史缺陷）；
* 地址拼接、Bearer 头、超时、方法分发都由基类统一处理；
* ``/api/v1`` 前缀只有一个来源，生成的 URL 与改造前逐字节一致。

本模块刻意不创建任何 ``QApplication``/``QCoreApplication`` 实例：假 reply
与假 manager 都是普通 ``QObject``，信号在无事件循环时也能直接连接。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Optional

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QByteArray, QObject, Signal  # noqa: E402
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest  # noqa: E402

from gomoku.client.config import (  # noqa: E402
    API_PREFIX,
    API_VERSION,
    WS_PATH,
    AppConfig,
    api_path,
    resolve_endpoints,
)
from gomoku.client.net.game_history_api import (  # noqa: E402
    GameHistoryHTTPClient,
)
from gomoku.client.net.http_api import AuthHTTPClient  # noqa: E402
from gomoku.client.net.json_api import (  # noqa: E402
    ERROR_DETAIL_LIMIT,
    JsonApiClient,
    ReplyOutcome,
    format_api_error,
    parse_json_body,
)
from gomoku.client.net.profile_api import ProfileHTTPClient  # noqa: E402
from gomoku.client.net.report_api import ReportHTTPClient  # noqa: E402
from gomoku.client.net.social_api import SocialHTTPClient  # noqa: E402
from gomoku.client.net.update_api import UpdateHTTPClient  # noqa: E402
from gomoku.shared.errors import ConfigurationError  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"


class FakeReply(QObject):
    """假的网络响应：按需返回状态码、错误枚举与响应正文。"""

    finished = Signal()

    def __init__(
        self,
        status: Optional[int] = None,
        body: bytes = b"",
        error: QNetworkReply.NetworkError = QNetworkReply.NetworkError.NoError,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._status = status
        self._body = QByteArray(body)
        self._error = error
        self.aborted = False

    def error(self) -> QNetworkReply.NetworkError:
        return self._error

    def attribute(self, key: Any) -> Optional[int]:
        return self._status

    def readAll(self) -> QByteArray:
        return self._body

    def abort(self) -> None:
        # 真实 Qt 在 abort 后也会发出 finished，这里保持一致。
        self.aborted = True
        self.finished.emit()


class FakeManager:
    """假的 ``QNetworkAccessManager``：记录请求并返回预置的 reply。"""

    def __init__(self, reply: FakeReply) -> None:
        self.reply = reply
        self.requests: list[QNetworkRequest] = []
        self.bodies: list[bytes] = []
        self.verbs: list[str] = []

    def get(self, request: QNetworkRequest) -> FakeReply:
        self.requests.append(request)
        return self.reply

    def post(self, request: QNetworkRequest, body: QByteArray) -> FakeReply:
        self.requests.append(request)
        self.bodies.append(bytes(body))
        return self.reply

    def sendCustomRequest(
        self, request: QNetworkRequest, verb: bytes, body: QByteArray
    ) -> FakeReply:
        self.requests.append(request)
        self.verbs.append(bytes(verb).decode("ascii"))
        self.bodies.append(bytes(body))
        return self.reply

    def deleteResource(self, request: QNetworkRequest) -> FakeReply:
        self.requests.append(request)
        return self.reply


class ProbeClient(JsonApiClient):
    """最小可用的基类客户端：把结果记到列表里，供断言检查。"""

    def __init__(self, base_url: str, manager: FakeManager) -> None:
        super().__init__(base_url, 10.0, manager=manager)
        self.oks: list[dict] = []
        self.errors: list[ReplyOutcome] = []
        self.local_errors: list[str] = []

    def fetch(
        self,
        path: str,
        method: str = "GET",
        token: str = "",
        payload: Optional[dict] = None,
    ) -> Optional[FakeReply]:
        url, message = self.prepare_url(path)
        if url is None:
            self.local_errors.append(message)
            return None
        reply = self.send_request(
            method, url, token=token, payload=payload
        )
        if reply is None:
            self.local_errors.append(self.unsupported_method_message)
            return None
        reply.finished.connect(
            lambda reply=reply: self.handle_reply(
                reply,
                on_success=self.oks.append,
                on_error=self.errors.append,
            )
        )
        return reply  # type: ignore[return-value]


def make_client(cls: Any, manager: FakeManager, *args: Any) -> Any:
    """构造业务客户端并注入假 manager。

    业务客户端的构造函数是对外契约，不能为了测试加参数；这里在构造之后
    替换掉基类里的 manager。
    """
    client = cls(*args)
    client._manager = manager
    return client


class FormatApiErrorTests(unittest.TestCase):
    """错误文案提取：形态兼容、长度可控。"""

    def test_detail_string(self) -> None:
        self.assertEqual(
            format_api_error({"detail": "用户名或密码错误"}),
            "用户名或密码错误",
        )

    def test_detail_list_is_joined(self) -> None:
        message = format_api_error(
            {"detail": [{"msg": "用户名太短"}, {"msg": "密码太短"}]}
        )
        self.assertEqual(message, "用户名太短；密码太短")

    def test_detail_list_ignores_broken_items(self) -> None:
        message = format_api_error(
            {"detail": [{"msg": "第一条"}, 42, {"msg": ""}, {"msg": "第二条"}]}
        )
        self.assertEqual(message, "第一条；第二条")

    def test_missing_detail_returns_empty(self) -> None:
        self.assertEqual(format_api_error({}), "")
        self.assertEqual(format_api_error({"detail": None}), "")
        self.assertEqual(format_api_error({"detail": []}), "")
        self.assertEqual(format_api_error(None), "")
        self.assertEqual(format_api_error("不是对象"), "")

    def test_detail_is_truncated_at_the_documented_limit(self) -> None:
        message = format_api_error({"detail": "长" * 1000})
        self.assertLessEqual(len(message), ERROR_DETAIL_LIMIT)
        self.assertTrue(message.endswith("…"))
        self.assertEqual(ERROR_DETAIL_LIMIT, 300)

    def test_short_detail_is_not_truncated(self) -> None:
        message = format_api_error({"detail": "短"})
        self.assertEqual(message, "短")


class ParseJsonBodyTests(unittest.TestCase):
    """响应正文的严格解析。"""

    def test_valid_json_object(self) -> None:
        ok, payload = parse_json_body(b'{"a": 1}')
        self.assertTrue(ok)
        self.assertEqual(payload, {"a": 1})

    def test_broken_json_is_a_failure(self) -> None:
        ok, payload = parse_json_body(b"<html>502</html>")
        self.assertFalse(ok)
        self.assertIsNone(payload)

    def test_broken_utf8_is_a_failure(self) -> None:
        ok, payload = parse_json_body(b"\xff\xfe")
        self.assertFalse(ok)
        self.assertIsNone(payload)

    def test_empty_body_is_an_empty_object(self) -> None:
        """服务端 204 No Content 不带正文，属于正常响应。"""
        ok, payload = parse_json_body(b"")
        self.assertTrue(ok)
        self.assertEqual(payload, {})

    def test_json_array_is_parsed_but_not_an_object(self) -> None:
        ok, payload = parse_json_body(b"[1, 2]")
        self.assertTrue(ok)
        self.assertEqual(payload, [1, 2])


class ApiPathTests(unittest.TestCase):
    """``/api/v1`` 前缀只有一个来源。"""

    def test_prefix_constants(self) -> None:
        self.assertEqual(API_VERSION, "v1")
        self.assertEqual(API_PREFIX, "/api/v1")
        self.assertEqual(WS_PATH, "/api/v1/ws")

    def test_api_path_normalises_slashes(self) -> None:
        self.assertEqual(api_path("auth", "login"), "/api/v1/auth/login")
        self.assertEqual(api_path("/auth/login"), "/api/v1/auth/login")
        self.assertEqual(api_path("auth/login"), "/api/v1/auth/login")
        self.assertEqual(api_path("/auth/", "/login"), "/api/v1/auth/login")
        self.assertEqual(api_path("friends"), "/api/v1/friends")
        self.assertEqual(api_path(), "/api/v1")


class UrlBuildingTests(unittest.TestCase):
    """地址拼接与校验。"""

    def test_valid_url_is_built_from_base_and_path(self) -> None:
        client = ProbeClient(BASE_URL, FakeManager(FakeReply(status=200)))
        url, message = client.prepare_url(api_path("friends"))
        self.assertEqual(message, "")
        self.assertIsNotNone(url)
        assert url is not None
        self.assertEqual(
            url.toString(), "http://127.0.0.1:8000/api/v1/friends"
        )

    def test_bad_scheme_is_rejected(self) -> None:
        client = ProbeClient("ftp://example.test", FakeManager(FakeReply()))
        url, message = client.prepare_url(api_path("friends"))
        self.assertIsNone(url)
        self.assertEqual(message, "服务器地址无效")

    def test_missing_host_is_rejected(self) -> None:
        client = ProbeClient("http://", FakeManager(FakeReply()))
        url, message = client.prepare_url(api_path("friends"))
        self.assertIsNone(url)
        self.assertEqual(message, "服务器地址无效")

    def test_missing_base_url_is_rejected(self) -> None:
        client = ProbeClient("", FakeManager(FakeReply()))
        url, message = client.prepare_url(api_path("friends"))
        self.assertIsNone(url)
        self.assertEqual(message, "未配置服务器地址")


class BaseClientRequestTests(unittest.TestCase):
    """基类统一处理请求头、超时与方法分发。"""

    def test_bearer_header_and_timeout(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"), token="secret")
        self.assertIsNotNone(reply)
        request = manager.requests[0]
        self.assertEqual(
            bytes(request.rawHeader("Authorization")), b"Bearer secret"
        )
        self.assertEqual(request.transferTimeout(), 10000)

    def test_no_token_means_no_authorization_header(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = ProbeClient(BASE_URL, manager)
        client.fetch(api_path("friends"))
        request = manager.requests[0]
        self.assertEqual(bytes(request.rawHeader("Authorization")), b"")

    def test_payload_is_serialized_as_json(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = ProbeClient(BASE_URL, manager)
        client.fetch(api_path("reports"), method="POST", payload={"a": 1})
        request = manager.requests[0]
        self.assertEqual(
            bytes(request.rawHeader("Content-Type")), b"application/json"
        )
        self.assertEqual(json.loads(manager.bodies[0]), {"a": 1})

    def test_methods_are_dispatched(self) -> None:
        for method, verb in (
            ("GET", ""),
            ("POST", ""),
            ("PATCH", "PATCH"),
            ("PUT", "PUT"),
            ("DELETE", ""),
        ):
            manager = FakeManager(FakeReply(status=200))
            client = ProbeClient(BASE_URL, manager)
            reply = client.fetch(
                api_path("friends"), method=method, payload={"a": 1}
            )
            self.assertIsNotNone(reply, method)
            self.assertEqual(len(manager.requests), 1, method)
            expected = [verb] if verb else []
            self.assertEqual(manager.verbs, expected, method)

    def test_unsupported_method_is_rejected(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"), method="TRACE")
        self.assertIsNone(reply)
        self.assertEqual(client.local_errors, ["不支持的请求方法"])
        self.assertEqual(manager.requests, [])

    def test_generated_url_is_byte_identical_to_before(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = ProbeClient(BASE_URL, manager)
        client.fetch(api_path("users", "u1"))
        self.assertEqual(
            manager.requests[0].url().toString(),
            "http://127.0.0.1:8000/api/v1/users/u1",
        )


class BaseClientReplyTests(unittest.TestCase):
    """基类统一处理响应：畸形正文必须是失败。"""

    def test_malformed_body_is_a_failure(self) -> None:
        """回归：畸形正文曾经被当成空的成功结果报给用户。"""
        manager = FakeManager(FakeReply(status=200, body=b"<html>502</html>"))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"))
        assert reply is not None
        reply.finished.emit()

        self.assertEqual(client.oks, [])
        self.assertEqual(len(client.errors), 1)
        self.assertTrue(client.errors[0].malformed)
        self.assertFalse(client.errors[0].success)
        self.assertEqual(
            client.errors[0].error_message, "服务器响应格式异常"
        )

    def test_json_array_body_is_a_failure(self) -> None:
        manager = FakeManager(FakeReply(status=200, body=b"[1, 2]"))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"))
        assert reply is not None
        reply.finished.emit()

        self.assertEqual(client.oks, [])
        self.assertEqual(len(client.errors), 1)
        self.assertTrue(client.errors[0].malformed)

    def test_empty_body_succeeds_with_empty_object(self) -> None:
        """204 No Content 没有正文，仍然算成功。"""
        manager = FakeManager(FakeReply(status=204, body=b""))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"))
        assert reply is not None
        reply.finished.emit()

        self.assertEqual(client.oks, [{}])
        self.assertEqual(client.errors, [])

    def test_error_detail_is_used_for_failure(self) -> None:
        body = json.dumps({"detail": "好友不存在"}).encode("utf-8")
        manager = FakeManager(FakeReply(status=404, body=body))
        client = ProbeClient(BASE_URL, manager)
        reply = client.fetch(api_path("friends"))
        assert reply is not None
        reply.finished.emit()

        self.assertEqual(client.oks, [])
        self.assertEqual(len(client.errors), 1)
        self.assertEqual(client.errors[0].error_message, "好友不存在")
        self.assertEqual(client.errors[0].status, 404)


class SocialClientTests(unittest.TestCase):
    """社交客户端：畸形响应不得被当成「没有好友/没有消息」。"""

    def _client(self, reply: FakeReply) -> SocialHTTPClient:
        manager = FakeManager(reply)
        client = make_client(SocialHTTPClient, manager, BASE_URL)
        self.manager = manager
        return client

    def test_malformed_response_is_reported_as_failure(self) -> None:
        """回归测试：畸形 JSON 必须报错，不能报成空列表。"""
        client = self._client(FakeReply(status=200, body=b"not a json"))
        ready: list[tuple[str, object]] = []
        errors: list[tuple[str, str]] = []
        client.response_ready.connect(
            lambda kind, data: ready.append((kind, data))
        )
        client.failed.connect(lambda kind, message: errors.append((kind, message)))

        self.assertTrue(client.fetch_friends("token"))
        self.manager.reply.finished.emit()

        self.assertEqual(ready, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "friends")
        self.assertEqual(errors[0][1], "服务器响应格式异常")

    def test_malformed_response_is_reported_as_failure_for_messages(self) -> None:
        client = self._client(FakeReply(status=200, body=b"<html/>"))
        errors: list[tuple[str, str]] = []
        client.failed.connect(lambda kind, message: errors.append((kind, message)))

        self.assertTrue(client.fetch_messages("token", "u1"))
        self.manager.reply.finished.emit()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0][0], "messages:u1")

    def test_valid_response_still_reports_data(self) -> None:
        body = json.dumps({"items": [{"id": "u2"}]}).encode("utf-8")
        client = self._client(FakeReply(status=200, body=body))
        ready: list[tuple[str, object]] = []
        client.response_ready.connect(
            lambda kind, data: ready.append((kind, data))
        )

        self.assertTrue(client.fetch_friends("token"))
        self.manager.reply.finished.emit()

        self.assertEqual(ready, [("friends", {"items": [{"id": "u2"}]})])

    def test_unauthorized_does_not_emit_failed(self) -> None:
        """401 只交给令牌刷新处理，不再重复报错。"""
        body = json.dumps({"detail": "未提供认证凭据"}).encode("utf-8")
        client = self._client(FakeReply(status=401, body=body))
        unauthorized: list[str] = []
        errors: list[tuple[str, str]] = []
        client.unauthorized.connect(unauthorized.append)
        client.failed.connect(lambda kind, message: errors.append((kind, message)))

        self.assertTrue(client.fetch_friends("token"))
        self.manager.reply.finished.emit()

        self.assertEqual(unauthorized, ["friends"])
        self.assertEqual(errors, [])

    def test_no_content_delete_still_succeeds(self) -> None:
        """删除好友返回 204 空正文，仍然算成功。"""
        client = self._client(FakeReply(status=204, body=b""))
        ready: list[tuple[str, object]] = []
        client.response_ready.connect(
            lambda kind, data: ready.append((kind, data))
        )

        self.assertTrue(client.remove_friend("token", "u1"))
        self.manager.reply.finished.emit()

        self.assertEqual(ready, [("friend_remove", {})])

    def test_missing_token_is_rejected_before_request(self) -> None:
        client = self._client(FakeReply(status=200))
        errors: list[tuple[str, str]] = []
        client.failed.connect(lambda kind, message: errors.append((kind, message)))

        self.assertFalse(client.fetch_friends(""))
        self.assertEqual(errors, [("friends", "登录状态失效，请重新登录")])
        self.assertEqual(self.manager.requests, [])

    def test_generated_urls_keep_the_v1_prefix(self) -> None:
        client = self._client(FakeReply(status=200))
        client.fetch_friends("token")
        client.fetch_user("token", "u1")
        client.search_users("token", "abc")
        client.fetch_messages("token", "u1", 30)
        urls = [req.url().toString() for req in self.manager.requests]
        self.assertEqual(
            urls,
            [
                "http://127.0.0.1:8000/api/v1/friends",
                "http://127.0.0.1:8000/api/v1/users/u1",
                "http://127.0.0.1:8000/api/v1/users/search?q=abc",
                "http://127.0.0.1:8000/api/v1/messages/u1?limit=30",
            ],
        )


class OtherClientsTests(unittest.TestCase):
    """其余业务客户端共用基类，且保留各自原有的失败语义。"""

    def test_all_clients_derive_from_the_shared_base(self) -> None:
        for cls in (
            AuthHTTPClient,
            SocialHTTPClient,
            ReportHTTPClient,
            ProfileHTTPClient,
            GameHistoryHTTPClient,
            UpdateHTTPClient,
        ):
            self.assertTrue(issubclass(cls, JsonApiClient), cls.__name__)

    def test_report_client_emits_unauthorized_and_failed(self) -> None:
        body = json.dumps({"detail": "令牌已过期"}).encode("utf-8")
        manager = FakeManager(FakeReply(status=401, body=body))
        client = make_client(ReportHTTPClient, manager, BASE_URL)
        unauthorized: list[bool] = []
        errors: list[str] = []
        client.unauthorized.connect(lambda: unauthorized.append(True))
        client.failed.connect(errors.append)

        self.assertTrue(client.fetch_audit("token", "g1"))
        manager.reply.finished.emit()

        self.assertEqual(unauthorized, [True])
        self.assertEqual(errors, ["令牌已过期"])

    def test_report_client_malformed_response_is_a_failure(self) -> None:
        manager = FakeManager(FakeReply(status=200, body=b"not a json"))
        client = make_client(ReportHTTPClient, manager, BASE_URL)
        submitted: list[object] = []
        errors: list[str] = []
        client.submitted.connect(submitted.append)
        client.failed.connect(errors.append)

        self.assertTrue(client.submit_report("token", "g1", "cheat", "描述"))
        manager.reply.finished.emit()

        self.assertEqual(submitted, [])
        self.assertEqual(errors, ["服务器返回的数据格式异常"])

    def test_profile_client_malformed_response_is_a_failure(self) -> None:
        manager = FakeManager(FakeReply(status=200, body=b"not a json"))
        client = make_client(ProfileHTTPClient, manager, BASE_URL)
        ready: list[object] = []
        errors: list[str] = []
        client.profile_ready.connect(ready.append)
        client.failed.connect(errors.append)

        self.assertTrue(client.fetch_profile("token"))
        manager.reply.finished.emit()

        self.assertEqual(ready, [])
        self.assertEqual(errors, ["服务器返回的排位数据格式异常"])

    def test_game_history_client_malformed_response_is_a_failure(self) -> None:
        manager = FakeManager(FakeReply(status=200, body=b"not a json"))
        client = make_client(GameHistoryHTTPClient, manager, BASE_URL)
        ready: list[object] = []
        errors: list[str] = []
        client.recent_ready.connect(ready.append)
        client.failed.connect(errors.append)

        self.assertTrue(client.fetch_recent("token"))
        manager.reply.finished.emit()

        self.assertEqual(ready, [])
        self.assertEqual(errors, ["服务器返回的对局数据格式异常"])

    def test_auth_client_malformed_response_is_a_failure(self) -> None:
        manager = FakeManager(FakeReply(status=200, body=b"not a json"))
        client = make_client(AuthHTTPClient, manager, BASE_URL)
        sessions: list[object] = []
        errors: list[str] = []
        client.session_ready.connect(sessions.append)
        client.failed.connect(errors.append)

        self.assertTrue(client.login("u", "p"))
        manager.reply.finished.emit()

        self.assertEqual(sessions, [])
        self.assertEqual(errors, ["服务器响应格式异常"])
        self.assertIsNone(client.last_error_status)

    def test_auth_client_uses_the_v1_prefix(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = make_client(AuthHTTPClient, manager, BASE_URL)
        client.login("u", "p")
        self.assertEqual(
            manager.requests[0].url().toString(),
            "http://127.0.0.1:8000/api/v1/auth/login",
        )

    def test_update_client_keeps_its_own_url_handling(self) -> None:
        """更新检查直连 GitHub：不拼基址，也不需要 Bearer 头。"""
        manager = FakeManager(FakeReply(status=200))
        client = make_client(
            UpdateHTTPClient,
            manager,
            "https://api.github.com/repos/ckm6666/Gomoku/releases/latest",
        )
        self.assertTrue(client.check())
        request = manager.requests[0]
        self.assertEqual(
            request.url().toString(),
            "https://api.github.com/repos/ckm6666/Gomoku/releases/latest",
        )
        self.assertEqual(bytes(request.rawHeader("Authorization")), b"")

    def test_report_client_urls_keep_the_v1_prefix(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = make_client(ReportHTTPClient, manager, BASE_URL)
        client.submit_report("token", "g1", "cheat", "描述")
        client.fetch_audit("token", "g1")
        self.assertEqual(
            [req.url().toString() for req in manager.requests],
            [
                "http://127.0.0.1:8000/api/v1/reports",
                "http://127.0.0.1:8000/api/v1/games/g1/audit",
            ],
        )

    def test_profile_client_urls_keep_the_v1_prefix(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = make_client(ProfileHTTPClient, manager, BASE_URL)
        client.fetch_profile("token")
        client.fetch_leaderboard("token", 20)
        self.assertEqual(
            [req.url().toString() for req in manager.requests],
            [
                "http://127.0.0.1:8000/api/v1/profile/me",
                "http://127.0.0.1:8000/api/v1/leaderboard?limit=20",
            ],
        )

    def test_game_history_client_urls_keep_the_v1_prefix(self) -> None:
        manager = FakeManager(FakeReply(status=200))
        client = make_client(GameHistoryHTTPClient, manager, BASE_URL)
        client.fetch_recent("token", 15)
        client.fetch_game("token", "g1")
        client.fetch_live("token")
        self.assertEqual(
            [req.url().toString() for req in manager.requests],
            [
                "http://127.0.0.1:8000/api/v1/games/recent?limit=15",
                "http://127.0.0.1:8000/api/v1/games/g1",
                "http://127.0.0.1:8000/api/v1/games/live",
            ],
        )


class WebSocketEndpointTests(unittest.TestCase):
    """A14：带路径的 WS 基址必须被拒绝。"""

    def test_ws_base_url_with_path_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError) as ctx:
            AppConfig.from_env(
                {"GOMOKU_WS_BASE_URL": "wss://api.example.test/foo"}
            )
        self.assertIn("GOMOKU_WS_BASE_URL", str(ctx.exception))

    def test_api_base_url_with_path_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError) as ctx:
            AppConfig.from_env(
                {"GOMOKU_API_BASE_URL": "https://api.example.test/foo"}
            )
        self.assertIn("GOMOKU_API_BASE_URL", str(ctx.exception))

    def test_ws_base_url_without_path_resolves(self) -> None:
        config = AppConfig.from_env(
            {"GOMOKU_WS_BASE_URL": "wss://api.example.test"}
        )
        _api, ws = resolve_endpoints(config)
        self.assertEqual(ws, "wss://api.example.test/api/v1/ws")

    def test_ws_base_url_with_trailing_slash_resolves(self) -> None:
        config = AppConfig.from_env(
            {"GOMOKU_WS_BASE_URL": "wss://api.example.test/"}
        )
        _api, ws = resolve_endpoints(config)
        self.assertEqual(ws, "wss://api.example.test/api/v1/ws")

    def test_resolve_endpoints_rejects_a_ws_path(self) -> None:
        """直接构造的配置也要拦下带路径的 WS 地址。"""
        config = AppConfig(
            environment="production",
            api_base_url="https://api.example.test",
            ws_base_url="wss://api.example.test/foo",
        )
        with self.assertRaises(ConfigurationError) as ctx:
            resolve_endpoints(config)
        self.assertIn("GOMOKU_WS_BASE_URL", str(ctx.exception))

    def test_explicit_ws_path_is_kept(self) -> None:
        config = AppConfig(
            environment="production",
            api_base_url="https://api.example.test",
            ws_base_url="wss://api.example.test/api/v1/ws",
        )
        _api, ws = resolve_endpoints(config)
        self.assertEqual(ws, "wss://api.example.test/api/v1/ws")


if __name__ == "__main__":
    unittest.main()
