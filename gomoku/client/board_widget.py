"""自绘五子棋棋盘控件。

职责：
* 使用 QPainter 绘制木纹棋盘、网格、星位、黑白棋子、最后落子和胜利连线；
* 将鼠标位置转换为棋盘交叉点；
* 在合法点击时发出 ``cell_clicked(row, col)`` 信号。

控件只消费不可变的 BoardView 快照，不直接持有或修改对局模型，也不弹出
任何对话框；鼠标点击通过信号交给主窗口/会话层处理。
"""

from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QLinearGradient,
    QMouseEvent,
    QPaintEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
    QResizeEvent,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from gomoku.client.constants import (
    BOARD_CORNER_RADIUS_RATIO,
    BOARD_FRAME_GOLD,
    BOARD_FRAME_INK,
    BOARD_FRAME_THICKNESS_RATIO,
    BOARD_GRID_MARGIN_RATIO,
    BOARD_GRAIN_LINE_COUNT,
    BOARD_LACQUER_BOTTOM,
    BOARD_LACQUER_TOP,
    BOARD_MIN_SIZE,
    BOARD_OUTER_PADDING,
    BOARD_SUGGESTED_SIZE,
    BOARD_SURFACE_CORNER_RATIO,
    CLICK_TOLERANCE_RATIO,
    GRID_COLOR,
    HINT_MARKER_COLOR,
    LAST_MOVE_COLOR,
    STAR_COLOR,
    STAR_POINTS,
    STONE_BLACK_BORDER,
    STONE_BLACK_EDGE,
    STONE_BLACK_HIGHLIGHT,
    STONE_BLACK_MIDDLE,
    STONE_RADIUS_RATIO,
    STONE_WHITE_BORDER,
    STONE_WHITE_EDGE,
    STONE_WHITE_HIGHLIGHT,
    STONE_WHITE_MIDDLE,
    WIN_HIGHLIGHT_COLOR,
    WOOD_BORDER_COLOR,
    WOOD_BOTTOM_COLOR,
    WOOD_GRAIN_COLOR,
    WOOD_MIDDLE_COLOR,
    WOOD_TOP_COLOR,
)
from gomoku.client.board_view import BoardView
from gomoku.client.ui_theme import make_seal_pixmap, paint_corner_ornament
from gomoku.shared.rules import BOARD_SIZE, Player


class BoardWidget(QWidget):
    """可点击、自适应尺寸的五子棋棋盘。"""

    # 参数为行、列坐标；仅在棋盘可落子且点击位置合法时发出。
    cell_clicked = Signal(int, int)

    def __init__(self, view: BoardView, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._view = view

        # 用于计算落子点、点击命中和绘制位置的几何缓存。
        self._panel_rect = QRectF()
        self._surface_rect = QRectF()
        self._grid_rect = QRectF()
        self._spacing = 0.0

        # 画框、木面与网格是静态的；按尺寸/DPR 缓存，避免悬停重绘时反复绘制。
        self._static_cache: Optional[QPixmap] = None
        self._static_cache_key: Optional[tuple[int, int, float]] = None

        # 当前鼠标悬停的空交叉点；None 表示不显示预落子预览。
        self._hover_cell: Optional[tuple[int, int]] = None
        # AI 提示标记及其对应的手数；手数变化后自动失效，避免旧提示残留。
        self._hint_cell: Optional[tuple[int, int]] = None
        self._hint_move_count = -1
        # AI 回合或终局时由主窗口关闭此开关，阻止鼠标事件与悬停高亮。
        self._interaction_enabled = True

        self.setMouseTracking(True)
        self.setMinimumSize(BOARD_MIN_SIZE, BOARD_MIN_SIZE)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        self.setAccessibleName("五子棋棋盘")

    # ------------------------------------------------------------------
    # 尺寸与基础设置
    # ------------------------------------------------------------------
    def sizeHint(self) -> QSize:
        """给布局系统一个接近正方形的大尺寸建议值。"""
        return QSize(BOARD_SUGGESTED_SIZE, BOARD_SUGGESTED_SIZE)

    def refresh(self) -> None:
        """在状态发生变化后请求重绘。"""
        self.update()

    def set_view(self, view: BoardView) -> None:
        """替换棋盘只读快照。

        快照来自 Local/AI/未来的 OnlineSession；棋盘控件不保存模型引用，
        因此服务器快照和本地模型可以走完全相同的绘制路径。
        """
        self._view = view
        # 状态切换时清除旧悬停，避免上一回合的幽灵棋子短暂残留。
        self._set_hover_cell(None)
        self.update()

    def set_hint_cell(
        self,
        cell: Optional[tuple[int, int]],
        move_count: int,
    ) -> None:
        """设置 AI 推荐落点标记；``cell`` 为 None 时清除。"""
        if cell is None:
            self.clear_hint()
            return
        row, col = cell
        self._hint_cell = (int(row), int(col))
        self._hint_move_count = int(move_count)
        self.update()

    def clear_hint(self) -> None:
        """清除推荐落点标记。"""
        if self._hint_cell is None:
            return
        self._hint_cell = None
        self._hint_move_count = -1
        self.update()

    def set_interaction_enabled(self, enabled: bool) -> None:
        """开启/关闭玩家通过棋盘落子的能力。

        这只影响“人类输入”，不影响 AI 通过主窗口提交的落子。禁用时会
        立即清除悬停幽灵棋和手型光标，避免玩家误以为还能点击。
        """
        if enabled == self._interaction_enabled:
            return

        self._interaction_enabled = enabled
        if not enabled:
            self._set_hover_cell(None)
        self.update()

    def board_rect(self) -> QRectF:
        """返回棋盘绘制区域（居中的正方形画框）在控件坐标中的矩形。

        棋盘控件可能是非正方形的，棋面永远画在居中的正方形里。覆盖层
        （弹幕/表情）需要用同一套坐标，动画才会贴着棋盘走，而不是落在
        控件四周的留白上。
        """
        self._calculate_geometry()
        return QRectF(self._panel_rect)

    # ------------------------------------------------------------------
    # QWidget 事件
    # ------------------------------------------------------------------
    def resizeEvent(self, event: QResizeEvent) -> None:
        """窗口尺寸变化时重新计算棋盘几何并清除旧缓存。"""
        super().resizeEvent(event)
        self._calculate_geometry()
        self._static_cache = None
        self._static_cache_key = None
        self._set_hover_cell(None)

    def paintEvent(self, event: QPaintEvent) -> None:
        """先绘制缓存的画框/木面/网格，再叠加棋子与交互层。"""
        del event  # Qt 的事件对象在本次绘制中无需读取。

        self._calculate_geometry()
        if self._spacing <= 0:
            self._static_cache = None
            self._static_cache_key = None
            return

        self._ensure_static_cache()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        if self._static_cache is not None:
            painter.drawPixmap(
                QRectF(0, 0, self.width(), self.height()),
                self._static_cache,
                QRectF(self._static_cache.rect()),
            )

        self._draw_stone_shadows(painter)
        self._draw_stones(painter)
        self._draw_winning_line(painter)
        self._draw_hover_preview(painter)
        self._draw_last_move_marker(painter)
        self._draw_hint_marker(painter)
        painter.end()

    def _ensure_static_cache(self) -> None:
        """按控件尺寸和设备像素比缓存静态棋盘层。"""
        dpr = max(1.0, self.devicePixelRatioF())
        key = (self.width(), self.height(), round(dpr, 3))
        if (
            self._static_cache is not None
            and self._static_cache_key == key
        ):
            return

        pixmap = QPixmap(
            round(self.width() * dpr), round(self.height() * dpr)
        )
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        self._draw_board_surface(painter)
        self._draw_grid(painter)
        painter.end()

        self._static_cache = pixmap
        self._static_cache_key = key

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        """根据鼠标位置更新半透明预落子棋子。"""
        if not self._interaction_enabled or self._view.is_game_over:
            self._set_hover_cell(None)
        else:
            cell = self._cell_at(event.position())
            if cell is not None:
                row, col = cell
                if self._view.board[row][col] is not None:
                    cell = None
            self._set_hover_cell(cell)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        """鼠标离开棋盘时隐藏预落子。"""
        self._set_hover_cell(None)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        """仅响应左键点击；合法空位才向主窗口发出落子信号。"""
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._interaction_enabled
            and not self._view.is_game_over
        ):
            cell = self._cell_at(event.position())
            if cell is not None:
                row, col = cell
                if self._view.board[row][col] is None:
                    self.cell_clicked.emit(row, col)
                    # 落子后当前预落子位置已经变成实际棋子，清除预览。
                    self._set_hover_cell(None)
                    self.update()
                    return

        super().mousePressEvent(event)

    # ------------------------------------------------------------------
    # 几何与点击命中
    # ------------------------------------------------------------------
    def _calculate_geometry(self) -> None:
        """根据控件当前尺寸计算居中正方形面板与网格几何。"""
        width = float(self.width())
        height = float(self.height())
        side = min(width, height) - 2.0 * BOARD_OUTER_PADDING

        if side <= 0:
            self._panel_rect = QRectF()
            self._surface_rect = QRectF()
            self._grid_rect = QRectF()
            self._spacing = 0.0
            return

        left = (width - side) / 2.0
        top = (height - side) / 2.0
        self._panel_rect = QRectF(left, top, side, side)

        frame = side * BOARD_FRAME_THICKNESS_RATIO
        self._surface_rect = self._panel_rect.adjusted(
            frame, frame, -frame, -frame
        )

        margin = self._surface_rect.width() * BOARD_GRID_MARGIN_RATIO
        self._grid_rect = self._surface_rect.adjusted(
            margin, margin, -margin, -margin
        )
        self._spacing = self._grid_rect.width() / (BOARD_SIZE - 1)

    def _point_for(self, row: int, col: int) -> QPointF:
        """把棋盘行列坐标转换为控件内的绘制中心点。"""
        return QPointF(
            self._grid_rect.left() + col * self._spacing,
            self._grid_rect.top() + row * self._spacing,
        )

    def _cell_at(self, point: QPointF) -> Optional[tuple[int, int]]:
        """找到鼠标附近最近的交叉点。

        只有距离交叉点中心足够近才视为命中，避免在木框或两格之间
        的空白位置误落子；越界时返回 None。
        """
        if self._spacing <= 0:
            return None

        col_float = (point.x() - self._grid_rect.left()) / self._spacing
        row_float = (point.y() - self._grid_rect.top()) / self._spacing
        col = int(round(col_float))
        row = int(round(row_float))

        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return None

        center = self._point_for(row, col)
        if math.hypot(point.x() - center.x(), point.y() - center.y()) > (
            self._spacing * CLICK_TOLERANCE_RATIO
        ):
            return None

        return row, col

    def _set_hover_cell(self, cell: Optional[tuple[int, int]]) -> None:
        """更新悬停交叉点及鼠标指针样式，必要时触发重绘。"""
        if cell == self._hover_cell:
            return

        self._hover_cell = cell
        if cell is None:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        else:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.update()

    # ------------------------------------------------------------------
    # 绘制辅助函数
    # ------------------------------------------------------------------
    def _draw_board_surface(self, painter: QPainter) -> None:
        """绘制深色漆框、金线、回纹角饰与内嵌暖木棋面。"""
        panel = self._panel_rect
        surface = self._surface_rect
        if panel.width() <= 0 or surface.width() <= 0:
            return

        outer_radius = panel.width() * BOARD_CORNER_RADIUS_RATIO
        surface_radius = surface.width() * BOARD_SURFACE_CORNER_RATIO
        frame = (panel.width() - surface.width()) / 2.0

        # 柔和投影，让画框与宣纸背景自然分离。
        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(30, 20, 10, 42))
        painter.drawRoundedRect(
            panel.translated(0.0, 7.0), outer_radius, outer_radius
        )
        painter.setBrush(QColor(30, 20, 10, 55))
        painter.drawRoundedRect(
            panel.translated(0.0, 3.0), outer_radius, outer_radius
        )
        painter.restore()

        # 深色漆框。
        lacquer = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        lacquer.setColorAt(0.0, QColor(BOARD_LACQUER_TOP))
        lacquer.setColorAt(1.0, QColor(BOARD_LACQUER_BOTTOM))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(lacquer))
        painter.drawRoundedRect(panel, outer_radius, outer_radius)

        # 框内细金线。
        gold_rect = panel.adjusted(3.0, 3.0, -3.0, -3.0)
        gold = QColor(BOARD_FRAME_GOLD)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(
            QPen(gold, max(0.9, panel.width() / 700.0))
        )
        painter.drawRoundedRect(gold_rect, outer_radius * 0.9, outer_radius * 0.9)

        # 回纹角饰（右下留给朱砂印）。
        ornament = QColor(BOARD_FRAME_GOLD)
        ornament.setAlpha(120)
        ornament_size = max(10.0, frame * 0.62)
        for corner in ("tl", "tr", "bl"):
            paint_corner_ornament(
                painter,
                panel,
                corner,
                ornament,
                size=ornament_size,
                inset=frame * 0.32,
            )

        # 内嵌棋面：先画一圈暗色倒角，再铺木纹。
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(18, 11, 5, 90))
        painter.drawRoundedRect(
            surface.translated(0.0, 1.8), surface_radius, surface_radius
        )

        wood = QLinearGradient(surface.topLeft(), surface.bottomLeft())
        wood.setColorAt(0.0, QColor(WOOD_TOP_COLOR))
        wood.setColorAt(0.5, QColor(WOOD_MIDDLE_COLOR))
        wood.setColorAt(1.0, QColor(WOOD_BOTTOM_COLOR))

        surface_path = QPainterPath()
        surface_path.addRoundedRect(surface, surface_radius, surface_radius)
        painter.save()
        painter.setClipPath(surface_path)
        painter.setBrush(QBrush(wood))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(surface, surface_radius, surface_radius)

        # 低对比木纹曲线。
        for index in range(BOARD_GRAIN_LINE_COUNT):
            ratio = (index + 1) / (BOARD_GRAIN_LINE_COUNT + 1)
            y = surface.top() + surface.height() * ratio
            amplitude = surface.height() * (0.006 + 0.004 * (index % 3))
            direction = 1.0 if index % 2 == 0 else -1.0
            grain_path = QPainterPath(QPointF(surface.left(), y))
            grain_path.cubicTo(
                QPointF(
                    surface.left() + surface.width() * 0.28,
                    y + amplitude,
                ),
                QPointF(
                    surface.left() + surface.width() * 0.68,
                    y - amplitude * direction,
                ),
                QPointF(
                    surface.right(),
                    y + amplitude * 0.4 * direction,
                ),
            )
            grain_color = QColor(WOOD_GRAIN_COLOR)
            grain_color.setAlpha(14 + (index % 4) * 4)
            painter.setPen(
                QPen(grain_color, max(0.8, surface.width() / 720.0))
            )
            painter.drawPath(grain_path)

        # 轻微渐晕与左上柔光，增加木面层次。
        vignette = QRadialGradient(
            surface.center(), surface.width() * 0.72
        )
        vignette.setColorAt(0.0, QColor(0, 0, 0, 0))
        vignette.setColorAt(0.72, QColor(0, 0, 0, 0))
        vignette.setColorAt(1.0, QColor(40, 22, 8, 26))
        painter.fillRect(surface, QBrush(vignette))

        sheen = QLinearGradient(surface.topLeft(), surface.bottomRight())
        sheen.setColorAt(0.0, QColor(255, 244, 220, 14))
        sheen.setColorAt(0.45, QColor(255, 244, 220, 0))
        painter.fillRect(surface, QBrush(sheen))
        painter.restore()

        # 棋面细金线 + 内侧墨线。
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(gold, max(0.8, surface.width() / 900.0)))
        painter.drawRoundedRect(
            surface, surface_radius, surface_radius
        )
        inner = surface.adjusted(2.0, 2.0, -2.0, -2.0)
        ink_line = QColor(BOARD_FRAME_INK)
        ink_line.setAlpha(70)
        painter.setPen(QPen(ink_line, 0.7))
        painter.drawRoundedRect(
            inner, surface_radius * 0.8, surface_radius * 0.8
        )

        # 右下角朱砂小印。
        seal_size = int(min(28.0, max(0.0, frame * 0.62)))
        if seal_size >= 10:
            seal = make_seal_pixmap(
                "弈", seal_size, dpr=self.devicePixelRatioF()
            )
            seal_x = panel.right() - frame * 0.52 - seal_size / 2.0
            seal_y = panel.bottom() - frame * 0.52 - seal_size / 2.0
            painter.drawPixmap(
                QRectF(seal_x, seal_y, seal_size, seal_size),
                seal,
                QRectF(seal.rect()),
            )

    def _draw_grid(self, painter: QPainter) -> None:
        """绘制 15×15 网格线和五个星位。"""
        grid = self._grid_rect
        line_width = max(1.0, self._spacing * 0.028)
        painter.setPen(QPen(QColor(GRID_COLOR), line_width))

        for index in range(BOARD_SIZE):
            offset = index * self._spacing
            x = grid.left() + offset
            y = grid.top() + offset
            painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.bottom()))
            painter.drawLine(QPointF(grid.left(), y), QPointF(grid.right(), y))

        # 外框略粗，增强棋盘边界感。
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(GRID_COLOR), line_width * 1.6))
        painter.drawRect(grid)

        # 星位圆点。
        star_radius = max(2.0, self._spacing * 0.082)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(STAR_COLOR)))
        for row, col in STAR_POINTS:
            painter.drawEllipse(self._point_for(row, col), star_radius, star_radius)

    def _stone_radius(self) -> float:
        """按网格间距计算棋子半径。"""
        return self._spacing * STONE_RADIUS_RATIO

    def _draw_stone_shadows(self, painter: QPainter) -> None:
        """先统一绘制所有棋子的轻阴影，避免后画的棋子盖住前一个阴影。"""
        radius = self._stone_radius()
        offset = QPointF(self._spacing * 0.035, self._spacing * 0.055)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(35, 22, 10, 52)))

        board = self._view.board
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                if board[row][col] is not None:
                    center = self._point_for(row, col) + offset
                    painter.drawEllipse(center, radius, radius)

    def _draw_stones(self, painter: QPainter) -> None:
        """绘制全部已有黑白棋子。"""
        radius = self._stone_radius()
        board = self._view.board
        for row in range(BOARD_SIZE):
            for col in range(BOARD_SIZE):
                stone = board[row][col]
                if stone is not None:
                    self._paint_stone(
                        painter,
                        self._point_for(row, col),
                        stone,
                        radius,
                    )

    def _paint_stone(
        self,
        painter: QPainter,
        center: QPointF,
        player: Player,
        radius: float,
    ) -> None:
        """绘制单个带渐变、描边和高光的棋子。"""
        # 将渐变中心稍微朝左上偏移，模拟固定环境光下的球形质感。
        highlight_center = QPointF(
            center.x() - radius * 0.34,
            center.y() - radius * 0.36,
        )
        gradient = QRadialGradient(highlight_center, radius * 1.45)

        if player is Player.BLACK:
            gradient.setColorAt(0.0, QColor(STONE_BLACK_HIGHLIGHT))
            gradient.setColorAt(0.34, QColor(STONE_BLACK_MIDDLE))
            gradient.setColorAt(1.0, QColor(STONE_BLACK_EDGE))
            border_color = QColor(STONE_BLACK_BORDER)
        else:
            gradient.setColorAt(0.0, QColor(STONE_WHITE_HIGHLIGHT))
            gradient.setColorAt(0.55, QColor(STONE_WHITE_MIDDLE))
            gradient.setColorAt(1.0, QColor(STONE_WHITE_EDGE))
            border_color = QColor(STONE_WHITE_BORDER)

        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(border_color, max(0.8, radius * 0.065)))
        painter.drawEllipse(center, radius, radius)

        # 在棋子顶部加一小段柔和反光，提高两种棋子的可辨识度。
        glint_color = QColor(255, 255, 255, 72 if player is Player.BLACK else 115)
        glint_rect = QRectF(
            center.x() - radius * 0.48,
            center.y() - radius * 0.64,
            radius * 0.55,
            radius * 0.34,
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(glint_color))
        painter.drawEllipse(glint_rect)

    def _draw_winning_line(self, painter: QPainter) -> None:
        """在获胜的五颗或更多棋子上绘制醒目但半透明的连线与圆环。"""
        line = self._view.winning_line
        if len(line) < 2:
            return

        start = self._point_for(*line[0])
        end = self._point_for(*line[-1])
        line_width = max(2.0, self._spacing * 0.075)

        painter.save()
        # 先铺一层浅色光晕，保证在深色棋盘/黑棋上也足够醒目。
        halo_pen = QPen(
            QColor(255, 248, 235, 135),
            line_width + max(2.0, self._spacing * 0.07),
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
        painter.setPen(halo_pen)
        painter.drawLine(start, end)

        win_pen = QPen(
            QColor(WIN_HIGHLIGHT_COLOR),
            line_width,
            Qt.PenStyle.SolidLine,
            Qt.PenCapStyle.RoundCap,
        )
        painter.setPen(win_pen)
        painter.drawLine(start, end)

        # 为每颗获胜棋子画细圆环，强调完整连子而不是只标两端。
        ring_pen = QPen(QColor(255, 246, 232, 215), max(1.2, line_width * 0.45))
        painter.setPen(ring_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        ring_radius = self._stone_radius() * 1.08
        for row, col in line:
            painter.drawEllipse(
                self._point_for(row, col),
                ring_radius,
                ring_radius,
            )
        painter.restore()

    def _draw_hover_preview(self, painter: QPainter) -> None:
        """在对局进行中、鼠标悬停空位时画半透明预落子。"""
        if (
            not self._interaction_enabled
            or self._view.is_game_over
            or self._hover_cell is None
        ):
            return

        row, col = self._hover_cell
        if self._view.board[row][col] is not None:
            return

        painter.save()
        painter.setOpacity(0.36)
        self._paint_stone(
            painter,
            self._point_for(row, col),
            self._view.current_player,
            self._stone_radius(),
        )
        painter.restore()

    def _draw_last_move_marker(self, painter: QPainter) -> None:
        """在最后一手棋子中心绘制朱红色小点。"""
        last_move = self._view.last_move
        if last_move is None:
            return

        radius = max(2.0, self._spacing * 0.075)
        center = self._point_for(last_move.row, last_move.col)
        painter.setPen(QPen(QColor(255, 250, 239, 150), max(0.8, radius * 0.24)))
        painter.setBrush(QBrush(QColor(LAST_MOVE_COLOR)))
        painter.drawEllipse(center, radius, radius)

    def _draw_hint_marker(self, painter: QPainter) -> None:
        """绘制 AI 推荐落点的朱砂虚线圆环与四角短刻度。"""
        if self._hint_cell is None or self._view.is_game_over:
            return
        # 手数不一致说明提示已过期（玩家已落子/悔棋/重开）。
        if self._view.move_count != self._hint_move_count:
            return

        row, col = self._hint_cell
        if not (0 <= row < BOARD_SIZE and 0 <= col < BOARD_SIZE):
            return
        if self._view.board[row][col] is not None:
            return

        center = self._point_for(row, col)
        radius = self._stone_radius() * 1.05
        pen = QPen(
            QColor(HINT_MARKER_COLOR),
            max(1.6, self._spacing * 0.045),
            Qt.PenStyle.DashLine,
        )
        pen.setDashPattern([3.0, 2.4])
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)

        painter.save()
        painter.setOpacity(0.92)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(center, radius, radius)

        for angle in (
            math.pi / 4,
            3 * math.pi / 4,
            5 * math.pi / 4,
            7 * math.pi / 4,
        ):
            inner = QPointF(
                center.x() + math.cos(angle) * radius * 1.02,
                center.y() + math.sin(angle) * radius * 1.02,
            )
            outer = QPointF(
                center.x() + math.cos(angle) * radius * 1.30,
                center.y() + math.sin(angle) * radius * 1.30,
            )
            painter.drawLine(inner, outer)
        painter.restore()
