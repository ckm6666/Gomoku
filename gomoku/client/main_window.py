"""五子棋主窗口：主菜单、界面编排与对局会话切换。

对局规则、棋盘状态、AI 线程和本地悔棋语义都已下沉到
:mod:`gomoku.client.sessions`。MainWindow 只负责：

* 主菜单导航（联机/双人/人机/残局占位/退出）与对局页切换；
* 绘制/更新界面，读取 GameSession 的只读 BoardView；
* 难度、先手方的用户交互与确认；
* 在需要时暂停会话、显示终局弹窗；
* 在关闭窗口时等待会话后台任务结束。

联机对战由独立的 OnlineWindow 承载；MainWindow 只负责本地/人机窗口，
避免网络状态影响已经稳定的离线对局。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Optional

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.ai_engine import AIDifficulty
from gomoku.client.audio import get_audio_manager
from gomoku.client.board_widget import BoardWidget
from gomoku.client.config import AppConfig
from gomoku.client.confirm_dialog import ConfirmDialog
from gomoku.client.constants import (
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
    WINDOW_DEFAULT_HEIGHT,
    WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_SCREEN_MARGIN,
)
from gomoku.client.online_window import OnlineWindow
from gomoku.client.result_dialog import ResultDialog, ResultPresentation
from gomoku.client.sessions import (
    AIGameSession,
    GameSession,
    LocalGameSession,
)
from gomoku.client.sound_control import SoundButton
from gomoku.client.theme_palette import INK_FAINT, INK_MUTED
from gomoku.client.ui_theme import (
    GAME_BACKDROP_SPEC,
    MENU_BACKDROP_SPEC,
    InkBackdrop,
    RicePaperCard,
)
from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import Player

logger = logging.getLogger(__name__)


class GameMode(Enum):
    """对局模式。"""

    LOCAL = "local"
    AI = "ai"

    @property
    def label(self) -> str:
        """返回界面展示名称。"""
        return "双人对战" if self is GameMode.LOCAL else "人机对战"


class ThemedComboBox(QComboBox):
    """在自定义 QSS 下仍显示下拉箭头的组合框。

    Qt 的部分样式在完全覆盖 QComboBox 背景/边框后会隐藏原生下拉箭头。
    这里在原生绘制结束后用 QPainter 补一个轻量 V 形箭头，不依赖外部图片。
    """

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        color = (
            QColor(INK_MUTED) if self.isEnabled() else QColor(INK_FAINT)
        )
        pen = QPen(
            color,
            1.4,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
            Qt.PenJoinStyle.RoundJoin,
        )
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        center_x = self.width() - 11.0
        center_y = self.height() / 2.0
        arrow = QPainterPath(QPointF(center_x - 3.5, center_y - 1.8))
        arrow.lineTo(QPointF(center_x, center_y + 1.8))
        arrow.lineTo(QPointF(center_x + 3.5, center_y - 1.8))
        painter.drawPath(arrow)
        painter.end()


class PlayerCard(QFrame):
    """顶部棋手信息卡片。

    通过动态属性 ``active`` 切换“当前行动方”的强调样式；
    卡片文字同时显示身份（你/AI/黑棋/白棋）、先后手和对局状态。
    """

    def __init__(self, player: Player, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._player = player
        self._base_caption = "先手" if player is Player.BLACK else "后手"

        self.setObjectName("playerCard")
        self.setProperty("active", "false")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumHeight(62)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(12)

        # 用一个固定尺寸的 QLabel 充当棋子颜色徽标，不依赖图片资源。
        self._stone_badge = QLabel(self)
        badge_object_name = (
            "stoneBadgeBlack" if player is Player.BLACK else "stoneBadgeWhite"
        )
        self._stone_badge.setObjectName(badge_object_name)
        self._stone_badge.setFixedSize(26, 26)
        self._stone_badge.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        self._stone_badge.setAccessibleName(f"{player.label}标志")

        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(0)

        self._name_label = QLabel(player.label, self)
        self._name_label.setObjectName("playerName")
        self._status_label = QLabel("等待", self)
        self._status_label.setObjectName("playerCaption")

        text_layout.addWidget(self._name_label)
        text_layout.addWidget(self._status_label)

        layout.addWidget(self._stone_badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text_layout, 1)

    def set_identity(self, display_name: str, base_caption: str) -> None:
        """更新卡片身份名称和状态副标题的基础前缀。"""
        if self._name_label.text() != display_name:
            self._name_label.setText(display_name)
            self._name_label.setAccessibleName(display_name)
        self._base_caption = base_caption

    def set_state(self, active: bool, status: str) -> None:
        """更新卡片高亮与状态副标题。

        参数：
            active: 是否强调此卡片（当前行动方或胜者）。
            status: 状态文字，例如“落子中”“思考中”“获胜”。
        """
        active_value = "true" if active else "false"
        if self.property("active") != active_value:
            self.setProperty("active", active_value)
            # 动态属性变化不会自动让后代标签重新套用 QSS，需手动刷新。
            self._repolish()
            self._name_label.style().unpolish(self._name_label)
            self._name_label.style().polish(self._name_label)
            self._status_label.style().unpolish(self._status_label)
            self._status_label.style().polish(self._status_label)

        self._status_label.setText(f"{self._base_caption}，{status}")
        self._stone_badge.setAccessibleDescription(status)

    def _repolish(self) -> None:
        """强制重新应用当前卡片的 QSS 动态属性选择器。"""
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, config: Optional[AppConfig] = None) -> None:
        super().__init__()
        # 服务器地址等非敏感配置传给独立联机窗口；本地棋盘不联网。
        self.config = config or AppConfig()

        # 对局配置：默认保持本地双人，切到人机模式后默认中等难度。
        self._game_mode = GameMode.LOCAL
        self._difficulty = AIDifficulty.MEDIUM
        self._human_player = Player.BLACK

        # 弹窗/关闭/程序化设置同步的保护标志。
        self._result_dialog_open = False
        self._modal_dialog_open = False
        self._closing = False
        self._syncing_settings = False

        # 每次会话替换/撤销递增，用于丢弃过期终局弹窗定时器。
        self._game_generation = 0
        # 联机窗口独立运行，离线/人机会话不受其网络状态影响。
        self._online_window = None

        # 启动页是主菜单；仍先创建一个占位会话，确保 BoardWidget 和
        # 状态同步有可读视图。用户首次选择模式时会替换为全新会话。
        self._session: GameSession = self._make_session()

        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT)

        self._build_ui()
        self._create_shortcuts()
        self._sync_ui()
        self._apply_initial_geometry()

    # ------------------------------------------------------------------
    # 兼容访问与基础状态
    # ------------------------------------------------------------------
    @property
    def session(self) -> GameSession:
        """当前对局会话。"""
        return self._session

    @property
    def game(self) -> Optional[GameModel]:
        """兼容本地/AI 单测读取底层模型；在线会话将返回 None。"""
        return self._session.model

    @property
    def _ai_thread(self):
        """兼容原有测试/诊断：读取当前会话的 AI 线程（若有）。"""
        return self._session.ai_thread

    @property
    def _ai_player(self) -> Player:
        """人机模式中 AI 执子颜色（与玩家相反）。"""
        return self._human_player.opponent

    def _make_session(self) -> GameSession:
        """按当前模式创建本地/AI 会话并连接统一信号。"""
        if self._game_mode is GameMode.AI:
            session: GameSession = AIGameSession(
                human_player=self._human_player,
                difficulty=self._difficulty,
            )
        else:
            session = LocalGameSession()

        session.state_changed.connect(self._on_session_state_changed)
        session.stone_placed.connect(self._on_session_stone_placed)
        session.game_over.connect(self._on_session_game_over)
        session.error.connect(self._on_session_error)
        return session

    def _replace_session(self) -> None:
        """结束旧会话，按当前设置创建全新的干净会话。

        模式/先手变更和重新开局都通过创建新会话实现；这样在线会话未来可以
        用服务器快照初始化，而不需要把“重置”硬编码成清空本地 GameModel。
        """
        old_session = self._session
        old_session.shutdown()

        self._session = self._make_session()
        self._game_generation += 1
        self._sync_ui()

        # 模式/先手/重新开局都等于开始一局新棋。
        self._play_start_sound()

        # 旧会话的线程已经结束，但保留 deleteLater 让 Qt 在事件循环中回收。
        old_session.deleteLater()

    def _human_can_play(self) -> bool:
        """当前是否允许玩家通过棋盘落子。

        这是 UI 侧唯一的人类输入开关，具体会话负责判断回合、暂停和终局。
        """
        if self._closing or self._modal_dialog_open or self._result_dialog_open:
            return False
        return self._session.accepts_human_input

    def _can_undo(self) -> bool:
        """当前悔棋按钮是否应有意义。"""
        return self._session.can_undo

    def _is_ai_turn(self) -> bool:
        """当前是否为人机模式且轮到 AI；供兼容查询和界面状态使用。"""
        return self._game_mode is GameMode.AI and self._session.busy

    # ------------------------------------------------------------------
    # 会话信号
    # ------------------------------------------------------------------
    def _on_session_state_changed(self) -> None:
        if self.sender() is not self._session or self._closing:
            return
        self._sync_ui()

    def _on_session_stone_placed(self) -> None:
        """会话确认落子后播放音效；悔棋和非法输入不会触发该信号。"""
        if self.sender() is not self._session or self._closing:
            return
        get_audio_manager().play_move()

    def _on_session_game_over(self) -> None:
        if self.sender() is not self._session or self._closing:
            return
        self._play_game_over_sound()
        self._schedule_result_dialog()

    def _play_start_sound(self) -> None:
        if not self._closing:
            get_audio_manager().play_start()

    def _play_game_over_sound(self) -> None:
        """按模式与胜者选择胜利/失败/平局音效。"""
        view = self._session.view
        audio = get_audio_manager()
        if view.winner is None:
            audio.play_draw()
        elif (
            self._game_mode is GameMode.AI
            and view.winner is not self._human_player
        ):
            audio.play_lose()
        else:
            # 本地双人没有“失败”视角：任一方获胜都播放胜利音。
            audio.play_win()

    def _on_session_error(self, message: str) -> None:
        if self.sender() is not self._session:
            return
        # 网络/AI 内部错误不直接弹模态框，避免阻塞在线事件；记录并保留
        # 会话自身的恢复路径（AI 会话已经选好兜底落点）。
        logger.warning("会话错误：%s", message)

    # ------------------------------------------------------------------
    # 界面搭建
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """创建主菜单/对局两页堆栈，应用启动时显示主菜单。"""
        central_widget = QWidget(self)
        central_widget.setObjectName("centralWidget")
        central_widget.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )

        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.stack = QStackedWidget(central_widget)
        self.stack.setObjectName("mainStack")
        self.menu_page = self._build_menu_page()
        self.game_page = self._build_game_page()
        self.stack.addWidget(self.menu_page)
        self.stack.addWidget(self.game_page)
        self.stack.setCurrentWidget(self.menu_page)

        root_layout.addWidget(self.stack)
        self.setCentralWidget(central_widget)

    def _build_menu_page(self) -> QWidget:
        """构建居中卡片式主菜单。"""
        page = InkBackdrop(self.stack, spec=MENU_BACKDROP_SPEC)
        page.setObjectName("menuPage")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)

        utility_row = QHBoxLayout()
        utility_row.setContentsMargins(0, 0, 2, 0)
        utility_row.addStretch(1)
        self.menu_sound_button = SoundButton(page)
        utility_row.addWidget(
            self.menu_sound_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        layout.addLayout(utility_row)
        layout.addStretch(1)

        self.menu_card = RicePaperCard(page, seal_text="弈")
        self.menu_card.setObjectName("menuCard")
        self.menu_card.setFixedWidth(430)

        card_layout = QVBoxLayout(self.menu_card)
        card_layout.setContentsMargins(34, 28, 34, 28)
        card_layout.setSpacing(8)

        title = QLabel(APP_NAME, self.menu_card)
        title.setObjectName("menuTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        subtitle = QLabel("双人对弈、人机过招、联机排位", self.menu_card)
        subtitle.setObjectName("menuSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setWordWrap(True)

        footnote = QLabel(
            "落子无悔，十五路纵横，连成五子即胜", self.menu_card
        )
        footnote.setObjectName("menuFootnote")
        footnote.setAlignment(Qt.AlignmentFlag.AlignCenter)

        card_layout.addWidget(title)
        card_layout.addWidget(subtitle)
        card_layout.addWidget(footnote)
        card_layout.addSpacing(10)

        self.online_button = self._make_menu_button(
            "联机对战",
            "menuPrimaryButton",
            "登录后参加排位匹配或好友房",
            self._open_online_window,
        )
        self.local_button = self._make_menu_button(
            "双人对战",
            "menuButton",
            "同一台电脑双人轮流落子",
            self.start_local_game,
        )
        self.ai_button = self._make_menu_button(
            "人机对战",
            "menuButton",
            "与三档本地 AI 对弈，可在对局中调整难度与先手",
            self.start_ai_game,
        )
        self.puzzle_button = self._make_menu_button(
            "残局挑战",
            "menuButton",
            "残局挑战即将上线",
            self._show_puzzle_placeholder,
        )
        self.exit_button = self._make_menu_button(
            "退出游戏",
            "menuQuietButton",
            "退出游戏",
            self._exit_game,
        )

        for button in (
            self.online_button,
            self.local_button,
            self.ai_button,
            self.puzzle_button,
        ):
            card_layout.addWidget(button)
        card_layout.addSpacing(4)
        card_layout.addWidget(self.exit_button)

        layout.addWidget(
            self.menu_card, 0, Qt.AlignmentFlag.AlignHCenter
        )
        layout.addStretch(2)

        version_label = QLabel(f"版本 {APP_VERSION}", page)
        version_label.setObjectName("menuVersion")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)
        return page

    def _make_menu_button(
        self,
        text: str,
        object_name: str,
        tooltip: str,
        handler: Callable[[], None],
    ) -> QPushButton:
        """创建一个主菜单大按钮。"""
        button = QPushButton(text, self.menu_card)
        button.setObjectName(object_name)
        button.setToolTip(tooltip)
        button.setAccessibleName(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setMinimumHeight(46)
        button.setAutoDefault(False)
        button.clicked.connect(handler)
        return button

    def _build_game_page(self) -> QWidget:
        """构建原有的对局界面。"""
        page = InkBackdrop(self.stack, spec=GAME_BACKDROP_SPEC)
        page.setObjectName("gamePage")

        root_layout = QVBoxLayout(page)
        root_layout.setContentsMargins(24, 18, 24, 18)
        root_layout.setSpacing(14)

        root_layout.addLayout(self._build_header())
        root_layout.addWidget(self._build_settings_bar())
        root_layout.addLayout(self._build_player_cards())

        self.board_widget = BoardWidget(self._session.view, page)
        self.board_widget.cell_clicked.connect(self._on_cell_clicked)
        root_layout.addWidget(self.board_widget, 1)

        root_layout.addLayout(self._build_footer())
        return page

    def _build_header(self) -> QHBoxLayout:
        """构建品牌标题、副标题与当前对局状态胶囊。"""
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(2, 0, 2, 0)
        header_layout.setSpacing(12)

        brand_layout = QVBoxLayout()
        brand_layout.setContentsMargins(0, 0, 0, 0)
        brand_layout.setSpacing(0)

        title_label = QLabel(APP_NAME, self)
        title_label.setObjectName("brandTitle")
        subtitle_label = QLabel(
            "双人 / 人机 / 联机排位，连成五子即胜", self
        )
        subtitle_label.setObjectName("brandSubtitle")

        brand_layout.addWidget(title_label)
        brand_layout.addWidget(subtitle_label)

        self.turn_pill = QLabel("黑棋回合", self)
        self.turn_pill.setObjectName("turnPill")
        self.turn_pill.setProperty("result", "false")
        self.turn_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.turn_pill.setMinimumWidth(124)
        self.turn_pill.setSizePolicy(
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Fixed,
        )
        self.turn_pill.setAccessibleName("当前对局状态")

        header_layout.addLayout(brand_layout)
        header_layout.addStretch(1)
        header_layout.addWidget(self.turn_pill, 0, Qt.AlignmentFlag.AlignVCenter)
        return header_layout

    def _build_settings_bar(self) -> QFrame:
        """构建难度/先手方的紧凑设置栏。"""
        settings_bar = QFrame(self)
        settings_bar.setObjectName("settingsBar")
        settings_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        settings_bar.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Fixed,
        )

        bar_layout = QHBoxLayout(settings_bar)
        bar_layout.setContentsMargins(12, 6, 12, 6)
        bar_layout.setSpacing(14)

        self.difficulty_combo = ThemedComboBox(settings_bar)
        self.difficulty_combo.setObjectName("difficultyCombo")
        self.difficulty_combo.setAccessibleName("AI 难度")
        self.difficulty_combo.setToolTip("人机模式下 AI 的棋力与搜索强度")
        for difficulty in AIDifficulty:
            self.difficulty_combo.addItem(difficulty.label, difficulty)
        self.difficulty_combo.setMinimumWidth(78)

        self.first_combo = ThemedComboBox(settings_bar)
        self.first_combo.setObjectName("firstPlayerCombo")
        self.first_combo.setAccessibleName("先手方")
        self.first_combo.setToolTip("玩家先手执黑；AI 先手时玩家执白")
        self.first_combo.addItem("玩家先手", Player.BLACK)
        self.first_combo.addItem("AI 先手", Player.WHITE)
        self.first_combo.setMinimumWidth(94)

        for combo in (self.difficulty_combo, self.first_combo):
            combo.setCursor(Qt.CursorShape.PointingHandCursor)
            combo.setMinimumHeight(28)

        self.difficulty_combo.currentIndexChanged.connect(
            self._on_difficulty_combo_changed
        )
        self.first_combo.currentIndexChanged.connect(
            self._on_first_combo_changed
        )

        self.difficulty_group = self._make_settings_group(
            "AI 难度", self.difficulty_combo
        )
        self.first_group = self._make_settings_group(
            "先手方", self.first_combo
        )
        self.local_mode_label = QLabel("双人对战，轮流落子", settings_bar)
        self.local_mode_label.setObjectName("settingsLabel")

        bar_layout.addWidget(self.difficulty_group)
        bar_layout.addWidget(self.first_group)
        bar_layout.addWidget(self.local_mode_label)
        bar_layout.addStretch(1)

        # 声音设置与模式设置同栏，不参与 AI 回合的锁定逻辑。
        self.sound_button = SoundButton(settings_bar)
        bar_layout.addWidget(
            self.sound_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        return settings_bar

    def _make_settings_group(
        self, title: str, combo: QComboBox
    ) -> QWidget:
        """创建一个“标签 + 组合框”的设置组，便于整组显示/隐藏。"""
        container = QWidget(self)
        group = QHBoxLayout(container)
        group.setContentsMargins(0, 0, 0, 0)
        group.setSpacing(6)

        label = QLabel(title, container)
        label.setObjectName("settingsLabel")
        label.setBuddy(combo)

        group.addWidget(label)
        group.addWidget(combo)
        return container

    def _build_player_cards(self) -> QHBoxLayout:
        """构建黑棋和白棋两张信息卡片。"""
        cards_layout = QHBoxLayout()
        cards_layout.setContentsMargins(0, 0, 0, 0)
        cards_layout.setSpacing(12)

        self.black_card = PlayerCard(Player.BLACK, self)
        self.white_card = PlayerCard(Player.WHITE, self)
        cards_layout.addWidget(self.black_card, 1)
        cards_layout.addWidget(self.white_card, 1)
        return cards_layout

    def _build_footer(self) -> QHBoxLayout:
        """构建手数、快捷键提示以及底部操作按钮。"""
        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(2, 0, 2, 0)
        footer_layout.setSpacing(10)

        info_layout = QVBoxLayout()
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(1)

        self.move_label = QLabel("第 0 手", self)
        self.move_label.setObjectName("moveInfo")
        hint_label = QLabel("Ctrl+Z 悔棋    Ctrl+N 重新开局", self)
        hint_label.setObjectName("hintLabel")

        info_layout.addWidget(self.move_label)
        info_layout.addWidget(hint_label)

        self.menu_back_button = QPushButton("返回主菜单", self)
        self.menu_back_button.setObjectName("menuBackButton")
        self.menu_back_button.setToolTip("结束当前棋局并返回主菜单")
        self.menu_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_back_button.setMinimumWidth(104)
        self.menu_back_button.setMinimumHeight(38)
        self.menu_back_button.setAutoDefault(False)
        self.menu_back_button.clicked.connect(self.show_main_menu)

        self.undo_button = QPushButton("悔棋", self)
        self.undo_button.setObjectName("undoButton")
        self.undo_button.setToolTip("撤销上一手（Ctrl+Z）")
        self.undo_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.undo_button.setMinimumWidth(84)
        self.undo_button.setMinimumHeight(38)
        self.undo_button.setAutoDefault(False)

        self.restart_button = QPushButton("重新开局", self)
        self.restart_button.setObjectName("restartButton")
        self.restart_button.setToolTip("清空棋盘并重新开始（Ctrl+N）")
        self.restart_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.restart_button.setMinimumWidth(102)
        self.restart_button.setMinimumHeight(38)
        self.restart_button.setAutoDefault(False)

        self.undo_button.clicked.connect(self._undo_move)
        self.restart_button.clicked.connect(self._request_restart)

        footer_layout.addLayout(info_layout)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.menu_back_button)
        footer_layout.addWidget(self.undo_button)
        footer_layout.addWidget(self.restart_button)
        return footer_layout

    def _create_shortcuts(self) -> None:
        """注册全局对局快捷键。"""
        self._undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), self)
        self._undo_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._undo_shortcut.activated.connect(self._undo_move)

        self._restart_shortcut = QShortcut(QKeySequence("Ctrl+N"), self)
        self._restart_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self._restart_shortcut.activated.connect(self._request_restart)

        # 启动页是主菜单，对局快捷键先禁用。
        self._set_game_shortcuts_enabled(False)

    # ------------------------------------------------------------------
    # 主菜单导航
    # ------------------------------------------------------------------
    def start_local_game(self) -> None:
        """从主菜单开始一局本地双人对战。"""
        self._start_game(GameMode.LOCAL)

    def start_ai_game(self) -> None:
        """从主菜单开始一局人机对战（默认中等、玩家先手）。"""
        self._difficulty = AIDifficulty.MEDIUM
        self._human_player = Player.BLACK
        self._start_game(GameMode.AI)

    def _start_game(self, mode: GameMode) -> None:
        """切换到对局页并创建全新会话。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
        ):
            return
        self._game_mode = mode
        # _replace_session 负责 shutdown 旧会话、同步界面和播放开局音。
        self._replace_session()
        self.stack.setCurrentWidget(self.game_page)
        self._set_game_shortcuts_enabled(True)

    def show_main_menu(self) -> None:
        """返回主菜单；进行中的棋局需要确认放弃。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
            or not self._is_game_page_active()
        ):
            return

        # 先暂停，确保确认框打开期间 AI/网络结果不会改变棋盘。
        self._session.pause()
        if self._session.view.move_count > 0:
            if not self._confirm_discard_game(
                "返回主菜单",
                "返回主菜单将结束当前棋局，确定要返回吗？",
                confirm_text="返回主菜单",
            ):
                self._session.resume()
                self._sync_ui()
                return

        # 使尚未执行的终局弹窗定时器失效。
        self._game_generation += 1
        self.stack.setCurrentWidget(self.menu_page)
        self._set_game_shortcuts_enabled(False)
        self.online_button.setFocus()

    def _show_puzzle_placeholder(self) -> None:
        """残局挑战入口占位提示。"""
        if self._closing or self._modal_dialog_open:
            return
        self._modal_dialog_open = True
        try:
            box = QMessageBox(self)
            box.setWindowTitle("残局挑战")
            box.setIcon(QMessageBox.Icon.Information)
            box.setTextFormat(Qt.TextFormat.PlainText)
            box.setText("残局挑战即将上线，敬请期待。")
            ok_button = box.addButton(
                "知道了", QMessageBox.ButtonRole.AcceptRole
            )
            box.setDefaultButton(ok_button)
            box.exec()
        finally:
            self._modal_dialog_open = False

    def _exit_game(self) -> None:
        """退出应用；closeEvent 负责关闭联机窗口与后台线程。"""
        if not self._closing:
            self.close()

    def _is_game_page_active(self) -> bool:
        """当前是否停留在对局页。"""
        return self.stack.currentWidget() is self.game_page

    def _set_game_shortcuts_enabled(self, enabled: bool) -> None:
        """菜单页禁用 Ctrl+Z/Ctrl+N，进入对局后恢复。"""
        self._undo_shortcut.setEnabled(enabled)
        self._restart_shortcut.setEnabled(enabled)

    def _open_online_window(self) -> None:
        """打开独立联机窗口；离线棋盘和 AI 状态不受影响。"""
        try:
            if self._online_window is None:
                window = OnlineWindow(self.config)
                window.setAttribute(
                    Qt.WidgetAttribute.WA_DeleteOnClose, True
                )
                window.destroyed.connect(self._on_online_window_destroyed)
                self._online_window = window
            self._online_window.show()
            self._online_window.raise_()
            self._online_window.activateWindow()
        except Exception as exc:  # 依赖/窗口初始化失败时不要让离线应用崩溃
            logger.exception("打开联机窗口失败")
            QMessageBox.warning(
                self,
                "无法打开联机对战",
                f"联机窗口初始化失败：{type(exc).__name__}",
            )

    def _on_online_window_destroyed(self, *_args) -> None:
        self._online_window = None

    def _apply_initial_geometry(self) -> None:
        """根据主屏可用区域设置初始大小并居中，兼容小屏幕和高 DPI。"""
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(WINDOW_DEFAULT_WIDTH, WINDOW_DEFAULT_HEIGHT)
            return

        available = screen.availableGeometry()
        width = min(
            WINDOW_DEFAULT_WIDTH,
            max(WINDOW_MIN_WIDTH, available.width() - WINDOW_SCREEN_MARGIN),
        )
        height = min(
            WINDOW_DEFAULT_HEIGHT,
            max(WINDOW_MIN_HEIGHT, available.height() - WINDOW_SCREEN_MARGIN),
        )
        self.resize(width, height)

        window_rect = self.frameGeometry()
        window_rect.moveCenter(available.center())
        self.move(window_rect.topLeft())

    # ------------------------------------------------------------------
    # 设置栏交互
    # ------------------------------------------------------------------
    @staticmethod
    def _combo_index_for_data(combo: QComboBox, data) -> int:
        """在组合框中找到指定 itemData 的索引。"""
        for index in range(combo.count()):
            if combo.itemData(index) is data:
                return index
        return 0

    def _sync_settings_controls(self) -> None:
        """让设置栏显示内部状态，并在 AI 回合禁用设置。"""
        self._syncing_settings = True
        try:
            self.difficulty_combo.setCurrentIndex(
                self._combo_index_for_data(
                    self.difficulty_combo, self._difficulty
                )
            )
            self.first_combo.setCurrentIndex(
                self._combo_index_for_data(
                    self.first_combo, self._human_player
                )
            )

            settings_enabled = not self._closing and not self._session.busy
            ai_mode = self._game_mode is GameMode.AI

            # 双人模式不展示 AI 难度/先手选择，保持设置栏简洁。
            self.difficulty_group.setVisible(ai_mode)
            self.first_group.setVisible(ai_mode)
            self.local_mode_label.setVisible(not ai_mode)

            self.difficulty_combo.setEnabled(settings_enabled and ai_mode)
            self.first_combo.setEnabled(settings_enabled and ai_mode)
        finally:
            self._syncing_settings = False

    def _on_first_combo_changed(self, index: int) -> None:
        """先手方切换：有落子时确认后重开，新设置决定谁执黑。"""
        if self._syncing_settings:
            return

        desired = self.first_combo.itemData(index)
        if not isinstance(desired, Player) or desired is self._human_player:
            return

        if self._game_mode is not GameMode.AI or self._session.busy:
            self._sync_settings_controls()
            return

        if self._session.view.move_count > 0:
            if not self._confirm_discard_game(
                "更改先手方",
                "更改先手方将清空当前棋局，确定要开始新对局吗？",
            ):
                self._sync_settings_controls()
                self._sync_ui()
                return

        self._human_player = desired
        self._replace_session()

    def _on_difficulty_combo_changed(self, index: int) -> None:
        """难度变更即时保存，从 AI 下一手开始生效，不重置当前棋局。"""
        if self._syncing_settings:
            return

        desired = self.difficulty_combo.itemData(index)
        if not isinstance(desired, AIDifficulty):
            return
        if self._game_mode is not GameMode.AI or self._session.busy:
            self._sync_settings_controls()
            return

        self._difficulty = desired
        self._session.set_difficulty(desired)
        self._sync_ui()

    # ------------------------------------------------------------------
    # 棋盘与对局提交
    # ------------------------------------------------------------------
    def _on_cell_clicked(self, row: int, col: int) -> None:
        """人类玩家点击棋盘时先做来源校验，再交给会话处理。"""
        if not self._is_game_page_active() or not self._human_can_play():
            return
        self._session.request_move(row, col)

    def _undo_move(self) -> None:
        """悔棋；具体撤回范围和 AI 取消由当前会话实现。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
            or not self._is_game_page_active()
            or not self._session.can_undo
        ):
            return

        if self._session.request_undo():
            self._game_generation += 1
            self._sync_ui()

    def _request_restart(self) -> None:
        """重新开局；已有落子时确认，并安全暂停/替换会话。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
            or not self._is_game_page_active()
        ):
            return

        if self._session.view.move_count > 0:
            # 暂停会话会在确认框的嵌套事件循环中阻止 AI/网络结果改变棋盘。
            self._session.pause()
            if not self._confirm_discard_game(
                "重新开局",
                "当前棋局将被清空，确定要重新开局吗？",
            ):
                self._session.resume()
                self._sync_ui()
                return

        self._replace_session()

    def _reset_game(self) -> None:
        """按当前模式/难度/先手方创建全新会话。"""
        self._replace_session()

    # ------------------------------------------------------------------
    # 终局弹窗
    # ------------------------------------------------------------------
    def _schedule_result_dialog(self) -> None:
        """用棋局代次安排终局弹窗，让棋盘先完成一次重绘。"""
        generation = self._game_generation
        QTimer.singleShot(
            0, lambda: self._show_result_dialog(generation)
        )

    def _show_result_dialog(self, generation: Optional[int] = None) -> None:
        """显示中文胜负/平局弹窗，并提供再来一局或查看棋盘。"""
        if self._closing or self._result_dialog_open:
            return
        if not self._is_game_page_active():
            return
        if generation is not None and generation != self._game_generation:
            return
        if not self._session.view.is_game_over:
            return
        if self._modal_dialog_open:
            # 返回主菜单/重新开局的确认框仍在，关闭后再展示终局结果。
            QTimer.singleShot(
                100, lambda: self._show_result_dialog(generation)
            )
            return

        view = self._session.view
        self._result_dialog_open = True
        self._modal_dialog_open = True
        restart = False
        try:
            presentation = self._build_result_presentation(
                view.winner, view.move_count
            )
            result_dialog = ResultDialog(presentation, self)
            result_dialog.exec()
            restart = result_dialog.restart_requested
        finally:
            self._result_dialog_open = False
            self._modal_dialog_open = False

        if restart:
            self._reset_game()

    def _build_result_presentation(
        self, winner: Optional[Player], move_count: int
    ) -> ResultPresentation:
        """根据模式与胜者生成古风终局文案。"""
        if winner is None:
            return ResultPresentation(
                kind="draw",
                title="和棋",
                subtitle="棋盘已满，胜负未分",
                detail=f"本局共 {move_count} 手",
                seal_text="和",
            )

        if self._game_mode is GameMode.AI:
            if winner is self._human_player:
                return ResultPresentation(
                    kind="win",
                    title="胜利",
                    subtitle=f"你获胜 / AI（{self._difficulty.label}）",
                    detail=f"本局共 {move_count} 手",
                    seal_text="胜",
                )
            return ResultPresentation(
                kind="lose",
                title="惜败",
                subtitle=f"AI（{self._difficulty.label}）获胜",
                detail=f"本局共 {move_count} 手，胜负乃兵家常事",
                seal_text="负",
            )

        return ResultPresentation(
            kind="win",
            title="胜局",
            subtitle=f"{winner.label}获胜，五子连珠",
            detail=f"本局共 {move_count} 手",
            seal_text="胜",
        )

    def _confirm_discard_game(
        self,
        title: str,
        message: str,
        *,
        confirm_text: str = "开始新对局",
    ) -> bool:
        """显示一个古风模态确认框，返回用户是否确认。"""
        self._modal_dialog_open = True
        try:
            dialog = ConfirmDialog(
                title,
                message,
                confirm_text=confirm_text,
                parent=self,
            )
            dialog.exec()
            return dialog.confirm_requested
        finally:
            self._modal_dialog_open = False

    # ------------------------------------------------------------------
    # 状态同步
    # ------------------------------------------------------------------
    def _sync_ui(self) -> None:
        """把会话只读视图、模式和 AI 忙碌状态同步到界面。"""
        view = self._session.view
        winner = view.winner
        game_over = view.is_game_over
        current_player = view.current_player

        for player, card in (
            (Player.BLACK, self.black_card),
            (Player.WHITE, self.white_card),
        ):
            display_name, base_caption = self._card_identity(player)
            card.set_identity(display_name, base_caption)

            if winner is not None:
                active = winner is player
                status = "获胜" if active else "本局结束"
            elif view.is_draw:
                active = False
                status = "平局"
            elif self._game_mode is GameMode.AI:
                active = current_player is player
                if player is self._ai_player:
                    status = "思考中" if active else "等待"
                else:
                    status = "落子中" if active else "等待"
            else:
                active = current_player is player
                status = "落子中" if active else "等待"

            card.set_state(active, status)

        if winner is not None:
            if self._game_mode is GameMode.AI:
                self.turn_pill.setText(
                    "你获胜" if winner is self._human_player else "AI 获胜"
                )
            else:
                self.turn_pill.setText(f"{winner.label}获胜")
        elif view.is_draw:
            self.turn_pill.setText("本局平局")
        elif self._game_mode is GameMode.AI:
            if current_player is self._human_player:
                self.turn_pill.setText("你的回合")
            else:
                self.turn_pill.setText("AI 思考中…")
        else:
            self.turn_pill.setText(f"{current_player.label}回合")

        self._set_result_style(game_over)
        self.move_label.setText(f"第 {view.move_count} 手")
        self.undo_button.setEnabled(self._session.can_undo)
        self.undo_button.setToolTip(
            "撤销上一手（Ctrl+Z）"
            if self._game_mode is GameMode.LOCAL
            else "撤销到你的上一次落子前（Ctrl+Z）"
        )

        # BoardWidget 只消费快照；输入开关由会话能力和模态状态共同决定。
        self.board_widget.set_view(view)
        self.board_widget.set_interaction_enabled(self._human_can_play())
        self.board_widget.update()
        self._sync_settings_controls()

    def _card_identity(self, player: Player) -> tuple[str, str]:
        """根据模式和执子分配生成卡片标题/基础副标题。"""
        order = "先手" if player is Player.BLACK else "后手"
        if self._game_mode is GameMode.LOCAL:
            return player.label, order

        color = "黑" if player is Player.BLACK else "白"
        base_caption = f"执{color}，{order}"
        if player is self._human_player:
            return "你", base_caption
        return f"AI（{self._difficulty.label}）", base_caption

    def _set_result_style(self, is_result: bool) -> None:
        """切换状态胶囊的终局配色。"""
        result_value = "true" if is_result else "false"
        if self.turn_pill.property("result") == result_value:
            return

        self.turn_pill.setProperty("result", result_value)
        self.turn_pill.style().unpolish(self.turn_pill)
        self.turn_pill.style().polish(self.turn_pill)
        self.turn_pill.update()

    # ------------------------------------------------------------------
    # 关闭保护
    # ------------------------------------------------------------------
    def closeEvent(self, event: QCloseEvent) -> None:
        """关闭窗口前停止 AI 会话与联机窗口。"""
        self._closing = True
        if self._online_window is not None:
            try:
                self._online_window.close()
            except RuntimeError:
                self._online_window = None
        self._session.shutdown()
        super().closeEvent(event)
