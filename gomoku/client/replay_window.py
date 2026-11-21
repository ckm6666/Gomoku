"""棋谱复盘窗口：只读棋盘、逐手导航与落子列表。"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.board_view import BoardView
from gomoku.client.board_widget import BoardWidget
from gomoku.client.constants import (
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_SCREEN_MARGIN,
)
from gomoku.client.replay import ReplayError, ReplayTimeline
from gomoku.client.ui_theme import GAME_BACKDROP_SPEC, InkBackdrop
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import BOARD_SIZE, Cell, Player

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


def _format_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d %H:%M")


class ReplayWindow(QMainWindow):
    """独立复盘窗口；单条记录时隐藏左侧列表。"""

    def __init__(
        self,
        records: Sequence[GameRecord],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._records = tuple(records)
        self._timeline: Optional[ReplayTimeline] = None
        self._index = 0
        self._syncing = False
        self._closing = False

        self._play_timer = QTimer(self)
        self._play_timer.setInterval(700)
        self._play_timer.timeout.connect(self._advance_play)

        self.setWindowTitle("五子棋 - 棋谱复盘")
        self.setMinimumSize(
            max(WINDOW_MIN_WIDTH, 820), max(WINDOW_MIN_HEIGHT, 560)
        )
        self._build_ui()
        self._create_shortcuts()
        self._apply_initial_geometry()
        self._populate_records()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        central = InkBackdrop(self, spec=GAME_BACKDROP_SPEC)
        central.setObjectName("replayCentral")
        root = QVBoxLayout(central)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(10)

        header = QHBoxLayout()
        title_layout = QVBoxLayout()
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title = QLabel("棋谱复盘", central)
        title.setObjectName("brandTitle")
        self.meta_label = QLabel("选择一条棋谱开始复盘", central)
        self.meta_label.setObjectName("brandSubtitle")
        self.meta_label.setWordWrap(True)
        title_layout.addWidget(title)
        title_layout.addWidget(self.meta_label)
        header.addLayout(title_layout)
        header.addStretch(1)
        self.close_button = QPushButton("关闭", central)
        self.close_button.setObjectName("undoButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(
            lambda _checked=False: self.close()
        )
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        content = QHBoxLayout()
        content.setSpacing(12)

        self.records_panel = QWidget(central)
        self.records_panel.setObjectName("replayRecordsPanel")
        self.records_panel.setMaximumWidth(260)
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
        self.board_widget = BoardWidget(_empty_view(), central)
        self.board_widget.set_interaction_enabled(False)
        board_column.addWidget(self.board_widget, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.first_button = self._make_control("|<", "第一手", central)
        self.prev_button = self._make_control("<", "上一手", central)
        self.play_button = self._make_control("播放", "自动播放", central)
        self.play_button.setMinimumWidth(64)
        self.next_button = self._make_control(">", "下一手", central)
        self.last_button = self._make_control(">|", "最后一手", central)
        for button in (
            self.first_button,
            self.prev_button,
            self.play_button,
            self.next_button,
            self.last_button,
        ):
            controls.addWidget(button)

        self.slider = QSlider(Qt.Orientation.Horizontal, central)
        self.slider.setObjectName("replaySlider")
        self.slider.setSingleStep(1)
        self.slider.setPageStep(5)
        self.slider.valueChanged.connect(self._on_slider_changed)
        controls.addWidget(self.slider, 1)

        self.index_label = QLabel("第 0 手", central)
        self.index_label.setObjectName("moveInfo")
        self.index_label.setMinimumWidth(116)
        self.index_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        controls.addWidget(self.index_label)
        board_column.addLayout(controls)

        self.move_info_label = QLabel("起始局面", central)
        self.move_info_label.setObjectName("hintLabel")
        self.move_info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        board_column.addWidget(self.move_info_label)
        content.addLayout(board_column, 1)

        moves_panel = QWidget(central)
        moves_panel.setObjectName("replayMovesPanel")
        moves_panel.setMaximumWidth(220)
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
        moves_layout.addWidget(moves_title)
        moves_layout.addWidget(self.moves_list, 1)
        self.moves_list.currentRowChanged.connect(self._on_move_selected)
        content.addWidget(moves_panel, 0)

        root.addLayout(content, 1)
        self.setCentralWidget(central)

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

    def _apply_initial_geometry(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(1080, 820)
            return
        available = screen.availableGeometry()
        width = min(
            1080, max(820, available.width() - WINDOW_SCREEN_MARGIN)
        )
        height = min(
            820, max(560, available.height() - WINDOW_SCREEN_MARGIN)
        )
        self.resize(width, height)
        rect = self.frameGeometry()
        rect.moveCenter(available.center())
        self.move(rect.topLeft())

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

    def _on_record_selected(self, row: int) -> None:
        if row < 0 or row >= len(self._records):
            return
        self._load_record(row)

    def _load_record(self, index: int) -> None:
        self._stop_play()
        record = self._records[index]
        self.meta_label.setText(
            f"{_mode_label(record.mode)} · {_result_text(record)} · "
            f"{_format_time(record.created_at)} · {len(record.moves)} 手"
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
        description = self._timeline.move_description(bounded)
        if bounded == self._last_index and self._last_index > 0:
            description += f"　·　{_result_text(self._timeline.record)}"
        self.move_info_label.setText(description)

        self.first_button.setEnabled(bounded > 0)
        self.prev_button.setEnabled(bounded > 0)
        self.next_button.setEnabled(bounded < self._last_index)
        self.last_button.setEnabled(bounded < self._last_index)
        self.play_button.setEnabled(self._last_index > 0)

    def _on_slider_changed(self, value: int) -> None:
        if not self._syncing:
            self._set_index(value)

    def _on_move_selected(self, row: int) -> None:
        if not self._syncing and row >= 0:
            self._set_index(row)

    def _set_controls_enabled(self, enabled: bool) -> None:
        for widget in (
            self.first_button,
            self.prev_button,
            self.play_button,
            self.next_button,
            self.last_button,
            self.slider,
        ):
            widget.setEnabled(enabled)

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
    # 关闭
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        self._closing = True
        self._stop_play()
        super().closeEvent(event)


def _mode_label(mode: str) -> str:
    return _MODE_LABELS.get(mode, mode)


__all__ = ["ReplayWindow"]
