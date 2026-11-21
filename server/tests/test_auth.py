"""账号与认证 API 测试。"""

from __future__ import annotations

import asyncio
import unittest

from server.app.security import LoginBackoff
from server.tests.helpers import ServerTestBase


class LoginBackoffTests(unittest.TestCase):
    """登录退避的单元测试（不经过 HTTP，也不真的等待）。"""

    def test_delay_grows_then_caps(self) -> None:
        backoff = LoginBackoff(
            base_delay_seconds=0.5, max_delay_seconds=3.0
        )

        async def _run() -> list[float]:
            delays = []
            for _ in range(5):
                delays.append(await backoff.delay_for("k"))
                await backoff.record_failure("k")
            return delays

        delays = asyncio.run(_run())
        # 第一次不等待；之后 0.5 → 1 → 2 → 3（封顶）。
        self.assertEqual(delays, [0.0, 0.5, 1.0, 2.0, 3.0])

    def test_success_clears_the_counter(self) -> None:
        backoff = LoginBackoff(base_delay_seconds=0.5)

        async def _run() -> float:
            await backoff.record_failure("k")
            await backoff.clear("k")
            return await backoff.delay_for("k")

        self.assertEqual(asyncio.run(_run()), 0.0)

    def test_keys_are_independent(self) -> None:
        backoff = LoginBackoff(base_delay_seconds=0.5)

        async def _run() -> float:
            await backoff.record_failure("a")
            return await backoff.delay_for("b")

        self.assertEqual(asyncio.run(_run()), 0.0)


class AuthAPITests(ServerTestBase):
    def test_health_is_public(self) -> None:
        response = self.client.get("/api/v1/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

    def test_register_login_me(self) -> None:
        response = self.register("棋手甲")
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        self.assertEqual(data["user"]["username"], "棋手甲")
        self.assertTrue(data["access_token"])
        self.assertTrue(data["refresh_token"])

        me = self.client.get(
            "/api/v1/auth/me",
            headers=self.auth_headers(data["access_token"]),
        )
        self.assertEqual(me.status_code, 200)
        self.assertEqual(me.json()["username"], "棋手甲")

        login = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "棋手甲",
                "password": "correct horse battery",
            },
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(login.json()["user"]["username"], "棋手甲")

    def test_duplicate_username_and_bad_password(self) -> None:
        self.assertEqual(self.register("same_name").status_code, 201)
        duplicate = self.register("SAME_NAME")
        self.assertEqual(duplicate.status_code, 409)

        bad = self.client.post(
            "/api/v1/auth/login",
            json={"username": "same_name", "password": "wrong-password"},
        )
        self.assertEqual(bad.status_code, 401)

    def test_repeated_bad_passwords_do_not_lock_out(self) -> None:
        """多次输错密码只返回 401，不再因次数过多拒绝登录。"""
        self.assertEqual(self.register("patient_user").status_code, 201)
        for _ in range(8):
            bad = self.client.post(
                "/api/v1/auth/login",
                json={
                    "username": "patient_user",
                    "password": "wrong-password",
                },
            )
            self.assertEqual(bad.status_code, 401, bad.text)

        good = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "patient_user",
                "password": "correct horse battery",
            },
        )
        self.assertEqual(good.status_code, 200, good.text)

    def test_refresh_rotation_and_reuse_detection(self) -> None:
        data = self.register("refresh_user").json()
        old_refresh = data["refresh_token"]

        refreshed = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        self.assertEqual(refreshed.status_code, 200, refreshed.text)
        new_refresh = refreshed.json()["refresh_token"]
        self.assertNotEqual(old_refresh, new_refresh)

        # 重复使用旧 refresh token，应视为重放并撤销当前新会话
        replay = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": old_refresh},
        )
        self.assertEqual(replay.status_code, 401)

        after_replay = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": new_refresh},
        )
        self.assertEqual(after_replay.status_code, 401)

    def test_logout_revokes_refresh_token(self) -> None:
        data = self.register("logout_user").json()
        response = self.client.post(
            "/api/v1/auth/logout",
            headers=self.auth_headers(data["access_token"]),
            json={"refresh_token": data["refresh_token"]},
        )
        self.assertEqual(response.status_code, 204)

        refresh = self.client.post(
            "/api/v1/auth/refresh",
            json={"refresh_token": data["refresh_token"]},
        )
        self.assertEqual(refresh.status_code, 401)

    def test_username_validation(self) -> None:
        response = self.client.post(
            "/api/v1/auth/register",
            json={
                "username": "bad name!",
                "password": "correct horse battery",
            },
        )
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
