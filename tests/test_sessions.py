"""对局会话层测试：本地、AI 与可注入 FakeTransport 的在线确认状态机。"""

from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.ai_engine import AIDifficulty  # noqa: E402
from gomoku.client.sessions import (  # noqa: E402
    AIGameSession,
    LocalGameSession,
    OnlineGameSession,
)
from gomoku.shared.protocol import (  # noqa: E402
    CLIENT_CANCEL_MATCH,
    CLIENT_QUICK_MATCH,
    SERVER_AUTHENTICATED,
    SERVER_GAME_STATE,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_OPPONENT_STATUS,
    SERVER_QUEUE_STATUS,
    SERVER_REMATCH_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_CREATED,
    make_message,
)
from gomoku.shared.rules import BOARD_SIZE, Player  # noqa: E402
from tests.fakes import FakeTransport  # noqa: E402

# 保持 QApplication 单例；若 UI 测试先创建则复用同一个实例。
QT_APP = QApplication.instance() or QApplication([])


def wait_until(predicate, timeout: float = 5.0) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QT_APP.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    QT_APP.processEvents()
    return bool(predicate())


def blank_board() -> list[list[str | None]]:
    return [[None for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]


def state_message(
    board: list[list[str | None]],
    *,
    current: str = "black",
    your_color: str = "black",
    move_no: int = 0,
    status: str = "playing",
    last_move: dict | None = None,
    winning_line: list[list[int]] | None = None,
    request_id: str | None = None,
    game_id: str = "game-1",
    rated: bool = False,
    room_code: str = "",
    your_rating_before: int | None = None,
    your_rating_after: int | None = None,
    opponent_rating_before: int | None = None,
    opponent_rating_after: int | None = None,
    rating_delta: int | None = None,
) -> dict:
    """构造一个符合协议的服务器局面消息。"""
    return make_message(
        SERVER_GAME_STATE,
        {
            "game_id": game_id,
            "board": board,
            "current_player": current,
            "your_color": your_color,
            "move_no": move_no,
            "status": status,
            "last_move": last_move,
            "winning_line": winning_line or [],
            "rated": rated,
            "room_code": room_code,
            "your_rating_before": your_rating_before,
            "your_rating_after": your_rating_after,
            "opponent_rating_before": opponent_rating_before,
            "opponent_rating_after": opponent_rating_after,
            "rating_delta": rating_delta,
        },
        request_id=request_id,
    )


class LocalSessionTests(unittest.TestCase):
    """本地会话保持原有单方落子/悔棋语义。"""

    def test_local_move_and_undo(self) -> None:
        changes: list[int] = []
        session = LocalGameSession()
        session.state_changed.connect(lambda: changes.append(1))

        self.assertTrue(session.request_move(7, 7))
        self.assertEqual(session.view.move_count, 1)
        self.assertIs(session.view.current_player, Player.WHITE)
        self.assertTrue(session.request_undo())
        self.assertEqual(session.view.move_count, 0)
        self.assertIs(session.view.current_player, Player.BLACK)
        self.assertGreaterEqual(len(changes), 2)
        session.shutdown()

    def test_local_game_over_signal(self) -> None:
        session = LocalGameSession()
        over: list[bool] = []
        session.game_over.connect(lambda: over.append(True))

        moves = [
            (7, 3),
            (9, 3),
            (7, 4),
            (9, 4),
            (7, 5),
            (9, 5),
            (7, 6),
            (9, 6),
            (7, 7),
        ]
        for row, col in moves:
            self.assertTrue(session.request_move(row, col))

        self.assertEqual(over, [True])
        self.assertEqual(session.view.winner, Player.BLACK)
        self.assertFalse(session.request_move(0, 0))
        session.shutdown()

    def test_stone_placed_only_for_real_moves(self) -> None:
        """只有真实落子发出 stone_placed；占用点与悔棋不发出。"""
        session = LocalGameSession()
        placed: list[bool] = []
        session.stone_placed.connect(lambda: placed.append(True))
        try:
            self.assertTrue(session.request_move(7, 7))
            self.assertEqual(len(placed), 1)

            self.assertFalse(session.request_move(7, 7))
            self.assertEqual(len(placed), 1)

            self.assertTrue(session.request_undo())
            self.assertEqual(len(placed), 1)

            self.assertTrue(session.request_move(8, 8))
            self.assertEqual(len(placed), 2)
        finally:
            session.shutdown()


class AISessionTests(unittest.TestCase):
    """人机会话负责自动搜索、取消与撤销回合。"""

    def test_human_then_ai_reply(self) -> None:
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        try:
            self.assertTrue(session.request_move(7, 7))
            self.assertTrue(
                wait_until(
                    lambda: session.view.move_count == 2
                    and session.ai_thread is None
                )
            )
            self.assertIs(session.view.current_player, Player.BLACK)
            self.assertTrue(session.accepts_human_input)
            self.assertTrue(session.request_undo())
            self.assertEqual(session.view.move_count, 0)
        finally:
            session.shutdown()

    def test_ai_first_opens_automatically(self) -> None:
        session = AIGameSession(Player.WHITE, AIDifficulty.EASY)
        try:
            self.assertTrue(
                wait_until(
                    lambda: session.view.move_count == 1
                    and session.ai_thread is None
                )
            )
            self.assertIs(session.view.last_move.player, Player.BLACK)
            self.assertIs(session.view.current_player, Player.WHITE)
            self.assertTrue(session.accepts_human_input)
        finally:
            session.shutdown()

    def test_stone_placed_once_per_committed_move(self) -> None:
        """人机对局中人类与 AI 的每次落子各发出一次，悔棋不发出。"""
        session = AIGameSession(Player.BLACK, AIDifficulty.EASY)
        placed: list[bool] = []
        session.stone_placed.connect(lambda: placed.append(True))
        try:
            self.assertTrue(session.request_move(7, 7))
            self.assertEqual(len(placed), 1)
            self.assertTrue(
                wait_until(
                    lambda: session.view.move_count == 2
                    and session.ai_thread is None
                )
            )
            self.assertEqual(len(placed), 2)
            self.assertTrue(session.request_undo())
            self.assertEqual(len(placed), 2)
        finally:
            session.shutdown()


class OnlineSessionTests(unittest.TestCase):
    """在线会话必须先收到服务器确认，不能本地抢先落子。"""

    @staticmethod
    def _authenticate(transport: FakeTransport, session: OnlineGameSession) -> None:
        transport.receive(
            make_message(
                SERVER_AUTHENTICATED,
                {"user": {"id": "u1", "username": "tester"}},
            )
        )

    def test_auth_room_lobby_lifecycle(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        auth_events = []
        room_events = []
        closed_events = []
        session.authenticated.connect(auth_events.append)
        session.room_created.connect(room_events.append)
        session.room_closed.connect(closed_events.append)
        try:
            self._authenticate(transport, session)
            self.assertTrue(session.is_authenticated)
            self.assertEqual(session.username, "tester")
            self.assertEqual(auth_events[0]["username"], "tester")

            transport.receive(
                make_message(
                    SERVER_ROOM_CREATED,
                    {
                        "room_code": "ABC123",
                        "game_id": "game-abc",
                        "your_color": "black",
                        "black_name": "tester",
                        "white_name": "",
                        "turn_seconds": 30,
                    },
                )
            )
            self.assertEqual(session.room_code, "ABC123")
            self.assertTrue(session.waiting_for_opponent)
            self.assertFalse(session.accepts_human_input)
            self.assertEqual(room_events, ["ABC123"])

            transport.receive(
                make_message(
                    SERVER_OPPONENT_STATUS,
                    {"username": "other", "online": False},
                )
            )
            self.assertFalse(session.opponent_online)
            self.assertEqual(session.opponent_username, "other")

            transport.receive(
                make_message(
                    SERVER_ROOM_CLOSED,
                    {"reason": "测试关闭"},
                )
            )
            self.assertEqual(session.room_code, "")
            self.assertFalse(session.waiting_for_opponent)
            self.assertEqual(closed_events, ["测试关闭"])
        finally:
            session.shutdown()
            transport.close()

    def test_snapshot_then_ack_updates_view(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        try:
            self._authenticate(transport, session)
            transport.receive(
                state_message(blank_board(), request_id=None)
            )
            self.assertTrue(session.accepts_human_input)
            self.assertEqual(session.view.move_count, 0)

            self.assertTrue(session.request_move(7, 7))
            # 关键：请求发出后本地棋盘仍未改变，必须等服务器快照。
            self.assertEqual(session.view.move_count, 0)
            self.assertTrue(session.busy)
            self.assertFalse(session.accepts_human_input)
            self.assertEqual(len(transport.sent_messages), 1)
            request_id = transport.sent_messages[0]["request_id"]

            board = blank_board()
            board[7][7] = "black"
            transport.receive(
                state_message(
                    board,
                    current="white",
                    move_no=1,
                    last_move={"row": 7, "col": 7, "player": "black"},
                    request_id=request_id,
                )
            )

            self.assertEqual(session.view.move_count, 1)
            self.assertIs(session.view.last_move.player, Player.BLACK)
            self.assertFalse(session.busy)
            self.assertFalse(session.accepts_human_input)  # 对手回合
        finally:
            session.shutdown()
            transport.close()

    def test_rejected_move_releases_pending(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        errors: list[str] = []
        session.error.connect(errors.append)
        try:
            self._authenticate(transport, session)
            transport.receive(state_message(blank_board()))
            self.assertTrue(session.request_move(7, 7))
            request_id = transport.sent_messages[-1]["request_id"]
            transport.receive(
                make_message(
                    SERVER_MOVE_REJECTED,
                    {"reason": "轮到对手"},
                    request_id=request_id,
                )
            )
            self.assertFalse(session.busy)
            self.assertTrue(session.accepts_human_input)
            self.assertTrue(errors)
        finally:
            session.shutdown()
            transport.close()

    def test_game_over_signal_from_snapshot(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        over: list[bool] = []
        session.game_over.connect(lambda: over.append(True))
        try:
            self._authenticate(transport, session)
            transport.receive(state_message(blank_board()))
            board = blank_board()
            for col in range(3, 8):
                board[7][col] = "black"
            transport.receive(
                state_message(
                    board,
                    current="black",
                    move_no=5,
                    status="black_won",
                    last_move={"row": 7, "col": 7, "player": "black"},
                    winning_line=[[7, c] for c in range(3, 8)],
                )
            )
            self.assertTrue(session.view.is_game_over)
            self.assertEqual(session.view.winner, Player.BLACK)
            self.assertEqual(over, [True])
            self.assertFalse(session.accepts_human_input)
        finally:
            session.shutdown()
            transport.close()

    def test_stone_placed_follows_confirmed_snapshots(self) -> None:
        """联机落子音效只跟随服务器确认的快照，且每个新对局独立计数。"""
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        placed: list[bool] = []
        session.stone_placed.connect(lambda: placed.append(True))
        try:
            self._authenticate(transport, session)
            transport.receive(state_message(blank_board(), move_no=0))
            self.assertEqual(placed, [])

            board = blank_board()
            board[7][7] = "black"
            transport.receive(
                state_message(
                    board,
                    current="white",
                    move_no=1,
                    last_move={"row": 7, "col": 7, "player": "black"},
                )
            )
            self.assertEqual(len(placed), 1)

            # 重复快照（手数未增加）不重复发声。
            transport.receive(
                state_message(
                    board,
                    current="white",
                    move_no=1,
                    last_move={"row": 7, "col": 7, "player": "black"},
                )
            )
            self.assertEqual(len(placed), 1)

            board[8][8] = "white"
            transport.receive(
                state_message(
                    board,
                    current="black",
                    move_no=2,
                    last_move={"row": 8, "col": 8, "player": "white"},
                )
            )
            self.assertEqual(len(placed), 2)

            # 本地意图被服务器拒绝时不发声。
            self.assertTrue(session.request_move(0, 0))
            request_id = transport.sent_messages[-1]["request_id"]
            transport.receive(
                make_message(
                    SERVER_MOVE_REJECTED,
                    {"reason": "轮到对手"},
                    request_id=request_id,
                )
            )
            self.assertEqual(len(placed), 2)

            # 再战新 game_id：空首帧不响，首帧已含 1 手则响一次。
            transport.receive(
                state_message(blank_board(), game_id="game-2", move_no=0)
            )
            self.assertEqual(len(placed), 2)

            board2 = blank_board()
            board2[0][0] = "black"
            transport.receive(
                state_message(
                    board2,
                    game_id="game-2",
                    current="white",
                    move_no=1,
                    last_move={"row": 0, "col": 0, "player": "black"},
                )
            )
            self.assertEqual(len(placed), 3)
        finally:
            session.shutdown()
            transport.close()

    def test_disconnect_clears_pending(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        errors: list[str] = []
        session.error.connect(errors.append)
        try:
            self._authenticate(transport, session)
            transport.receive(state_message(blank_board()))
            self.assertTrue(session.request_move(7, 7))
            transport.drop("network unreachable")
            self.assertFalse(session.busy)
            self.assertFalse(session.accepts_human_input)
            self.assertTrue(errors)
        finally:
            session.shutdown()
            transport.close()

    def test_quick_match_queue_and_rated_snapshot(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        queue_events: list[tuple[bool, int]] = []
        matches: list[tuple[str, str]] = []
        session.queue_status_changed.connect(
            lambda queued, size: queue_events.append((queued, size))
        )
        session.matched.connect(
            lambda game_id, name: matches.append((game_id, name))
        )
        try:
            self._authenticate(transport, session)
            self.assertTrue(session.quick_match())
            self.assertEqual(
                transport.sent_messages[-1]["type"], CLIENT_QUICK_MATCH
            )

            transport.receive(
                make_message(
                    SERVER_QUEUE_STATUS,
                    {
                        "state": "queued",
                        "queue_size": 1,
                        "your_rating": 1200,
                        "rank_name": "黄金",
                    },
                )
            )
            self.assertTrue(session.queued)
            self.assertEqual(session.queue_size, 1)
            self.assertEqual(queue_events[-1], (True, 1))

            transport.receive(
                make_message(
                    SERVER_MATCH_FOUND,
                    {
                        "game_id": "ranked-1",
                        "your_color": "white",
                        "opponent_name": "对手甲",
                        "your_rating": 1200,
                        "opponent_rating": 1180,
                        "rated": True,
                    },
                )
            )
            self.assertFalse(session.queued)
            self.assertTrue(session.rated)
            self.assertEqual(session.your_rating, 1200)
            self.assertEqual(session.opponent_rating, 1180)
            self.assertEqual(matches, [("ranked-1", "对手甲")])

            transport.receive(
                state_message(
                    blank_board(),
                    current="black",
                    your_color="white",
                    move_no=0,
                    game_id="ranked-1",
                    rated=True,
                    your_rating_before=1200,
                    opponent_rating_before=1180,
                )
            )
            self.assertIsNotNone(session.snapshot)
            self.assertTrue(session.snapshot.rated)
            self.assertEqual(session.snapshot.room_code, "")
            self.assertFalse(session.accepts_human_input)  # 对手黑棋先手

            transport.receive(
                state_message(
                    blank_board(),
                    current="black",
                    your_color="white",
                    move_no=5,
                    status="black_won",
                    game_id="ranked-1",
                    rated=True,
                    your_rating_before=1200,
                    your_rating_after=1188,
                    opponent_rating_before=1180,
                    opponent_rating_after=1192,
                    rating_delta=-12,
                )
            )
            self.assertEqual(session.rating_delta, -12)
            self.assertEqual(session.your_rating_after, 1188)
            self.assertTrue(session.snapshot.is_game_over)
            self.assertFalse(session.request_rematch())
        finally:
            session.shutdown()
            transport.close()

    def test_cancel_match_sends_command_and_clears_queue(self) -> None:
        transport = FakeTransport()
        session = OnlineGameSession(transport)
        try:
            self._authenticate(transport, session)
            self.assertTrue(session.quick_match())
            transport.receive(
                make_message(
                    SERVER_QUEUE_STATUS,
                    {"state": "queued", "queue_size": 1},
                )
            )
            self.assertTrue(session.queued)
            self.assertTrue(session.cancel_match())
            self.assertEqual(
                transport.sent_messages[-1]["type"], CLIENT_CANCEL_MATCH
            )
            transport.receive(
                make_message(
                    SERVER_QUEUE_STATUS,
                    {"state": "idle", "queue_size": 0},
                )
            )
            self.assertFalse(session.queued)
            self.assertEqual(session.queue_size, 0)
        finally:
            session.shutdown()
            transport.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
