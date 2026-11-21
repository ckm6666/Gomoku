"""在后台线程中执行五子棋 AI 搜索。

为什么不直接在 UI 线程调用 AI？
--------------------------------
即使是有时间上限的搜索，也会在 Python 主线程中占用大量 CPU 时间，导致
窗口无法及时处理重绘、按钮点击和窗口关闭事件。这里把一次搜索封装成一个
只运行一次的 QThread；主窗口只通过 Qt 信号接收结果，绝不从后台线程操作
任何 QWidget 或 GameModel 实例。

线程的生命周期（保存引用、取消、等待 finished、写入结果代次）由
MainWindow 统一管理；本模块只保证：
1. 搜索可被线程安全的 Event 取消；
2. 任何异常都通过信号上报，不让界面永远停在“AI 思考中”；
3. run() 中不触碰 Qt 控件，也不修改调用方的棋盘快照。
"""

from __future__ import annotations

import threading
from typing import Optional

from PySide6.QtCore import QThread, Signal

from gomoku.client.ai_engine import AIDifficulty, GomokuAI
from gomoku.shared.rules import BoardLike, Player


class AIWorker(QThread):
    """一次性 AI 搜索线程。

    每个对象只应 start() 一次；搜索结束后由 MainWindow 清理。传入的棋盘
    快照和棋手是值类型/枚举，线程内不会读写主界面的对象。
    """

    # (request_id, row, col)。后台线程发出，Qt 会自动排队到主线程槽。
    move_ready = Signal(int, int, int)
    # (request_id, 用户可读的错误说明)
    search_failed = Signal(int, str)

    def __init__(
        self,
        request_id: int,
        board_snapshot: BoardLike,
        player: Player,
        difficulty: AIDifficulty,
        parent=None,
        seed: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self._request_id = request_id
        self._board_snapshot = board_snapshot
        self._player = player
        self._difficulty = difficulty
        self._seed = seed
        self._cancel_event = threading.Event()

    @property
    def request_id(self) -> int:
        """本次搜索的请求编号，用于丢弃撤消/重开后的过期结果。"""
        return self._request_id

    def cancel(self) -> None:
        """请求停止搜索；引擎会在最近的安全检查点退出。"""
        self._cancel_event.set()

    @property
    def cancelled(self) -> bool:
        """线程是否已经被请求取消。"""
        return self._cancel_event.is_set()

    def run(self) -> None:
        """在 QThread 的事件循环之外执行纯 Python 搜索。"""
        try:
            if self._cancel_event.is_set():
                return

            engine = GomokuAI(seed=self._seed)
            move = engine.find_move(
                board_snapshot=self._board_snapshot,
                player=self._player,
                difficulty=self._difficulty,
                cancel_event=self._cancel_event,
            )

            # 如果用户在搜索期间点了悔棋/重开/关闭，取消事件会同时使
            # 主窗口的 request_id 失效；这里不再发回任何结果。
            if self._cancel_event.is_set():
                return

            if move is None:
                self.search_failed.emit(
                    self._request_id,
                    "AI 未找到可落子的空位",
                )
            else:
                row, col = move
                self.move_ready.emit(self._request_id, row, col)
        except Exception as exc:  # noqa: BLE001 - 线程必须自带最终兜底
            # 把异常转为信号，避免线程静默结束导致 UI 永远无法恢复。
            if not self._cancel_event.is_set():
                self.search_failed.emit(
                    self._request_id,
                    f"AI 搜索异常：{type(exc).__name__}: {exc}",
                )
