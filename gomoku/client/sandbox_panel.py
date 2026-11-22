"""沙盘推演面板：覆盖在联机棋盘之上的本地推演棋盘。

面板以真实联机局面为基线，允许棋手在同一个棋盘上同时扮演黑白双方，自由
推演后续变化。沙盘完全运行在客户端内存中，不会向服务器发送任何请求，
也不会修改真实对局的权威状态；面板只负责展示与交互，规则判定交给
:mod:`gomoku.client.sandbox` 中的 :class:`SandboxModel`。

面板刻意不依赖网络与会话层：外部通过 :meth:`SandboxPanel.load_position`
载入基准局面，通过 :meth:`SandboxPanel.sync_base` 同步真实对局的进展，
其余交互全部由面板自行处理。
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.board_view import BoardView
from gomoku.client.board_widget import BoardWidget
from gomoku.client.sandbox import SandboxModel, format_coordinate
from gomoku.shared.rules import BOARD_SIZE, Cell, Player

# 空棋盘快照：面板尚未载入局面时用它占位。
_EMPTY_BOARD: tuple[tuple[Cell, ...], ...] = tuple(
    tuple(None for _ in range(BOARD_SIZE)) for _ in range(BOARD_SIZE)
)


def _empty_view() -> BoardView:
    """构造一张 15×15 的空棋盘快照。"""
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


class SandboxPanel(QWidget):
    """沙盘推演面板。

    面板会被叠放到与真实棋盘相同的网格单元中，因此它必须使用不透明背景
    把真实棋盘完全遮住；背景色由样式表 ``QWidget#sandboxPanel`` 提供。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("sandboxPanel")
        # 让样式表中的背景与边框在普通 QWidget 上也能正常绘制。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        # 当前沙盘模型；未载入局面时为 None。
        self._model: Optional[SandboxModel] = None
        # 最近一次已知的真实对局局面。
        self._view: Optional[BoardView] = None
        # 真实对局是否已经推进到新的局面。
        self._base_stale = False
        # 载入基准局面时真实对局是否已经终局：终局局面照样可以推演，
        # 但状态栏要说明基线本身已经分出胜负，免得玩家误以为对局还在继续。
        self._base_finished = False
        # 刷新时抑制按钮信号，避免“同步勾选状态”触发递归。
        self._syncing = False

        self._build_ui()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    @property
    def is_loaded(self) -> bool:
        """是否已经载入过基准局面。"""
        return self._model is not None

    @property
    def base_stale(self) -> bool:
        """真实对局是否已经推进到新的局面（基准局面已过期）。"""
        return self._base_stale

    @property
    def model(self) -> Optional[SandboxModel]:
        """当前沙盘模型；未载入局面时为 None。"""
        return self._model

    def load_position(self, view: BoardView) -> None:
        """载入（或重新载入）基准局面，并清空已有的推演手数。"""
        self._view = view
        self._model = SandboxModel.from_view(view)
        self._base_stale = False
        self._base_finished = view.is_game_over
        self._refresh()

    def sync_base(self, view: BoardView) -> bool:
        """对局有新进展时更新提示文字。

        返回 True 表示真实局面已经变化（调用方可以据此提示用户重新载入）。
        """
        self._view = view
        if self._model is None:
            return False

        if view.move_count != self._model.base_move_count:
            self._base_stale = True
            self._refresh()
            return True

        # 真实局面重新回到基线手数时，过期提示不再成立。
        if self._base_stale:
            self._base_stale = False
            self._refresh()
        return False

    def clear(self) -> None:
        """清空沙盘，回到未载入状态。"""
        self._model = None
        self._view = None
        self._base_stale = False
        self._base_finished = False
        self._refresh()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel("沙盘推演", self)
        self.title_label.setObjectName("sandboxTitle")
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("sandboxStatus")
        header.addWidget(self.title_label)
        header.addWidget(self.status_label)
        header.addStretch(1)
        root.addLayout(header)

        self.board_widget = BoardWidget(_empty_view(), self)
        self.board_widget.set_interaction_enabled(True)
        self.board_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.board_widget.setMinimumSize(250, 250)
        self.board_widget.cell_clicked.connect(self._on_cell_clicked)
        root.addWidget(self.board_widget, 1)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.black_button = self._make_button(
            "黑棋", "sandboxSideButton", checkable=True
        )
        self.white_button = self._make_button(
            "白棋", "sandboxSideButton", checkable=True
        )
        self.undo_button = self._make_button("撤销", "sandboxControlButton")
        self.reset_button = self._make_button(
            "回到当前局面", "sandboxControlButton"
        )
        for button in (
            self.black_button,
            self.white_button,
            self.undo_button,
            self.reset_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        root.addLayout(controls)

        # 黑白按钮构成互斥的单选组，始终只有一个处于选中状态。
        self._side_group = QButtonGroup(self)
        self._side_group.setExclusive(True)
        self._side_group.addButton(self.black_button)
        self._side_group.addButton(self.white_button)
        self.black_button.toggled.connect(self._on_side_toggled)
        self.white_button.toggled.connect(self._on_side_toggled)

        self.undo_button.clicked.connect(self._on_undo_clicked)
        self.reset_button.clicked.connect(self._on_reset_clicked)

        # 初始化控件状态（空棋盘、黑棋先行、撤销不可用）。
        self._refresh()

    def _make_button(
        self,
        text: str,
        object_name: str,
        *,
        checkable: bool = False,
    ) -> QPushButton:
        """创建一个统一样式的按钮。"""
        button = QPushButton(text, self)
        button.setObjectName(object_name)
        button.setCheckable(checkable)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setAutoDefault(False)
        button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        return button

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _on_cell_clicked(self, row: int, col: int) -> None:
        """在沙盘上落子。"""
        if self._model is None:
            return
        # 终局或位置非法时 place 返回 False，直接忽略即可。
        if not self._model.place(row, col):
            return
        self._refresh()

    def _on_undo_clicked(self) -> None:
        """撤销最后一手推演。"""
        if self._model is None:
            return
        if not self._model.undo():
            return
        self._refresh()

    def _on_reset_clicked(self) -> None:
        """回到当前局面：丢弃全部推演，恢复真实局面基线。"""
        if self._model is None:
            return
        if self._base_stale and self._view is not None:
            # 真实对局已经推进：直接载入最新局面。
            self.load_position(self._view)
            return
        self._model.reset()
        self._base_stale = False
        self._refresh()

    def _on_side_toggled(self, checked: bool) -> None:
        """切换沙盘中的执子方。"""
        if self._syncing or not checked or self._model is None:
            return
        button = self._side_group.checkedButton()
        if button is self.black_button:
            player = Player.BLACK
        elif button is self.white_button:
            player = Player.WHITE
        else:
            return
        self._model.switch_player(player)
        self._refresh()

    # ------------------------------------------------------------------
    # 刷新
    # ------------------------------------------------------------------
    def _refresh(self) -> None:
        """把模型状态同步到棋盘与各个控件。"""
        model = self._model
        if model is None:
            self.board_widget.set_view(_empty_view())
            self.board_widget.setToolTip("")
            self.status_label.setText("")
            self.undo_button.setEnabled(False)
            self._sync_side_buttons(Player.BLACK)
            return

        self.board_widget.set_view(model.to_view())
        self.undo_button.setEnabled(model.has_sandbox_moves)
        self._sync_side_buttons(model.current_player)
        self.status_label.setText(self._status_text(model))

        last_move = model.last_move
        if last_move is None:
            self.board_widget.setToolTip("")
        else:
            self.board_widget.setToolTip(
                f"最后一手：{format_coordinate(last_move.row, last_move.col)}"
            )

    def _status_text(self, model: SandboxModel) -> str:
        """按优先级生成状态栏文字。"""
        if self._base_stale and self._view is not None:
            return (
                f"对局已更新到第 {self._view.move_count} 手，"
                "可点「回到当前局面」重新载入"
            )

        if model.is_game_over:
            if model.is_draw:
                return "棋盘已满，推演结束"
            if model.winner is not None:
                return f"{model.winner.label}五连，推演结束"

        if model.has_sandbox_moves:
            return (
                f"基于第 {model.base_move_count} 手 · "
                f"已推演 {len(model.moves)} 手 · "
                f"{model.current_player.label}方行棋"
                f"{self._finished_note()}"
            )

        return (
            f"基于第 {model.base_move_count} 手 · "
            f"{model.current_player.label}方行棋"
            f"{self._finished_note()}"
        )

    def _finished_note(self) -> str:
        """基准局面本身已经终局时的补充说明。"""
        if not self._base_finished:
            return ""
        return " · 真实对局已终局"

    def _sync_side_buttons(self, player: Player) -> None:
        """让黑白按钮的选中状态跟随当前执子方。"""
        self._syncing = True
        try:
            self.black_button.setChecked(player is Player.BLACK)
            self.white_button.setChecked(player is Player.WHITE)
        finally:
            self._syncing = False


__all__ = ["SandboxPanel"]
