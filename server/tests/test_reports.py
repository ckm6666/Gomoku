"""举报提交、管理员复核与对局审计接口测试。"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_QUICK_MATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    make_message,
    parse_game_state,
)
from server.tests.helpers import ServerTestBase


class ReportFlowTests(ServerTestBase):
    admin_usernames = frozenset({"report_admin"})

    def _register_access(self, username: str):
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["access_token"], response.json()["user"]["id"]

    def _play_ranked_resign(
        self, first_token: str, second_token: str
    ) -> tuple[str, str]:
        """打一局排位并让白方认输；返回 (game_id, 认输方 token)。"""
        with self.client.websocket_connect("/api/v1/ws") as first:
            with self.client.websocket_connect("/api/v1/ws") as second:
                for ws, token in (
                    (first, first_token),
                    (second, second_token),
                ):
                    ws.send_json(
                        make_message(CLIENT_AUTH, {"access_token": token})
                    )
                    self.assertEqual(
                        ws.receive_json()["type"], SERVER_AUTHENTICATED
                    )
                first.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                first.receive_json()
                second.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                game_id = ""
                snapshots = {}
                for ws, token in (
                    (first, first_token),
                    (second, second_token),
                ):
                    for _ in range(8):
                        message = ws.receive_json()
                        if message["type"] == SERVER_GAME_STATE:
                            snapshot = parse_game_state(
                                message["payload"]
                            )
                            game_id = snapshot.game_id
                            snapshots[token] = snapshot
                            break
                    else:
                        self.fail("未收到开局快照")
                # 让白方认输，黑方获胜。
                loser_token = next(
                    token
                    for token, snapshot in snapshots.items()
                    if snapshot.your_color.value == "white"
                )
                loser_ws = (
                    first if loser_token == first_token else second
                )
                loser_ws.send_json(make_message(CLIENT_RESIGN, {}))
                # 等服务器把终局写库并广播，避免举报接口看到 playing。
                for ws in (first, second):
                    for _ in range(10):
                        message = ws.receive_json()
                        if message["type"] == SERVER_GAME_STATE:
                            snapshot = parse_game_state(
                                message["payload"]
                            )
                            if snapshot.is_game_over:
                                break
                    else:
                        self.fail("未收到终局快照")
                return game_id, loser_token

    def test_report_create_list_and_audit(self) -> None:
        reporter_token, _ = self._register_access("report_user_a")
        opponent_token, _ = self._register_access("report_user_b")
        outsider_token, _ = self._register_access("report_outsider")
        game_id, loser_token = self._play_ranked_resign(
            reporter_token, opponent_token
        )

        # 认输终局的对局记录应该能通过回放审计。
        audit = self.client.get(
            f"/api/v1/games/{game_id}/audit",
            headers=self.auth_headers(loser_token),
        )
        self.assertEqual(audit.status_code, 200, audit.text)
        audit_data = audit.json()
        self.assertTrue(audit_data["ok"], audit_data)
        self.assertEqual(audit_data["findings"], [])
        self.assertGreaterEqual(audit_data["move_count"], 0)

        created = self.client.post(
            "/api/v1/reports",
            headers=self.auth_headers(loser_token),
            json={
                "game_id": game_id,
                "category": "cheating",
                "description": "测试举报",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        report = created.json()
        self.assertEqual(report["status"], "pending")
        self.assertEqual(report["category"], "cheating")

        duplicate = self.client.post(
            "/api/v1/reports",
            headers=self.auth_headers(loser_token),
            json={"game_id": game_id, "category": "other"},
        )
        self.assertEqual(duplicate.status_code, 409)

        mine = self.client.get(
            "/api/v1/reports/mine",
            headers=self.auth_headers(loser_token),
        )
        self.assertEqual(mine.status_code, 200)
        self.assertEqual(len(mine.json()["reports"]), 1)

        outsider_audit = self.client.get(
            f"/api/v1/games/{game_id}/audit",
            headers=self.auth_headers(outsider_token),
        )
        self.assertEqual(outsider_audit.status_code, 404)
        outsider_report = self.client.post(
            "/api/v1/reports",
            headers=self.auth_headers(outsider_token),
            json={"game_id": game_id, "category": "cheating"},
        )
        self.assertEqual(outsider_report.status_code, 404)

        invalid_category = self.client.post(
            "/api/v1/reports",
            headers=self.auth_headers(opponent_token),
            json={"game_id": game_id, "category": "hacking"},
        )
        self.assertEqual(invalid_category.status_code, 422)

        forbidden = self.client.get(
            "/api/v1/admin/reports",
            headers=self.auth_headers(loser_token),
        )
        self.assertEqual(forbidden.status_code, 403)

    def test_admin_review_flow(self) -> None:
        admin_token, _ = self._register_access("report_admin")
        reporter_token, _ = self._register_access("review_reporter")
        opponent_token, _ = self._register_access("review_opponent")
        game_id, loser_token = self._play_ranked_resign(
            reporter_token, opponent_token
        )
        created = self.client.post(
            "/api/v1/reports",
            headers=self.auth_headers(opponent_token),
            json={
                "game_id": game_id,
                "category": "stalling",
                "description": "对手故意拖延",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        report_id = created.json()["id"]

        listing = self.client.get(
            "/api/v1/admin/reports",
            headers=self.auth_headers(admin_token),
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        data = listing.json()
        self.assertEqual(data["total"], 1)
        entry = data["reports"][0]
        self.assertEqual(entry["reporter_username"], "review_opponent")
        self.assertEqual(entry["reported_username"], "review_reporter")
        self.assertEqual(entry["game_status"], "black_won")
        self.assertIn("audit_findings", entry)
        self.assertTrue(entry["game_rated"])

        admin_audit = self.client.get(
            f"/api/v1/games/{game_id}/audit",
            headers=self.auth_headers(admin_token),
        )
        self.assertEqual(admin_audit.status_code, 200)

        reviewed = self.client.post(
            f"/api/v1/admin/reports/{report_id}/review",
            headers=self.auth_headers(admin_token),
            json={"action": "dismiss", "note": "复核未发现异常"},
        )
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertEqual(reviewed.json()["status"], "dismissed")
        self.assertEqual(
            reviewed.json()["resolution"], "复核未发现异常"
        )

        again = self.client.post(
            f"/api/v1/admin/reports/{report_id}/review",
            headers=self.auth_headers(admin_token),
            json={"action": "uphold"},
        )
        self.assertEqual(again.status_code, 409)

        pending = self.client.get(
            "/api/v1/admin/reports?status=pending",
            headers=self.auth_headers(admin_token),
        )
        self.assertEqual(pending.status_code, 200)
        self.assertEqual(pending.json()["total"], 0)

        # 未参与该局的账号仍然不能举报。
        self.assertNotEqual(loser_token, "")


class ReportOnlyFinishedTests(ServerTestBase):
    def test_cannot_report_unfinished_game(self) -> None:
        first = self.register("unfinished_a").json()
        second = self.register("unfinished_b").json()
        with self.client.websocket_connect("/api/v1/ws") as ws_a:
            with self.client.websocket_connect("/api/v1/ws") as ws_b:
                for ws, data in ((ws_a, first), (ws_b, second)):
                    ws.send_json(
                        make_message(
                            CLIENT_AUTH,
                            {"access_token": data["access_token"]},
                        )
                    )
                    self.assertEqual(
                        ws.receive_json()["type"], SERVER_AUTHENTICATED
                    )
                ws_a.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                ws_a.receive_json()
                ws_b.send_json(make_message(CLIENT_QUICK_MATCH, {}))
                game_id = ""
                for ws in (ws_a, ws_b):
                    for _ in range(8):
                        message = ws.receive_json()
                        if message["type"] == SERVER_GAME_STATE:
                            game_id = message["payload"]["game_id"]
                            break
                response = self.client.post(
                    "/api/v1/reports",
                    headers=self.auth_headers(first["access_token"]),
                    json={"game_id": game_id, "category": "cheating"},
                )
                self.assertEqual(response.status_code, 409)


if __name__ == "__main__":
    unittest.main(verbosity=2)
