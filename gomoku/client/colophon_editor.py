"""题跋拓片编辑器：选手、拟题、题名与闲章，左预览右表单。

编辑器是主窗口内的一层模态遮罩（见 :mod:`gomoku.client.modal_overlay`），
不创建第二个原生窗口——应用全局始终只有一个窗口是本项目的硬约定。

左边是实时预览：改一个字，拓片就跟着变。题跋是玩家写给自己的东西，
所见即所得比任何表单都重要。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.colophon import (
    MAX_COLOPHON_LENGTH,
    MAX_TITLE_LENGTH,
    Colophon,
    draft_colophons,
    make_colophon,
    pivotal_ply,
    suggest_title,
)
from gomoku.client.colophon_card import ColophonCard
from gomoku.client.modal_overlay import ModalOverlay
from gomoku.client.ui_theme import PaperComboBox, render_result_backdrop
from gomoku.shared.avatar import AVATARS, AVATAR_BY_KEY, DEFAULT_AVATAR
from gomoku.shared.record import GameRecord

#: 编辑器卡片尺寸（逻辑像素）。
CARD_WIDTH: int = 680
CARD_HEIGHT: int = 520

#: 预览卡片宽度；高度按拓片的固定比例推出，保证版式不被压扁。
_PREVIEW_WIDTH: int = 236
_PREVIEW_ASPECT: float = 600.0 / 340.0

#: 闲章选项里的“无”。
_NO_SEAL = "（不钤印）"

#: 题跋语气下拉框的占位项。
_NO_VOICE = "（选择语气）"


class ColophonEditor(ModalOverlay):
    """题跋编辑器。

    ``ply`` 指定初始刻哪一手；若给了 AI 复盘结果，默认落在"关键手"
    （评分损失最大的那一手）上——那通常就是决定胜负的一步。
    """

    #: 用户点“存册”时发出，携带最终保存的 :class:`Colophon`。
    saved = Signal(object)

    def __init__(
        self,
        record: GameRecord,
        *,
        parent: Optional[QWidget] = None,
        ply: int = 0,
        reviews: Sequence[Any] = (),
        initial_colophon: str = "",
    ) -> None:
        super().__init__(parent, object_name="colophonEditor")

        self._record = record
        self._reviews = tuple(reviews)
        self._pivotal = pivotal_ply(self._reviews)

        total = len(record.moves)
        if ply <= 0:
            ply = self._pivotal or total
        self._ply = max(1, min(ply, total)) if total else 0

        card = self.card
        card.setObjectName("colophonEditorCard")
        card.setFixedSize(CARD_WIDTH, CARD_HEIGHT)
        card.set_renderer(
            lambda size, dpr: render_result_backdrop(size, dpr, "draw")
        )

        layout = QHBoxLayout(card)
        layout.setContentsMargins(26, 22, 26, 22)
        layout.setSpacing(18)

        layout.addLayout(self._build_preview_column())
        layout.addLayout(self._build_form_column(total))

        self._rebuild_base()
        self._refresh_preview()
        self._sync_ply_label()
        if initial_colophon:
            # 联机对局没有本地复盘可用，自动生成的那句直接摆进正文，
            # 玩家改写或直接存册都行。
            self.text_edit.setPlainText(initial_colophon)
            self._refresh_preview()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def _build_preview_column(self) -> QVBoxLayout:
        """左侧预览。"""
        column = QVBoxLayout()
        column.setSpacing(6)

        caption = QLabel("拓片预览", self.card)
        caption.setObjectName("sectionTitle")
        caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        column.addWidget(caption)

        self.preview = ColophonCard(
            self._placeholder(),
            self.card,
            width=_PREVIEW_WIDTH,
            height=int(_PREVIEW_WIDTH * _PREVIEW_ASPECT),
        )
        holder = QHBoxLayout()
        holder.addStretch(1)
        holder.addWidget(self.preview)
        holder.addStretch(1)
        column.addLayout(holder)
        column.addStretch(1)
        return column

    def _build_form_column(self, total: int) -> QVBoxLayout:
        """右侧表单。"""
        column = QVBoxLayout()
        column.setSpacing(8)

        # --- 选手 ---
        ply_row = QHBoxLayout()
        ply_row.setSpacing(6)
        ply_label = QLabel("手数", self.card)
        ply_label.setObjectName("formLabel")
        # 用文字而不是箭头符号：字体里未必有 ◀ ▶，缺字会渲染成豆腐块。
        self.prev_ply_button = QPushButton("上一手", self.card)
        self.prev_ply_button.setObjectName("smallButton")
        self.next_ply_button = QPushButton("下一手", self.card)
        self.next_ply_button.setObjectName("smallButton")
        self.ply_label = QLabel("", self.card)
        self.ply_label.setObjectName("moveInfo")
        self.ply_label.setMinimumWidth(88)
        self.ply_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pivotal_button = QPushButton("取关键手", self.card)
        self.pivotal_button.setObjectName("smallButton")
        self.pivotal_button.setToolTip("跳到 AI 复盘认定损失最大的一手")
        ply_row.addWidget(ply_label)
        ply_row.addWidget(self.prev_ply_button)
        ply_row.addWidget(self.ply_label)
        ply_row.addWidget(self.next_ply_button)
        ply_row.addWidget(self.pivotal_button)
        ply_row.addStretch(1)
        column.addLayout(ply_row)

        self.pivotal_hint = QLabel("", self.card)
        self.pivotal_hint.setObjectName("hintLabel")
        self.pivotal_hint.setWordWrap(True)
        column.addWidget(self.pivotal_hint)

        # --- 题名 ---
        title_row = QHBoxLayout()
        title_row.setSpacing(6)
        title_label = QLabel("题名", self.card)
        title_label.setObjectName("formLabel")
        self.title_input = QLineEdit(self.card)
        self.title_input.setObjectName("onlineInput")
        self.title_input.setMaxLength(MAX_TITLE_LENGTH)
        self.title_input.setPlaceholderText("2~4 字，作印文")
        self.title_input.setToolTip(
            "题名会刻成右上角的引首印，最多 4 字。"
        )
        # 先给一个默认题名：玩家多半只是改两个字，而不是从空白里
        # 憋一个。空着的题名既刻不出印，存册时也会被挡回来。
        self.title_input.setText(suggest_title(self._record.record_id))
        title_row.addWidget(title_label)
        title_row.addWidget(self.title_input, 1)
        column.addLayout(title_row)

        # --- 题跋 ---
        text_label = QLabel("题跋", self.card)
        text_label.setObjectName("formLabel")
        column.addWidget(text_label)

        self.text_edit = QPlainTextEdit(self.card)
        self.text_edit.setObjectName("reportDescription")
        self.text_edit.setFixedHeight(104)
        self.text_edit.setPlaceholderText(
            "为这一手写一句话。最多 60 字。"
        )
        column.addWidget(self.text_edit)

        voice_row = QHBoxLayout()
        voice_row.setSpacing(6)
        self.voice_combo = PaperComboBox(self.card)
        self.draft_button = QPushButton("拟题", self.card)
        self.draft_button.setObjectName("smallButton")
        self.draft_button.setToolTip("按引擎评定起草一句，可继续改写")
        self.char_label = QLabel("", self.card)
        self.char_label.setObjectName("hintLabel")
        self.char_label.setMinimumWidth(72)
        self.char_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        voice_row.addWidget(self.voice_combo, 1)
        voice_row.addWidget(self.draft_button)
        voice_row.addWidget(self.char_label)
        column.addLayout(voice_row)

        # --- 闲章 ---
        seal_row = QHBoxLayout()
        seal_row.setSpacing(6)
        seal_label = QLabel("落款闲章", self.card)
        seal_label.setObjectName("formLabel")
        self.seal_combo = PaperComboBox(self.card)
        self.seal_combo.addItem(_NO_SEAL, "")
        for preset in AVATARS:
            # 12 枚预置印章的标签与字本身相同，只显示字就够辨认。
            self.seal_combo.addItem(preset.glyph, preset.glyph)
        self.seal_combo.setCurrentIndex(
            self._seal_index(AVATAR_BY_KEY[DEFAULT_AVATAR].glyph)
        )
        seal_row.addWidget(seal_label)
        seal_row.addWidget(self.seal_combo, 1)
        column.addLayout(seal_row)

        column.addStretch(1)

        # --- 操作 ---
        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        buttons.addStretch(1)
        self.save_button = QPushButton("存册", self.card)
        self.save_button.setObjectName("resultPrimaryButton")
        self.save_button.setDefault(True)
        self.save_button.setAutoDefault(True)
        self.save_button.setMinimumWidth(104)
        self.save_button.setMinimumHeight(36)
        self.cancel_button = QPushButton("取消", self.card)
        self.cancel_button.setObjectName("resultSecondaryButton")
        self.cancel_button.setAutoDefault(False)
        self.cancel_button.setMinimumWidth(104)
        self.cancel_button.setMinimumHeight(36)
        buttons.addWidget(self.save_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        column.addLayout(buttons)

        # --- 信号 ---
        self.prev_ply_button.clicked.connect(
            lambda _checked=False: self._step_ply(-1)
        )
        self.next_ply_button.clicked.connect(
            lambda _checked=False: self._step_ply(1)
        )
        self.pivotal_button.clicked.connect(
            lambda _checked=False: self._go_pivotal()
        )
        self.title_input.textChanged.connect(
            lambda _text: self._on_text_changed()
        )
        self.text_edit.textChanged.connect(
            lambda: self._on_text_changed()
        )
        self.voice_combo.currentIndexChanged.connect(
            lambda _index: self._on_voice_selected()
        )
        self.draft_button.clicked.connect(
            lambda _checked=False: self._apply_draft()
        )
        self.seal_combo.currentIndexChanged.connect(
            lambda _index: self._on_seal_changed()
        )
        self.save_button.clicked.connect(lambda _checked=False: self._on_save())
        self.cancel_button.clicked.connect(self.reject)
        # 题名框里回车即存册：焦点一落到输入框，遮罩就收不到回车键了，
        # 玩家按回车什么也不会发生，只能去点「存册」。
        self.title_input.returnPressed.connect(self._on_save)
        self.set_default_action(self._on_save)

        return column

    def _seal_index(self, glyph: str) -> int:
        """找到闲章在下拉框里的下标。"""
        for index in range(self.seal_combo.count()):
            if self.seal_combo.itemData(index) == glyph:
                return index
        return 0

    def _placeholder(self) -> Colophon:
        """编辑器尚未成形时给预览一个空壳。"""
        return make_colophon(
            self._record,
            title="",
            colophon="",
            ply=self._ply,
            reviews=self._reviews,
        )

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _rebuild_base(self) -> None:
        """按当前手数重建拓片底稿。"""
        self._base = make_colophon(
            self._record,
            title=suggest_title(self._record.record_id),
            colophon="",
            ply=self._ply,
            reviews=self._reviews,
        )
        self._refresh_voices()

    def _refresh_voices(self) -> None:
        """按当前手数重拟题跋候选。

        候选里带着手数，换了手数就得重拟——否则「第 9 手」会挂在
        第 12 手的拓片上。
        """
        drafts = draft_colophons(
            self._record, ply=self._ply, reviews=self._reviews
        )
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        # 开头留一个空项：下拉框里选中了什么，不等于已经写进题跋——
        # 套用与否由「拟题」按钮决定，免得正文里凭空多出一段。
        self.voice_combo.addItem(_NO_VOICE, "")
        for _voice, text in drafts:
            self.voice_combo.addItem(text)
        self.voice_combo.blockSignals(False)

    def _current(self) -> Colophon:
        """返回按当前表单内容组装出来的拓片。"""
        seal = self.seal_combo.currentData() or ""
        return replace(
            self._base,
            title=self.title_input.text().strip(),
            colophon=self.text_edit.toPlainText().strip(),
            seal_glyph=str(seal),
        )

    def _refresh_preview(self) -> None:
        """把当前表单内容刷到预览上。"""
        self.preview.set_colophon(self._current())
        length = len(self.text_edit.toPlainText())
        self.char_label.setText(f"{length}/{MAX_COLOPHON_LENGTH}")

    def _sync_ply_label(self) -> None:
        """刷新手数标签与关键手提示。"""
        total = len(self._record.moves)
        self.ply_label.setText(f"第 {self._ply} / {total} 手")
        self.prev_ply_button.setEnabled(self._ply > 1)
        self.next_ply_button.setEnabled(self._ply < total)

        if self._pivotal:
            self.pivotal_button.setEnabled(True)
            self.pivotal_hint.setText(
                f"AI 复盘认定第 {self._pivotal} 手损失最大，可直接取作关键手。"
            )
        else:
            self.pivotal_button.setEnabled(False)
            self.pivotal_hint.setText(
                "尚无 AI 复盘结果，默认刻最后一手；复盘后可取关键手。"
            )

    # ------------------------------------------------------------------
    # 交互
    # ------------------------------------------------------------------
    def _step_ply(self, delta: int) -> None:
        """把手数前后挪一手。"""
        total = len(self._record.moves)
        target = self._ply + delta
        if target < 1 or target > total:
            return
        self._ply = target
        self._rebuild_base()
        self._sync_ply_label()
        self._refresh_preview()

    def _go_pivotal(self) -> None:
        """跳到关键手。"""
        if not self._pivotal:
            return
        self._ply = self._pivotal
        self._rebuild_base()
        self._sync_ply_label()
        self._refresh_preview()

    def _on_text_changed(self) -> None:
        """题名或题跋变化时刷新预览。"""
        # 题跋有字数上限：超出的部分直接截断，不让文本框无限增长。
        text = self.text_edit.toPlainText()
        if len(text) > MAX_COLOPHON_LENGTH:
            cursor = self.text_edit.textCursor()
            position = cursor.position()
            self.text_edit.blockSignals(True)
            self.text_edit.setPlainText(text[:MAX_COLOPHON_LENGTH])
            self.text_edit.blockSignals(False)
            cursor = self.text_edit.textCursor()
            cursor.setPosition(min(position, MAX_COLOPHON_LENGTH))
            self.text_edit.setTextCursor(cursor)
        self._refresh_preview()

    def _on_voice_selected(self) -> None:
        """在下拉框里换了一种语气。

        这里只换候选，不动正文：玩家可能已经写了自己的题跋，一碰下拉
        框就被覆盖掉是很糟的体验。要套用得按「拟题」。
        """
        return

    def _apply_draft(self) -> None:
        """把下拉框里选中的拟题写进正文。"""
        text = self.voice_combo.currentText()
        if not text or text == _NO_VOICE:
            return
        self.text_edit.setPlainText(text)
        self._refresh_preview()

    def _on_seal_changed(self) -> None:
        """闲章变化时刷新预览。"""
        self._refresh_preview()

    def _on_save(self) -> None:
        """保存拓片并关闭编辑器。"""
        title = self.title_input.text().strip()
        if not title:
            self.title_input.setFocus()
            return
        self.saved.emit(self._current())
        self.accept()


__all__ = ["CARD_HEIGHT", "CARD_WIDTH", "ColophonEditor"]
