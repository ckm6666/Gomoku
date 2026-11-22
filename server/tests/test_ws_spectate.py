"""观战（spectate）服务端行为的端到端测试。

覆盖：旁观者只读观战、实时快照推送、观战者的操作一律被拒、
观战者断线/退出不影响对局，以及“进行中对局”列表接口。
"""

from __future__ import annotations

import unittest

from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CHAT,
    CLIENT_CREATE_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_LEAVE_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_REMATCH,
    CLIENT_RESIGN,
    CLIENT_UNDO_REQUEST,
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_CHAT,
    SERVER_ERROR,
    SERVER_GAME_STATE,
    SERVER_LIVE_GAMES_CHANGED,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_CREATED,
    make_message,
    parse_game_state,
)
from server.tests.helpers import ServerTestBase


class _SpectateTestBase(ServerTestBase):
    """三人（两名玩家 + 一名观战者）的联机测试夹具。"""

    def _register_access(self, username: str) -> tuple[str, str]:
        response = self.register(username)
        self.assertEqual(response.status_code, 201, response.text)
        data = response.json()
        return data["access_token"], data["user"]["id"]

    def _authenticate(self, ws, token: str) -> None:
        ws.send_json(make_message(CLIENT_AUTH, {"access_token": token}))
        message = ws.receive_json()
        self.assertEqual(message["type"], SERVER_AUTHENTICATED)

    def _open_game(self, host, guest) -> str:
        host.send_json(make_message(CLIENT_CREATE_ROOM, {}))
        created = host.receive_json()
        self.assertEqual(created["type"], SERVER_ROOM_CREATED)
        code = created["payload"]["room_code"]
        guest.send_json(make_message(CLIENT_JOIN_ROOM, {"room_code": code}))
        host_state = host.receive_json()
        guest_state = guest.receive_json()
        self.assertEqual(host_state["type"], SERVER_GAME_STATE)
        self.assertEqual(guest_state["type"], SERVER_GAME_STATE)
        return host_state["payload"]["game_id"]

    def _receive_type(self, ws, expected: str, attempts: int = 20) -> dict:
        """读取直到收到期望类型；跳过期间插入的其他消息。"""
        for _ in range(attempts):
            message = ws.receive_json()
            if message["type"] == expected:
                return message
        self.fail(f"未收到 {expected}")

    def _collect_until(
        self, ws, expected: str, attempts: int = 20
    ) -> tuple[dict, list[dict]]:
        """读取到期望类型为止，并返回期间收到的所有消息。

        返回中间消息是为了断言“本该被拒绝的操作没有产生副作用”，
        例如观战者聊天时玩家不应该收到 chat_message。
        """
        seen: list[dict] = []
        for _ in range(attempts):
            message = ws.receive_json()
            seen.append(message)
            if message["type"] == expected:
                return message, seen
        self.fail(f"未收到 {expected}")

    def _watch(self, viewer, game_id: str) -> dict:
        viewer.send_json(
            make_message(CLIENT_WATCH_GAME, {"game_id": game_id})
        )
        return self._receive_type(viewer, SERVER_GAME_STATE)

    def _move(self, sender, row: int, col: int, request_id: str) -> None:
        sender.send_json(
            make_message(
                CLIENT_MOVE_INTENT,
                {"row": row, "col": col},
                request_id=request_id,
            )
        )


class SpectateWatchTests(_SpectateTestBase):
    def test_spectator_receives_snapshot_and_live_updates(self) -> None:
        """观战者拿到只读快照，并在玩家落子后收到实时更新。"""
        host_token, _ = self._register_access("spec_host")
        guest_token, _ = self._register_access("spec_guest")
        viewer_token, _ = self._register_access("spec_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)

                    snapshot = self._watch(viewer, game_id)
                    state = parse_game_state(snapshot["payload"])
                    self.assertTrue(state.is_spectator)
                    self.assertIsNone(state.your_color)
                    self.assertEqual(state.game_id, game_id)
                    self.assertEqual(state.move_no, 0)
                    self.assertFalse(state.is_game_over)
                    # 观战者没有执子颜色，也没有自己的积分信息。
                    self.assertIsNone(state.your_rating_before)
                    self.assertIsNone(state.rating_delta)
                    self.assertIsNone(state.opponent_rating_before)

                    # 黑棋落子：三名参与者收到的是同一份权威快照。
                    self._move(host, 7, 7, "spec-m1")
                    host_message = self._receive_type(host, SERVER_GAME_STATE)
                    guest_message = self._receive_type(
                        guest, SERVER_GAME_STATE
                    )
                    viewer_message = self._receive_type(
                        viewer, SERVER_GAME_STATE
                    )
                    host_state = parse_game_state(host_message["payload"])
                    guest_state = parse_game_state(guest_message["payload"])
                    viewer_state = parse_game_state(viewer_message["payload"])
                    self.assertEqual(host_state.board, viewer_state.board)
                    self.assertEqual(guest_state.board, viewer_state.board)
                    self.assertEqual(viewer_state.board[7][7].value, "black")
                    self.assertEqual(viewer_state.move_no, 1)
                    self.assertTrue(viewer_state.is_spectator)

    def test_spectator_cannot_move(self) -> None:
        """观战者的落子意图被拒绝，棋盘不受影响。"""
        host_token, _ = self._register_access("nomove_host")
        guest_token, _ = self._register_access("nomove_guest")
        viewer_token, _ = self._register_access("nomove_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)
                    self._watch(viewer, game_id)

                    viewer.send_json(
                        make_message(
                            CLIENT_MOVE_INTENT,
                            {"row": 3, "col": 3},
                            request_id="spec-move",
                        )
                    )
                    error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(error["payload"]["reason"], "你不在房间中")

                    # 玩家照常落子，棋盘上只有玩家那一手。
                    self._move(host, 7, 7, "nomove-m1")
                    host_state = parse_game_state(
                        self._receive_type(host, SERVER_GAME_STATE)["payload"]
                    )
                    guest_state = parse_game_state(
                        self._receive_type(guest, SERVER_GAME_STATE)["payload"]
                    )
                    self.assertEqual(host_state.move_no, 1)
                    self.assertIsNone(host_state.board[3][3])
                    self.assertEqual(host_state.board[7][7].value, "black")
                    self.assertEqual(guest_state.board, host_state.board)

    def test_spectator_cannot_resign_undo_rematch_or_chat(self) -> None:
        """观战者不能认输、悔棋、再战或聊天，对局一切照旧。"""
        host_token, _ = self._register_access("act_host")
        guest_token, _ = self._register_access("act_guest")
        viewer_token, _ = self._register_access("act_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)
                    self._watch(viewer, game_id)

                    # 认输：观战者没有执子资格，服务器明确拒绝。
                    viewer.send_json(make_message(CLIENT_RESIGN, {}))
                    resign_error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        resign_error["payload"]["reason"], "观战者不能认输"
                    )
                    # 悔棋：观战者不是本局玩家。
                    viewer.send_json(
                        make_message(
                            CLIENT_UNDO_REQUEST, {}, request_id="spec-undo"
                        )
                    )
                    undo_error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        undo_error["payload"]["reason"], "你不在房间中"
                    )
                    # 聊天：观战者不能发言，更不能把消息带给玩家。
                    viewer.send_json(
                        make_message(
                            CLIENT_CHAT,
                            {"kind": "text", "content": "观战者发言"},
                            request_id="spec-chat",
                        )
                    )
                    chat_error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        chat_error["payload"]["reason"], "你不在房间中"
                    )
                    # 再战：观战者不是本局玩家，不能发起。
                    viewer.send_json(make_message(CLIENT_REMATCH, {}))
                    rematch_error = self._receive_type(viewer, SERVER_ERROR)
                    self.assertEqual(
                        rematch_error["payload"]["reason"], "观战者不能发起再战"
                    )

                    # 对局照常进行：玩家收到的第一条消息仍然是局面快照，
                    # 中间没有被插入聊天广播。
                    self._move(host, 7, 7, "act-m1")
                    host_message, host_seen = self._collect_until(
                        host, SERVER_GAME_STATE
                    )
                    guest_message, guest_seen = self._collect_until(
                        guest, SERVER_GAME_STATE
                    )
                    self.assertFalse(
                        any(m["type"] == SERVER_CHAT for m in host_seen)
                    )
                    self.assertFalse(
                        any(m["type"] == SERVER_CHAT for m in guest_seen)
                    )
                    host_state = parse_game_state(host_message["payload"])
                    self.assertFalse(host_state.is_game_over)
                    self.assertEqual(host_state.move_no, 1)
                    self.assertEqual(
                        parse_game_state(guest_message["payload"]).board,
                        host_state.board,
                    )

    def test_player_cannot_watch_own_game(self) -> None:
        """本局玩家不能观战自己的对局。"""
        host_token, _ = self._register_access("own_host")
        guest_token, _ = self._register_access("own_guest")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                game_id = self._open_game(host, guest)

                host.send_json(
                    make_message(CLIENT_WATCH_GAME, {"game_id": game_id})
                )
                error = self._receive_type(host, SERVER_ERROR)
                self.assertEqual(
                    error["payload"]["reason"], "你不能观摩自己的对局"
                )

    def test_watch_unknown_or_empty_game_is_rejected(self) -> None:
        """观战不存在的对局、或者对局编号为空，都必须报错。"""
        viewer_token, _ = self._register_access("bogus_viewer")

        with self.client.websocket_connect("/api/v1/ws") as viewer:
            self._authenticate(viewer, viewer_token)

            viewer.send_json(
                make_message(
                    CLIENT_WATCH_GAME, {"game_id": "no-such-game"}
                )
            )
            error = self._receive_type(viewer, SERVER_ERROR)
            self.assertEqual(
                error["payload"]["reason"], "对局不存在或已结束"
            )

            viewer.send_json(
                make_message(CLIENT_WATCH_GAME, {"game_id": "   "})
            )
            empty_error = self._receive_type(viewer, SERVER_ERROR)
            self.assertEqual(
                empty_error["payload"]["reason"], "对局编号无效"
            )

    def test_spectator_disconnect_does_not_break_game(self) -> None:
        """观战者断线不影响对局：不判逃跑、不作废、不关房间。"""
        host_token, _ = self._register_access("drop_host")
        guest_token, _ = self._register_access("drop_guest")
        viewer_token, _ = self._register_access("drop_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)
                    self._watch(viewer, game_id)

                    # 双方各走一手，保证断开之后轮到黑棋落子。
                    self._move(host, 7, 7, "drop-m1")
                    self._receive_type(host, SERVER_GAME_STATE)
                    self._receive_type(guest, SERVER_GAME_STATE)
                    self._receive_type(viewer, SERVER_GAME_STATE)
                    self._move(guest, 7, 8, "drop-m2")
                    self._receive_type(guest, SERVER_GAME_STATE)
                    self._receive_type(host, SERVER_GAME_STATE)
                    self._receive_type(viewer, SERVER_GAME_STATE)

                # 观战者断线（退出 websocket 上下文）。
                # 玩家继续落子：房间没有被关闭，对局也没有被判负。
                self._move(host, 8, 8, "drop-m3")
                host_message, host_seen = self._collect_until(
                    host, SERVER_GAME_STATE
                )
                guest_message, guest_seen = self._collect_until(
                    guest, SERVER_GAME_STATE
                )
                self.assertFalse(
                    any(m["type"] == SERVER_ROOM_CLOSED for m in host_seen)
                )
                self.assertFalse(
                    any(m["type"] == SERVER_ROOM_CLOSED for m in guest_seen)
                )
                host_state = parse_game_state(host_message["payload"])
                self.assertFalse(host_state.is_game_over)
                self.assertEqual(host_state.move_no, 3)
                self.assertEqual(host_state.result_reason, "")
                self.assertEqual(
                    parse_game_state(guest_message["payload"]).board,
                    host_state.board,
                )

    def test_spectator_reconnect_restores_snapshot(self) -> None:
        """观战者断线重连：服务器补发一份观战快照，棋盘不会空掉。"""
        host_token, _ = self._register_access("reconn_host")
        guest_token, _ = self._register_access("reconn_guest")
        viewer_token, _ = self._register_access("reconn_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)
                    self._watch(viewer, game_id)
                    self._move(host, 7, 7, "reconn-m1")
                    self._receive_type(host, SERVER_GAME_STATE)
                    self._receive_type(guest, SERVER_GAME_STATE)
                    self._receive_type(viewer, SERVER_GAME_STATE)

                # 观战者断线后重新连接：服务器要补发观战快照，
                # 否则重连回来的客户端只剩一块空棋盘。
                with self.client.websocket_connect("/api/v1/ws") as viewer2:
                    self._authenticate(viewer2, viewer_token)
                    state = self._receive_type(viewer2, SERVER_GAME_STATE)
                    snapshot = parse_game_state(state["payload"])
                    self.assertTrue(snapshot.is_spectator)
                    self.assertIsNone(snapshot.your_color)
                    self.assertEqual(snapshot.move_no, 1)
                    self.assertIsNotNone(snapshot.board[7][7])
                    self.assertEqual(snapshot.board[7][7].value, "black")

    def test_leave_room_stops_spectating(self) -> None:
        """观战者退出观战：只影响自己，不关闭玩家的房间。"""
        host_token, _ = self._register_access("leave_host")
        guest_token, _ = self._register_access("leave_guest")
        viewer_token, viewer_id = self._register_access("leave_viewer")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as viewer:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(viewer, viewer_token)
                    game_id = self._open_game(host, guest)
                    self._watch(viewer, game_id)

                    viewer.send_json(make_message(CLIENT_LEAVE_ROOM, {}))
                    closed = self._receive_type(viewer, SERVER_ROOM_CLOSED)
                    self.assertEqual(closed["payload"]["reason"], "已退出观战")

                    # 房间仍然属于玩家，玩家照常落子。
                    self._move(host, 7, 7, "leave-m1")
                    host_message, host_seen = self._collect_until(
                        host, SERVER_GAME_STATE
                    )
                    _, guest_seen = self._collect_until(
                        guest, SERVER_GAME_STATE
                    )
                    self.assertFalse(
                        any(m["type"] == SERVER_ROOM_CLOSED for m in host_seen)
                    )
                    self.assertFalse(
                        any(
                            m["type"] == SERVER_ROOM_CLOSED
                            for m in guest_seen
                        )
                    )
                    host_state = parse_game_state(host_message["payload"])
                    self.assertFalse(host_state.is_game_over)
                    self.assertEqual(host_state.move_no, 1)

        # 服务器端也已经把观战者从观战名单里摘掉。
        manager = getattr(self.app.state, "room_manager", None)
        self.assertIsNotNone(manager)
        self.assertNotIn(viewer_id, manager._spectator_by_user)


class LiveGamesChangedTests(_SpectateTestBase):
    """对局开始/结束时，服务器要通知所有在线客户端刷新对局列表。"""

    def test_clients_notified_when_game_starts_and_ends(self) -> None:
        """开局时通知一次，终局时再通知一次。"""
        host_token, _ = self._register_access("push_host")
        guest_token, _ = self._register_access("push_guest")
        watcher_token, _ = self._register_access("push_watcher")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                with self.client.websocket_connect("/api/v1/ws") as watcher:
                    self._authenticate(host, host_token)
                    self._authenticate(guest, guest_token)
                    self._authenticate(watcher, watcher_token)

                    # 双方配对开局：旁观者要收到“对局列表变了”。
                    self._open_game(host, guest)
                    self._receive_type(watcher, SERVER_LIVE_GAMES_CHANGED)

                    # 对局结束：列表里少了一条，同样要通知。
                    host.send_json(make_message(CLIENT_RESIGN, {}))
                    self._receive_type(watcher, SERVER_LIVE_GAMES_CHANGED)


class LiveGamesApiTests(_SpectateTestBase):
    """``GET /api/v1/games/live``：列出正在进行中的对局。"""

    def test_live_games_is_empty_without_games(self) -> None:
        token, _ = self._register_access("live_empty")
        response = self.client.get(
            "/api/v1/games/live", headers=self.auth_headers(token)
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["entries"], [])
        self.assertEqual(body["total"], 0)

    def test_live_games_lists_running_game(self) -> None:
        host_token, _ = self._register_access("live_host")
        guest_token, _ = self._register_access("live_guest")
        api_token, _ = self._register_access("live_api")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                game_id = self._open_game(host, guest)

                response = self.client.get(
                    "/api/v1/games/live",
                    headers=self.auth_headers(api_token),
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["total"], 1)
                entry = body["entries"][0]
                self.assertEqual(entry["game_id"], game_id)
                self.assertEqual(entry["black_username"], "live_host")
                self.assertEqual(entry["white_username"], "live_guest")
                self.assertTrue(entry["black_user_id"])
                self.assertTrue(entry["white_user_id"])
                self.assertFalse(entry["rated"])
                self.assertEqual(entry["move_no"], 0)
                self.assertEqual(entry["status"], "playing")

    def test_live_games_excludes_finished_game(self) -> None:
        host_token, _ = self._register_access("live_done_host")
        guest_token, _ = self._register_access("live_done_guest")
        api_token, _ = self._register_access("live_done_api")

        with self.client.websocket_connect("/api/v1/ws") as host:
            with self.client.websocket_connect("/api/v1/ws") as guest:
                self._authenticate(host, host_token)
                self._authenticate(guest, guest_token)
                game_id = self._open_game(host, guest)

                # 白棋认输，对局结束。
                guest.send_json(make_message(CLIENT_RESIGN, {}))
                host_state = parse_game_state(
                    self._receive_type(host, SERVER_GAME_STATE)["payload"]
                )
                self._receive_type(guest, SERVER_GAME_STATE)
                self.assertTrue(host_state.is_game_over)

                response = self.client.get(
                    "/api/v1/games/live",
                    headers=self.auth_headers(api_token),
                )
                self.assertEqual(response.status_code, 200, response.text)
                body = response.json()
                self.assertEqual(body["entries"], [])
                self.assertEqual(body["total"], 0)
                self.assertNotIn(
                    game_id, [entry["game_id"] for entry in body["entries"]]
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
