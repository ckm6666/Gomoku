"""题跋拓片卡片：自绘宣纸卡片、墨拓局面与导出。

卡片是一张"竖排题跋"的样子：题名写在左上，右上压一枚引首印，中间是
用墨拓出来的局面，下面是墨线、题跋正文与落款闲章。

与对局棋盘不同，拓片里的局面是**单色墨拓**：黑子画成实心墨点，白子
留白只勾一圈墨线。这既贴合"拓"的本意，也让整张卡片只剩墨、朱砂与
宣纸三种颜色，不与木纹漆框的棋盘抢戏。

卡片完全自绘（没有子控件），因此同一套绘制函数既能画在屏幕上，也能
直接画进 :class:`QPixmap` 导出成图片。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QSizePolicy, QWidget

from gomoku.client.colophon import Colophon
from gomoku.client.constants import STAR_POINTS
from gomoku.client.font_settings import BODY_FONT_FAMILIES
from gomoku.client.theme_palette import (
    CINNABAR,
    GOLD,
    INK,
    INK_FAINT,
    INK_MUTED,
    INK_SOFT,
    PAPER_LIGHT,
)
from gomoku.client.ui_theme import (
    DISPLAY_FONT_FAMILY,
    make_seal_pixmap,
    make_title_seal,
    paper_texture_tile,
)
from gomoku.shared.rules import BOARD_SIZE, Player, find_winning_line

#: 卡片尺寸（逻辑像素）。竖长条，像一页竖排题跋。
CARD_WIDTH: int = 340
CARD_HEIGHT: int = 600

#: 拓片卡片内边距占宽度的比例。
_PAD_RATIO: float = 0.07

#: 落款闲章的边长（逻辑像素）。
_SEAL_GLYPH_SIZE: int = 26


@dataclass(frozen=True)
class _CardLayout:
    """卡片内部的各区块位置（全部由外框尺寸推出，可任意缩放）。"""

    rect: QRectF
    title_rect: QRectF
    seal_rect: QRectF
    board_rect: QRectF
    divider_upper_rect: QRectF
    text_rect: QRectF
    divider_lower_rect: QRectF
    footer_rect: QRectF
    glyph_rect: QRectF


def _build_layout(rect: QRectF) -> _CardLayout:
    """按外框尺寸推出卡片各区块的位置。"""
    width = rect.width()
    pad = width * _PAD_RATIO
    inner = rect.adjusted(pad, pad, -pad, -pad)

    header_height = width * 0.155
    gap = width * 0.03
    footer_height = width * 0.10
    divider_height = width * 0.042
    text_height = width * 0.20

    title_rect = QRectF(
        inner.left(), inner.top(), inner.width() * 0.66, header_height
    )
    seal_rect = QRectF(
        inner.right() - header_height,
        inner.top(),
        header_height,
        header_height,
    )

    # 落款行贴底，其上依次是下墨线、题跋正文、上墨线。
    footer_rect = QRectF(
        inner.left(),
        inner.bottom() - footer_height,
        inner.width(),
        footer_height,
    )
    divider_lower_rect = QRectF(
        inner.left(),
        footer_rect.top() - gap - divider_height,
        inner.width(),
        divider_height,
    )
    text_rect = QRectF(
        inner.left(),
        divider_lower_rect.top() - gap - text_height,
        inner.width(),
        text_height,
    )
    divider_upper_rect = QRectF(
        inner.left(),
        text_rect.top() - gap - divider_height,
        inner.width(),
        divider_height,
    )

    # 局面取"上方剩余空间"与"内宽"的较小值，保证是正方形且不越界。
    board_top = seal_rect.bottom() + gap
    available = divider_upper_rect.top() - gap - board_top
    board_side = max(60.0, min(inner.width(), available))
    board_rect = QRectF(
        inner.left() + (inner.width() - board_side) / 2.0,
        board_top,
        board_side,
        board_side,
    )

    glyph_size = _SEAL_GLYPH_SIZE * (width / CARD_WIDTH)
    glyph_rect = QRectF(
        footer_rect.right() - glyph_size,
        footer_rect.top() + (footer_height - glyph_size) / 2.0,
        glyph_size,
        glyph_size,
    )

    return _CardLayout(
        rect=rect,
        title_rect=title_rect,
        seal_rect=seal_rect,
        board_rect=board_rect,
        divider_upper_rect=divider_upper_rect,
        text_rect=text_rect,
        divider_lower_rect=divider_lower_rect,
        footer_rect=footer_rect,
        glyph_rect=glyph_rect,
    )


def _paint_paper(painter: QPainter, rect: QRectF, dpr: float) -> None:
    """画宣纸底、墨框与金线。"""
    radius = 8.0
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(PAPER_LIGHT))
    painter.drawRoundedRect(rect, radius, radius)

    tile = paper_texture_tile(dpr)
    if not tile.isNull():
        painter.drawTiledPixmap(rect, tile, QPointF(0, 0))

    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(QColor(INK), 1.1))
    painter.drawRoundedRect(rect, radius, radius)

    inner = rect.adjusted(6.0, 6.0, -6.0, -6.0)
    painter.setPen(QPen(QColor(GOLD), 0.8))
    painter.drawRoundedRect(inner, radius - 2.0, radius - 2.0)


def _alpha_color(color: str, alpha: int) -> QColor:
    """在十六进制色值上附加透明度。"""
    result = QColor(color)
    result.setAlpha(alpha)
    return result


def _paint_divider(painter: QPainter, rect: QRectF) -> None:
    """画一道淡墨分隔线。"""
    y = rect.center().y()
    painter.setPen(QPen(_alpha_color(INK_SOFT, 110), 0.9))
    painter.drawLine(
        QPointF(rect.left() + 4.0, y), QPointF(rect.right() - 4.0, y)
    )


def _paint_title(painter: QPainter, rect: QRectF, title: str) -> None:
    """写题名（毛笔楷书）。"""
    if not title:
        return
    font = QFont(DISPLAY_FONT_FAMILY)
    font.setPixelSize(max(12, int(rect.height() * 0.62)))
    painter.setFont(font)
    painter.setPen(QColor(INK))
    painter.drawText(
        rect,
        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
        title,
    )


def _paint_body_text(
    painter: QPainter,
    rect: QRectF,
    text: str,
    *,
    size: float,
    color: str = INK,
) -> None:
    """写正文（仿宋/楷体优先，自动换行）。"""
    if not text:
        return
    font = QFont()
    font.setFamilies(list(BODY_FONT_FAMILIES))
    font.setPixelSize(max(9, int(size)))
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(
        rect,
        int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        | int(Qt.TextFlag.TextWordWrap),
        text,
    )


def _paint_ink_board(
    painter: QPainter,
    rect: QRectF,
    colophon: Colophon,
) -> None:
    """画墨拓局面：墨线网格 + 墨点棋子 + 朱砂点睛与连线。"""
    board = colophon.board()
    margin = rect.width() * 0.035
    grid = rect.adjusted(margin, margin, -margin, -margin)
    spacing = grid.width() / (BOARD_SIZE - 1)

    def point_for(row: int, col: int) -> QPointF:
        return QPointF(
            grid.left() + col * spacing,
            grid.top() + row * spacing,
        )

    line_width = max(0.6, spacing * 0.030)
    painter.setPen(QPen(QColor(INK_MUTED), line_width))
    for index in range(BOARD_SIZE):
        offset = index * spacing
        x = grid.left() + offset
        y = grid.top() + offset
        painter.drawLine(QPointF(x, grid.top()), QPointF(x, grid.bottom()))
        painter.drawLine(QPointF(grid.left(), y), QPointF(grid.right(), y))

    star_radius = max(1.2, spacing * 0.075)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(INK_FAINT)))
    for row, col in STAR_POINTS:
        painter.drawEllipse(point_for(row, col), star_radius, star_radius)

    radius = spacing * 0.40
    painter.setPen(Qt.PenStyle.NoPen)
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            cell = board[row][col]
            if cell is None:
                continue
            center = point_for(row, col)
            if cell is Player.BLACK:
                # 黑子：实心墨点。
                painter.setBrush(QBrush(QColor(INK)))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(center, radius, radius)
            else:
                # 白子：留白，只勾一圈墨线。
                painter.setBrush(QBrush(QColor(PAPER_LIGHT)))
                painter.setPen(QPen(QColor(INK), max(0.8, radius * 0.16)))
                painter.drawEllipse(center, radius, radius)

    focus = colophon.focus()
    side = colophon.focus_side()
    if focus is not None and side is not None:
        # 点睛：给关键手套一枚朱砂圈。
        center = point_for(*focus)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(CINNABAR), max(1.2, spacing * 0.075)))
        painter.drawEllipse(
            center, radius * 1.32, radius * 1.32
        )

        line = find_winning_line(board, focus[0], focus[1], side)
        if len(line) >= 2:
            line_width = max(1.4, spacing * 0.085)
            start = point_for(*line[0])
            end = point_for(*line[-1])
            # 先铺一层纸色光晕：连线正好压在黑棋上，不垫底就看不见。
            painter.setPen(
                QPen(
                    QColor(PAPER_LIGHT),
                    line_width + max(2.0, spacing * 0.06),
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(start, end)
            painter.setPen(
                QPen(
                    QColor(CINNABAR),
                    line_width,
                    Qt.PenStyle.SolidLine,
                    Qt.PenCapStyle.RoundCap,
                )
            )
            painter.drawLine(start, end)


def _format_date(value: str) -> str:
    """把 ISO 时间串格式化成落款用的短日期。"""
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:10]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
    return parsed.strftime("%Y-%m-%d")


def _footer_text(colophon: Colophon) -> str:
    """拼落款文字：黑白双方、手数与日期。"""
    parts: list[str] = []
    if colophon.black_name:
        parts.append(f"黑 {colophon.black_name}")
    if colophon.white_name:
        parts.append(f"白 {colophon.white_name}")
    if colophon.ply:
        parts.append(f"第 {colophon.ply} 手")
    date_text = _format_date(colophon.created_at)
    if date_text:
        parts.append(date_text)
    return "　".join(parts)


def paint_colophon(
    painter: QPainter,
    rect: QRectF,
    colophon: Colophon,
    *,
    dpr: float = 1.0,
) -> None:
    """把一份拓片画进给定矩形。

    这是卡片与导出共用的唯一绘制入口：屏幕上画进控件矩形，导出时画进
    离屏 :class:`QPixmap`，两边的效果完全一致。
    """
    layout = _build_layout(rect)
    _paint_paper(painter, layout.rect, dpr)

    _paint_title(painter, layout.title_rect, colophon.title)

    # 题名为空时不刻引首印：印面是朱砂底，四个字都没写就画一个空
    # 红方块，看着像刻坏了的印。
    if colophon.title.strip():
        seal = make_title_seal(
            colophon.title,
            int(layout.seal_rect.height()),
            dpr=dpr,
        )
        painter.drawPixmap(
            QPointF(layout.seal_rect.topLeft()), seal
        )

    _paint_ink_board(painter, layout.board_rect, colophon)

    _paint_divider(painter, layout.divider_upper_rect)

    text_size = layout.text_rect.height() * 0.26
    _paint_body_text(
        painter,
        layout.text_rect,
        colophon.colophon,
        size=text_size,
    )

    _paint_divider(painter, layout.divider_lower_rect)

    footer_rect = layout.footer_rect.adjusted(
        0.0, 0.0, -layout.glyph_rect.width(), 0.0
    )
    _paint_body_text(
        painter,
        footer_rect,
        _footer_text(colophon),
        size=footer_rect.height() * 0.42,
        color=INK_MUTED,
    )

    if colophon.seal_glyph:
        glyph = make_seal_pixmap(
            colophon.seal_glyph,
            int(layout.glyph_rect.width()),
            dpr=dpr,
        )
        painter.drawPixmap(QPointF(layout.glyph_rect.topLeft()), glyph)


class ColophonCard(QWidget):
    """一张题跋拓片卡片。"""

    def __init__(
        self,
        colophon: Colophon,
        parent: Optional[QWidget] = None,
        *,
        width: int = CARD_WIDTH,
        height: int = CARD_HEIGHT,
    ) -> None:
        super().__init__(parent)
        self._colophon = colophon
        self.setFixedSize(width, height)
        self.setSizePolicy(
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed
        )

    def colophon(self) -> Colophon:
        """返回当前展示的拓片。"""
        return self._colophon

    def set_colophon(self, colophon: Colophon) -> None:
        """换一张拓片并重绘。"""
        self._colophon = colophon
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        paint_colophon(
            painter,
            QRectF(self.rect()),
            self._colophon,
            dpr=self.devicePixelRatioF(),
        )
        painter.end()

    def to_pixmap(self) -> QPixmap:
        """把卡片渲染成一张离屏位图，用于导出图片。"""
        return render_pixmap(
            self._colophon,
            self.width(),
            self.height(),
            dpr=self.devicePixelRatioF(),
        )


def render_pixmap(
    colophon: Colophon,
    width: int = CARD_WIDTH,
    height: int = CARD_HEIGHT,
    *,
    dpr: float = 1.0,
) -> QPixmap:
    """把一份拓片渲染成离屏位图。

    网格里的卡片是缩到 300 宽的，导出时要按整幅 340 宽来渲染：导出
    的图是给玩家留着看的，不该跟着网格一起缩水。
    """
    pixmap = QPixmap(round(width * dpr), round(height * dpr))
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    paint_colophon(
        painter,
        QRectF(0, 0, width, height),
        colophon,
        dpr=dpr,
    )
    painter.end()
    return pixmap


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "ColophonCard",
    "paint_colophon",
    "render_pixmap",
]
