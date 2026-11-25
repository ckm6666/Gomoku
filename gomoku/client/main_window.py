"""五子棋主窗口：主菜单、界面编排与对局会话切换。

对局规则、棋盘状态、AI 线程和本地悔棋语义都已下沉到
:mod:`gomoku.client.sessions`。MainWindow 只负责：

* 主菜单导航（联机/双人/人机/残局/棋谱/退出）与页面切换；
* 绘制/更新界面，读取 GameSession 的只读 BoardView；
* 难度、先手方的用户交互与确认；
* 在需要时暂停会话、显示终局弹窗；
* 在关闭窗口时等待会话后台任务结束。

联机对战由同一窗口内的 OnlinePanel 承载；MainWindow 负责本地/人机页面，
避免网络状态影响已经稳定的离线对局。

设计原则：应用全局只保留一个原生窗口。联机大厅、在线棋盘、棋谱复盘
都是页面栈中的一页，弹窗则是覆盖在中央部件之上的模态层。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QCloseEvent,
    QDesktopServices,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.ai_engine import AIDifficulty
from gomoku.client.audio import get_audio_manager
from gomoku.client import auto_update
from gomoku.client.board_widget import BoardWidget
from gomoku.client.config import AppConfig
from gomoku.client.confirm_dialog import ConfirmDialog
from gomoku.client.font_control import FontSizeButton
from gomoku.client.constants import (
    APP_NAME,
    APP_TITLE,
    APP_VERSION,
    GITHUB_RELEASES_PAGE_URL,
    GITHUB_RELEASE_API_URL,
    WINDOW_DEFAULT_HEIGHT,
    WINDOW_DEFAULT_WIDTH,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    WINDOW_SCREEN_MARGIN,
)
from gomoku.client.modal_overlay import ModalOverlay, NoticeOverlay
from gomoku.client.net.update_api import UpdateHTTPClient
from gomoku.client.net.update_download import UpdateDownloadClient
from gomoku.client.online_panel import OnlinePanel
from gomoku.client.puzzle_bank import load_puzzle_bank
from gomoku.client.puzzle_page import PuzzlePage
from gomoku.client.records import LocalRecordStore
from gomoku.client.replay_panel import ReplayPanel
from gomoku.client.result_dialog import ResultDialog, ResultPresentation
from gomoku.client.sessions import (
    AIGameSession,
    GameSession,
    LocalGameSession,
    PuzzleSession,
)
from gomoku.client.sound_control import SoundButton
from gomoku.client.ui_theme import (
    GAME_BACKDROP_SPEC,
    MENU_BACKDROP_SPEC,
    InkBackdrop,
    PaperComboBox,
    RicePaperCard,
)
from gomoku.client.update_dialog import (
    UpdateAvailableDialog,
    UpdateProgressDialog,
)
from gomoku.shared.game_model import GameModel
from gomoku.shared.puzzle import Puzzle
from gomoku.shared.record import GameRecord
from gomoku.shared.rules import Player
from gomoku.shared.update_check import (
    ReleaseInfo,
    is_newer_version,
    parse_version,
    select_windows_asset,
    summarize_notes,
)

logger = logging.getLogger(__name__)


class GameMode(Enum):
    """对局模式。"""

    LOCAL = "local"
    AI = "ai"
    PUZZLE = "puzzle"

    @property
    def label(self) -> str:
        """返回界面展示名称。"""
        return {
            GameMode.LOCAL: "双人对战",
            GameMode.AI: "人机对战",
            GameMode.PUZZLE: "残局挑战",
        }[self]


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


class TransientNotice(QWidget):
    """窗口底部的非阻塞提示条。

    用于“本地棋谱没能保存”这类必须告知、但不该打断当前操作的提示：
    它不进入模态状态、不拦截鼠标事件，几秒后自动消失。相比模态弹窗，
    它不会把终局结果挡在后面。
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("transientNotice")
        self.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True
        )
        self.setStyleSheet(
            "background-color: rgba(26, 22, 17, 225);"
            "border: 1px solid rgba(212, 175, 55, 180);"
            "border-radius: 10px;"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 8)
        self.label = QLabel("", self)
        self.label.setObjectName("transientNoticeLabel")
        self.label.setStyleSheet(
            "color: #f6ead6; background: transparent; border: none;"
        )
        layout.addWidget(self.label)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()

    def show_message(self, text: str, *, timeout_ms: int = 5000) -> None:
        """显示一条提示，并在若干毫秒后自动隐藏。"""
        self.label.setText(text)
        self._reposition()
        self.show()
        self.raise_()
        self._timer.start(timeout_ms)

    def _reposition(self) -> None:
        """把提示条贴在宿主底部居中。"""
        host = self.parentWidget()
        if host is None:
            return
        label_hint = self.label.sizeHint()
        width = min(
            max(label_hint.width() + 36, 280),
            max(host.width() - 32, 280),
        )
        height = label_hint.height() + 18
        x = max(0, (host.width() - width) // 2)
        y = max(0, host.height() - height - 24)
        self.setGeometry(x, y, width, height)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(
        self,
        config: Optional[AppConfig] = None,
        *,
        puzzles: Optional[Sequence[Puzzle]] = None,
        record_store: Optional[LocalRecordStore] = None,
    ) -> None:
        super().__init__()
        # 服务器地址等非敏感配置传给联机页；本地棋盘不联网。
        self.config = config or AppConfig()

        # 残局题库/进度与本地棋谱；测试可注入临时目录/固定题库。
        self._puzzles = (
            tuple(puzzles) if puzzles is not None else load_puzzle_bank()
        )
        self._record_store = record_store or LocalRecordStore()
        self._current_puzzle: Optional[Puzzle] = None
        self._puzzle_result: Optional[tuple[bool, str]] = None
        self._recorded_generation = -1
        # 联机与复盘都是主窗口页面栈中的一页，不再是独立窗口。
        self._online_panel: Optional[OnlinePanel] = None
        self._replay_panel: Optional[ReplayPanel] = None
        self._replay_return_page: Optional[QWidget] = None
        # 当前正在显示的窗口内模态层（终局弹窗/确认框），供测试定位。
        self._active_modal: Optional[ModalOverlay] = None
        # 窗口底部的非阻塞提示条（棋谱保存失败等）。
        self._transient_notice: Optional[TransientNotice] = None

        # 更新检查：版本信息只从 GitHub Releases 检索；Windows 打包产物
        # 支持直接下载并原地替换，其余环境退回「打开发布页手动下载」。
        self._update_client = UpdateHTTPClient(
            api_url=GITHUB_RELEASE_API_URL,
            token=self.config.github_token,
            parent=self,
        )
        self._update_client.release_ready.connect(self._on_release_ready)
        self._update_client.failed.connect(self._on_update_failed)
        # 忙状态以客户端信号为准：请求被 abort 或内部提前返回时，客户端
        # 只会发 busy_changed(False)，按钮不会永远停在“检查中…”。
        self._update_client.busy_changed.connect(self._set_update_busy)
        # 用户在更新弹窗里点了「前往下载页」；弹窗关闭后才真正打开浏览器，
        # 避免两个模态层叠在一起。
        self._pending_download_url: Optional[str] = None
        # 用户在更新弹窗里点了「下载并更新」；同样等弹窗关闭后再开始下载。
        self._pending_update_url: Optional[str] = None
        # 下载与替换流程的状态：idle / downloading / downloaded / failed。
        self._update_state: str = "idle"
        self._update_error_message: str = ""
        self._update_download_client: Optional[UpdateDownloadClient] = None
        self._update_progress_dialog: Optional[UpdateProgressDialog] = None
        self._update_workspace: Optional[Path] = None
        self._update_staging: Optional[Path] = None

        # 对局配置：默认保持本地双人，切到人机模式后默认中等难度。
        self._game_mode = GameMode.LOCAL
        self._difficulty = AIDifficulty.MEDIUM
        self._human_player = Player.BLACK

        # 弹窗/关闭/程序化设置同步的保护标志。
        self._result_dialog_open = False
        self._modal_dialog_open = False
        # 被模态层挡住的终局弹窗：记下代次，等模态层关闭后一次性补上。
        self._pending_result_dialog: Optional[int] = None
        self._closing = False
        self._syncing_settings = False
        # 提示结果显示后，_sync_ui 不应立即覆盖“备选着法”tooltip。
        self._hint_tooltip_active = False
        # 提示分析是否正在进行：进行中时提示按钮变成“取消提示”。
        self._hint_analysis_pending = False

        # 每次会话替换/撤销递增，用于丢弃过期终局弹窗定时器。
        self._game_generation = 0

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
        """按当前模式创建会话并连接统一信号。"""
        if self._game_mode is GameMode.PUZZLE:
            if self._current_puzzle is None:
                # 启动占位/异常兜底：没有选关时不创建残局会话。
                session: GameSession = LocalGameSession()
            else:
                session = PuzzleSession(self._current_puzzle)
        elif self._game_mode is GameMode.AI:
            session = AIGameSession(
                human_player=self._human_player,
                difficulty=self._difficulty,
            )
        else:
            session = LocalGameSession()

        session.state_changed.connect(self._on_session_state_changed)
        session.stone_placed.connect(self._on_session_stone_placed)
        session.game_over.connect(self._on_session_game_over)
        session.error.connect(self._on_session_error)
        if isinstance(session, PuzzleSession):
            session.puzzle_finished.connect(self._on_puzzle_finished)
        if isinstance(session, AIGameSession):
            session.hint_ready.connect(self._on_ai_hint_ready)
            session.hint_failed.connect(self._on_ai_hint_failed)
            # 会话侧作废提示（取消、落子、暂停、重开）时同步清掉界面标记。
            session.hint_cleared.connect(self._on_ai_hint_cleared)
        return session

    def _replace_session(self) -> None:
        """结束旧会话，按当前设置创建全新的干净会话。

        模式/先手变更和重新开局都通过创建新会话实现；这样在线会话未来可以
        用服务器快照初始化，而不需要把“重置”硬编码成清空本地 GameModel。
        """
        old_session = self._session
        old_session.shutdown()

        self._puzzle_result = None
        self._clear_hint_display()
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
        # 棋盘已变化，旧提示（文字与标记）立即失效。
        self._clear_hint_display()

    def _on_session_game_over(self) -> None:
        if self.sender() is not self._session or self._closing:
            return
        self._clear_hint_display()
        self._record_current_game()
        self._play_game_over_sound()
        self._schedule_result_dialog()

    def _on_puzzle_finished(self, success: bool, reason: str) -> None:
        """残局成功/失败：记录棋谱、更新进度并展示结果。"""
        if self.sender() is not self._session or self._closing:
            return
        session = self._session
        if not isinstance(session, PuzzleSession):
            return

        self._puzzle_result = (success, reason)
        self.puzzle_page.record_daily_result(session.puzzle.id, success)
        self._record_current_game()
        if success:
            get_audio_manager().play_win()
        else:
            get_audio_manager().play_lose()

        self._sync_ui()
        self._schedule_result_dialog()

    def _record_current_game(self) -> None:
        """把已结束的本地/AI 对局写入本地棋谱；残局不记录成功。"""
        if self._recorded_generation == self._game_generation:
            return
        if self._game_mode is GameMode.PUZZLE:
            return
        session = self._session
        model = session.model
        if model is None or not model.is_game_over:
            return

        mode = "local" if self._game_mode is GameMode.LOCAL else "ai"
        if model.winner is not None:
            result_reason = "five"
        elif model.is_draw:
            result_reason = "draw"
        else:
            result_reason = ""
        metadata = {}

        human = session.human_player
        if self._game_mode is GameMode.AI:
            ai_label = f"AI（{self._difficulty.label}）"
            black_name = "你" if human is Player.BLACK else ai_label
            white_name = "你" if human is Player.WHITE else ai_label
            metadata["ai_difficulty"] = self._difficulty.value
            metadata["human_color"] = (
                human.value if human is not None else ""
            )
        else:
            black_name = "黑棋"
            white_name = "白棋"

        record = GameRecord(
            record_id=uuid.uuid4().hex,
            created_at=datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            mode=mode,
            moves=tuple(model.moves),
            winner=model.winner,
            result_reason=result_reason,
            black_name=black_name,
            white_name=white_name,
            rated=False,
            metadata=metadata,
        )
        try:
            saved = self._record_store.append(record)
        except Exception:
            logger.exception("保存本地棋谱失败")
            saved = False
        self._recorded_generation = self._game_generation
        if not saved:
            # 只记日志的话，用户会以为棋谱已经存好；这里给一条不打断
            # 操作的提示。
            self._show_transient_notice(
                "本地棋谱写入失败，本局棋谱没有保存。"
            )

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
        """创建主菜单/残局列表/对局三页堆栈，启动时显示主菜单。"""
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
        self.puzzle_page = PuzzlePage(self._puzzles, self.stack)
        self.puzzle_page.puzzle_selected.connect(self._start_puzzle)
        self.puzzle_page.daily_selected.connect(self._start_puzzle)
        self.puzzle_page.back_requested.connect(
            self._return_to_menu_from_puzzle
        )
        self.game_page = self._build_game_page()
        self.stack.addWidget(self.menu_page)
        self.stack.addWidget(self.puzzle_page)
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
        self.menu_font_button = FontSizeButton(page)
        utility_row.addWidget(
            self.menu_font_button, 0, Qt.AlignmentFlag.AlignVCenter
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

        subtitle = QLabel(
            "双人、人机、残局、联机排位与棋谱复盘", self.menu_card
        )
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
            self._open_online_page,
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
            "限手数残局，考验攻杀与算计",
            self._open_puzzle_page,
        )
        self.replay_button = self._make_menu_button(
            "棋谱复盘",
            "menuButton",
            "查看并逐步回放最近的本地棋谱",
            lambda _checked=False: self._open_replay_page(),
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
            self.replay_button,
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

        # 更新检查入口：只查版本号，真正的下载交给用户在 GitHub 发布页完成。
        self.update_button = QPushButton("检查更新", page)
        self.update_button.setObjectName("menuLinkButton")
        self.update_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update_button.setAutoDefault(False)
        self.update_button.setToolTip("从 GitHub Releases 检查是否有新版本")
        self.update_button.setAccessibleName("检查更新")
        self.update_button.clicked.connect(self._check_for_update)

        version_row = QHBoxLayout()
        version_row.setContentsMargins(0, 0, 0, 0)
        version_row.addStretch(1)
        version_row.addWidget(version_label)
        version_row.addStretch(1)
        layout.addLayout(version_row)
        layout.addWidget(
            self.update_button, 0, Qt.AlignmentFlag.AlignHCenter
        )
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
            "双人 / 人机 / 残局 / 联机排位，连成五子即胜", self
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

        self.difficulty_combo = PaperComboBox(settings_bar)
        self.difficulty_combo.setObjectName("difficultyCombo")
        self.difficulty_combo.setAccessibleName("AI 难度")
        self.difficulty_combo.setToolTip("人机模式下 AI 的棋力与搜索强度")
        for difficulty in AIDifficulty:
            self.difficulty_combo.addItem(difficulty.label, difficulty)
        self.difficulty_combo.setMinimumWidth(78)

        self.first_combo = PaperComboBox(settings_bar)
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
        self.puzzle_info_label = QLabel("", settings_bar)
        self.puzzle_info_label.setObjectName("settingsLabel")
        self.puzzle_info_label.setVisible(False)

        bar_layout.addWidget(self.difficulty_group)
        bar_layout.addWidget(self.first_group)
        bar_layout.addWidget(self.local_mode_label)
        bar_layout.addWidget(self.puzzle_info_label)
        bar_layout.addStretch(1)

        # 声音与字号设置同栏，不参与 AI 回合的锁定逻辑。
        self.sound_button = SoundButton(settings_bar)
        bar_layout.addWidget(
            self.sound_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        self.font_button = FontSizeButton(settings_bar)
        bar_layout.addWidget(
            self.font_button, 0, Qt.AlignmentFlag.AlignVCenter
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
        self.puzzle_hint_label = QLabel("", self)
        self.puzzle_hint_label.setObjectName("hintLabel")
        self.puzzle_hint_label.setWordWrap(True)
        self.puzzle_hint_label.setVisible(False)

        info_layout.addWidget(self.move_label)
        info_layout.addWidget(hint_label)
        info_layout.addWidget(self.puzzle_hint_label)

        self.menu_back_button = QPushButton("返回主菜单", self)
        self.menu_back_button.setObjectName("menuBackButton")
        self.menu_back_button.setToolTip("结束当前棋局并返回主菜单")
        self.menu_back_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.menu_back_button.setMinimumWidth(104)
        self.menu_back_button.setMinimumHeight(38)
        self.menu_back_button.setAutoDefault(False)
        self.menu_back_button.clicked.connect(self._on_back_requested)

        self.hint_button = QPushButton("提示", self)
        self.hint_button.setObjectName("undoButton")
        self.hint_button.setToolTip("查看提示")
        self.hint_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.hint_button.setMinimumWidth(74)
        self.hint_button.setMinimumHeight(38)
        self.hint_button.setAutoDefault(False)
        self.hint_button.setVisible(False)

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

        self.hint_button.clicked.connect(self._request_hint)
        self.undo_button.clicked.connect(self._undo_move)
        self.restart_button.clicked.connect(self._request_restart)

        footer_layout.addLayout(info_layout)
        footer_layout.addStretch(1)
        footer_layout.addWidget(self.menu_back_button)
        footer_layout.addWidget(self.hint_button)
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
    def _leave_game_page(self) -> None:
        """离开对局页的统一入口。

        所有切走对局页的路径都必须先经过这里：暂停会话，让后台 AI 停止
        思考并作废在途结果。否则用户在复盘页或联机页浏览时，隐藏的棋盘
        仍会被 AI 悄悄改掉。
        """
        if self._closing:
            return
        self._session.pause()

    def _enter_game_page(self) -> None:
        """回到对局页：恢复被 :meth:`_leave_game_page` 暂停的会话。"""
        if self._closing:
            return
        self._session.resume()

    def _show_page(self, widget: QWidget) -> None:
        """切换页面栈，并同步联机面板的可见状态。

        页面切换一律走这里：联机面板藏在页面栈里时必须停掉它的界面
        定时器与终局提示，否则隐藏的页面还在刷新、还在弹窗。
        """
        self.stack.setCurrentWidget(widget)
        self._sync_online_panel_visibility()

    def _sync_online_panel_visibility(self) -> None:
        """按当前页面同步联机面板的可见状态生命周期。"""
        panel = self._online_panel
        if panel is None:
            return
        if self.stack.currentWidget() is panel:
            panel.resume_visible_state()
        else:
            panel.pause_visible_state()

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
        self._show_page(self.game_page)
        self._set_game_shortcuts_enabled(True)

    def _on_back_requested(self) -> None:
        """对局页“返回”按钮：残局回列表，其余回主菜单。"""
        if self._game_mode is GameMode.PUZZLE:
            self._request_puzzle_list()
        else:
            self.show_main_menu()

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
        self._leave_game_page()
        if self._session.view.move_count > 0:
            if not self._confirm_discard_game(
                "返回主菜单",
                "返回主菜单将结束当前棋局，确定要返回吗？",
                confirm_text="返回主菜单",
            ):
                self._enter_game_page()
                self._sync_ui()
                return

        # 使尚未执行的终局弹窗定时器失效。
        self._game_generation += 1
        self._show_page(self.menu_page)
        self._set_game_shortcuts_enabled(False)
        self.online_button.setFocus()

    def _open_puzzle_page(self) -> None:
        """从主菜单打开残局关卡列表。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
        ):
            return
        self._leave_game_page()
        self.puzzle_page.refresh_daily()
        self._show_page(self.puzzle_page)
        self._set_game_shortcuts_enabled(False)
        self.puzzle_page.back_button.setFocus()

    def _return_to_menu_from_puzzle(self) -> None:
        """残局列表返回主菜单。"""
        if self._closing or self._modal_dialog_open:
            return
        self._leave_game_page()
        self._show_page(self.menu_page)
        self._set_game_shortcuts_enabled(False)
        self.online_button.setFocus()

    def _start_puzzle(self, puzzle: object) -> None:
        """从关卡列表进入选定残局。"""
        if not isinstance(puzzle, Puzzle):
            return
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
        ):
            return
        self._current_puzzle = puzzle
        self._game_mode = GameMode.PUZZLE
        self._puzzle_result = None
        self._replace_session()
        self._show_page(self.game_page)
        self._set_game_shortcuts_enabled(True)
        self.board_widget.setFocus()

    def _request_puzzle_list(self) -> None:
        """从残局对局返回关卡列表；进行中的残局需要确认。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
        ):
            return
        session = self._session
        if (
            isinstance(session, PuzzleSession)
            and not session.finished
            and session.player_moves > 0
        ):
            session.pause()
            if not self._confirm_discard_game(
                "返回残局列表",
                "返回残局列表将放弃当前残局，确定要返回吗？",
                confirm_text="返回残局列表",
            ):
                session.resume()
                self._sync_ui()
                return

        self._game_generation += 1
        self._leave_game_page()
        self.puzzle_page.refresh_daily()
        self._show_page(self.puzzle_page)
        self._set_game_shortcuts_enabled(False)
        self.puzzle_page.back_button.setFocus()

    def _open_replay_page(
        self,
        records: Optional[Sequence[GameRecord]] = None,
        *,
        return_to: Optional[QWidget] = None,
    ) -> None:
        """打开棋谱复盘页；复盘是页面而不是独立窗口。

        ``records`` 为空时从本地棋谱库读取。注意这个槽也可能被
        ``QPushButton.clicked`` 直接触发——那时传进来的是
        ``checked=False``，必须当成“没有指定棋谱”处理。
        """
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
        ):
            return

        if not isinstance(records, (list, tuple)):
            records = None
        if records is None:
            try:
                records = self._record_store.list_recent(50)
            except Exception:
                logger.exception("读取本地棋谱失败")
                records = ()

        panel = self._replay_panel
        if panel is None:
            panel = ReplayPanel(records, parent=self.stack)
            panel.closed.connect(self._on_replay_panel_closed)
            self._replay_panel = panel
            self.stack.addWidget(panel)
        else:
            panel.set_records(records)

        self._replay_return_page = return_to or self.stack.currentWidget()
        # 复盘页不是对局页：离开对局页前先暂停会话。
        self._leave_game_page()
        self._show_page(panel)
        self._set_game_shortcuts_enabled(False)

    def _on_replay_panel_closed(self) -> None:
        """复盘页关闭：回到来源页面。"""
        target = self._replay_return_page or self.menu_page
        self._replay_return_page = None
        self._show_page(target)
        if target is self.game_page:
            # 回到对局页：恢复之前被暂停的会话。
            self._enter_game_page()
        if target is self.menu_page:
            self.online_button.setFocus()

    def _exit_game(self) -> None:
        """退出应用；closeEvent 负责停止联机页与后台线程。"""
        if not self._closing:
            self.close()

    def _is_game_page_active(self) -> bool:
        """当前是否停留在对局页。"""
        return self.stack.currentWidget() is self.game_page

    def _set_game_shortcuts_enabled(self, enabled: bool) -> None:
        """菜单页禁用 Ctrl+Z/Ctrl+N；残局不提供悔棋。"""
        self._undo_shortcut.setEnabled(
            enabled and self._game_mode is not GameMode.PUZZLE
        )
        self._restart_shortcut.setEnabled(enabled)

    def _open_online_page(self) -> None:
        """打开联机页；离线棋盘和 AI 状态不受影响。"""
        try:
            panel = self._online_panel
            if panel is None:
                panel = OnlinePanel(self.config, self.stack)
                panel.back_requested.connect(
                    self._return_to_menu_from_online
                )
                panel.replay_requested.connect(self._open_replay_page)
                self._online_panel = panel
                self.stack.addWidget(panel)
            # 联机页不是对局页：离开对局页前先暂停会话。
            self._leave_game_page()
            self._show_page(panel)
            self._set_game_shortcuts_enabled(False)
        except Exception as exc:  # 依赖/页面初始化失败时不要让离线应用崩溃
            logger.exception("打开联机页失败")
            self._show_notice(
                "无法打开联机对战",
                f"联机页面初始化失败：{type(exc).__name__}",
            )

    def _return_to_menu_from_online(self) -> None:
        """从联机页返回主菜单；联机会话状态保持不变。"""
        if self._closing or self._modal_dialog_open:
            return
        panel = self._online_panel
        if panel is not None and panel.in_room():
            confirmed = self._confirm_discard_game(
                "返回主菜单",
                "对局仍在继续，返回主菜单只是切走界面，"
                "连接会保持（心跳继续），确定要返回吗？",
                confirm_text="返回主菜单",
            )
            if not confirmed:
                return
        self._leave_game_page()
        self._show_page(self.menu_page)
        self._set_game_shortcuts_enabled(False)
        self.online_button.setFocus()

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
            puzzle_mode = self._game_mode is GameMode.PUZZLE

            # 双人/残局不展示 AI 难度/先手选择，保持设置栏简洁。
            self.difficulty_group.setVisible(ai_mode)
            self.first_group.setVisible(ai_mode)
            self.local_mode_label.setVisible(
                self._game_mode is GameMode.LOCAL
            )
            self.puzzle_info_label.setVisible(puzzle_mode)
            if puzzle_mode:
                puzzle = self._current_puzzle
                if puzzle is not None:
                    self.puzzle_info_label.setText(
                        f"残局：{puzzle.title} · "
                        f"限 {puzzle.max_player_moves} 手"
                    )
                else:
                    self.puzzle_info_label.setText("残局")

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
        self._clear_hint_display()
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
            self._clear_hint_display()
            self._sync_ui()

    def _request_hint(self) -> None:
        """按模式请求提示：残局显示文字，人机模式请求 AI 分析。

        分析进行中时这个按钮变成“取消提示”：再点一次就能中止后台搜索，
        不必干等它算完。
        """
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
            or not self._is_game_page_active()
        ):
            return

        if self._game_mode is GameMode.PUZZLE:
            self._show_puzzle_hint()
            return
        if self._game_mode is not GameMode.AI:
            return

        session = self._session
        if not isinstance(session, AIGameSession):
            return

        if self._hint_analysis_pending:
            self._cancel_hint_analysis(session)
            return

        if not session.can_hint:
            return
        if not session.request_hint():
            return

        # 新一轮分析开始：上一轮的提示文字、tooltip 与棋盘标记全部作废，
        # 否则旧局面的推荐会一直挂在界面上。
        self._clear_hint_display()
        self._hint_analysis_pending = True
        self.puzzle_hint_label.setText("提示：正在分析局面…")
        self.puzzle_hint_label.setVisible(True)
        self._sync_hint_button()

    def _cancel_hint_analysis(self, session: AIGameSession) -> None:
        """中止正在进行的提示分析。

        会话侧的取消入口由 AIGameSession 提供（``cancel_hint``）。这里按
        公开接口优先、其次兼容旧名字的顺序调用；接口缺失时只把界面复位，
        避免按钮永远停在“取消提示”。
        """
        for name in (
            "cancel_hint",
            "cancel_hint_search",
            "_cancel_hint_search",
        ):
            cancel = getattr(session, name, None)
            if callable(cancel):
                cancel()
                break
        else:
            logger.warning("当前 AI 会话没有取消提示分析的接口")
        self._clear_hint_display()
        self._sync_hint_button()

    def _default_hint_tooltip(self) -> str:
        """返回提示按钮在“没有备选着法”时使用的默认提示文字。"""
        if self._game_mode is GameMode.PUZZLE:
            return "查看当前残局的提示"
        if self._game_mode is GameMode.AI:
            return "让 AI 分析局面并推荐一手"
        return "查看提示"

    def _sync_hint_button(self) -> None:
        """同步提示按钮的文案与可用状态。

        分析进行中时按钮保持可点，作用从“请求提示”变成“取消提示”。
        ``AIGameSession.hint_running`` 是唯一事实来源：会话因暂停、悔棋或
        棋盘变化作废提示后，按钮不能停留在“取消提示”。
        """
        button = self.hint_button
        if isinstance(self._session, AIGameSession):
            self._hint_analysis_pending = self._session.hint_running
        if self._hint_analysis_pending:
            button.setText("取消提示")
            button.setEnabled(True)
            button.setToolTip("取消正在进行的提示分析")
            return

        view = self._session.view
        game_over = view.is_game_over
        puzzle_over = (
            self._game_mode is GameMode.PUZZLE
            and self._puzzle_result is not None
        )
        button.setText("提示")
        if self._game_mode is GameMode.PUZZLE:
            button.setEnabled(not puzzle_over)
            button.setToolTip(self._default_hint_tooltip())
        elif self._game_mode is GameMode.AI:
            session = self._session
            button.setEnabled(
                isinstance(session, AIGameSession)
                and session.can_hint
                and not game_over
            )
            if not self._hint_tooltip_active:
                button.setToolTip(self._default_hint_tooltip())
        else:
            button.setEnabled(False)

    def _show_puzzle_hint(self) -> None:
        """在当前残局中展示题库提供的文字提示。"""
        if self._game_mode is not GameMode.PUZZLE:
            return
        session = self._session
        if not isinstance(session, PuzzleSession):
            return
        hint = session.hint or session.puzzle.description
        if not hint:
            hint = "观察棋盘上的双威胁，优先制造对手无法同时封堵的棋形。"
        self.puzzle_hint_label.setText(f"提示：{hint}")
        self.puzzle_hint_label.setVisible(True)

    def _on_ai_hint_ready(self, analysis: object) -> None:
        """显示后台分析返回的推荐落点、评分与候选。"""
        if self.sender() is not self._session or self._closing:
            return

        session = self._session
        move = getattr(analysis, "best_move", None)
        if not isinstance(session, AIGameSession) or move is None:
            return
        self._hint_analysis_pending = False
        row, col = move
        self.board_widget.set_hint_cell(move, session.model.move_count)

        coordinate = self._format_board_coordinate(row, col)
        source = getattr(analysis, "source", "search")
        if source == "vcf":
            text = f"提示：{coordinate}，VCF 强制胜"
        elif source == "vct":
            text = f"提示：{coordinate}，双威胁强制胜"
        elif source == "forced":
            text = f"提示：{coordinate}，可直接成五"
        elif source == "block":
            text = f"提示：{coordinate}，必须封堵对手"
        elif source == "book":
            text = f"提示：{coordinate}（开局库）"
        else:
            depth = getattr(analysis, "completed_depth", 0)
            score = getattr(analysis, "best_score", 0)
            text = (
                f"提示：推荐 {coordinate}"
                f"（深度 {depth}，评分 {score:+d}）"
            )
        self.puzzle_hint_label.setText(text)
        self.puzzle_hint_label.setVisible(True)

        alternatives = [
            candidate
            for candidate in getattr(analysis, "candidates", ())
            if candidate.move != move
        ][:2]
        if alternatives:
            parts = [
                f"{self._format_board_coordinate(*candidate.move)}"
                f"（{candidate.score:+d}）"
                for candidate in alternatives
            ]
            self.hint_button.setToolTip("备选：" + "，".join(parts))
            self._hint_tooltip_active = True
        self._sync_hint_button()

    def _on_ai_hint_failed(self, message: str) -> None:
        """提示分析失败时给出简短反馈，不阻断对局。"""
        if self.sender() is not self._session or self._closing:
            return
        logger.warning("AI 提示分析失败：%s", message)
        self._hint_analysis_pending = False
        self.puzzle_hint_label.setText("提示：分析失败，请稍后重试")
        self.puzzle_hint_label.setVisible(True)
        self._sync_hint_button()

    def _on_ai_hint_cleared(self) -> None:
        """会话侧作废提示：清除旧文字、标记并在需要时复位按钮。"""
        if self.sender() is not self._session or self._closing:
            return
        self._clear_hint_display()

    def _clear_hint_display(self) -> None:
        """清除提示文字、棋盘标记与按钮上的备选着法。"""
        self._hint_tooltip_active = False
        self._hint_analysis_pending = False
        if hasattr(self, "puzzle_hint_label"):
            self.puzzle_hint_label.clear()
            self.puzzle_hint_label.setVisible(False)
        if hasattr(self, "board_widget"):
            self.board_widget.clear_hint()
        if hasattr(self, "hint_button"):
            # 备选着法已经作废，tooltip 不能继续留在按钮上。
            self.hint_button.setToolTip(self._default_hint_tooltip())
            # 旧提示被清除时，按钮文案也要立刻回到“提示”。
            self._sync_hint_button()

    @staticmethod
    def _format_board_coordinate(row: int, col: int) -> str:
        """把行列坐标格式化为 H8 形式。"""
        return f"{chr(ord('A') + col)}{row + 1}"

    def _request_restart(self) -> None:
        """重新开局；已有落子时确认，并安全暂停/替换会话。"""
        if (
            self._closing
            or self._modal_dialog_open
            or self._result_dialog_open
            or not self._is_game_page_active()
        ):
            return

        if self._game_mode is GameMode.PUZZLE:
            self._replace_session()
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
        puzzle_over = (
            self._game_mode is GameMode.PUZZLE
            and self._puzzle_result is not None
        )
        if not (self._session.view.is_game_over or puzzle_over):
            return
        if self._modal_dialog_open:
            # 返回主菜单/重新开局的确认框仍在：记下待显示的终局结果，
            # 等模态层关闭后一次性补上。旧实现每 100ms 轮询一次，
            # 用户不点掉确认框就会一直占着定时器队列。
            self._pending_result_dialog = (
                self._game_generation if generation is None else generation
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
            result_dialog = ResultDialog(presentation, self._overlay_host())
            self._active_modal = result_dialog
            result_dialog.exec()
            restart = result_dialog.restart_requested
            result_dialog.deleteLater()
            self._active_modal = None
        finally:
            self._result_dialog_open = False
            self._modal_dialog_open = False

        if restart:
            self._reset_game()
        elif self._game_mode is GameMode.PUZZLE:
            self._request_puzzle_list()

        QTimer.singleShot(0, self._flush_pending_result_dialog)

    def _flush_pending_result_dialog(self) -> None:
        """模态层关闭后补显示被推迟的终局弹窗。

        终局弹窗只挂起一次，不会变成轮询；代次校验保证上一局的终局
        不会盖到新一局头上。
        """
        generation = self._pending_result_dialog
        if generation is None:
            return
        self._pending_result_dialog = None
        if self._modal_dialog_open:
            # 还有别的模态层开着：继续挂起，等它关闭时再补。
            self._pending_result_dialog = generation
            return
        self._show_result_dialog(generation)

    def _build_result_presentation(
        self, winner: Optional[Player], move_count: int
    ) -> ResultPresentation:
        """根据模式与胜者生成古风终局文案。"""
        if self._game_mode is GameMode.PUZZLE:
            success, reason = self._puzzle_result or (False, "")
            puzzle = self._current_puzzle
            puzzle_title = puzzle.title if puzzle is not None else "残局"
            if success:
                return ResultPresentation(
                    kind="win",
                    title="破局",
                    subtitle=f"残局「{puzzle_title}」已破局",
                    detail="关卡已重置，可再次挑战",
                    seal_text="胜",
                    primary_text="再试一次",
                    secondary_text="返回残局列表",
                )
            return ResultPresentation(
                kind="lose",
                title="未破局",
                subtitle=f"残局「{puzzle_title}」{reason or '挑战结束'}",
                detail="关卡已重置，可再次挑战",
                seal_text="负",
                primary_text="再试一次",
                secondary_text="返回残局列表",
            )

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
                parent=self._overlay_host(),
            )
            self._active_modal = dialog
            dialog.exec()
            confirmed = dialog.confirm_requested
            dialog.deleteLater()
            self._active_modal = None
            return confirmed
        finally:
            self._modal_dialog_open = False
            # 延后一拍再补终局弹窗：让当前这次页面切换先走完。
            QTimer.singleShot(0, self._flush_pending_result_dialog)

    def _overlay_host(self) -> QWidget:
        """模态层的宿主：主窗口中央部件。"""
        host = self.centralWidget()
        return host if host is not None else self

    def _show_notice(self, title: str, message: str) -> None:
        """在窗口内显示一条提示（不创建原生消息框窗口）。"""
        notice = NoticeOverlay(title, message, parent=self._overlay_host())
        self._active_modal = notice
        # 提示也是一层模态遮罩：置位后终局弹窗会推迟显示，避免两个模态层
        # 叠在一起（两个嵌套事件循环会让窗口关不掉）。
        previous_modal_state = self._modal_dialog_open
        self._modal_dialog_open = True
        try:
            notice.exec()
        finally:
            self._modal_dialog_open = previous_modal_state
            notice.deleteLater()
            self._active_modal = None
            if not self._modal_dialog_open:
                # 提示层关掉了：补上被推迟的终局弹窗。
                QTimer.singleShot(0, self._flush_pending_result_dialog)

    def _show_transient_notice(self, text: str) -> None:
        """显示一条非阻塞提示：窗口底部的提示条，几秒后自动消失。

        与 :meth:`_show_notice` 的模态提示不同，它不会挡住终局弹窗，
        也不会让用户必须点“知道了”才能继续。
        """
        if self._closing:
            return
        notice = self._transient_notice
        if notice is None:
            notice = TransientNotice(self._overlay_host())
            self._transient_notice = notice
        notice.show_message(text)

    # ------------------------------------------------------------------
    # 更新检查（版本信息只来自 GitHub Releases）
    # ------------------------------------------------------------------
    def _check_for_update(self) -> None:
        """从 GitHub Releases 检索最新版本。

        只读取版本信息；安装包始终由用户从发布页自行下载，客户端不经过
        游戏服务端分发新版本。
        """
        if self._closing or self._update_client.busy:
            return
        self._set_update_busy(True)
        try:
            started = self._update_client.check()
        except Exception:  # pragma: no cover - 兜底：按钮不能永久卡住
            logger.exception("更新检查发起失败")
            started = False
        if not started:
            # check() 自身会发出 failed 信号，这里只需要恢复按钮状态。
            self._set_update_busy(False)

    def _set_update_busy(self, busy: bool) -> None:
        """更新「检查更新」按钮的忙碌状态。"""
        self.update_button.setEnabled(not busy)
        self.update_button.setText("检查中…" if busy else "检查更新")

    def _on_release_ready(self, release: ReleaseInfo) -> None:
        """收到 GitHub 最新发布信息：比较版本号并给出提示。"""
        self._set_update_busy(False)
        if self._closing:
            return
        if self._active_modal is not None:
            # 已经有别的模态层（例如终局弹窗）开着：等它关掉再弹，
            # 两个嵌套事件循环叠在一起会让窗口无法关闭。
            QTimer.singleShot(100, lambda: self._on_release_ready(release))
            return

        version = release.version
        notes = release.notes
        url = release.url

        if parse_version(version) is None:
            self._show_notice(
                "检查更新",
                f"无法识别远端版本号（{version or '空'}），"
                f"当前版本 {APP_VERSION}。",
            )
            return

        if not is_newer_version(version, APP_VERSION):
            self._show_notice(
                "检查更新", f"当前已是最新版本 {APP_VERSION}。"
            )
            return

        asset = select_windows_asset(release)
        # 产物地址必须先过白名单：地址来自远端响应，不能直接拿去下载。
        asset_url = (
            auto_update.asset_download_url(asset.download_url)
            if asset is not None
            else ""
        )
        # 只有 Windows 打包产物才能原地替换；其余环境退回发布页。
        can_update = bool(asset_url) and auto_update.is_supported()

        dialog = UpdateAvailableDialog(
            version=version,
            current_version=APP_VERSION,
            notes=summarize_notes(notes),
            release_page_url=self._safe_release_url(url),
            asset_name=asset.name if asset is not None else "",
            asset_url=asset_url,
            can_update=can_update,
            parent=self._overlay_host(),
        )
        self._pending_download_url = None
        self._pending_update_url = None
        dialog.download_requested.connect(self._note_download_requested)
        dialog.update_requested.connect(self._note_update_requested)
        self._active_modal = dialog
        # 与终局弹窗/确认框一样，打开模态层期间置位该标志：AI 继续走棋，
        # 终局弹窗也会推迟到本弹窗关闭之后再显示。
        self._modal_dialog_open = True
        try:
            dialog.exec()
        finally:
            self._modal_dialog_open = False
            dialog.deleteLater()
            self._active_modal = None
            # 弹窗关掉了：补上被推迟的终局弹窗与更新提示。
            QTimer.singleShot(0, self._flush_pending_result_dialog)

        # 弹窗已经关闭，此时再打开浏览器或开始下载，避免两个模态层同时存在。
        pending = self._pending_download_url
        self._pending_download_url = None
        if pending:
            self._open_release_page(pending)
            return
        update_url = self._pending_update_url
        self._pending_update_url = None
        if update_url:
            self._start_update_download(update_url, version)

    def _on_update_failed(self, message: str) -> None:
        """更新检查失败：恢复按钮并提示原因。"""
        self._set_update_busy(False)
        if self._closing:
            return
        if self._active_modal is not None:
            QTimer.singleShot(100, lambda: self._on_update_failed(message))
            return
        self._show_notice("检查更新失败", message)

    def _note_download_requested(self, url: str) -> None:
        """记录用户的下载意图，等弹窗关闭后再打开浏览器。"""
        self._pending_download_url = url

    def _note_update_requested(self, url: str) -> None:
        """记录用户的更新意图，等弹窗关闭后再开始下载。"""
        self._pending_update_url = url

    # ------------------------------------------------------------------
    # 下载与替换
    # ------------------------------------------------------------------
    def _start_update_download(self, asset_url: str, version: str) -> None:
        """下载更新包；下载完成后进入替换流程。"""
        if self._closing:
            return
        if not auto_update.is_supported():
            self._show_notice(
                "无法自动更新",
                "当前环境不支持自动替换，请从发布页手动下载新版本。",
            )
            return

        try:
            workspace = auto_update.create_workspace(version)
        except OSError:
            self._show_notice(
                "更新失败", "无法创建临时目录，请检查磁盘空间与权限。"
            )
            return

        self._update_workspace = workspace
        self._update_staging = None
        self._update_error_message = ""
        self._update_state = "downloading"

        client = UpdateDownloadClient(parent=self)
        previous = self._update_download_client
        if previous is not None:
            # 上一次的下载客户端已经没有用了：先中止，别让它继续往
            # 已经删掉的工作目录里写数据（那会冒出一句误导性的
            # 「无法写入更新包」，把新下载顶掉）。
            try:
                previous.abort()
            except RuntimeError:
                pass
            previous.deleteLater()
        self._update_download_client = client
        client.progress.connect(self._on_update_progress)
        # 信号带上发起方：过期的客户端迟到的失败通知不能影响当前下载。
        client.finished.connect(
            lambda path, source=client: self._on_update_downloaded(path, source)
        )
        client.failed.connect(
            lambda message, source=client: self._on_update_download_failed(
                message, source
            )
        )

        dialog = UpdateProgressDialog(
            version=version, parent=self._overlay_host()
        )
        self._update_progress_dialog = dialog
        dialog.cancel_requested.connect(self._cancel_update_download)

        archive = workspace / "update.zip"
        started = client.start(asset_url, archive)
        if not started:
            # 失败信号已经在 start() 里同步发出，这里只负责把原因告诉用户。
            self._update_state = "failed"
            self._update_progress_dialog = None
            dialog.deleteLater()
            self._show_notice(
                "更新失败",
                self._update_error_message or "无法开始下载更新包。",
            )
            auto_update.cleanup_workspace(self._update_workspace)
            self._update_workspace = None
            return

        self._active_modal = dialog
        self._modal_dialog_open = True
        try:
            # 下载若已经同步完成（状态不再是 downloading），弹窗就没有
            # 继续存在的必要，直接进入下一步。
            if self._update_state == "downloading":
                dialog.exec()
        finally:
            self._modal_dialog_open = False
            self._active_modal = None
            self._update_progress_dialog = None
            dialog.deleteLater()

        state = self._update_state
        if state == "failed":
            self._show_notice("更新失败", self._update_error_message)
            auto_update.cleanup_workspace(self._update_workspace)
            self._update_workspace = None
            return
        if state == "downloaded":
            self._apply_downloaded_update(version)
            return
        # 其余情况是用户取消了下载。
        self._update_state = "cancelled"
        auto_update.cleanup_workspace(self._update_workspace)
        self._update_workspace = None

    def _on_update_progress(self, received: int, total: int) -> None:
        """刷新下载进度。"""
        dialog = self._update_progress_dialog
        if dialog is not None:
            dialog.set_progress(received, total)

    def _on_update_downloaded(self, path: str, source: object = None) -> None:
        """更新包下载完成：解压校验，准备替换。"""
        if self._closing:
            return
        if source is not None and source is not self._update_download_client:
            # 过期客户端迟到的通知，忽略。
            return
        dialog = self._update_progress_dialog
        if dialog is not None:
            dialog.set_status("下载完成，正在准备安装…")
        archive = Path(path)
        try:
            staging = auto_update.extract_archive(
                archive, self._staging_directory()
            )
        except auto_update.UpdateError as exc:
            self._update_error_message = str(exc)
            self._update_state = "failed"
        except OSError:
            self._update_error_message = "无法解压更新包，请检查磁盘空间。"
            self._update_state = "failed"
        else:
            self._update_staging = staging
            self._update_state = "downloaded"
            # 解压完成后压缩包就没用了：留着会在临时目录里留下几十兆
            # 的垃圾文件。删不掉也不影响后续流程。
            try:
                archive.unlink()
            except OSError:
                logger.warning("删除更新包失败：%s", archive)
        if dialog is not None:
            dialog.accept()

    def _on_update_download_failed(self, message: str, source: object = None) -> None:
        """下载失败：记录原因并关闭进度弹窗。"""
        if self._closing:
            return
        if source is not None and source is not self._update_download_client:
            # 过期客户端迟到的失败通知，不能算作当前下载的失败。
            return
        self._update_error_message = message
        self._update_state = "failed"
        dialog = self._update_progress_dialog
        if dialog is not None:
            dialog.reject()

    def _cancel_update_download(self) -> None:
        """用户取消下载。"""
        client = self._update_download_client
        if client is not None:
            client.abort()
        self._update_state = "cancelled"

    def _staging_directory(self) -> Path:
        """本次更新的暂存目录。"""
        workspace = self._update_workspace
        if workspace is None:
            workspace = auto_update.create_workspace()
            self._update_workspace = workspace
        staging = workspace / "stage"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def _apply_downloaded_update(self, version: str) -> None:
        """询问是否立即重启以完成替换。"""
        if self._closing or self._update_staging is None:
            return
        if self._active_modal is not None:
            QTimer.singleShot(100, lambda: self._apply_downloaded_update(version))
            return

        confirm = ConfirmDialog(
            "更新已下载",
            f"新版本 {version} 已下载完成，需要重启客户端完成替换。现在重启吗？",
            confirm_text="立即重启",
            cancel_text="稍后",
            seal_text="更",
            parent=self._overlay_host(),
        )
        self._active_modal = confirm
        self._modal_dialog_open = True
        try:
            confirm.exec()
        finally:
            self._modal_dialog_open = False
            self._active_modal = None
            confirm.deleteLater()
            QTimer.singleShot(0, self._flush_pending_result_dialog)

        if not confirm.confirm_requested:
            self._show_notice(
                "已取消更新",
                "新版本未安装，可稍后再次检查更新重新下载。",
            )
            auto_update.cleanup_workspace(self._update_workspace)
            self._update_workspace = None
            self._update_staging = None
            self._update_state = "idle"
            return
        self._launch_update_and_exit(version)

    def _launch_update_and_exit(self, version: str) -> None:
        """启动替换脚本，然后退出客户端。"""
        staging = self._update_staging
        if staging is None:
            self._show_notice("更新失败", "更新包已丢失，请重新下载。")
            return
        try:
            plan = auto_update.prepare_update(staging, self._update_workspace)
            auto_update.launch_helper(plan)
        except auto_update.UpdateError as exc:
            self._show_notice("更新失败", str(exc))
            return
        except OSError:
            self._show_notice("更新失败", "无法启动更新程序，请检查权限。")
            return

        logger.info("更新包已就绪，正在替换并重启：version=%s", version)
        self._show_transient_notice("正在替换文件并重启客户端…")
        # 替换脚本会等待本进程退出后再动文件，因此必须先退出。
        QTimer.singleShot(1200, self.close)

    @staticmethod
    def _safe_release_url(url: str) -> str:
        """只放行本仓库的 GitHub 发布页地址。

        地址来自 GitHub API 的响应，理论上可被劫持改写；这里既校验主机，
        也校验路径，避免被诱导到 ``github.com`` 下的任意钓鱼页面。
        """
        candidate = (url or "").strip()
        if not candidate:
            return GITHUB_RELEASES_PAGE_URL
        parsed = QUrl(candidate)
        if parsed.scheme().lower() != "https":
            return GITHUB_RELEASES_PAGE_URL
        if parsed.host().lower() != "github.com":
            return GITHUB_RELEASES_PAGE_URL
        if not parsed.path().startswith("/ckm6666/Gomoku/releases"):
            return GITHUB_RELEASES_PAGE_URL
        return candidate

    def _open_release_page(self, url: str) -> None:
        """用系统默认浏览器打开 GitHub 发布页。"""
        target = self._safe_release_url(url)
        if not QDesktopServices.openUrl(QUrl(target)):
            logger.warning("无法打开发布页：%s", target)
            self._show_notice(
                "无法打开下载页", f"请手动访问：{target}"
            )

    def active_modal(self) -> Optional[ModalOverlay]:
        """当前正在显示的窗口内模态层（测试用）。"""
        return self._active_modal

    # ------------------------------------------------------------------
    # 状态同步
    # ------------------------------------------------------------------
    def _sync_ui(self) -> None:
        """把会话只读视图、模式和 AI 忙碌状态同步到界面。"""
        view = self._session.view
        winner = view.winner
        game_over = view.is_game_over
        current_player = view.current_player
        human = self._session.human_player
        puzzle_over = (
            self._game_mode is GameMode.PUZZLE
            and self._puzzle_result is not None
        )

        for player, card in (
            (Player.BLACK, self.black_card),
            (Player.WHITE, self.white_card),
        ):
            display_name, base_caption = self._card_identity(player)
            card.set_identity(display_name, base_caption)

            if puzzle_over:
                success = bool(self._puzzle_result[0])
                active = success and player is human
                status = "破局" if success else "挑战结束"
            elif winner is not None:
                active = winner is player
                status = "获胜" if active else "本局结束"
            elif view.is_draw:
                active = False
                status = "平局"
            elif self._game_mode is GameMode.PUZZLE:
                active = current_player is player
                if active and player is human:
                    status = "落子中"
                elif active:
                    status = "防守中"
                else:
                    status = "等待"
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

        if puzzle_over:
            self.turn_pill.setText(
                "破局成功" if self._puzzle_result[0] else "挑战失败"
            )
        elif winner is not None:
            if self._game_mode is GameMode.AI:
                self.turn_pill.setText(
                    "你获胜" if winner is self._human_player else "AI 获胜"
                )
            else:
                self.turn_pill.setText(f"{winner.label}获胜")
        elif view.is_draw:
            self.turn_pill.setText("本局平局")
        elif self._game_mode is GameMode.PUZZLE:
            self.turn_pill.setText(
                "你的回合" if current_player is human else "AI 防守中…"
            )
        elif self._game_mode is GameMode.AI:
            if current_player is self._human_player:
                self.turn_pill.setText("你的回合")
            else:
                self.turn_pill.setText("AI 思考中…")
        else:
            self.turn_pill.setText(f"{current_player.label}回合")

        self._set_result_style(game_over or puzzle_over)
        if isinstance(self._session, PuzzleSession):
            self.move_label.setText(
                f"第 {self._session.player_moves} / "
                f"{self._session.move_limit} 手"
            )
        else:
            self.move_label.setText(f"第 {view.move_count} 手")
        self.undo_button.setEnabled(self._session.can_undo)
        if self._game_mode is GameMode.PUZZLE:
            self.undo_button.setToolTip("残局挑战不支持悔棋")
        elif self._game_mode is GameMode.LOCAL:
            self.undo_button.setToolTip("撤销上一手（Ctrl+Z）")
        else:
            self.undo_button.setToolTip(
                "撤销到你的上一次落子前（Ctrl+Z）"
            )
        hint_visible = self._game_mode in (GameMode.PUZZLE, GameMode.AI)
        self.hint_button.setVisible(hint_visible)
        self._sync_hint_button()
        self.restart_button.setText(
            "重新挑战"
            if self._game_mode is GameMode.PUZZLE
            else "重新开局"
        )
        self.menu_back_button.setText(
            "残局列表"
            if self._game_mode is GameMode.PUZZLE
            else "返回主菜单"
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

        if self._game_mode is GameMode.PUZZLE:
            human = self._session.human_player
            if player is human:
                return "你", f"执{color}，先手"
            difficulty = (
                self._session.difficulty.label
                if isinstance(self._session, PuzzleSession)
                else "防守"
            )
            return f"防守 AI（{difficulty}）", f"执{color}，后手"

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
        """关闭窗口前停止 AI 会话与联机/复盘面板。"""
        self._closing = True
        # 模态层用嵌套事件循环阻塞主流程；退出前必须先结束它，
        # 否则关闭动作会卡在循环里，进程无法退出。
        modal = self._active_modal
        if modal is not None:
            modal.reject()
        # 取消在途的更新请求：窗口正在关闭，此时收到响应再弹窗会让
        # 进程卡在嵌套事件循环里出不来。
        try:
            self._update_client.abort()
        except RuntimeError:
            pass
        # 同理，正在下载的更新包也要中止，避免进程退出时留下半个文件。
        download_client = self._update_download_client
        if download_client is not None:
            try:
                download_client.abort()
            except RuntimeError:
                pass
        if self._online_panel is not None:
            try:
                self._online_panel.shutdown()
            except RuntimeError:
                self._online_panel = None
        if self._replay_panel is not None:
            try:
                self._replay_panel.shutdown()
            except RuntimeError:
                self._replay_panel = None
        self._session.shutdown()
        super().closeEvent(event)
