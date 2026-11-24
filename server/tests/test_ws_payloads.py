"""纯载荷构造器的单元测试（不启动 FastAPI / 数据库）。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from gomoku.shared.avatar import default_avatar_for
from gomoku.shared.protocol import (
    SERVER_CHAT,
    SERVER_GAME_STATE,
    SERVER_OPPONENT_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CREATED,
    SERVER_UNDO_STATUS,
)
from gomoku.shared.rules import Player
from gomoku.shared.time_control import TimeControl
from server.app.ws import payloads
from server.app.ws.entities import (
    ClientConnection,
    QueuedPlayer,
    Room,
    RoomInvite,
)


def _settings(**overrides):
    values = {
        "undo_limit_per_game": 3,
        "undo_cooldown_seconds": 0.0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _conn(
    user_id: str,
    username: str = "user",
    *,
    rating: int = 1200,
    avatar: str = "mei",
    avatar_version: int = 0,
    achievements: tuple[str, ...] = (),
) -> ClientConnection:
    return ClientConnection(
        connection_id=f"cid-{user_id}",
        user_id=user_id,
        username=username,
        websocket=SimpleNamespace(),  # type: ignore[arg-type]
        rating=rating,
        avatar=avatar,
        avatar_version=avatar_version,
        achievements=achievements,
    )


def _room(*, rated: bool = False, matchmade: bool = False) -> Room:
    return Room(
        code="ROOM1",
        game_id="game-1",
        black_user_id="black-id",
        white_user_id="white-id",
        black_name="Black",
        white_name="White",
        time_control=TimeControl.per_move(30),
        rated=rated,
        matchmade=matchmade,
    )


class PayloadBasicsTests(unittest.TestCase):
    def test_color_text(self) -> None:
        self.assertEqual(payloads.color_text(Player.BLACK), "black")
        self.assertEqual(payloads.color_text(Player.WHITE), "white")

    def test_status_for_game(self) -> None:
        self.assertEqual(
            payloads.status_for_game(
                SimpleNamespace(winner=None, is_draw=False)
            ),
            "playing",
        )
        self.assertEqual(
            payloads.status_for_game(
                SimpleNamespace(winner=Player.BLACK, is_draw=False)
            ),
            "black_won",
        )
        self.assertEqual(
            payloads.status_for_game(
                SimpleNamespace(winner=Player.WHITE, is_draw=False)
            ),
            "white_won",
        )
        self.assertEqual(
            payloads.status_for_game(
                SimpleNamespace(winner=None, is_draw=True)
            ),
            "draw",
        )

    def test_connection_user_payload(self) -> None:
        conn = _conn(
            "u1",
            "Alice",
            rating=1200,
            avatar="mei",
            avatar_version=-2,
            achievements=("first_win",),
        )
        payload = payloads.connection_user_payload(conn)
        self.assertEqual(payload["id"], "u1")
        self.assertEqual(payload["username"], "Alice")
        self.assertEqual(payload["avatar"], "mei")
        self.assertEqual(payload["avatar_version"], 0)
        self.assertEqual(payload["achievements"], ["first_win"])
        self.assertEqual(payload["rating"], 1200)
        self.assertEqual(payload["rank_name"], "黄金")

    def test_connection_user_payload_normalizes_bad_avatar(self) -> None:
        conn = _conn("u1", avatar="not-a-real-avatar")
        payload = payloads.connection_user_payload(conn)
        self.assertEqual(
            payload["avatar"], default_avatar_for("u1")
        )


class QueuePayloadTests(unittest.TestCase):
    def test_idle_queue_payload(self) -> None:
        payload = payloads.queue_state_payload("u1", {})
        self.assertEqual(payload["state"], "idle")
        self.assertEqual(payload["queue_size"], 0)
        self.assertIsNone(payload["your_rating"])
        self.assertEqual(payload["rank_name"], "")

    def test_queued_payload(self) -> None:
        conn = _conn("u1")
        queue = {
            "u1": QueuedPlayer(
                conn=conn,
                rating=1500,
                queued_at=1.0,
                time_control=TimeControl.per_move(30),
            )
        }
        payload = payloads.queue_state_payload("u1", queue)
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(payload["queue_size"], 1)
        self.assertEqual(payload["your_rating"], 1500)
        self.assertEqual(payload["rank_name"], "钻石")


class MatchFoundPayloadTests(unittest.TestCase):
    def test_match_found_payload(self) -> None:
        room = _room(rated=True)
        room.rating_before = {"black-id": 1200, "white-id": 1300}
        black = payloads.match_found_payload(room, "black-id", "white-id")
        self.assertEqual(black["game_id"], "game-1")
        self.assertEqual(black["your_color"], "black")
        self.assertEqual(black["opponent_name"], "White")
        self.assertEqual(black["your_rating"], 1200)
        self.assertEqual(black["opponent_rating"], 1300)
        self.assertTrue(black["rated"])
        self.assertIsNotNone(black["time_control"]["mode"])

        white = payloads.match_found_payload(room, "white-id", "black-id")
        self.assertEqual(white["your_color"], "white")
        self.assertEqual(white["opponent_name"], "Black")

    def test_match_found_payload_without_opponent(self) -> None:
        payload = payloads.match_found_payload(_room(), "black-id", None)
        self.assertEqual(payload["opponent_name"], "对手")
        self.assertIsNone(payload["opponent_rating"])


class RoomMessageTests(unittest.TestCase):
    def test_room_created_message(self) -> None:
        message = payloads.room_created_message(_room())
        self.assertEqual(message["type"], SERVER_ROOM_CREATED)
        self.assertEqual(message["payload"]["room_code"], "ROOM1")
        self.assertEqual(message["payload"]["your_color"], "black")
        self.assertEqual(message["payload"]["turn_seconds"], 30)
        self.assertEqual(
            message["payload"]["time_control"],
            TimeControl.per_move(30).to_payload(),
        )

    def test_opponent_status_message(self) -> None:
        message = payloads.opponent_status_message("Rival", False)
        self.assertEqual(message["type"], SERVER_OPPONENT_STATUS)
        self.assertEqual(
            message["payload"], {"username": "Rival", "online": False}
        )

    def test_rematch_status_message(self) -> None:
        room = _room()
        room.rematch_votes = {"black-id"}
        mine = payloads.rematch_status_message(room, "black-id")
        theirs = payloads.rematch_status_message(room, "white-id")
        self.assertEqual(mine["type"], SERVER_REMATCH_STATUS)
        self.assertTrue(mine["payload"]["your_requested"])
        self.assertFalse(mine["payload"]["opponent_requested"])
        self.assertFalse(theirs["payload"]["your_requested"])
        self.assertTrue(theirs["payload"]["opponent_requested"])

    def test_undo_status_message(self) -> None:
        room = _room()
        message = payloads.undo_status_message(
            room, "declined", "white-id", "对手拒绝了悔棋请求"
        )
        self.assertEqual(message["type"], SERVER_UNDO_STATUS)
        self.assertEqual(message["payload"]["state"], "declined")
        self.assertEqual(message["payload"]["requester_id"], "white-id")
        self.assertEqual(message["payload"]["requester_name"], "White")
        self.assertEqual(
            message["payload"]["reason"], "对手拒绝了悔棋请求"
        )
        empty = payloads.undo_status_message(room, "cancelled", None)
        self.assertEqual(empty["payload"]["requester_id"], "")
        self.assertEqual(empty["payload"]["requester_name"], "")

    def test_chat_message(self) -> None:
        message = payloads.chat_message(_conn("u1", "Alice"), "emote", "Hi", 42)
        self.assertEqual(message["type"], SERVER_CHAT)
        self.assertEqual(
            message["payload"],
            {
                "kind": "emote",
                "content": "Hi",
                "sender_id": "u1",
                "sender_name": "Alice",
                "sent_at_ms": 42,
            },
        )


class InvitePayloadTests(unittest.TestCase):
    def _invite(self) -> RoomInvite:
        return RoomInvite(
            invite_id="invite-1",
            room=_room(),
            inviter_id="black-id",
            invitee_id="white-id",
            created_at=0.0,
        )

    def test_invite_payload_online_inviter(self) -> None:
        payload = payloads.invite_payload(
            self._invite(), _conn("black-id", "Black")
        )
        self.assertEqual(payload["invite_id"], "invite-1")
        self.assertFalse(payload["rated"])
        self.assertEqual(payload["inviter"]["username"], "Black")

    def test_invite_payload_offline_inviter(self) -> None:
        payload = payloads.invite_payload(self._invite(), None)
        self.assertEqual(
            payload["inviter"],
            {
                "id": "black-id",
                "username": "好友",
                "avatar": "yun",
                "rating": 0,
                "rank_name": "",
            },
        )

    def test_invite_result_payload(self) -> None:
        payload = payloads.invite_result_payload(
            self._invite(), "accepted", True, {"id": "white-id"}
        )
        self.assertEqual(payload["status"], "accepted")
        self.assertTrue(payload["accepted"])
        self.assertEqual(payload["user"], {"id": "white-id"})
        self.assertEqual(payload["reason"], "")


class RoomStatePayloadTests(unittest.TestCase):
    def _started_room(self) -> Room:
        room = _room()
        room.status = "playing"
        room.connections = {"black-id": _conn("black-id")}
        room.game.place_stone(7, 7)
        room.turn_deadline_ms = 31_000
        room.undo_requester_id = "black-id"
        room.undo_counts = {"black-id": 1}
        room.undo_last_request = {"black-id": 10.0}
        return room

    def test_player_view(self) -> None:
        room = self._started_room()
        payload = payloads.room_state_payload(
            room,
            "black-id",
            settings=_settings(undo_cooldown_seconds=2.0),
            remaining_ms=lambda room, uid: 1234,
            now_ms=lambda: 99,
        )
        self.assertEqual(payload["board"][7][7], "black")
        self.assertEqual(payload["current_player"], "white")
        self.assertEqual(payload["your_color"], "black")
        self.assertFalse(payload["is_spectator"])
        self.assertEqual(payload["move_no"], 1)
        self.assertEqual(payload["status"], "playing")
        self.assertEqual(payload["undo_remaining"], 2)
        self.assertEqual(payload["undo_limit"], 3)
        self.assertEqual(payload["undo_cooldown_until_ms"], 12_000)
        self.assertEqual(payload["undo_cooldown_seconds"], 2)
        self.assertEqual(
            payload["last_move"],
            {"row": 7, "col": 7, "player": "black"},
        )
        self.assertEqual(payload["room_code"], "ROOM1")
        self.assertEqual(payload["server_now_ms"], 99)
        self.assertEqual(payload["black_remaining_ms"], 1234)
        self.assertIsNotNone(payload["white_user_id"])
        self.assertTrue(payload["opponent_online"] is False)
        self.assertFalse(payload["rated"])
        self.assertIsNone(payload["your_rating_before"])

    def test_matchmade_room_hides_room_code(self) -> None:
        room = _room(matchmade=True)
        room.connections = {"black-id": _conn("black-id")}
        payload = payloads.room_state_payload(
            room,
            "black-id",
            settings=_settings(),
            remaining_ms=lambda room, uid: None,
            now_ms=lambda: 0,
        )
        self.assertEqual(payload["room_code"], "")
        self.assertIsNone(payload["white_remaining_ms"])

    def test_spectator_view(self) -> None:
        room = self._started_room()
        room.connections["white-id"] = _conn("white-id")
        room.spectators = {"viewer": _conn("viewer")}
        payload = payloads.room_state_payload(
            room,
            "viewer",
            settings=_settings(),
            remaining_ms=lambda room, uid: None,
            now_ms=lambda: 0,
        )
        self.assertTrue(payload["is_spectator"])
        self.assertIsNone(payload["your_color"])
        self.assertIsNone(payload["your_rating_before"])
        self.assertTrue(payload["opponent_online"])
        self.assertEqual(payload["undo_limit"], 0)

    def test_outsider_raises(self) -> None:
        with self.assertRaises(RuntimeError):
            payloads.room_state_payload(
                self._started_room(),
                "stranger",
                settings=_settings(),
                remaining_ms=lambda room, uid: None,
                now_ms=lambda: 0,
            )

    def test_game_state_message(self) -> None:
        room = self._started_room()
        conn = _conn("black-id")
        message = payloads.game_state_message(
            room,
            conn,
            settings=_settings(),
            remaining_ms=lambda room, uid: None,
            now_ms=lambda: 0,
            request_id="req-1",
        )
        assert message is not None
        self.assertEqual(message["type"], SERVER_GAME_STATE)
        self.assertEqual(message["request_id"], "req-1")
        self.assertIsNone(
            payloads.game_state_message(
                room,
                _conn("stranger"),
                settings=_settings(),
                remaining_ms=lambda room, uid: None,
                now_ms=lambda: 0,
            )
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
