"""本地双人对局会话。"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject

from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.shared.game_model import GameModel


class LocalGameSession(GameSession):
    """两人共用同一台电脑的本地对局。

    这里保留原有单方悔棋/新开一局的语义；它是本地功能，不应直接照搬到
    将来的联机会话。
    """

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._model = GameModel()
        self._paused = False

    # ------------------------------------------------------------------
    # 只读状态
    # ------------------------------------------------------------------
    @property
    def view(self) -> BoardView:
        return BoardView.from_model(self._model)

    @property
    def model(self) -> GameModel:
        """兼容/测试用；界面不要通过它修改模型。"""
        return self._model

    @property
    def accepts_human_input(self) -> bool:
        return not self._paused and not self._model.is_game_over

    @property
    def can_undo(self) -> bool:
        return self._model.move_count > 0

    # ------------------------------------------------------------------
    # 操作
    # ------------------------------------------------------------------
    def request_move(self, row: int, col: int) -> bool:
        if not self.accepts_human_input:
            return False
        if not self._model.place_stone(row, col):
            return False

        self.state_changed.emit()
        self.stone_placed.emit()
        if self._model.is_game_over:
            self.game_over.emit()
        return True

    def request_undo(self) -> bool:
        if not self.can_undo:
            return False
        if not self._model.undo():
            return False
        self.state_changed.emit()
        return True

    def request_restart(self) -> bool:
        self._model.reset()
        self._paused = False
        self.state_changed.emit()
        return True

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self.state_changed.emit()

    def resume(self) -> None:
        if self._paused:
            self._paused = False
            self.state_changed.emit()

    def shutdown(self) -> None:
        """本地会话没有后台任务。"""
        return None


__all__ = ["LocalGameSession"]
