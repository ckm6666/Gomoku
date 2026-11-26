"""棋谱复盘面板：只读棋盘、逐手导航与落子列表。

复盘不再是独立窗口，而是主窗口页面栈中的一页：应用全局只保留一个
原生窗口。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.board_view import BoardView
from gomoku.client.board_widget import BoardWidget
from gomoku.client.replay import ReplayError, ReplayTimeline
from gomoku.client.review import (
    LABEL_BEST,
    LABEL_BLUNDER,
    LABEL_GOOD,
    LABEL_INACCURATE,
    LABEL_MISTAKE,
    MoveReview,
    ReviewResult,
)
from gomoku.client.review_worker import AIReviewWorker
from gomoku.client.ui_theme import GAME_BACKDROP_SPEC, InkBackdrop
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import BOARD_SIZE, Cell, Player
from gomoku.shared.sgf import SgfError, record_from_sgf, record_to_sgf

logger = logging.getLogger(__name__)

_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)

_MODE_LABELS = {
    "local": "双人",
    "ai": "人机",
    "puzzle": "残局",
    "online": "联机",
}

_REVIEW_COLORS = {
    LABEL_BEST: QColor("#2E7D4F"),
    LABEL_GOOD: QColor("#2E7D4F"),
    LABEL_INACCURATE: QColor("#8A6A1F"),
    LABEL_MISTAKE: QColor("#C0621E"),
    LABEL_BLUNDER: QColor("#B03A2E"),
}


def _coord(row: int, col: int) -> str:
    return f"{chr(ord('A') + col)}{row + 1}"


def _empty_view() -> BoardView:
    return BoardView(
        board=_EMPTY_BOARD,
        current_player=Player.BLACK,
        last_move=None,
        winner=None,
        winning_line=(),
        is_draw=False,
        is_game_over=False,
        move_count=0,
    )


def _result_text(record: GameRecord) -> str:
    if record.winner is None:
        if record.result_reason == "draw":
            return "和棋"
        if record.mode == "puzzle":
            return "未破局"
        return "未分胜负"
    name = (
        record.black_name
        if record.winner.value == "black"
        else record.white_name
    )
    return f"{name or ('黑棋' if record.winner.value == 'black' else '白棋')}胜"


def _rule_mode_suffix(record: GameRecord) -> str:
    """连珠棋谱追加规则标记；自由规则是默认值，不额外显示。"""
    return " · 连珠" if record.rule_mode == "renju" else ""


def _format_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


class ReplayPanel(InkBackdrop):
    """复盘面板；单条记录时隐藏左侧列表。"""

    #: 用户点击“关闭”时发出，由主窗口切回来源页面。
    closed = Signal()

    #: 用户点“拓此手”时发出，携带当前所在的手数。
    colophon_requested = Signal(int)

    #: 用户点“拓片册”时发出，由主窗口切到册页。
    album_requested = Signal()

    def __init__(
        self,
        records: Sequence[GameRecord] = (),
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, spec=GAME_BACKDROP_SPEC)
        self.setObjectName("replayPanel")
        self._records = tuple(records)
        self._timeline: Optional[ReplayTimeline] = None
        self._current_index = 0
        self._index = 0
        self._syncing = False
        self._closing = False
        self._controls_enabled = True
        self._shortcuts: list[QShortcut] = []
        self._review_worker: Optional[AIReviewWorker] = None
        self._review_result: Optional[ReviewResult] = None
        self._reviews_by_move: dict[int, MoveReview] = {}

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(700)
        self._play_timer.timeout.connect(self._advance_play)

        self._build_ui()
        self._create_shortcuts()
        self._populate_records()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_records(self, records: Sequence[GameRecord]) -> None:
        """载入一组棋谱并复位到第一条。"""
        self._stop_review()
        self._records = tuple(records)
        self._populate_records()

    def shutdown(self) -> None:
        """停止自动播放与 AI 复盘等后台活动。"""
        self._closing = True
        self._stop_play()
        self._stop_review(wait=True)

    # ------------------------------------------------------------------
    # 供拓片编辑器使用
    # ------------------------------------------------------------------
    @property
    def current_ply(self) -> int:
        """当前所在的手数（0 表示起始局面）。"""
        return self._index

    def current_record(self) -> Optional[GameRecord]:
        """返回当前正在复盘的棋谱；没有选中棋谱时是 ``None``。"""
        return self._current_record()

    def review_results(self) -> tuple[MoveReview, ...]:
        """返回本次复盘的逐手评定；没跑过复盘时是空元组。

        拓片编辑器靠它自动定位关键手、起草题跋——玩家跑过复盘，拓片
        就刻在真正要紧的那一手上；没跑过就退回到普通棋谱。
        """
        return tuple(self._reviews_by_move.values())

    # ------------------------------------------------------------------
    # 可见性
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._set_shortcuts_enabled(True)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().hideEvent(event)
        self._set_shortcuts_enabled(False)

    def _set_shortcuts_enabled(self, enabled: bool) -> None:
        """复盘快捷键只在面板可见时生效，避免抢走对局页的按键。"""
        for shortcut in self._shortcuts:
            shortcut.setEnabled(enabled)


    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title = QLabel("棋谱复盘", self)
        title.setObjectName("brandTitle")
        self.meta_label = QLabel("选择一条棋谱开始复盘", self)
        self.meta_label.setObjectName("brandSubtitle")
        self.meta_label.setWordWrap(True)
        self.review_status_label = QLabel("", self)
        self.review_status_label.setObjectName("hintLabel")
        self.review_status_label.setWordWrap(True)
        title_layout.addWidget(title)
        title_layout.addWidget(self.meta_label)
        title_layout.addWidget(self.review_status_label)
        header.addLayout(title_layout)
        header.addStretch(1)
        self.import_button = QPushButton("导入 SGF", self)
        self.import_button.setObjectName("smallButton")
        self.import_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.import_button.clicked.connect(self._import_sgf)
        self.export_button = QPushButton("导出 SGF", self)
        self.export_button.setObjectName("smallButton")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.clicked.connect(self._export_sgf)
        self.review_button = QPushButton("AI 复盘", self)
        self.review_button.setObjectName("smallButton")
        self.review_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.review_button.setToolTip(
            "逐手分析：推荐着法、评分损失与准确率（本地运行）"
        )
        self.review_button.clicked.connect(self._toggle_ai_review)
        self.colophon_button = QPushButton("拓此手", self)
        self.colophon_button.setObjectName("smallButton")
        self.colophon_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.colophon_button.setToolTip(
            "为当前这一手刻一张题跋拓片，存进自己的拓片册"
        )
        self.colophon_button.clicked.connect(self._emit_colophon)
        self.album_button = QPushButton("拓片册", self)
        self.album_button.setObjectName("smallButton")
        self.album_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.album_button.setToolTip("浏览已保存的题跋拓片")
        self.album_button.clicked.connect(
            lambda _checked=False: self.album_requested.emit()
        )
        self.close_button = QPushButton("关闭", self)
        self.close_button.setObjectName("undoButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(
            lambda _checked=False: self.closed.emit()
        )
        header.addWidget(
            self.import_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(
            self.export_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(
            self.review_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(
            self.colophon_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(
            self.album_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        content = QHBoxLayout()
        content.setSpacing(12)

        self.records_panel = QWidget(self)
        self.records_panel.setObjectName("replayRecordsPanel")
        self.records_panel.setMaximumWidth(220)
        self.records_panel.setMinimumWidth(140)
        records_layout = QVBoxLayout(self.records_panel)
        records_layout.setContentsMargins(0, 0, 0, 0)
        records_layout.setSpacing(6)
        records_title = QLabel("最近棋谱", self.records_panel)
        records_title.setObjectName("sectionTitle")
        self.records_list = QListWidget(self.records_panel)
        self.records_list.setObjectName("replayList")
        self.records_list.setWordWrap(True)
        self.records_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        records_layout.addWidget(records_title)
        records_layout.addWidget(self.records_list, 1)
        self.records_list.currentRowChanged.connect(self._on_record_selected)
        content.addWidget(self.records_panel, 0)

        board_column = QVBoxLayout()
        board_column.setSpacing(8)
        self.board_widget = BoardWidget(_empty_view(), self)
        self.board_widget.set_interaction_enabled(False)
        board_column.addWidget(self.board_widget, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.first_button = self._make_control("|<", "第一手", self)
        self.prev_button = self._make_control("<", "上一手", self)
        self.play_button = self._make_control("播放", "自动播放", self)
        self.play_button.setMinimumWidth(64)
        self.next_button = self._make_control(">", "下一手", self)
        self.last_button = self._make_control(">|", "最后一手", self)
        for button in (
            self.first_button,
            self.prev_button,
            self.play_button,
            self.next_button,
            self.last_button,
        ):
            controls.addWidget(button)

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setObjectName("replaySlider")
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.valueChanged.connect(self._on_slider_changed)
        controls.addWidget(self.slider, 1)

        self.index_label = QLabel("第 0 手", self)
        self.index_label.setObjectName("moveInfo")
        self.index_label.setMinimumWidth(116)
        self.index_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        controls.addWidget(self.index_label)
        board_column.addLayout(controls)

        self.move_info_label = QLabel("起始局面", self)
        self.move_info_label.setObjectName("hintLabel")
        self.move_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        board_column.addWidget(self.move_info_label)
        content.addLayout(board_column, 1)

        moves_panel = QWidget(self)
        moves_panel.setObjectName("replayMovesPanel")
        moves_panel.setMaximumWidth(220)
        # 落子列表优先于棋谱列表保住宽度：复盘评定要逐手完整显示，
        # 挤成省略号就等于没写出来。
        moves_panel.setMinimumWidth(170)
        self.moves_panel = moves_panel
        moves_layout = QVBoxLayout(moves_panel)
        moves_layout.setContentsMargins(0, 0, 0, 0)
        moves_layout.setSpacing(6)
        moves_title = QLabel("落子", moves_panel)
        moves_title.setObjectName("sectionTitle")
        self.moves_list = QListWidget(moves_panel)
        self.moves_list.setObjectName("replayMoveList")
        self.moves_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # 落子面板很窄，Qt 默认会把过长的条目省略成“…”；那样复盘评定
        # （最佳/失误、推荐着法）就会被截掉。改为自动换行且不省略，
        # 保证每一手的评定都能完整显示。
        self.moves_list.setWordWrap(True)
        self.moves_list.setTextElideMode(Qt.TextElideMode.ElideNone)
        moves_layout.addWidget(moves_title)
        moves_layout.addWidget(self.moves_list, 1)
        self.moves_list.currentRowChanged.connect(self._on_move_selected)
        content.addWidget(moves_panel, 0)

        root.addLayout(content, 1)

        self.first_button.clicked.connect(
            lambda _checked=False: self._set_index(0)
        )
        self.prev_button.clicked.connect(
            lambda _checked=False: self._set_index(self._index - 1)
        )
        self.next_button.clicked.connect(
            lambda _checked=False: self._set_index(self._index + 1)
        )
        self.last_button.clicked.connect(
            lambda _checked=False: self._set_index(self._last_index)
        )
        self.play_button.clicked.connect(
            lambda _checked=False: self._toggle_play()
        )

    def _make_control(
        self, text: str, tooltip: str, parent: QWidget
    ) -> QPushButton:
        button = QPushButton(text, parent)
        button.setObjectName("smallButton")
        button.setToolTip(tooltip)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setMinimumWidth(38)
        button.setMinimumHeight(28)
        button.setAutoDefault(False)
        button.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )
        return button

    def _create_shortcuts(self) -> None:
        for keys, handler in (
            ("Left", lambda: self._set_index(self._index - 1)),
            ("Right", lambda: self._set_index(self._index + 1)),
            ("Home", lambda: self._set_index(0)),
            ("End", lambda: self._set_index(self._last_index)),
            ("Space", self._toggle_play),
        ):
            shortcut = QShortcut(QKeySequence(keys), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(handler)
            self._shortcuts.append(shortcut)
        # 面板还没进入页面栈时先禁用，showEvent 里再打开。
        self._set_shortcuts_enabled(False)

    # ------------------------------------------------------------------
    # 记录列表
    # ------------------------------------------------------------------
    def _populate_records(self) -> None:
        self.records_list.clear()
        for index, record in enumerate(self._records):
            item = QListWidgetItem(
                f"{_mode_label(record.mode)} · {_result_text(record)}\n"
                f"{_format_time(record.created_at)} · "
                f"{len(record.moves)} 手"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            self.records_list.addItem(item)

        if len(self._records) <= 1:
            self.records_panel.setVisible(False)
        if self._records:
            self.records_list.blockSignals(True)
            self.records_list.setCurrentRow(0)
            self.records_list.blockSignals(False)
            self._load_record(0)
        else:
            self._show_empty()
            self._set_controls_enabled(False)

    def _current_record(self) -> Optional[GameRecord]:
        if not self._records:
            return None
        index = self._current_index
        if index < 0 or index >= len(self._records):
            index = 0
        return self._records[index]

    def _export_sgf(self) -> None:
        record = self._current_record()
        if record is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出 SGF 棋谱",
            f"{record.record_id}.sgf",
            "SGF 棋谱 (*.sgf);;所有文件 (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".sgf"):
            path += ".sgf"
        try:
            Path(path).write_text(
                record_to_sgf(record), encoding="utf-8"
            )
        except (OSError, SgfError) as exc:
            self.meta_label.setText(f"导出失败：{exc}")
            return
        self.meta_label.setText(f"已导出 SGF：{path}")

    def _import_sgf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "导入 SGF 棋谱",
            "",
            "SGF 棋谱 (*.sgf);;所有文件 (*)",
        )
        if not path:
            return
        try:
            text = Path(path).read_text(
                encoding="utf-8", errors="replace"
            )
            record = record_from_sgf(text)
        except (OSError, SgfError) as exc:
            self.meta_label.setText(f"导入失败：{exc}")
            return
        self._records = (record, *self._records)
        self._populate_records()
        self.meta_label.setText(f"已导入 SGF：{path}")

    def _on_record_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._records):
            return
        self._load_record(row)

    def _load_record(self, index: int) -> None:
        self._stop_play()
        self._stop_review()
        self._review_result = None
        self._reviews_by_move.clear()
        self.review_status_label.setText("")
        self.review_button.setText("AI 复盘")
        self._current_index = index
        record = self._records[index]
        self.meta_label.setText(
            f"{_mode_label(record.mode)} · {_result_text(record)} · "
            f"{_format_time(record.created_at)} · {len(record.moves)} 手"
            f"{_rule_mode_suffix(record)}"
        )
        try:
            self._timeline = ReplayTimeline.from_record(record)
        except ReplayError as exc:
            logger.warning("棋谱无法复盘：%s", exc)
            self._timeline = None
            self._show_empty(f"棋谱损坏：{exc}")
            self._set_controls_enabled(False)
            return

        last = self._last_index
        self._fill_moves_list()
        self._syncing = True
        try:
            self.slider.setRange(0, last)
        finally:
            self._syncing = False
        self._set_controls_enabled(True)
        self._set_index(self._timeline.setup_index)

    def _fill_moves_list(self) -> None:
        self.moves_list.clear()
        if self._timeline is None:
            return
        self.moves_list.addItem(QListWidgetItem("起始局面"))
        for index in range(1, self._timeline.move_count + 1):
            self.moves_list.addItem(
                QListWidgetItem(self._timeline.move_description(index))
            )

    def _show_empty(self, message: str = "暂无可复盘的棋谱") -> None:
        self.board_widget.set_view(_empty_view())
        self.index_label.setText("第 0 手")
        self.move_info_label.setText(message)
        self.slider.setRange(0, 0)
        self.moves_list.clear()
        self._review_result = None
        self._reviews_by_move.clear()
        self.review_status_label.setText("")

    # ------------------------------------------------------------------
    # 导航
    # ------------------------------------------------------------------
    @property
    def _last_index(self) -> int:
        return self._timeline.last_index if self._timeline is not None else 0

    def _set_index(self, index: int, *, from_play: bool = False) -> None:
        if self._timeline is None:
            return
        if self._play_timer.isActive() and not from_play:
            self._stop_play()
        bounded = max(0, min(int(index), self._last_index))
        self._index = bounded
        view = self._timeline.frame_at(bounded)
        self.board_widget.set_view(view)
        self.board_widget.update()

        self._syncing = True
        try:
            self.slider.setValue(bounded)
            self.moves_list.setCurrentRow(bounded)
            item = self.moves_list.item(bounded)
            if item is not None:
                self.moves_list.scrollToItem(item)
        finally:
            self._syncing = False

        self.index_label.setText(f"第 {bounded} / {self._last_index} 手")
        self._update_move_info()
        # “拓此手”的可用性跟着当前手数走：第 0 手是空局面，无从拓起。
        self.colophon_button.setEnabled(
            self._controls_enabled and bounded >= 1
        )

        self.first_button.setEnabled(bounded > 0)
        self.prev_button.setEnabled(bounded > 0)
        self.next_button.setEnabled(bounded < self._last_index)
        self.last_button.setEnabled(bounded < self._last_index)
        self.play_button.setEnabled(self._last_index > 0)

    def _update_move_info(self) -> None:
        if self._timeline is None:
            return
        bounded = self._index
        description = self._timeline.move_description(bounded)
        review = self._reviews_by_move.get(bounded)
        if review is not None:
            description += f"　·　{self._review_move_text(review)}"
        if bounded == self._last_index and self._last_index > 0:
            description += f"　·　{_result_text(self._timeline.record)}"
        self.move_info_label.setText(description)

    def _on_slider_changed(self, value: int) -> None:
        if not self._syncing:
            self._set_index(value)

    def _on_move_selected(self, row: int) -> None:
        if not self._syncing and row >= 0:
            self._set_index(row)

    def _set_controls_enabled(self, enabled: bool) -> None:
        self._controls_enabled = enabled
        for widget in (
            self.first_button,
            self.prev_button,
            self.play_button,
            self.next_button,
            self.last_button,
            self.slider,
        ):
            widget.setEnabled(enabled)
        self.export_button.setEnabled(enabled and bool(self._records))
        self.album_button.setEnabled(enabled)
        # “拓此手”至少要有一手之后才有局面可拓。
        self.colophon_button.setEnabled(
            enabled and bool(self._records) and self._index >= 1
        )
        if self._review_worker is None:
            self.review_button.setEnabled(
                enabled and bool(self._records)
            )

    def _emit_colophon(self) -> None:
        """把当前手数交给主窗口去开编辑器。"""
        record = self._current_record()
        if record is None or self._index < 1:
            return
        self.colophon_requested.emit(self._index)

    # ------------------------------------------------------------------
    # 自动播放
    # ------------------------------------------------------------------
    def _toggle_play(self) -> None:
        if self._timeline is None or self._last_index <= 0:
            return
        if self._play_timer.isActive():
            self._stop_play()
            return
        if self._index >= self._last_index:
            self._set_index(self._timeline.setup_index)
        self._play_timer.start()
        self.play_button.setText("暂停")

    def _advance_play(self) -> None:
        if self._index >= self._last_index:
            self._stop_play()
            return
        self._set_index(self._index + 1, from_play=True)

    def _stop_play(self) -> None:
        self._play_timer.stop()
        if hasattr(self, "play_button"):
            self.play_button.setText("播放")

    # ------------------------------------------------------------------
    # AI 复盘
    # ------------------------------------------------------------------
    def _toggle_ai_review(self) -> None:
        worker = self._review_worker
        if worker is not None and worker.isRunning():
            worker.cancel()
            self.review_button.setEnabled(False)
            self.review_status_label.setText("正在取消 AI 复盘…")
            return
        record = self._current_record()
        if record is None or not record.moves:
            self.review_status_label.setText("这局棋谱没有可分析的落子")
            return
        self._stop_play()
        self._stop_review()
        self._review_result = None
        self._reviews_by_move.clear()
        self.review_button.setText("取消分析")
        self.review_status_label.setText(
            f"AI 复盘 0/{len(record.moves)}…"
        )
        worker = AIReviewWorker(record, parent=self)
        worker.progress.connect(self._on_review_progress)
        worker.move_ready.connect(self._on_review_move)
        worker.review_finished.connect(self._on_review_finished)
        worker.review_failed.connect(self._on_review_failed)
        worker.review_cancelled.connect(self._on_review_cancelled)
        worker.finished.connect(worker.deleteLater)
        self._review_worker = worker
        worker.start()

    def _stop_review(self, *, wait: bool = False) -> None:
        worker = self._review_worker
        self._review_worker = None
        if worker is None:
            return
        # 先断开回调：取消后的 cancelled/finished 不应再改新棋谱的界面。
        for signal, slot in (
            (worker.progress, self._on_review_progress),
            (worker.move_ready, self._on_review_move),
            (worker.review_finished, self._on_review_finished),
            (worker.review_failed, self._on_review_failed),
            (worker.review_cancelled, self._on_review_cancelled),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        worker.cancel()
        if wait:
            worker.wait(1500)

    def _review_active_record(self) -> Optional[GameRecord]:
        record = self._current_record()
        worker = self._review_worker
        if record is None or worker is None:
            return None
        if worker.record_id != record.record_id:
            return None
        return record

    def _on_review_progress(self, done: int, total: int) -> None:
        if self._review_active_record() is None:
            return
        self.review_status_label.setText(f"AI 复盘 {done}/{total}…")

    def _on_review_move(self, move_no: int, review: object) -> None:
        if not isinstance(review, MoveReview):
            return
        if self._review_active_record() is None:
            return
        self._reviews_by_move[move_no] = review
        self._update_move_item(move_no)
        if self._index == move_no:
            self._update_move_info()

    def _on_review_finished(self, result: object) -> None:
        if not isinstance(result, ReviewResult):
            return
        record = self._current_record()
        if record is None or result.record_id != record.record_id:
            return
        self._review_worker = None
        self._review_result = result
        self._reviews_by_move = {
            item.move_no: item for item in result.reviews
        }
        self.review_button.setText("重新分析")
        self.review_button.setEnabled(True)
        self.review_status_label.setText(f"AI 复盘：{result.summary_text}")
        self._apply_review_labels()
        self._update_move_info()

    def _on_review_failed(self, message: str) -> None:
        self._review_worker = None
        self.review_button.setText("AI 复盘")
        self.review_button.setEnabled(True)
        self.review_status_label.setText(message or "AI 复盘失败")

    def _on_review_cancelled(self) -> None:
        self._review_worker = None
        self.review_button.setText("AI 复盘")
        self.review_button.setEnabled(True)
        self.review_status_label.setText("已取消 AI 复盘")

    def _apply_review_labels(self) -> None:
        if self._timeline is None:
            return
        for move_no in range(1, self._timeline.move_count + 1):
            self._update_move_item(move_no)

    def _update_move_item(self, move_no: int) -> None:
        if self._timeline is None:
            return
        item = self.moves_list.item(move_no)
        if item is None:
            return
        base = self._timeline.move_description(move_no)
        review = self._reviews_by_move.get(move_no)
        if review is None:
            item.setText(base)
            item.setForeground(QBrush())
            return
        # 评定另起一行：窄面板下换行显示，不会被省略号截断。
        item.setText(f"{base}\n{self._review_item_text(review)}")
        item.setForeground(
            QBrush(_REVIEW_COLORS.get(review.label, QColor("#3A3226")))
        )

    def _review_item_text(self, review: MoveReview) -> str:
        """复盘标注行：这一手的评定与 AI 推荐着法。"""
        if review.best_move is None:
            return f"· {review.label}"
        best = _coord(review.best_move[0], review.best_move[1])
        return f"· {review.label} · 推荐 {best}"

    def _review_move_text(self, review: MoveReview) -> str:
        if review.best_move is None:
            return "AI 无法分析这一手"
        best = _coord(review.best_move[0], review.best_move[1])
        played = _coord(review.move[0], review.move[1])
        before = int(round(review.win_rate_before * 100))
        after = int(round(review.win_rate_after * 100))
        return (
            f"AI 推荐 {best}（胜率 {before}%），实战 {played}"
            f"（{after}%），损失 {review.loss} · {review.label}"
        )

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    # shutdown() 见上方“对外接口”一节：停止自动播放，面板本身由主窗口
    # 的页面栈管理。


def _mode_label(mode: str) -> str:
    return _MODE_LABELS.get(mode, mode)


__all__ = ["ReplayPanel"]
