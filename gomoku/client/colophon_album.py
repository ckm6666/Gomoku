"""拓片册页面：网格浏览已保存的题跋拓片。

拓片是玩家写给自己的收藏（见 :mod:`gomoku.client.colophon`），这一页把
它们摆成一册可以翻看、导出、删去的手稿。它是主窗口页面栈中的一页，与
复盘页一样不另开窗口。

三处特别在意的地方：

* **列数随窗口宽度走。** 卡片是固定 340×600 的竖条，而默认窗口只有
  720 宽，硬摆三列会把卡片挤出可视区；因此按可用宽度算列数，最多三列。
* **确认走窗口内遮罩。** 应用全局只保留一个原生窗口，删拓片前用
  :class:`gomoku.client.confirm_dialog.ConfirmDialog` 确认，不弹
  ``QMessageBox``。
* **导出失败不弹窗。** 导出结果写进页内状态行，失败只记日志。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.colophon import Colophon, ColophonStore, colophons_to_sgf
from gomoku.client.colophon_card import (
    ColophonCard,
    render_pixmap,
)
from gomoku.client.confirm_dialog import ConfirmDialog
from gomoku.client.theme_palette import CINNABAR, PAPER_MUTED
from gomoku.client.ui_theme import (
    GAME_BACKDROP_SPEC,
    InkBackdrop,
    RicePaperCard,
)

logger = logging.getLogger(__name__)

#: 一行最多摆几张拓片。卡片是竖长条，三列已经占满常见窗口宽度。
MAX_COLUMNS: int = 3

#: 网格里显示用的卡片尺寸。
#:
#: 整幅拓片有 340 宽（导出 PNG 时按这个尺寸），而默认窗口只有 720，
#: 两列并排放不下。网格里缩到 300 宽，两列才排得开——两幅并排也更像
#: 一册翻开的书，而不是孤零零挂一张。
DISPLAY_CARD_WIDTH: int = 300
DISPLAY_CARD_HEIGHT: int = 530

#: 网格中相邻卡片的间距。
GRID_SPACING: int = 16

#: 卡片外框留白（逻辑像素），让墨框与拓片之间透一口气。
CARD_MARGIN: int = 6

#: 卡片外框的墨线宽度（逻辑像素）。选中与未选中同宽，避免切换时抖动。
CARD_BORDER: int = 2

#: 网格内容区的左右留白。
GRID_MARGIN: int = 8


class _CardFrame(QFrame):
    """拓片卡片的外框：只画一圈墨框表示选中态。

    拓片本身完全由 :class:`ColophonCard` 自绘，外框不碰它，只在外围
    加一道朱砂线表示"这一张被选中"。
    """

    def __init__(
        self,
        colophon: Colophon,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("colophonAlbumCard")
        self.setFrameShape(QFrame.Shape.Box)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            CARD_MARGIN, CARD_MARGIN, CARD_MARGIN, CARD_MARGIN
        )
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.card = ColophonCard(
            colophon,
            self,
            width=DISPLAY_CARD_WIDTH,
            height=DISPLAY_CARD_HEIGHT,
        )
        layout.addWidget(self.card)
        # 外框比卡片大一圈：留给边距与墨线，尺寸写死以免重排时抖动。
        self.setFixedSize(
            DISPLAY_CARD_WIDTH + CARD_MARGIN * 2 + CARD_BORDER * 2,
            DISPLAY_CARD_HEIGHT + CARD_MARGIN * 2 + CARD_BORDER * 2,
        )
        self.set_selected(False)

    def set_selected(self, selected: bool) -> None:
        """切换选中态：朱砂框表示选中，淡墨框表示未选。"""
        color = CINNABAR if selected else PAPER_MUTED
        self.setStyleSheet(
            "QFrame#colophonAlbumCard {"
            f" border: {CARD_BORDER}px solid {color};"
            " border-radius: 10px; background: transparent; }"
        )


class ColophonAlbum(InkBackdrop):
    """拓片册：网格浏览已保存的题跋。"""

    #: 用户点击"关闭"时发出，由主窗口切回来源页面。
    closed = Signal()

    def __init__(
        self,
        store: ColophonStore,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent, spec=GAME_BACKDROP_SPEC)
        self.setObjectName("colophonAlbum")
        self._store = store
        self._colophons: tuple[Colophon, ...] = ()
        self._by_id: dict[str, Colophon] = {}
        self._frames: dict[str, _CardFrame] = {}
        self._cards: dict[str, ColophonCard] = {}
        self._selected_id = ""
        self._columns = 0
        self._rebuilding = False
        self._scroll: Optional[QScrollArea] = None

        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def set_store(self, store: ColophonStore) -> None:
        """换一份册子并刷新。"""
        self._store = store
        self.refresh()

    def refresh(self) -> None:
        """重新读取册子并重建网格。"""
        try:
            loaded = self._store.read_all()
        except Exception:
            # 册子文件损坏不该让整页崩掉：记日志，退回空册。
            logger.exception("读取拓片册子失败")
            loaded = None

        if loaded is None:
            # "册子坏了"和"册子是空的"必须分开说：后者是玩家还没存过
            # 拓片，前者是文件损坏或版本对不上。把坏的显示成空的，玩家
            # 会以为自己的收藏凭空没了。
            colophons: tuple[Colophon, ...] = ()
            self._set_status("册子读取失败，暂时显示空册。")
        else:
            colophons = loaded

        self._colophons = colophons
        self._by_id = {item.id: item for item in colophons}
        self._selected_id = ""
        self._rebuild_grid()

    # ------------------------------------------------------------------
    # 可见性
    # ------------------------------------------------------------------
    def showEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().showEvent(event)
        # 册子可能在别处添过拓片：每次露面都重读一遍。
        self.refresh()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 接口
        super().resizeEvent(event)
        self._update_columns()

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
        title = QLabel("拓片册", self)
        title.setObjectName("brandTitle")
        self.count_label = QLabel("还没有拓片", self)
        self.count_label.setObjectName("brandSubtitle")
        title_layout.addWidget(title)
        title_layout.addWidget(self.count_label)
        header.addLayout(title_layout)
        header.addStretch(1)

        self.export_button = QPushButton("导出整册 SGF", self)
        self.export_button.setObjectName("smallButton")
        self.export_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_button.setToolTip("把整册拓片导出成一个 SGF 集")
        self.export_button.clicked.connect(self._export_album_sgf)
        self.close_button = QPushButton("关闭", self)
        self.close_button.setObjectName("undoButton")
        self.close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.close_button.clicked.connect(
            lambda _checked=False: self.closed.emit()
        )
        header.addWidget(
            self.export_button, 0, Qt.AlignmentFlag.AlignVCenter
        )
        header.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(header)

        self.body = QStackedWidget(self)
        self.body.setObjectName("colophonAlbumBody")
        self.body.addWidget(self._build_grid_page())
        self.body.addWidget(self._build_empty_page())
        root.addWidget(self.body, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status_label = QLabel("", self)
        self.status_label.setObjectName("hintLabel")
        footer.addWidget(self.status_label, 1)
        self.selection_label = QLabel("未选中拓片", self)
        self.selection_label.setObjectName("moveInfo")
        footer.addWidget(self.selection_label, 0, Qt.AlignmentFlag.AlignVCenter)
        self.export_png_button = QPushButton("导出 PNG", self)
        self.export_png_button.setObjectName("undoButton")
        self.export_png_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.export_png_button.setToolTip("把选中的拓片存成一张图片")
        self.export_png_button.clicked.connect(self._export_png)
        self.delete_button = QPushButton("删除", self)
        self.delete_button.setObjectName("restartButton")
        self.delete_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.delete_button.setToolTip("从册子里删去选中的拓片")
        self.delete_button.clicked.connect(self._delete_selected)
        footer.addWidget(self.export_png_button, 0, Qt.AlignmentFlag.AlignVCenter)
        footer.addWidget(self.delete_button, 0, Qt.AlignmentFlag.AlignVCenter)
        root.addLayout(footer)

    def _build_grid_page(self) -> QWidget:
        """拓片网格：滚动区域里摆一张网格，溢出交给滚动条。"""
        scroll = QScrollArea(self)
        scroll.setObjectName("colophonAlbumScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        viewport = scroll.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        self._scroll = scroll

        container = QWidget(scroll)
        self._grid_widget = container
        # 网格自己撑不满容器，外面用两个弹性空白把它夹在中间。
        # 直接给网格设 AlignHCenter 没用：网格被拉伸到容器全宽，
        # 里面的列还是贴着左边排。
        outer = QHBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        grid_host = QWidget(container)
        grid_host.setSizePolicy(
            QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred
        )
        self._grid_host = grid_host
        self._grid = QGridLayout(grid_host)
        self._grid.setContentsMargins(
            GRID_MARGIN, GRID_MARGIN, GRID_MARGIN, GRID_MARGIN
        )
        self._grid.setSpacing(GRID_SPACING)
        self._grid.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addStretch(1)
        outer.addWidget(grid_host, 0, Qt.AlignmentFlag.AlignTop)
        outer.addStretch(1)
        scroll.setWidget(container)
        # setWidget() 会把容器重新设成自动填底色；这里再关掉，
        # 让页面自己的宣纸背景透上来。
        container.setAutoFillBackground(False)
        viewport.installEventFilter(self)
        return scroll

    def _build_empty_page(self) -> QWidget:
        """空册页面：一张宣纸卡片，说明去哪里拓片。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)

        card = RicePaperCard(page, seal_text="册", opaque=True)
        card.setFixedWidth(430)
        inner = QVBoxLayout(card)
        inner.setContentsMargins(34, 26, 34, 30)
        inner.setSpacing(8)
        title = QLabel("还没有拓片", card)
        title.setObjectName("sectionTitle")
        hint = QLabel(
            "在终局或复盘页点『拓片』，为对局刻下一张。", card
        )
        hint.setObjectName("hintLabel")
        hint.setWordWrap(True)
        inner.addWidget(title)
        inner.addWidget(hint)

        layout.addWidget(card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------
    # 网格
    # ------------------------------------------------------------------
    def _rebuild_grid(self) -> None:
        """按当前列数重排卡片。"""
        if self._rebuilding:
            # 重排会牵动滚动条，滚动条又牵动视口宽度：这里挡住重入，
            # 免得两处互相触发。
            return
        self._rebuilding = True
        try:
            self._fill_grid()
        finally:
            self._rebuilding = False

    def _fill_grid(self) -> None:
        """清空旧卡片并按当前列数重新摆好。"""
        while self._grid.count():
            item = self._grid.takeAt(0)
            if item is None:
                break
            widget = item.widget()
            if widget is not None:
                # 先摘掉父子关系：否则旧卡片会作为游离子控件留在页面上。
                widget.setParent(None)
                widget.deleteLater()
        self._frames.clear()
        self._cards.clear()

        columns = max(1, self._columns)
        for index, colophon in enumerate(self._colophons):
            frame = _CardFrame(colophon, self._grid_host)
            card = frame.card
            card.setProperty("colophonId", colophon.id)
            card.installEventFilter(self)
            self._frames[colophon.id] = frame
            self._cards[colophon.id] = card
            self._grid.addWidget(
                frame, index // columns, index % columns
            )

        self._update_header()
        self._update_actions()

    def _update_header(self) -> None:
        """更新数量副标题，并在网格与空册之间切换。"""
        count = len(self._colophons)
        if count:
            self.count_label.setText(f"共 {count} 幅拓片")
        else:
            self.count_label.setText("还没有拓片")
        self.body.setCurrentIndex(0 if count else 1)

    def _desired_columns(self) -> int:
        """按可用宽度算列数。

        卡片是固定宽的竖条，窗口窄时硬摆三列会被挤出可视区，因此这里
        按实际能放下的张数算，最多三列。
        """
        scroll = self._scroll
        if scroll is None:
            return 1
        usable = scroll.viewport().width() - GRID_MARGIN * 2
        per_column = (
            DISPLAY_CARD_WIDTH
            + CARD_MARGIN * 2
            + CARD_BORDER * 2
            + GRID_SPACING
        )
        return max(1, min(MAX_COLUMNS, usable // per_column))

    def _update_columns(self) -> None:
        """列数变了才重排，省得每次滚动都重建卡片。"""
        if self._rebuilding:
            # 重排会牵动滚动条，滚动条又牵动视口宽度：重排期间算出的
            # 列数不能直接记下来——网格还没按它排，记下就再也对不上，
            # 下一次视口宽度没变时就不会再排了。
            return
        columns = self._desired_columns()
        if columns == self._columns:
            return
        self._columns = columns
        self._rebuild_grid()

    # ------------------------------------------------------------------
    # 选中
    # ------------------------------------------------------------------
    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        """拦截卡片点击与视口宽度变化。"""
        if event.type() == QEvent.Type.MouseButtonPress:
            colophon_id = obj.property("colophonId")
            if isinstance(colophon_id, str) and colophon_id:
                self._select(colophon_id)
                return True
        elif event.type() == QEvent.Type.Resize and self._is_viewport(obj):
            self._update_columns()
        return super().eventFilter(obj, event)

    def _is_viewport(self, obj: QObject) -> bool:
        """判断事件来自网格视口（视口变窄会改变能摆下的列数）。"""
        scroll = self._scroll
        return scroll is not None and obj is scroll.viewport()

    def _select(self, colophon_id: str) -> None:
        """选中一份拓片并刷新动作条。"""
        if colophon_id == self._selected_id:
            return
        self._selected_id = colophon_id
        for identifier, frame in self._frames.items():
            frame.set_selected(identifier == colophon_id)
        self._update_actions()

    def _current_colophon(self) -> Optional[Colophon]:
        """返回当前选中的拓片。"""
        if not self._selected_id:
            return None
        return self._by_id.get(self._selected_id)

    def _update_actions(self) -> None:
        """没有选中时，动作条上的两个按钮不可用。"""
        colophon = self._current_colophon()
        enabled = colophon is not None
        self.export_png_button.setEnabled(enabled)
        self.delete_button.setEnabled(enabled)
        if colophon is None:
            self.selection_label.setText("未选中拓片")
        else:
            self.selection_label.setText(f"已选：{colophon.title}")

    def _set_status(self, text: str) -> None:
        """写一行页内状态。"""
        self.status_label.setText(text)

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------
    def _export_album_sgf(self) -> None:
        """把整册拓片导出成一个 SGF 集。"""
        if not self._colophons:
            self._set_status("册子里还没有拓片。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出整册 SGF",
            "拓片册.sgf",
            "SGF 棋谱 (*.sgf);;所有文件 (*)",
        )
        if not path:
            self._set_status("已取消导出。")
            return
        if not path.lower().endswith(".sgf"):
            path += ".sgf"
        try:
            text = colophons_to_sgf(self._colophons)
            Path(path).write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.exception("导出拓片册失败 path=%s", path)
            self._set_status(f"导出失败：{exc}")
            return
        self._set_status(f"已导出整册 SGF：{path}")

    def _export_png(self) -> None:
        """把选中的拓片存成一张图片。"""
        card = self._cards.get(self._selected_id)
        if card is None:
            return
        colophon = card.colophon()
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出拓片图片",
            f"{colophon.title}.png",
            "PNG 图片 (*.png);;所有文件 (*)",
        )
        if not path:
            self._set_status("已取消导出。")
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        # 按整幅尺寸渲染：网格里的卡片是缩到 300 宽的，导出的图不该
        # 跟着一起缩水。
        pixmap = render_pixmap(
            colophon,
            dpr=max(1.0, self.devicePixelRatioF()),
        )
        if not pixmap.save(path, "PNG"):
            logger.error("导出拓片图片失败 path=%s", path)
            self._set_status(f"导出失败：无法写入 {path}")
            return
        self._set_status(f"已导出图片：{path}")

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------
    def _delete_selected(self) -> None:
        """删去选中的拓片（删前要过一道确认）。"""
        colophon = self._current_colophon()
        if colophon is None:
            return
        if not self._confirm_delete(colophon):
            self._set_status("已取消删除。")
            return
        try:
            removed = self._store.remove(colophon.id)
        except Exception:
            logger.exception("删除拓片失败 id=%s", colophon.id)
            self._set_status("删除失败，册子没能写入。")
            return
        if removed:
            self._set_status(f"已删去《{colophon.title}》。")
        else:
            self._set_status("这一幅拓片已经不在册子里了。")
        self.refresh()

    def _confirm_delete(self, colophon: Colophon) -> bool:
        """删拓片前用窗口内的古风确认框过一道。"""
        dialog = ConfirmDialog(
            "删去这一幅拓片？",
            f"《{colophon.title}》将从册子里消失，找不回来了。",
            confirm_text="删去",
            cancel_text="留下",
            seal_text="删",
            parent=self._overlay_host(),
        )
        try:
            dialog.exec()
            return dialog.confirm_requested
        finally:
            dialog.deleteLater()

    def _overlay_host(self) -> QWidget:
        """模态遮罩的宿主：主窗口中央部件。

        应用全局只保留一个原生窗口，确认框因此是覆盖在主窗口上的一层
        遮罩，而不是第二个窗口。
        """
        window = self.window()
        if isinstance(window, QMainWindow):
            central = window.centralWidget()
            if central is not None:
                return central
        return window


__all__ = ["ColophonAlbum"]
