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

异常处理约定：界面信号只携带固定的通用文案，避免把异常原文（可能包含
内部文件路径、SQL 片段等实现细节）暴露给用户；异常细节连同堆栈写入
日志。
"""

from __future__ import annotations

import logging
import threading
import warnings
from typing import Any, Iterable, Optional, Sequence

from PySide6.QtCore import QThread, Signal

from gomoku.client.ai_engine import (
    AIDifficulty,
    GomokuAI,
    SearchAnalysis,
    SearchConfig,
)
from gomoku.client.opening_book import get_default_opening_book
from gomoku.shared.rules import BoardLike, Player

logger = logging.getLogger(__name__)

# 界面统一使用的失败文案：只说明“失败了”，不暴露内部细节。
SEARCH_FAILED_MESSAGE = "AI 搜索失败，请重试"
ANALYSIS_FAILED_MESSAGE = "AI 分析失败，请重试"


def disconnect_signals(signals: Iterable[Any]) -> None:
    """断开给定信号上的全部连接。

    PySide6 在断开一个没有连接的信号时会抛出无意义的 RuntimeWarning，
    这里统一忽略，避免污染日志。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        for signal in signals:
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass


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
        move_history: Sequence[tuple[int, int]] = (),
    ) -> None:
        super().__init__(parent)
        self._request_id = request_id
        self._board_snapshot = board_snapshot
        self._player = player
        self._difficulty = difficulty
        self._seed = seed
        self._move_history = tuple(move_history)
        self._cancel_event = threading.Event()

    @property
    def request_id(self) -> int:
        """本次搜索的请求编号，用于丢弃撤消/重开后的过期结果。"""
        return self._request_id

    def cancel(self) -> None:
        """请求停止搜索；引擎会在最近的安全检查点退出。"""
        self._cancel_event.set()

    def detach(self) -> None:
        """断开本线程发出的所有信号。

        会话关闭时若线程迟迟不结束，调用它可以保证线程即使随后发出信号
        也不会再碰到界面对象。
        """
        disconnect_signals((self.move_ready, self.search_failed, self.finished))

    @property
    def cancelled(self) -> bool:
        """线程是否已经被请求取消。"""
        return self._cancel_event.is_set()

    def run(self) -> None:
        """在 QThread 的事件循环之外执行纯 Python 搜索。"""
        try:
            if self._cancel_event.is_set():
                return

            engine = GomokuAI(
                seed=self._seed,
                opening_book=get_default_opening_book(),
                move_history=self._move_history,
            )
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
                # 引擎没有返回落点也属于失败路径：界面同样只收到通用文案。
                logger.warning(
                    "AI 搜索未返回落点（request_id=%s）",
                    self._request_id,
                )
                self.search_failed.emit(
                    self._request_id,
                    SEARCH_FAILED_MESSAGE,
                )
            else:
                row, col = move
                self.move_ready.emit(self._request_id, row, col)
        except Exception as exc:  # noqa: BLE001 - 线程必须自带最终兜底
            # 把异常转为信号，避免线程静默结束导致 UI 永远无法恢复。
            # 界面只显示通用文案；异常原文与堆栈写日志，不外泄内部细节。
            if not self._cancel_event.is_set():
                logger.exception(
                    "AI 搜索失败（request_id=%s, %s）",
                    self._request_id,
                    type(exc).__name__,
                )
                self.search_failed.emit(
                    self._request_id,
                    SEARCH_FAILED_MESSAGE,
                )


class AIHintWorker(QThread):
    """一次性 AI 局面分析线程，用于人机模式提示。

    与 :class:`AIWorker` 相同，只接触纯 Python 引擎和只读棋盘快照；结果
    通过 ``analysis_ready`` 信号回到主线程。
    """

    # (request_id, SearchAnalysis)
    analysis_ready = Signal(int, object)
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
        move_history: Sequence[tuple[int, int]] = (),
        config_override: Optional[SearchConfig] = None,
        top_n: int = 3,
    ) -> None:
        super().__init__(parent)
        self._request_id = request_id
        self._board_snapshot = board_snapshot
        self._player = player
        self._difficulty = difficulty
        self._seed = seed
        self._move_history = tuple(move_history)
        self._config_override = config_override
        self._top_n = top_n
        self._cancel_event = threading.Event()

    @property
    def request_id(self) -> int:
        """本次分析请求编号。"""
        return self._request_id

    def cancel(self) -> None:
        """请求停止分析；引擎会在最近的安全检查点退出。"""
        self._cancel_event.set()

    def detach(self) -> None:
        """断开本线程发出的所有信号（线程已无法安全回调界面时使用）。"""
        disconnect_signals(
            (self.analysis_ready, self.search_failed, self.finished)
        )

    @property
    def cancelled(self) -> bool:
        """线程是否已经被请求取消。"""
        return self._cancel_event.is_set()

    def run(self) -> None:
        """在后台线程中执行分析，不触碰任何 Qt 控件。"""
        try:
            if self._cancel_event.is_set():
                return

            engine = GomokuAI(
                seed=self._seed,
                opening_book=get_default_opening_book(),
                move_history=self._move_history,
            )
            analysis: Optional[SearchAnalysis] = engine.analyze(
                board_snapshot=self._board_snapshot,
                player=self._player,
                difficulty=self._difficulty,
                cancel_event=self._cancel_event,
                top_n=self._top_n,
                config_override=self._config_override,
            )

            if self._cancel_event.is_set():
                return

            if analysis is None:
                # 与异常路径一致：界面只显示通用文案，不暴露内部状态。
                logger.warning(
                    "AI 分析未返回结果（request_id=%s）",
                    self._request_id,
                )
                self.search_failed.emit(
                    self._request_id,
                    ANALYSIS_FAILED_MESSAGE,
                )
            else:
                self.analysis_ready.emit(self._request_id, analysis)
        except Exception as exc:  # noqa: BLE001 - 线程必须自带最终兜底
            # 与搜索线程一致：界面只显示通用文案，细节写日志。
            if not self._cancel_event.is_set():
                logger.exception(
                    "AI 分析失败（request_id=%s, %s）",
                    self._request_id,
                    type(exc).__name__,
                )
                self.search_failed.emit(
                    self._request_id,
                    ANALYSIS_FAILED_MESSAGE,
                )
