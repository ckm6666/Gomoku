"""人机对局会话：本地 GameModel + 后台 AIWorker 生命周期。

MainWindow 不再直接管理 AI QThread；本会话负责：
* AI 首手/每次轮到 AI 时启动一次后台搜索；
* 忽略悔棋、重开、暂停后迟到的搜索结果；
* 取消、恢复和关闭时安全地等待 QThread 结束；
* AI 异常时用合法空点兜底，保证棋局不悬挂。

所有模型写入和信号都在 GUI 主线程；AIWorker 只拿只读棋盘快照和取消事件。
"""

from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import QObject, QTimer

from gomoku.client.ai_engine import AIDifficulty
from gomoku.client.ai_worker import AIWorker
from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Player

logger = logging.getLogger(__name__)


class AIGameSession(GameSession):
    """玩家与本地三档 AI 的对局会话。"""

    def __init__(
        self,
        human_player: Player,
        difficulty: AIDifficulty,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._model = GameModel()
        self._human_player = human_player
        self._ai_player = human_player.opponent
        self._difficulty = difficulty

        self._paused = False
        self._closed = False
        self._ai_thread: Optional[AIWorker] = None
        self._ai_request_seq = 0
        self._ai_start_pending = False

        # AI 执黑时在构造完成后自动开局。使用 0ms 定时器让 MainWindow
        # 有机会先连接信号、装配界面，再进入异步搜索。
        QTimer.singleShot(0, self._maybe_start_ai)

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def view(self) -> BoardView:
        return BoardView.from_model(self._model)

    @property
    def model(self) -> GameModel:
        """兼容/测试用；界面不应通过它修改模型。"""
        return self._model

    @property
    def human_player(self) -> Player:
        return self._human_player

    @property
    def ai_player(self) -> Player:
        return self._ai_player

    @property
    def difficulty(self) -> AIDifficulty:
        return self._difficulty

    @property
    def ai_thread(self) -> Optional[AIWorker]:
        """供测试/诊断读取当前 AI 线程；正常 UI 不应操作它。"""
        return self._ai_thread

    @property
    def busy(self) -> bool:
        """轮到 AI 的整个期间都视为忙，锁定设置和棋盘输入。"""
        return (
            not self._model.is_game_over
            and self._model.current_player is self._ai_player
        )

    @property
    def accepts_human_input(self) -> bool:
        return (
            not self._closed
            and not self._paused
            and not self._model.is_game_over
            and self._model.current_player is self._human_player
        )

    @property
    def can_undo(self) -> bool:
        """只有玩家已经落过子，才存在可撤销的“玩家决策点”。"""
        return any(
            move.player is self._human_player for move in self._model.moves
        )

    # ------------------------------------------------------------------
    # 玩家操作
    # ------------------------------------------------------------------
    def request_move(self, row: int, col: int) -> bool:
        if not self.accepts_human_input:
            return False
        if not self._model.place_stone(row, col):
            return False
        self._after_any_move()
        return True

    def request_undo(self) -> bool:
        if self._closed or self._paused or not self.can_undo:
            return False

        self._cancel_ai_search()
        last_move = self._model.last_move
        if last_move is None:
            return False

        self._model.undo()
        if last_move.player is self._ai_player and self._model.move_count > 0:
            self._model.undo()

        # 防御性兜底：撤销后必须回到玩家回合，且不自动启动 AI。
        while (
            self._model.move_count > 0
            and self._model.current_player is not self._human_player
        ):
            self._model.undo()

        self.state_changed.emit()
        return True

    def request_restart(self) -> bool:
        if self._closed:
            return False
        self._paused = False
        self._cancel_ai_search()
        self._model.reset()
        self.state_changed.emit()
        QTimer.singleShot(0, self._maybe_start_ai)
        return True

    def set_difficulty(self, difficulty: object) -> None:
        """更新后续 AI 搜索难度，不清空当前棋局。"""
        if not isinstance(difficulty, AIDifficulty) or difficulty is self._difficulty:
            return
        self._difficulty = difficulty
        self.state_changed.emit()

    # ------------------------------------------------------------------
    # 暂停/恢复/关闭
    # ------------------------------------------------------------------
    def pause(self) -> None:
        """暂停 AI 并作废在途结果；恢复后若仍轮到 AI 会重新搜索。"""
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
        """取消并等待 AI 线程结束，防止 QThread 在运行中被析构。"""
        if self._closed:
            return
        self._closed = True
        self._paused = True
        self._cancel_ai_search()

        worker = self._ai_thread
        if worker is not None:
            worker.cancel()
            if not worker.wait(3000):
                # 引擎每次检查都有超时和取消，正常不会走到这里；不调用
                # terminate()，继续等待以换取进程退出安全。
                worker.wait()

    # ------------------------------------------------------------------
    # AI 线程编排
    # ------------------------------------------------------------------
    def _is_ai_turn(self) -> bool:
        return (
            not self._closed
            and not self._model.is_game_over
            and self._model.current_player is self._ai_player
        )

    def _after_any_move(self) -> None:
        """统一推送状态，并在对局继续时安排 AI 搜索。"""
        self.state_changed.emit()
        self.stone_placed.emit()
        if self._model.is_game_over:
            self.game_over.emit()
            return
        if self._is_ai_turn():
            QTimer.singleShot(0, self._maybe_start_ai)

    def _maybe_start_ai(self) -> None:
        """幂等入口：只在轮到 AI 且没有活动线程时启动一次搜索。"""
        if self._closed or self._paused or not self._is_ai_turn():
            if not self._is_ai_turn():
                self._ai_start_pending = False
            return

        if self._ai_thread is not None:
            # 旧线程正在退出，结束后由 finished 槽再次尝试。
            self._ai_start_pending = True
            return

        self._ai_start_pending = False
        self._ai_request_seq += 1
        request_id = self._ai_request_seq

        worker = AIWorker(
            request_id=request_id,
            board_snapshot=self._model.board,
            player=self._ai_player,
            difficulty=self._difficulty,
        )
        worker.move_ready.connect(self._on_ai_move_ready)
        worker.search_failed.connect(self._on_ai_search_failed)
        worker.finished.connect(self._on_ai_thread_finished)
        self._ai_thread = worker
        worker.start()

    def _cancel_ai_search(self) -> None:
        """使当前请求失效并请求取消，但保留线程对象直到 finished。"""
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
            or request_id != self._ai_request_seq
            or not self._is_ai_turn()
        ):
            return

        if (
            not 0 <= row < len(self._model.board)
            or not 0 <= col < len(self._model.board)
            or self._model.board[row][col] is not None
        ):
            logger.warning("AI 返回非法落点 %s,%s，使用兜底落点", row, col)
            self._apply_ai_fallback()
            return

        if not self._model.place_stone(row, col):
            self._apply_ai_fallback()
            return
        self._after_any_move()

    def _on_ai_search_failed(self, request_id: int, message: str) -> None:
        if (
            self._closed
            or self._paused
            or request_id != self._ai_request_seq
            or not self._is_ai_turn()
        ):
            return

        logger.warning("AI 搜索失败：%s", message)
        self.error.emit("AI 搜索失败，已自动选择合法落点")
        self._apply_ai_fallback()

    def _apply_ai_fallback(self) -> None:
        """AI 异常时选一个合法空点，保证会话不会卡在 AI 回合。"""
        empty_cells = self._model.empty_cells()
        if not empty_cells:
            self.state_changed.emit()
            return

        center = (len(self._model.board) // 2, len(self._model.board) // 2)
        fallback = center if center in empty_cells else empty_cells[0]
        self._model.place_stone(*fallback)
        self._after_any_move()

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
        # 若旧线程是在暂停/取消后结束，且当前仍需 AI 搜索，这里补启动。
        QTimer.singleShot(0, self._maybe_start_ai)


__all__ = ["AIGameSession"]
