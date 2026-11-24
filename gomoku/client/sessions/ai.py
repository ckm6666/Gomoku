"""人机对局会话：本地 GameModel + 后台 AIWorker 生命周期。

MainWindow 不再直接管理 AI QThread；本会话负责：
* AI 首手/每次轮到 AI 时启动一次后台搜索；
* 忽略悔棋、重开、暂停后迟到的搜索结果；
* 取消、恢复和关闭时安全地等待 QThread 结束；
* AI 异常时用合法空点兜底，保证棋局不悬挂。

所有模型写入和信号都在 GUI 主线程；AIWorker 只拿只读棋盘快照和取消事件。

关闭时的线程处理（G10）：
后台线程的等待必须是有界的——无参数的 ``QThread.wait()`` 会把 GUI 线程
卡死，窗口失去响应且无法退出。这里统一走 :func:`shutdown_worker`：先取消
再分两次有界等待；仍未结束就断开线程与界面的连接、记录警告，并把它交给
:class:`_DetachedThreadKeeper` 保活，让线程自行结束。
"""

from __future__ import annotations

import atexit
import logging
import time
from typing import Any, Optional

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot

from gomoku.client.ai_engine import AIDifficulty, HINT_CONFIG, SearchAnalysis
from gomoku.client.ai_worker import (
    AIHintWorker,
    AIWorker,
    disconnect_signals,
)
from gomoku.client.board_view import BoardView
from gomoku.client.sessions.base import GameSession
from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Player, in_bounds

logger = logging.getLogger(__name__)

# 关闭会话时第一次等待后台线程的上限（毫秒）。
_WORKER_WAIT_TIMEOUT_MS = 3000
# 第一次等待超时后再给一次较短的有界等待；仍不结束就断开信号并放飞。
_WORKER_DETACH_WAIT_TIMEOUT_MS = 1000
# 用户主动取消提示分析后，等待线程退出的上限（毫秒）。引擎在搜索循环中
# 检查取消事件，正常会立刻返回；超过上限就让它自行结束，结果已经作废。
_HINT_CANCEL_WAIT_MS = 1000
# 进程退出前等待残余线程的总预算（毫秒）。
_EXIT_WAIT_BUDGET_MS = 2000


class _DetachedThreadKeeper(QObject):
    """保活已经与界面脱离的后台线程。

    关闭会话时若线程在有界等待内没有结束：既不能无限等待（会卡死 GUI
    线程），也不能直接析构——运行中的 QThread 被析构会触发
    “QThread: Destroyed while thread is still running” 并直接中止进程。
    这里把线程收编过来，等它自己结束后再释放引用。
    """

    def __init__(self) -> None:
        super().__init__()
        self._threads: list[QThread] = []

    def keep(self, worker: QThread) -> None:
        """接管仍在运行的线程，直到它自然结束。"""
        self._threads.append(worker)
        worker.finished.connect(self.release)

    @Slot()
    def release(self) -> None:
        """线程结束后释放引用并回收对象。"""
        worker = self.sender()
        if worker is None:
            return
        if worker in self._threads:
            self._threads.remove(worker)
        try:
            worker.deleteLater()
        except RuntimeError:
            # 对象已被 Qt 回收，无需再处理。
            pass

    def wait_all(self, budget_ms: int) -> None:
        """有界等待被接管的线程结束；总预算耗尽即放弃。

        进程退出前调用：给已经被取消的线程一点时间自行收尾，避免运行中的
        QThread 在解释器收尾时被析构。这里不做强制结束——强杀一个正在跑
        Python 代码的线程会破坏 GIL 状态，反而让退出更不可靠。
        """
        deadline = time.monotonic() + budget_ms / 1000.0
        for worker in list(self._threads):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                worker.wait(int(remaining * 1000))
            except (RuntimeError, AttributeError):
                break


# 已与界面脱离的线程由模块级引用保活，不随会话销毁而丢失。
_DETACHED_THREADS = _DetachedThreadKeeper()


def _wait_detached_threads() -> None:
    """进程退出前有界等待残余线程，避免运行中的 QThread 被析构。"""
    _DETACHED_THREADS.wait_all(_EXIT_WAIT_BUDGET_MS)


atexit.register(_wait_detached_threads)


def _detach_worker(worker: Any) -> None:
    """断开线程与界面之间的全部连接。

    PySide6 的 ``QObject.disconnect()`` 无参形式并不可用，这里优先调用
    线程自带的 ``detach()``，否则按已知的信号名逐个断开。
    """
    detach = getattr(worker, "detach", None)
    if callable(detach):
        try:
            detach()
        except (RuntimeError, TypeError):
            pass
        return

    signals = [
        signal
        for name in (
            "move_ready",
            "search_failed",
            "analysis_ready",
            "progress",
            "review_finished",
            "review_failed",
            "review_cancelled",
            "finished",
        )
        for signal in (getattr(worker, name, None),)
        if signal is not None
    ]
    disconnect_signals(signals)


def shutdown_worker(worker: Any, label: str) -> bool:
    """取消并有界等待一个后台线程。

    第一次等待超时后会再给一次较短的有界等待；仍然没有结束就断开它与
    界面的连接、记录警告，并交给 :class:`_DetachedThreadKeeper` 保活，
    让线程自行结束——既不调用 terminate()，也绝不无限等待。

    返回 True 表示线程已经在限定时间内结束。
    """
    if worker is None:
        return True

    worker.cancel()
    if worker.wait(_WORKER_WAIT_TIMEOUT_MS):
        return True
    if worker.wait(_WORKER_DETACH_WAIT_TIMEOUT_MS):
        return True

    logger.warning(
        "%s 线程在 %d 毫秒内没有结束，已断开界面连接并等待其自行结束",
        label,
        _WORKER_WAIT_TIMEOUT_MS + _WORKER_DETACH_WAIT_TIMEOUT_MS,
    )
    # 断开全部出站连接：线程即便随后发信号也不会再碰到界面对象。
    _detach_worker(worker)
    _DETACHED_THREADS.keep(worker)
    return False


class AIGameSession(GameSession):
    """玩家与本地四档 AI 的对局会话。"""

    # 提示分析完成；参数为 SearchAnalysis。
    hint_ready = Signal(object)
    # 提示分析失败；参数为用户可读说明。
    hint_failed = Signal(str)
    # 缓存的提示结果已作废；界面应清除棋盘上的旧标记与提示文字。
    hint_cleared = Signal()

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

        self._hint_thread: Optional[AIHintWorker] = None
        self._hint_request_seq = 0
        self._hint_move_count = -1
        # 是否有一轮提示分析尚未结束（界面据此切换“提示/取消提示”）。
        self._hint_pending = False
        # 最近一次有效提示结果；作废后为 None。
        self._hint_analysis: Optional[SearchAnalysis] = None
        # 所有已启动、尚未收到 finished 的后台线程；保证线程存活期间
        # 始终有 Python 引用，避免运行中的 QThread 被提前析构。
        self._workers: list[QThread] = []

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
    def hint_thread(self) -> Optional[AIHintWorker]:
        """供测试/诊断读取当前提示分析线程。"""
        return self._hint_thread

    @property
    def hint_running(self) -> bool:
        """是否有一轮提示分析尚未结束。

        界面用它把“提示”按钮切换成“取消提示”：为 True 表示正在分析，
        此时调用 :meth:`cancel_hint` 可以中止本轮分析。
        """
        return self._hint_pending

    @property
    def hint_analysis(self) -> Optional[SearchAnalysis]:
        """最近一次有效提示结果；被作废（新一轮分析/取消/棋盘变化）后为 None。"""
        return self._hint_analysis

    @property
    def busy(self) -> bool:
        """轮到 AI 的整个期间都视为忙，锁定设置和棋盘输入。"""
        return (
            not self._model.is_game_over
            and self._model.current_player is self._ai_player
        )

    @property
    def can_hint(self) -> bool:
        """玩家回合且没有正在进行的提示分析时，可以请求提示。"""
        return (
            not self._closed
            and not self._paused
            and not self._model.is_game_over
            and self._model.current_player is self._human_player
            and not self._hint_pending
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
        self._cancel_hint_search()
        self._after_any_move()
        return True

    def request_hint(self) -> bool:
        """启动一次后台局面分析；结果通过 ``hint_ready`` 返回。"""
        if not self.can_hint:
            return False

        # 新一轮分析开始前先作废上一轮结果：界面不应继续显示陈旧的
        # 推荐标记或文字。
        self._clear_hint_state()

        self._hint_request_seq += 1
        request_id = self._hint_request_seq
        self._hint_move_count = self._model.move_count
        self._hint_pending = True

        worker = AIHintWorker(
            request_id=request_id,
            board_snapshot=self._model.board,
            player=self._human_player,
            difficulty=AIDifficulty.MASTER,
            move_history=tuple(
                (move.row, move.col) for move in self._model.moves
            ),
            config_override=HINT_CONFIG,
        )
        worker.analysis_ready.connect(self._on_hint_ready)
        worker.search_failed.connect(self._on_hint_failed)
        worker.finished.connect(self._on_hint_thread_finished)
        self._hint_thread = worker
        self._workers.append(worker)
        worker.start()
        self.state_changed.emit()
        return True

    def cancel_hint(self) -> bool:
        """取消正在进行的提示分析。

        返回是否确实作废了某个提示状态：既没有在途分析、也没有缓存结果
        时返回 False。取消后本会话立即回到“可以再次请求提示”的状态，并
        发出 ``hint_cleared`` 通知界面清除旧标记。

        取消是协作式的：后台线程会在最近的安全检查点自行退出，它的结果
        会被丢弃，不会再影响棋局。返回前最多有界等待线程一小段时间，既不
        让界面干等，也保证正常引擎已经停下。
        """
        if self._closed:
            return False
        if not self._hint_pending and self._hint_analysis is None:
            return False
        self._cancel_hint_search()
        self._wait_hint_thread_stopped()
        self.state_changed.emit()
        return True

    def request_undo(self) -> bool:
        if self._closed or self._paused or not self.can_undo:
            return False

        self._cancel_ai_search()
        self._cancel_hint_search()
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
        self._cancel_hint_search()
        self._model.reset()
        self.state_changed.emit()
        QTimer.singleShot(0, self._maybe_start_ai)
        return True

    def set_difficulty(self, difficulty: object) -> None:
        """更新后续 AI 搜索难度，不清空当前棋局。"""
        if not isinstance(difficulty, AIDifficulty) or difficulty is self._difficulty:
            return
        self._difficulty = difficulty
        self._cancel_hint_search()
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
        self._cancel_hint_search()
        self.state_changed.emit()

    def resume(self) -> None:
        if self._closed or not self._paused:
            return
        self._paused = False
        self.state_changed.emit()
        QTimer.singleShot(0, self._maybe_start_ai)

    def shutdown(self) -> None:
        """取消并有界等待 AI 线程结束，防止 QThread 在运行中被析构。

        等待是有界的：引擎若迟迟不响应取消，也不会把 GUI 线程卡死。
        """
        if self._closed:
            return
        self._closed = True
        self._paused = True
        self._cancel_ai_search()
        self._cancel_hint_search()

        for worker, label in (
            (self._ai_thread, "AI 搜索"),
            (self._hint_thread, "提示分析"),
        ):
            if worker is None:
                continue
            if not shutdown_worker(worker, label):
                # 线程没有结束：界面连接已断开，改由保活器等待其自行结束。
                self._drop_worker(worker)

        self._ai_thread = None
        self._hint_thread = None

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
            move_history=tuple(
                (move.row, move.col) for move in self._model.moves
            ),
        )
        worker.move_ready.connect(self._on_ai_move_ready)
        worker.search_failed.connect(self._on_ai_search_failed)
        worker.finished.connect(self._on_ai_thread_finished)
        self._ai_thread = worker
        self._workers.append(worker)
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
            not in_bounds(row, col)
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

        self._drop_worker(worker)
        if worker is self._ai_thread:
            self._ai_thread = None
        if worker is not None:
            worker.deleteLater()

        if self._closed:
            return
        # 若旧线程是在暂停/取消后结束，且当前仍需 AI 搜索，这里补启动。
        QTimer.singleShot(0, self._maybe_start_ai)

    # ------------------------------------------------------------------
    # 提示分析线程
    # ------------------------------------------------------------------
    def _cancel_hint_search(self) -> None:
        """使当前提示请求失效、请求取消，并清空提示状态。"""
        self._hint_request_seq += 1
        self._hint_pending = False
        worker = self._hint_thread
        if worker is not None:
            worker.cancel()
        self._clear_hint_state()

    def _wait_hint_thread_stopped(self) -> None:
        """有界等待提示线程退出；超时则留给 finished 槽自行清理。

        不允许无参数 ``wait()``：取消是协作式的，引擎若在极端情况下仍不
        响应，界面也不能被卡住。
        """
        worker = self._hint_thread
        if worker is None:
            return
        try:
            worker.wait(_HINT_CANCEL_WAIT_MS)
        except (RuntimeError, AttributeError):
            # 线程对象已被回收：无需再等待。
            pass

    def _clear_hint_state(self) -> None:
        """作废缓存的提示结果，并通知界面清除棋盘上的旧标记。"""
        self._hint_analysis = None
        self.hint_cleared.emit()

    def _on_hint_ready(self, request_id: int, analysis: object) -> None:
        if self._closed or self._paused:
            return
        if request_id != self._hint_request_seq:
            # 结果已被更新的一轮请求取代（或本轮已被取消）：直接丢弃。
            return

        # 请求仍然有效：无论结果能不能用，本轮提示到此结束。
        self._hint_pending = False

        if (
            self._model.move_count != self._hint_move_count
            or not self.accepts_human_input
        ):
            # 棋盘已经变化或已不是玩家回合：这份建议已经过期。
            return

        best_move = getattr(analysis, "best_move", None)
        cancelled = getattr(analysis, "cancelled", False)
        if cancelled or best_move is None:
            return

        row, col = best_move
        if (
            not in_bounds(row, col)
            or self._model.board[row][col] is not None
        ):
            return
        self._hint_analysis = analysis
        self.hint_ready.emit(analysis)

    def _on_hint_failed(self, request_id: int, message: str) -> None:
        if self._closed or self._paused:
            return
        if request_id != self._hint_request_seq:
            return
        self._hint_pending = False
        if not self.accepts_human_input:
            return
        self.hint_failed.emit(message)

    def _on_hint_thread_finished(self) -> None:
        worker = self.sender()
        if worker is None:
            worker = self._hint_thread

        self._drop_worker(worker)
        if worker is self._hint_thread:
            self._hint_thread = None
        if worker is not None:
            worker.deleteLater()
        if not self._closed:
            self.state_changed.emit()

    # ------------------------------------------------------------------
    # 后台线程引用管理
    # ------------------------------------------------------------------
    def _drop_worker(self, worker: Optional[QThread]) -> None:
        """释放线程引用；线程结束前必须保留它，避免被提前析构。"""
        if worker is None:
            return
        if worker in self._workers:
            self._workers.remove(worker)


__all__ = ["AIGameSession", "shutdown_worker"]
