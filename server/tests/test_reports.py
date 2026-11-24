"""举报提交、管理员复核与对局审计接口测试。"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_QUICK_MATCH,
    CLIENT_RESIGN,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    make_message,
    parse_game_state,
)
from server.app.db import create_engine, create_session_factory
from server.app.models import Game, GameReport, Move
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


class AdminReportQueryCountTests(ServerTestBase):
    """管理员举报列表不能按对局数 N+1 查询落子表。"""

    admin_usernames = frozenset({"batch_admin"})

    def _seed_games(self, specs: list[dict[str, Any]]) -> None:
        """直接写库造数据：每条 spec 描述一局对局及其落子。

        写入用独立的引擎/事件循环，避免和应用自己的连接池互相干扰。
        """

        async def seed() -> None:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    for spec in specs:
                        game = Game(
                            id=spec["game_id"],
                            room_code=spec["game_id"][:8],
                            black_user_id=spec["black_id"],
                            white_user_id=spec["white_id"],
                            status="black_won",
                            winner_user_id=spec["black_id"],
                            result_reason="resign",
                            move_no=len(spec["moves"]),
                            rated=True,
                        )
                        session.add(game)
                        for index, (row, col) in enumerate(
                            spec["moves"], start=1
                        ):
                            color = "black" if index % 2 == 1 else "white"
                            user_id = (
                                spec["black_id"]
                                if color == "black"
                                else spec["white_id"]
                            )
                            session.add(
                                Move(
                                    game_id=spec["game_id"],
                                    move_no=index,
                                    user_id=user_id,
                                    color=color,
                                    row=row,
                                    col=col,
                                    client_move_id=f"cm-{index}",
                                )
                            )
                        session.add(
                            GameReport(
                                game_id=spec["game_id"],
                                reporter_id=spec["white_id"],
                                reported_user_id=spec["black_id"],
                                category="cheating",
                                description="批量查询测试",
                                status="pending",
                            )
                        )
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(seed())

    def _count_move_queries(self, path: str, token: str) -> int:
        """统计一次请求里对 moves 表的 SELECT 次数。"""
        statements: list[str] = []
        engine = self.app.state.engine

        def _record(
            conn: Any,
            cursor: Any,
            statement: str,
            parameters: Any,
            context: Any,
            executemany: bool,
        ) -> None:
            statements.append(statement)

        from sqlalchemy import event

        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            response = self.client.get(
                path, headers=self.auth_headers(token)
            )
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            event.remove(
                engine.sync_engine, "before_cursor_execute", _record
            )
        return sum(1 for item in statements if "FROM moves" in item)

    def test_audit_uses_one_query_for_many_games(self) -> None:
        admin = self.register("batch_admin").json()
        black = self.register("batch_black").json()
        white = self.register("batch_white").json()

        specs: list[dict[str, Any]] = []
        for index in range(3):
            specs.append(
                {
                    "game_id": f"game-{index}",
                    "black_id": black["user"]["id"],
                    "white_id": white["user"]["id"],
                    "moves": [(7, 7), (7, 8), (7, 9)],
                }
            )
        self._seed_games(specs)

        move_queries = self._count_move_queries(
            "/api/v1/admin/reports", admin["access_token"]
        )
        # 三局对局只查一次 moves 表，不能随对局数线性增长。
        self.assertEqual(move_queries, 1)

        listing = self.client.get(
            "/api/v1/admin/reports",
            headers=self.auth_headers(admin["access_token"]),
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        data = listing.json()
        self.assertEqual(data["total"], 3)
        self.assertEqual(len(data["reports"]), 3)
        for entry in data["reports"]:
            # 数据一致（座位、颜色、终局）时审计没有异常。
            self.assertEqual(entry["audit_findings"], 0, entry)
            self.assertEqual(entry["game_status"], "black_won")

    def test_audit_findings_match_direct_audit(self) -> None:
        admin = self.register("batch_admin").json()
        black = self.register("batch_black").json()
        white = self.register("batch_white").json()
        # move_no 与实际落子数对不上：审计必须报出 1 条异常。
        self._seed_games(
            [
                {
                    "game_id": "game-broken",
                    "black_id": black["user"]["id"],
                    "white_id": white["user"]["id"],
                    "moves": [(7, 7), (7, 8)],
                }
            ]
        )
        # 篡改 games.move_no，制造与落子数不一致的记录。
        self._break_move_count("game-broken")

        listing = self.client.get(
            "/api/v1/admin/reports",
            headers=self.auth_headers(admin["access_token"]),
        )
        self.assertEqual(listing.status_code, 200, listing.text)
        entries = listing.json()["reports"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["audit_findings"], 1)

    def _break_move_count(self, game_id: str) -> None:
        async def patch() -> None:
            engine = create_engine(self.settings)
            factory = create_session_factory(engine)
            try:
                async with factory() as session:
                    game = await session.get(Game, game_id)
                    assert game is not None
                    game.move_no = 9
                    await session.commit()
            finally:
                await engine.dispose()

        asyncio.run(patch())


if __name__ == "__main__":
    unittest.main(verbosity=2)
