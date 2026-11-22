"""好友列表、好友申请与用户搜索面板（宣纸金线风格）。"""

from __future__ import annotations

from typing import Callable, Mapping, NamedTuple, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from gomoku.client.avatar_widgets import AvatarView
from gomoku.client.paper_panel import PaperPanel


class _FriendRow(NamedTuple):
    """好友行信息：行控件与好友的在线状态。"""

    frame: QFrame
    online: bool


class FriendsPanel(PaperPanel):
    """非模态好友面板；所有网络操作通过信号交给联机面板。"""

    profile_requested = Signal(str)
    chat_requested = Signal(str, str)
    invite_requested = Signal(str, str)
    add_requested = Signal(str)
    accept_requested = Signal(str)
    decline_requested = Signal(str)
    remove_requested = Signal(str)
    search_requested = Signal(str)
    refresh_requested = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        invite_mode: bool = False,
        online_only_invite: bool = False,
    ) -> None:
        super().__init__(
            parent,
            seal_text="友",
            window_title="好友",
            min_width=700,
            min_height=640,
        )
        self._invite_mode = invite_mode
        self._section_cards: dict[str, QFrame] = {}
        self._section_counts: dict[str, QLabel] = {}
        self._friend_rows: dict[str, _FriendRow] = {}

        title = QLabel(
            "邀请好友对局" if invite_mode else "我的好友", self.card
        )
        title.setObjectName("socialPageTitle")
        subtitle = QLabel(
            "选一位在线好友，邀请进入自建房"
            if invite_mode
            else "与好友聊天，或邀请一局自建房对弈",
            self.card,
        )
        subtitle.setObjectName("socialSubtitle")
        subtitle.setWordWrap(True)
        self._title_label = title
        self._subtitle_label = subtitle
        self.content.addWidget(title)
        self.content.addWidget(subtitle)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search_input = QLineEdit(self.card)
        self.search_input.setObjectName("socialSearchInput")
        self.search_input.setPlaceholderText("搜索用户名，添加新好友")
        self.search_input.setMaxLength(24)
        search_button = QPushButton("搜索", self.card)
        search_button.setObjectName("socialPrimaryButton")
        search_button.setCursor(Qt.CursorShape.PointingHandCursor)
        search_button.setAutoDefault(False)
        refresh_button = QPushButton("刷新", self.card)
        refresh_button.setObjectName("socialSecondaryButton")
        refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh_button.setAutoDefault(False)
        search_row.addWidget(self.search_input, 1)
        search_row.addWidget(search_button)
        search_row.addWidget(refresh_button)
        self.content.addLayout(search_row)

        self.status_label = QLabel("", self.card)
        self.status_label.setObjectName("socialStatus")
        self.status_label.setWordWrap(True)
        self.content.addWidget(self.status_label)

        scroll = QScrollArea(self.card)
        scroll.setObjectName("socialScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        viewport = scroll.viewport()
        viewport.setAutoFillBackground(False)
        viewport.setAttribute(
            Qt.WidgetAttribute.WA_TranslucentBackground, True
        )
        body = QWidget(scroll)
        body.setObjectName("socialScrollBody")
        body.setAutoFillBackground(False)
        self.body_layout = QVBoxLayout(body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(10)
        scroll.setWidget(body)
        self.content.addWidget(scroll, 1)

        # 搜索结果放在最上方，搜索后无需滚动即可看到；未搜索时隐藏。
        self.search_layout = self._add_section("搜索结果", "搜索结果")
        self._add_hint(self.search_layout, "输入用户名搜索")
        self._set_section_visible("搜索结果", False)
        self.incoming_layout = self._add_section(
            "好友申请", "好友申请", accent=True
        )
        self.outgoing_layout = self._add_section(
            "已发出的申请", "已发出的申请"
        )
        self.friends_layout = self._add_section("好友", "好友")
        self.body_layout.addStretch(1)

        footer = QHBoxLayout()
        footer_hint = QLabel("仅好友可私聊与邀请对局", self.card)
        footer_hint.setObjectName("socialSubtitle")
        close_button = QPushButton("关闭", self.card)
        close_button.setObjectName("socialSecondaryButton")
        close_button.setCursor(Qt.CursorShape.PointingHandCursor)
        close_button.setAutoDefault(False)
        close_button.clicked.connect(self.close)
        footer.addWidget(footer_hint)
        footer.addStretch(1)
        footer.addWidget(close_button)
        self.content.addLayout(footer)

        search_button.clicked.connect(
            lambda _checked=False: self._emit_search()
        )
        refresh_button.clicked.connect(
            lambda _checked=False: self.refresh_requested.emit()
        )
        self.search_input.returnPressed.connect(self._emit_search)

    # ------------------------------------------------------------------
    def set_invite_mode(self, invite_mode: bool) -> None:
        """切换“好友列表 / 邀请好友对局”两种标题。

        浮层会被复用：从对局页点“邀请好友”时，已经打开的好友浮层要
        跟着切换标题，否则用户看到的是“我的好友”。
        """
        if invite_mode == self._invite_mode:
            return
        self._invite_mode = invite_mode
        self._title_label.setText(
            "邀请好友对局" if invite_mode else "我的好友"
        )
        self._subtitle_label.setText(
            "选一位在线好友，邀请进入自建房"
            if invite_mode
            else "与好友聊天，或邀请一局自建房对弈"
        )

    # ------------------------------------------------------------------
    def _add_section(
        self,
        key: str,
        title: str,
        *,
        accent: bool = False,
    ) -> QVBoxLayout:
        card = QFrame(self.card)
        card.setObjectName("friendsSection")
        card.setProperty("accent", "true" if accent else "false")
        card.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 10, 12, 10)
        card_layout.setSpacing(6)

        header = QHBoxLayout()
        header.setSpacing(6)
        marker = QLabel("◆", card)
        marker.setObjectName("friendsSectionMarker")
        marker.setProperty("accent", "true" if accent else "false")
        title_label = QLabel(title, card)
        title_label.setObjectName("socialSectionTitle")
        count_label = QLabel("", card)
        count_label.setObjectName("socialSectionCount")
        header.addWidget(marker)
        header.addWidget(title_label)
        header.addWidget(count_label)
        header.addStretch(1)
        card_layout.addLayout(header)

        container = QWidget(card)
        section = QVBoxLayout(container)
        section.setContentsMargins(0, 0, 0, 0)
        section.setSpacing(4)
        card_layout.addWidget(container)
        self.body_layout.addWidget(card)
        self._section_cards[key] = card
        self._section_counts[key] = count_label
        return section

    def _set_section_count(self, key: str, text: str) -> None:
        label = self._section_counts.get(key)
        if label is not None:
            label.setText(text)

    def _set_section_accent(self, key: str, accent: bool) -> None:
        card = self._section_cards.get(key)
        if card is None:
            return
        value = "true" if accent else "false"
        if card.property("accent") == value:
            return
        card.setProperty("accent", value)
        card.style().unpolish(card)
        card.style().polish(card)

    def _set_section_visible(self, key: str, visible: bool) -> None:
        card = self._section_cards.get(key)
        if card is not None:
            card.setVisible(visible)

    def _emit_search(self) -> None:
        query = self.search_input.text().strip()
        if query:
            self.search_requested.emit(query)

    @staticmethod
    def _clear_layout(layout: QVBoxLayout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # 先脱离父控件再删除：否则在事件循环处理删除之前，旧行
                # 还会留在原来的位置上，和新行重叠一帧。
                widget.setParent(None)
                widget.deleteLater()

    def _add_hint(self, layout: QVBoxLayout, text: str) -> None:
        label = QLabel(f"—— {text} ——", self.card)
        label.setObjectName("friendsEmpty")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addWidget(label, 0, Qt.AlignmentFlag.AlignCenter)

    @staticmethod
    def _user_id(entry: Mapping) -> str:
        value = entry.get("user_id") or entry.get("id") or ""
        return value if isinstance(value, str) else ""

    def _row(
        self,
        entry: Mapping,
        *,
        presence: Optional[str] = None,
        online: Optional[bool] = None,
        unread: int = 0,
        buttons: Sequence[
            tuple[str, str, Callable[[], None], bool]
        ] = (),
    ) -> QFrame:
        frame = QFrame(self.card)
        frame.setObjectName("friendRow")
        frame.setAttribute(
            Qt.WidgetAttribute.WA_StyledBackground, True
        )
        if online is not None:
            frame.setProperty(
                "online", "true" if online else "false"
            )
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(9)

        user_id = self._user_id(entry)
        username = str(entry.get("username", "玩家"))
        avatar_version = entry.get("avatar_version", 0)
        if not isinstance(avatar_version, int) or isinstance(
            avatar_version, bool
        ):
            avatar_version = 0
        avatar = AvatarView(
            entry.get("avatar", ""),
            user_id,
            36,
            frame,
            avatar_version=max(0, avatar_version),
        )
        if online is not None:
            avatar.set_online(online)
        layout.addWidget(avatar)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(1)
        name = QLabel(username, frame)
        name.setObjectName("friendName")
        text_layout.addWidget(name)
        if presence is not None:
            label = QLabel(presence, frame)
            label.setObjectName("friendPresence")
            if online is not None:
                label.setProperty(
                    "online", "true" if online else "false"
                )
            text_layout.addWidget(label)
        layout.addLayout(text_layout, 1)

        if unread > 0:
            badge = QLabel(f"{unread} 条未读", frame)
            badge.setObjectName("friendUnread")
            layout.addWidget(badge)

        for text, object_name, callback, lock_row in buttons:
            button = QPushButton(text, frame)
            button.setObjectName(object_name)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAutoDefault(False)
            button.clicked.connect(
                lambda _checked=False, cb=callback, row=frame, lock=lock_row: (
                    self._run_row_action(row, cb, lock)
                )
            )
            layout.addWidget(button)
        return frame

    @staticmethod
    def _run_row_action(
        frame: QFrame,
        callback: Callable[[], None],
        lock_row: bool,
    ) -> None:
        """执行行内操作。

        接受、拒绝这类操作重复提交没有意义：第二次请求会得到
        “已经处理过”的错误提示，白白在界面上弹一条红字。所以点过之后
        就把这一行的按钮锁住，等列表刷新后重建。

        删除好友要等用户确认，所以不在这里锁行。
        """
        if lock_row:
            for button in frame.findChildren(QPushButton):
                button.setEnabled(False)
        callback()

    @staticmethod
    def _sync_invite_button(row: QFrame, online: bool) -> None:
        """离线好友不能被邀请对局。"""
        for button in row.findChildren(QPushButton):
            if button.text() == "邀请对局":
                button.setEnabled(online)
                button.setToolTip("" if online else "好友不在线")

    def lock_friend_row(self, user_id: str) -> None:
        """锁住某位好友所在行的按钮。

        删除好友要等用户在确认弹窗里点头，确认之后才锁行，避免重复提交。
        """
        info = self._friend_rows.get(user_id)
        if info is None:
            return
        for button in info.frame.findChildren(QPushButton):
            button.setEnabled(False)

    def unlock_friend_row(self, user_id: str) -> None:
        """解开某位好友所在行的按钮（删除失败时用）。"""
        info = self._friend_rows.get(user_id)
        if info is None:
            return
        for button in info.frame.findChildren(QPushButton):
            if button.text() == "邀请对局" and not info.online:
                button.setEnabled(False)
                button.setToolTip("好友不在线")
            else:
                button.setEnabled(True)
                button.setToolTip("")

    # ------------------------------------------------------------------
    def set_data(self, data: Mapping) -> None:
        """刷新好友、收到与发出的申请。"""
        friends = data.get("friends", [])
        incoming = data.get("incoming", [])
        outgoing = data.get("outgoing", [])
        incoming_list = incoming if isinstance(incoming, list) else []
        outgoing_list = outgoing if isinstance(outgoing, list) else []
        friends_list = friends if isinstance(friends, list) else []

        self._set_section_count(
            "好友申请", f"{len(incoming_list)} 条待处理"
        )
        self._set_section_count(
            "已发出的申请", f"{len(outgoing_list)} 条"
        )
        self._set_section_count(
            "好友", f"{len(friends_list)} 位"
        )
        self._set_section_accent("好友申请", bool(incoming_list))

        self._clear_layout(self.incoming_layout)
        if incoming_list:
            for entry in incoming_list:
                if not isinstance(entry, Mapping):
                    continue
                request_id = str(entry.get("request_id", ""))
                user_id = self._user_id(entry)
                self.incoming_layout.addWidget(
                    self._row(
                        entry,
                        presence="请求添加你为好友",
                        buttons=(
                            (
                                "接受",
                                "socialPrimaryButton",
                                lambda _=False, rid=request_id: (
                                    self.accept_requested.emit(rid)
                                ),
                                True,
                            ),
                            (
                                "拒绝",
                                "socialSecondaryButton",
                                lambda _=False, rid=request_id: (
                                    self.decline_requested.emit(rid)
                                ),
                                True,
                            ),
                        ),
                    )
                )
        else:
            self._add_hint(self.incoming_layout, "暂无好友申请")

        self._clear_layout(self.outgoing_layout)
        if outgoing_list:
            for entry in outgoing_list:
                if isinstance(entry, Mapping):
                    self.outgoing_layout.addWidget(
                        self._row(entry, presence="等待对方同意")
                    )
        else:
            self._add_hint(self.outgoing_layout, "暂无已发出的申请")

        self._clear_layout(self.friends_layout)
        self._friend_rows.clear()
        if friends_list:
            for entry in friends_list:
                if not isinstance(entry, Mapping):
                    continue
                user_id = self._user_id(entry)
                username = str(entry.get("username", "玩家"))
                online = bool(entry.get("online"))
                unread = entry.get("unread", 0)
                try:
                    unread_count = int(unread)
                except (TypeError, ValueError):
                    unread_count = 0
                buttons: list[
                    tuple[str, str, Callable[[], None], bool]
                ] = [
                    (
                        "邀请对局",
                        "socialPrimaryButton",
                        lambda _=False, uid=user_id, name=username, is_online=online: (
                            self.invite_requested.emit(uid, name)
                            if is_online
                            else None
                        ),
                        False,
                    ),
                    (
                        "聊天",
                        "socialSecondaryButton",
                        lambda _=False, uid=user_id, name=username: (
                            self.chat_requested.emit(uid, name)
                        ),
                        False,
                    ),
                    (
                        "资料",
                        "socialCompactButton",
                        lambda _=False, uid=user_id: (
                            self.profile_requested.emit(uid)
                        ),
                        False,
                    ),
                    (
                        "删除",
                        "socialCompactButton",
                        lambda _=False, uid=user_id: (
                            self.remove_requested.emit(uid)
                        ),
                        # 删除要等用户确认后才算提交，这里先不锁行。
                        False,
                    ),
                ]
                row = self._row(
                    entry,
                    presence="● 在线" if online else "○ 离线",
                    online=online,
                    unread=unread_count,
                    buttons=buttons,
                )
                self._friend_rows[user_id] = _FriendRow(row, online)
                self._sync_invite_button(row, online)
                self.friends_layout.addWidget(row)
        else:
            self._add_hint(
                self.friends_layout, "还没有好友，搜索用户名添加"
            )

    def set_search_results(self, entries: Sequence[Mapping]) -> None:
        self._clear_layout(self.search_layout)
        valid = [
            entry for entry in entries if isinstance(entry, Mapping)
        ]
        self._set_section_count("搜索结果", f"{len(valid)} 位")
        self._set_section_visible("搜索结果", True)
        if not valid:
            self._add_hint(self.search_layout, "没有找到匹配的玩家")
            return
        for entry in valid:
            user_id = self._user_id(entry)
            online = bool(entry.get("online"))
            self.search_layout.addWidget(
                self._row(
                    entry,
                    presence="● 在线" if online else "○ 离线",
                    online=online,
                    buttons=(
                        (
                            "加为好友",
                            "socialPrimaryButton",
                            lambda _=False, uid=user_id: (
                                self.add_requested.emit(uid)
                            ),
                            True,
                        ),
                        (
                            "资料",
                            "socialCompactButton",
                            lambda _=False, uid=user_id: (
                                self.profile_requested.emit(uid)
                            ),
                            False,
                        ),
                    ),
                )
            )

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def clear_search(self) -> None:
        self.search_input.clear()
        self._clear_layout(self.search_layout)
        self._set_section_count("搜索结果", "")
        self._add_hint(self.search_layout, "输入用户名搜索")
        self._set_section_visible("搜索结果", False)


__all__ = ["FriendsPanel"]
