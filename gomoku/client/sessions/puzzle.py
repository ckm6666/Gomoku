"""残局挑战会话：初始局面 + 限手数 + 后台 AI 防守。

与 :class:`AIGameSession` 的区别：
* 玩家执残局指定的先手方，目标是限手数内成五；
* 失败不一定是棋盘终局（手数用尽也算失败）；
* 不提供悔棋，只允许重开当前残局；
* 用 ``puzzle_finished`` 明确报告破局成功/失败，界面不依赖
  ``view.is_game_over`` 判断残局结果。

AI 线程生命周期沿用 AIGameSession 的取消/暂停/等待模式。
"""

from __future__ import annotations

import logging
import zlib
from typing import Optional

from PySide6.QtCore import QObject, QTimer, Signal

from gomoku.client.ai_engine import AIDifficulty
from gomoku.client.ai_worker import AIWorker
from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.shared.game_model import GameModel
from gomoku.shared.puzzle import Puzzle
from gomoku.shared.rules import Player

logger = logging.getLogger(__name__)


class PuzzleSession(GameSession):
    """一局残局挑战。"""

    # (是否破局成功, 结果说明)
    puzzle_finished = Signal(bool, str)

    def __init__(
        self,
        puzzle: Puzzle,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._puzzle = puzzle
        self._model = GameModel.from_moves(puzzle.moves)
        self._player = puzzle.to_move
        self._defender = puzzle.to_move.opponent
        self._defender_difficulty = AIDifficulty(puzzle.defender)
        self._seed = zlib.crc32(puzzle.id.encode("utf-8")) & 0xFFFFFFFF

        self._player_moves = 0
        self._finished = False
        self._succeeded = False
        self._result_reason = ""
        self._paused = False
        self._closed = False
        self._ai_thread: Optional[AIWorker] = None
        self._ai_request_seq = 0
        self._ai_start_pending = False

        # 玩家固定先手；保留调用以覆盖未来支持“防守方先手”的残局。
        QTimer.singleShot(0, self._maybe_start_ai)

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def view(self) -> BoardView:
        return BoardView.from_model(self._model)

    @property
    def model(self) -> GameModel:
        """供棋谱记录/测试读取；界面不应通过它修改模型。"""
        return self._model

    @property
    def puzzle(self) -> Puzzle:
        return self._puzzle

    @property
    def human_player(self) -> Player:
        return self._player

    @property
    def ai_player(self) -> Player:
        return self._defender

    @property
    def difficulty(self) -> AIDifficulty:
        return self._defender_difficulty

    @property
    def ai_thread(self) -> Optional[AIWorker]:
        return self._ai_thread

    @property
    def player_moves(self) -> int:
        return self._player_moves

    @property
    def move_limit(self) -> int:
        return self._puzzle.max_player_moves

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def succeeded(self) -> bool:
        return self._succeeded

    @property
    def result_reason(self) -> str:
        return self._result_reason

    @property
    def hint(self) -> str:
        return self._puzzle.hint

    @property
    def busy(self) -> bool:
        return (
            not self._finished
            and not self._model.is_game_over
            and self._model.current_player is self._defender
        )

    @property
    def accepts_human_input(self) -> bool:
        return (
            not self._closed
            and not self._paused
            and not self._finished
            and not self._model.is_game_over
            and self._model.current_player is self._player
        )

    @property
    def can_undo(self) -> bool:
        """残局不提供悔棋；需要重试时使用重新挑战。"""
        return False

    # ------------------------------------------------------------------
    # 玩家操作
    # ------------------------------------------------------------------
    def request_move(self, row: int, col: int) -> bool:
        if not self.accepts_human_input:
            return False
        if not self._model.place_stone(row, col):
            return False

        self._player_moves += 1
        self.state_changed.emit()
        self.stone_placed.emit()

        if self._model.winner is self._player:
            self._finish(True, "破局成功")
            return True
        if self._model.is_game_over:
            self._finish(False, "棋盘已满，未能破局")
            return True
        if self._player_moves >= self._puzzle.max_player_moves:
            self._finish(
                False,
                f"{self._puzzle.max_player_moves} 手内未形成五连",
            )
            return True

        QTimer.singleShot(0, self._maybe_start_ai)
        return True

    def request_undo(self) -> bool:
        return False

    def request_restart(self) -> bool:
        if self._closed:
            return False
        self._paused = False
        self._cancel_ai_search()
        self._model = GameModel.from_moves(self._puzzle.moves)
        self._player_moves = 0
        self._finished = False
        self._succeeded = False
        self._result_reason = ""
        self.state_changed.emit()
        QTimer.singleShot(0, self._maybe_start_ai)
        return True

    # ------------------------------------------------------------------
    # 暂停/恢复/关闭
    # ------------------------------------------------------------------
    def pause(self) -> None:
        if self._closed or self._paused:
            return
        self._paused = True
        self._cancel_ai_search()
        self.state_changed.emit()

    def resume(self) -> None:
        if self._closed or not self._paused:
            return
        self._paused = False
        self.state_changed.emit()
        QTimer.singleShot(0, self._maybe_start_ai)

    def shutdown(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._paused = True
        self._cancel_ai_search()

        worker = self._ai_thread
        if worker is not None:
            worker.cancel()
            if not worker.wait(3000):
                worker.wait()

    # ------------------------------------------------------------------
    # 结果
    # ------------------------------------------------------------------
    def _finish(self, success: bool, reason: str) -> None:
        if self._finished:
            return
        self._finished = True
        self._succeeded = success
        self._result_reason = reason
        self._cancel_ai_search()
        self.puzzle_finished.emit(success, reason)

    # ------------------------------------------------------------------
    # AI 防守线程
    # ------------------------------------------------------------------
    def _is_ai_turn(self) -> bool:
        return (
            not self._closed
            and not self._paused
            and not self._finished
            and not self._model.is_game_over
            and self._model.current_player is self._defender
        )

    def _maybe_start_ai(self) -> None:
        if not self._is_ai_turn():
            self._ai_start_pending = False
            return

        if self._ai_thread is not None:
            self._ai_start_pending = True
            return

        self._ai_start_pending = False
        self._ai_request_seq += 1
        request_id = self._ai_request_seq

        worker = AIWorker(
            request_id=request_id,
            board_snapshot=self._model.board,
            player=self._defender,
            difficulty=self._defender_difficulty,
            seed=self._seed,
        )
        worker.move_ready.connect(self._on_ai_move_ready)
        worker.search_failed.connect(self._on_ai_search_failed)
        worker.finished.connect(self._on_ai_thread_finished)
        self._ai_thread = worker
        worker.start()

    def _cancel_ai_search(self) -> None:
        self._ai_request_seq += 1
        self._ai_start_pending = False
        worker = self._ai_thread
        if worker is not None:
            worker.cancel()

    def _on_ai_move_ready(
        self, request_id: int, row: int, col: int
    ) -> None:
        if (
            self._closed
            or self._paused
            or self._finished
            or request_id != self._ai_request_seq
            or not self._is_ai_turn()
        ):
            return

        if (
            not 0 <= row < len(self._model.board)
            or not 0 <= col < len(self._model.board)
            or self._model.board[row][col] is not None
        ):
            logger.warning("残局防守 AI 返回非法落点 %s,%s", row, col)
            self._apply_fallback_move()
            return
        if not self._model.place_stone(row, col):
            self._apply_fallback_move()
            return
        self._after_defender_move()

    def _on_ai_search_failed(self, request_id: int, message: str) -> None:
        if (
            self._closed
            or self._paused
            or self._finished
            or request_id != self._ai_request_seq
            or not self._is_ai_turn()
        ):
            return
        logger.warning("残局防守 AI 搜索失败：%s", message)
        self.error.emit("防守 AI 搜索失败，已选择合法落点")
        self._apply_fallback_move()

    def _apply_fallback_move(self) -> None:
        empty_cells = self._model.empty_cells()
        if not empty_cells:
            self.state_changed.emit()
            self._finish(False, "棋盘已满，未能破局")
            return
        center = (
            len(self._model.board) // 2,
            len(self._model.board) // 2,
        )
        fallback = center if center in empty_cells else empty_cells[0]
        if not self._model.place_stone(*fallback):
            self.state_changed.emit()
            self._finish(False, "残局状态异常")
            return
        self._after_defender_move()

    def _after_defender_move(self) -> None:
        self.state_changed.emit()
        self.stone_placed.emit()
        if self._model.winner is self._defender:
            self._finish(False, "防守 AI 成五，挑战失败")
        elif self._model.is_game_over:
            self._finish(False, "棋盘已满，未能破局")

    def _on_ai_thread_finished(self) -> None:
        worker = self.sender()
        if worker is None:
            worker = self._ai_thread

        if worker is self._ai_thread:
            self._ai_thread = None
        if worker is not None:
            worker.deleteLater()

        if self._closed:
            return
        QTimer.singleShot(0, self._maybe_start_ai)


__all__ = ["PuzzleSession"]
