"""Matchmaker 的单元测试：队列清理、选对手与失败回填。"""

from __future__ import annotations

import asyncio
import unittest

from gomoku.shared.time_control import TimeControl
from server.app.ws.entities import QueuedPlayer, Room
from server.app.ws.matchmaker import Matchmaker


class _Registry:
    def __init__(self) -> None:
        self.connections: dict[str, object] = {}

    def current_connection(self, user_id: str):
        return self.connections.get(user_id)


class _Conn:
    def __init__(self, user_id: str, connected: bool = True) -> None:
        self.user_id = user_id
        self.connected = connected


def _player(
    user_id: str,
    rating: int = 1200,
    *,
    connected: bool = True,
    queued_at: float = 0.0,
) -> QueuedPlayer:
    return QueuedPlayer(
        conn=_Conn(user_id, connected),  # type: ignore[arg-type]
        rating=rating,
        queued_at=queued_at,
        time_control=TimeControl.per_move(30),
    )


def _matchmaker() -> tuple[Matchmaker, _Registry, dict[str, Room]]:
    registry = _Registry()
    room_by_user: dict[str, Room] = {}
    matchmaker = Matchmaker(
        asyncio.Lock(), registry, room_by_user  # type: ignore[arg-type]
    )
    return matchmaker, registry, room_by_user


class QueueStateTests(unittest.TestCase):
    def test_state_payload_idle_and_queued(self) -> None:
        matchmaker, _, _ = _matchmaker()
        self.assertEqual(
            matchmaker.state_payload("u1")["state"], "idle"
        )
        matchmaker.put(_player("u1", 1500))
        payload = matchmaker.state_payload("u1")
        self.assertEqual(payload["state"], "queued")
        self.assertEqual(payload["queue_size"], 1)
        self.assertEqual(payload["your_rating"], 1500)
        self.assertEqual(len(matchmaker), 1)

    def test_rebind_and_refresh(self) -> None:
        matchmaker, _, _ = _matchmaker()
        matchmaker.put(_player("u1", 1500, queued_at=5.0))
        new_conn = _Conn("u1")
        matchmaker.rebind_connection(new_conn)  # type: ignore[arg-type]
        queued = matchmaker.get("u1")
        assert queued is not None
        self.assertIs(queued.conn, new_conn)
        self.assertEqual(queued.queued_at, 5.0)

        control = TimeControl.fischer(300, 3)
        matchmaker.refresh(new_conn, control)  # type: ignore[arg-type]
        self.assertEqual(queued.time_control, control)

    def test_remove_and_clear(self) -> None:
        matchmaker, _, _ = _matchmaker()
        matchmaker.put(_player("u1"))
        matchmaker.put(_player("u2"))
        matchmaker.remove("u1")
        self.assertIsNone(matchmaker.get("u1"))
        matchmaker.clear()
        self.assertEqual(len(matchmaker), 0)


class PruneTests(unittest.TestCase):
    def test_prune_drops_disconnected_and_replaced_connections(self) -> None:
        matchmaker, registry, _ = _matchmaker()
        live = _player("live")
        dead = _player("dead", connected=False)
        replaced = _player("replaced")
        matchmaker.put(live)
        matchmaker.put(dead)
        matchmaker.put(replaced)
        registry.connections = {
            "live": live.conn,
            "replaced": _Conn("replaced"),
        }
        matchmaker.prune_locked()
        self.assertEqual(set(matchmaker.queue), {"live"})


class PopMatchTests(unittest.TestCase):
    def test_pop_chooses_nearest_rating_and_removes_both(self) -> None:
        matchmaker, _, _ = _matchmaker()
        matchmaker.put(_player("u1", 1200))
        matchmaker.put(_player("u2", 1800))
        matchmaker.put(_player("u3", 1250))
        pair = matchmaker.pop_match_locked(
            matchmaker.get("u1").conn  # type: ignore[union-attr]
        )
        assert pair is not None
        self.assertEqual(
            {pair[0].conn.user_id, pair[1].conn.user_id},
            {"u1", "u3"},
        )
        self.assertEqual(set(matchmaker.queue), {"u2"})

    def test_pop_without_candidates_keeps_self_queued(self) -> None:
        matchmaker, _, _ = _matchmaker()
        matchmaker.put(_player("u1"))
        self.assertIsNone(
            matchmaker.pop_match_locked(
                matchmaker.get("u1").conn  # type: ignore[union-attr]
            )
        )
        self.assertIn("u1", matchmaker.queue)

    def test_pop_unknown_player_returns_none(self) -> None:
        matchmaker, _, _ = _matchmaker()
        self.assertIsNone(
            matchmaker.pop_match_locked(_Conn("ghost"))  # type: ignore[arg-type]
        )


class RequeueTests(unittest.TestCase):
    def test_requeue_restores_opponent_with_new_timestamp(self) -> None:
        async def scenario() -> None:
            matchmaker, registry, room_by_user = _matchmaker()
            first = _player("u1")
            second = _player("u2")
            registry.connections = {
                "u1": first.conn,
                "u2": second.conn,
            }
            await matchmaker.requeue_opponent_after_failure(
                first.conn,  # type: ignore[arg-type]
                (first, second),
            )
            queued = matchmaker.get("u2")
            assert queued is not None
            self.assertGreater(queued.queued_at, 0.0)
            self.assertNotIn("u1", matchmaker.queue)

        asyncio.run(scenario())

    def test_requeue_skips_opponent_in_room_or_replaced(self) -> None:
        async def scenario() -> None:
            matchmaker, registry, room_by_user = _matchmaker()
            first = _player("u1")
            second = _player("u2")
            registry.connections = {
                "u1": first.conn,
                "u2": second.conn,
            }
            room_by_user["u2"] = object()  # type: ignore[assignment]
            await matchmaker.requeue_opponent_after_failure(
                first.conn,  # type: ignore[arg-type]
                (first, second),
            )
            self.assertIsNone(matchmaker.get("u2"))

            room_by_user.clear()
            registry.connections["u2"] = _Conn("u2")
            await matchmaker.requeue_opponent_after_failure(
                first.conn,  # type: ignore[arg-type]
                (first, second),
            )
            self.assertIsNone(matchmaker.get("u2"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
