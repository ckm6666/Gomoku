"""排位资料、排行榜、头像、历史对局与对战广场控制器。

``OnlinePanel`` 只保留页面框架；资料展示、最近对局/复盘请求和进行中
对局列表的拉取与渲染都放在这里。
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Mapping, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTableWidgetItem

from gomoku.client.achievement_widgets import AchievementPanel
from gomoku.client.avatar_images import avatar_image_cache
from gomoku.client.avatar_widgets import avatar_icon
from gomoku.client.history_records import record_from_game_detail
from gomoku.client.profile_panel import AvatarPickerPanel, ProfilePanel
from gomoku.client.online_controllers.common import (
    PanelController,
    entry_avatar_version,
    format_game_time,
    result_label,
)
from gomoku.shared.achievements import normalize_achievement_keys
from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.record import GameRecord, RecordError

logger = logging.getLogger(__name__)


class ProfileController(PanelController):
    """资料、排行榜、头像、历史对局与广场列表。"""

    def rating_source(self) -> Optional[dict]:
        if isinstance(self.panel._profile_user, Mapping):
            return dict(self.panel._profile_user)
        user = self.panel._auth_session.user if self.panel._auth_session else None
        if user is None:
            return None
        return {
            "rating": user.rating,
            "peak_rating": user.peak_rating,
            "rated_games": user.rated_games,
            "wins": user.wins,
            "losses": user.losses,
            "draws": user.draws,
            "rank_name": user.rank_name,
        }

    def update_rating_summary(self) -> None:
        data = self.panel._rating_source()
        if not data:
            self.panel.rating_badge_label.setText("未定级")
            self.panel.rating_summary_label.setText("登录后展示段位与积分")
            return

        rating = data.get("rating")
        if not isinstance(rating, int) or isinstance(rating, bool):
            rating = 1200
        rank_name = data.get("rank_name")
        self.panel.rating_badge_label.setText(
            rank_name if isinstance(rank_name, str) and rank_name else "已定级"
        )

        rated_games = data.get("rated_games", 0)
        wins = data.get("wins", 0)
        losses = data.get("losses", 0)
        draws = data.get("draws", 0)
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in (rated_games, wins, losses, draws)
        ):
            rated_games = wins = losses = draws = 0
        record = (
            f"{wins}胜 {losses}负 {draws}平"
            if rated_games
            else "尚未完成排位赛"
        )
        summary = (
            f"积分 {rating}，最高 {data.get('peak_rating', rating)}，{record}"
            if rated_games
            else f"积分 {rating}，{record}"
        )
        season_name = ""
        days_remaining = None
        if isinstance(self.panel._season_info, Mapping):
            raw_name = self.panel._season_info.get("name")
            if isinstance(raw_name, str) and raw_name:
                season_name = raw_name
            raw_days = self.panel._season_info.get("days_remaining")
            if isinstance(raw_days, int) and not isinstance(raw_days, bool):
                days_remaining = max(0, raw_days)
        if season_name:
            season_text = season_name
            if days_remaining is not None:
                season_text += f"（剩余 {days_remaining} 天）"
            if isinstance(self.panel._season_rank, int) and not isinstance(
                self.panel._season_rank, bool
            ):
                season_text += f" · 赛季排名 #{self.panel._season_rank}"
            summary = f"{season_text}\n{summary}"
        self.panel.rating_summary_label.setText(summary)

    def refresh_profile_data(self) -> None:
        if self.panel._auth_session is None or not self.panel._api_base_url:
            return
        token = self.panel._auth_session.access_token
        self.panel._profile_client.fetch_profile(token)
        self.panel._profile_client.fetch_leaderboard(token, 50)
        self.panel._history_client.fetch_recent(token, 20)
        self.panel._social_client.fetch_friends(token)

    def on_refresh_leaderboard(self) -> None:
        if self.panel._auth_session is None:
            return
        self.panel._profile_client.fetch_leaderboard(
            self.panel._auth_session.access_token, 50
        )

    def on_profile_ready(self, data: object) -> None:
        if not isinstance(data, Mapping):
            return
        raw_user = data.get("user")
        if isinstance(raw_user, Mapping):
            self.panel._profile_user = dict(raw_user)
            self.panel._sync_auth_user_from_profile(raw_user)
        raw_season = data.get("season")
        self.panel._season_info = (
            dict(raw_season) if isinstance(raw_season, Mapping) else None
        )
        raw_rank = data.get("season_rank")
        self.panel._season_rank = (
            raw_rank
            if isinstance(raw_rank, int) and not isinstance(raw_rank, bool)
            else None
        )
        self.panel._update_self_avatar()
        self.panel._update_rating_summary()

    def sync_auth_user_from_profile(self, raw_user: Mapping) -> None:
        """把资料接口的最新头像/成就/战绩同步进登录会话。"""
        if self.panel._auth_session is None:
            return
        current = self.panel._auth_session.user
        avatar = raw_user.get("avatar")
        if not isinstance(avatar, str) or not avatar:
            avatar = current.avatar
        achievements = normalize_achievement_keys(
            raw_user.get("achievements")
        )
        if not achievements:
            achievements = current.achievement_keys

        def int_value(key: str, fallback: int) -> int:
            value = raw_user.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            return fallback

        user = replace(
            current,
            avatar=normalize_avatar(avatar, current.id),
            avatar_version=entry_avatar_version(raw_user),
            rating=int_value("rating", current.rating),
            peak_rating=int_value("peak_rating", current.peak_rating),
            rated_games=int_value(
                "rated_games", current.rated_games
            ),
            wins=int_value("wins", current.wins),
            losses=int_value("losses", current.losses),
            draws=int_value("draws", current.draws),
            current_win_streak=int_value(
                "current_win_streak", current.current_win_streak
            ),
            best_win_streak=int_value(
                "best_win_streak", current.best_win_streak
            ),
            achievements=achievements,
        )
        self.panel._auth_session = replace(self.panel._auth_session, user=user)

    def on_leaderboard_ready(self, data: object) -> None:
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        total = data.get("total", len(entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(entries)
        self.panel._fill_leaderboard(entries, total)

    def fill_leaderboard(self, entries: list, total: int) -> None:
        table = self.panel.leaderboard_table
        valid = [entry for entry in entries if isinstance(entry, Mapping)]
        # 行号必须和 _leaderboard_entries 一一对应，否则点击第 i 行打开的
        # 是另一个人。
        self.panel._leaderboard_entries = valid
        table.setRowCount(len(valid))
        own_id = (
            self.panel._auth_session.user.id if self.panel._auth_session else ""
        )
        for row, entry in enumerate(valid):
            username = entry.get("username")
            username_text = (
                username if isinstance(username, str) else "未知玩家"
            )
            if entry.get("user_id") == own_id:
                username_text += "（你）"
            values = (
                str(entry.get("position", row + 1)),
                username_text,
                str(entry.get("rank_name", "")),
                str(entry.get("rating", "")),
                (
                    f"{entry.get('wins', 0)}-"
                    f"{entry.get('losses', 0)}-"
                    f"{entry.get('draws', 0)}"
                ),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(
                    item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if column == 1:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter
                        | Qt.AlignmentFlag.AlignLeft
                    )
                    item.setIcon(
                        avatar_icon(
                            entry.get("avatar"),
                            str(entry.get("user_id", "")),
                            20,
                            avatar_version=entry_avatar_version(entry),
                        )
                    )
                    item.setToolTip("点击玩家查看个人信息")
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setItem(row, column, item)

        if valid:
            self.panel.leaderboard_status_label.setText(
                f"共 {total} 名玩家完成过排位赛"
            )
        else:
            self.panel.leaderboard_status_label.setText(
                "还没有玩家完成排位赛，成为第一个上榜的人吧"
            )

    def on_avatar_image_loaded(self, user_id: str, version: int) -> None:
        """头像图片异步到货后，刷新排行榜里对应的图标。"""
        for row, entry in enumerate(self.panel._leaderboard_entries):
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("user_id", "")) != user_id:
                continue
            item = self.panel.leaderboard_table.item(row, 1)
            if item is not None:
                item.setIcon(
                    avatar_icon(
                        entry.get("avatar"),
                        user_id,
                        20,
                        avatar_version=version,
                    )
                )

    def on_profile_failed(self, message: str) -> None:
        logger.warning("读取排位资料失败：%s", message)
        if (
            self.panel.leaderboard_table.rowCount() == 0
            and self.panel.stack.currentWidget() is self.panel.lobby_page
        ):
            self.panel.leaderboard_status_label.setText(
                message or "无法加载排行榜"
            )

    def on_profile_unauthorized(self) -> None:
        if self.panel._auth_session is None or self.panel._auth_client.busy:
            return
        self.panel._refresh_after_auth_refresh = True
        if not self.panel._auth_client.refresh():
            self.panel._refresh_after_auth_refresh = False
            self.panel._force_relogin("登录已过期，请重新登录")

    def on_profile_busy_changed(self, _busy: bool) -> None:
        if hasattr(self.panel, "refresh_leaderboard_button"):
            self.panel._refresh_lobby_controls()

    def on_leaderboard_cell_clicked(self, row: int, column: int) -> None:
        """点击排行榜玩家列打开个人信息。"""
        if column != 1:
            return
        if 0 <= row < len(self.panel._leaderboard_entries):
            entry = self.panel._leaderboard_entries[row]
            if isinstance(entry, Mapping):
                user_id = entry.get("user_id")
                if isinstance(user_id, str) and user_id:
                    self.panel._open_user_profile(user_id)

    def update_self_avatar(self) -> None:
        if self.panel._auth_session is None:
            return
        user = self.panel._auth_session.user
        self.panel.self_avatar.set_avatar(
            user.avatar, user.id, user.avatar_version
        )
        self.panel.self_avatar.setVisible(True)

    def self_profile(self) -> dict:
        user = dict(self.panel._profile_user or {})
        if self.panel._auth_session is not None:
            auth_user = self.panel._auth_session.user
            user.setdefault("id", auth_user.id)
            user.setdefault("username", auth_user.username)
            user.setdefault("avatar", auth_user.avatar)
            user.setdefault("avatar_version", auth_user.avatar_version)
            user.setdefault("rating", auth_user.rating)
            user.setdefault("peak_rating", auth_user.peak_rating)
            user.setdefault("rated_games", auth_user.rated_games)
            user.setdefault("wins", auth_user.wins)
            user.setdefault("losses", auth_user.losses)
            user.setdefault("draws", auth_user.draws)
            user.setdefault("rank_name", auth_user.rank_name)
            user.setdefault("win_rate", auth_user.win_rate)
            user.setdefault(
                "current_win_streak", auth_user.current_win_streak
            )
            user.setdefault(
                "best_win_streak", auth_user.best_win_streak
            )
            user.setdefault(
                "achievements", list(auth_user.achievement_keys)
            )
        return {"user": user, "online": True, "friendship": "self"}

    def open_user_profile(self, user_id: str) -> None:
        if not user_id or self.panel._auth_session is None:
            return
        self.panel._pending_profile_user_id = user_id
        if self.panel._auth_session.user.id == user_id:
            self.panel._show_profile_panel(self.panel._self_profile())
            return
        self.panel._social_client.fetch_user(
            self.panel._auth_session.access_token, user_id
        )

    def show_profile_panel(self, profile: object) -> None:
        if not isinstance(profile, Mapping):
            return
        self.panel._current_profile = dict(profile)
        if self.panel._profile_panel is None:
            window = ProfilePanel(self.panel)
            window.add_friend_requested.connect(
                self.panel._send_friend_request
            )
            window.message_requested.connect(
                self.panel._open_private_chat
            )
            window.remove_friend_requested.connect(self.panel._remove_friend)
            window.avatar_requested.connect(self.panel._open_avatar_picker)
            window.open_friends_requested.connect(
                self.panel._open_friends_panel
            )
            window.achievements_requested.connect(
                self.panel._open_achievements_panel
            )
            # destroyed 可能在面板包装对象释放之后才发出；把面板绑进
            # 默认参数，保持与旧实现相同的强引用生命周期。
            window.destroyed.connect(
                lambda *_, panel=self.panel: setattr(
                    panel, "_profile_panel", None
                )
            )
            self.panel._profile_panel = window
        self.panel._profile_panel.set_profile(profile)
        self.panel._profile_panel.show()

    def open_achievements_panel(self) -> None:
        """打开当前资料对应玩家的完整成就墙。"""
        profile = getattr(self.panel, "_current_profile", None)
        if not isinstance(profile, Mapping):
            return
        user = profile.get("user")
        if not isinstance(user, Mapping):
            return
        if self.panel._achievements_panel is not None:
            self.panel._achievements_panel.close()
            self.panel._achievements_panel.deleteLater()
            self.panel._achievements_panel = None
        panel = AchievementPanel(user, self.panel)
        panel.destroyed.connect(
            lambda *_, panel=self.panel: setattr(
                panel, "_achievements_panel", None
            )
        )
        self.panel._achievements_panel = panel
        panel.show()

    def open_avatar_picker(self) -> None:
        if self.panel._auth_session is None:
            return
        picker = AvatarPickerPanel(
            self.panel._auth_session.user.avatar,
            self.panel,
            current_version=self.panel._auth_session.user.avatar_version,
        )
        picker.avatar_selected.connect(self.panel._on_avatar_selected)
        picker.image_selected.connect(self.panel._on_avatar_image_selected)
        picker.exec()

    def on_avatar_selected(self, avatar: str) -> None:
        if self.panel._auth_session is None:
            return
        self.panel._social_client.update_avatar(
            self.panel._auth_session.access_token, avatar
        )

    def on_avatar_image_selected(self, data_url: str) -> None:
        if self.panel._auth_session is None or not data_url:
            return
        self.panel._show_status("正在上传头像…")
        self.panel._social_client.update_avatar_image(
            self.panel._auth_session.access_token, data_url
        )

    def apply_avatar_update(self, data: object) -> None:
        if not isinstance(data, Mapping) or self.panel._auth_session is None:
            return
        avatar = data.get("avatar")
        if not isinstance(avatar, str):
            return
        avatar_version = entry_avatar_version(data)
        user = replace(
            self.panel._auth_session.user,
            avatar=normalize_avatar(
                avatar, self.panel._auth_session.user.id
            ),
            avatar_version=avatar_version,
        )
        self.panel._auth_session = replace(self.panel._auth_session, user=user)
        avatar_image_cache().invalidate(user.id)
        if self.panel._profile_user is not None:
            self.panel._profile_user["avatar"] = user.avatar
            self.panel._profile_user["avatar_version"] = user.avatar_version
        self.panel._update_self_avatar()
        # 已经打开的私聊窗也要换上新的头像。
        for window in self.panel._chat_panels.values():
            window.set_self_avatar(
                user.avatar, avatar_version=user.avatar_version
            )
        if (
            self.panel._profile_panel is not None
            and self.panel._profile_panel.user_id == user.id
        ):
            # 资料浮层此刻显示的不是自己时，不能把本人的头像盖上去。
            self.panel._profile_panel.set_avatar(
                user.avatar, user.avatar_version
            )
        self.panel._show_status("头像已更新")

    def refresh_open_profile(self) -> None:
        if (
            self.panel._profile_panel is not None
            and self.panel._pending_profile_user_id
            and self.panel._auth_session is not None
        ):
            self.panel._social_client.fetch_user(
                self.panel._auth_session.access_token,
                self.panel._pending_profile_user_id,
            )

    def on_refresh_history(self) -> None:
        if self.panel._auth_session is None:
            return
        self.panel._history_client.fetch_recent(
            self.panel._auth_session.access_token, 20
        )

    def on_history_recent_ready(self, data: object) -> None:
        if self.panel._auth_session is None or self.panel._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        self.panel._recent_games_entries = [
            entry for entry in entries if isinstance(entry, Mapping)
        ]
        total = data.get("total", len(self.panel._recent_games_entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(self.panel._recent_games_entries)
        self.panel._fill_recent_games(self.panel._recent_games_entries, total)
        self.panel._refresh_lobby_controls()

    def fill_recent_games(self, entries: list, total: int) -> None:
        table = self.panel.recent_games_table
        table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                format_game_time(entry.get("ended_at")),
                str(entry.get("opponent_username", "对手")),
                result_label(entry.get("result")),
                "排位" if entry.get("rated") else "自建房",
                str(entry.get("move_no", 0)),
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                item.setFlags(
                    item.flags() & ~Qt.ItemFlag.ItemIsEditable
                )
                if column == 1:
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignVCenter
                        | Qt.AlignmentFlag.AlignLeft
                    )
                else:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                # 最近对局摘要也带规则字段：连珠对局用悬停提示标出来，
                # 不额外占用表格列。
                if column == 3 and entry.get("rule_mode") == "renju":
                    item.setToolTip(
                        "连珠规则：黑棋长连/双四/双三禁手"
                    )
                table.setItem(row, column, item)

        if entries:
            self.panel.history_status_label.setText(
                f"共 {total} 局已完成对局，双击可复盘"
            )
        else:
            self.panel.history_status_label.setText(
                "暂无已完成对局，完成一局排位或自建房后会显示在这里"
            )
        self.panel.replay_history_button.setEnabled(False)

    def on_history_game_ready(self, data: object) -> None:
        if self.panel._auth_session is None or self.panel._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        try:
            record = record_from_game_detail(data)
        except (RecordError, TypeError, ValueError) as exc:
            logger.warning("棋谱数据无效：%s", exc)
            self.panel.history_status_label.setText("该局棋谱无法复盘")
            return
        self.panel._open_replay(record)

    def open_replay(self, record: GameRecord) -> None:
        """请求主窗口打开复盘页；复盘面板由主窗口统一管理。"""
        self.panel.replay_requested.emit([record])

    def on_history_failed(self, message: str) -> None:
        logger.warning("读取最近对局失败：%s", message)
        if self.panel.stack.currentWidget() is self.panel.lobby_page:
            self.panel.history_status_label.setText(
                message or "无法加载最近对局"
            )
        self.panel._refresh_lobby_controls()

    def on_history_unauthorized(self) -> None:
        if self.panel._auth_session is None or self.panel._auth_client.busy:
            return
        self.panel._refresh_after_auth_refresh = True
        if not self.panel._auth_client.refresh():
            self.panel._refresh_after_auth_refresh = False
            self.panel._force_relogin("登录已过期，请重新登录")

    def on_history_busy_changed(self, _busy: bool) -> None:
        if hasattr(self.panel, "refresh_history_button"):
            self.panel._refresh_lobby_controls()

    def on_recent_selection_changed(self) -> None:
        self.panel._refresh_lobby_controls()

    def on_recent_double_clicked(self, row: int, _column: int) -> None:
        self.panel._request_replay_row(row)

    def on_replay_history(self) -> None:
        self.panel._request_replay_row(self.panel.recent_games_table.currentRow())

    def request_replay_row(self, row: int) -> None:
        if self.panel._auth_session is None:
            return
        if row < 0 or row >= len(self.panel._recent_games_entries):
            return
        entry = self.panel._recent_games_entries[row]
        game_id = entry.get("game_id")
        if not isinstance(game_id, str) or not game_id:
            return
        self.panel.history_status_label.setText("正在加载棋谱…")
        self.panel._history_client.fetch_game(
            self.panel._auth_session.access_token, game_id
        )

    def fetch_live_games(self) -> None:
        """拉取进行中的对局列表（对战广场）。

        只在广场页可见时拉取：其它页面要这个列表没有意义，白白发请求。
        """
        if self.panel._auth_session is None or self.panel._session is None:
            return
        if self.panel.stack.currentWidget() is not self.panel.plaza_page:
            return
        if self.panel._live_client.busy:
            return
        self.panel._live_client.fetch_live(self.panel._auth_session.access_token)

    def on_refresh_live(self) -> None:
        """手动刷新进行中的对局列表。"""
        self.panel._fetch_live_games()

    def on_live_timer(self) -> None:
        """兜底刷新：推送可能因为断线丢失，定时补一次。"""
        self.panel._fetch_live_games()

    def on_live_games_changed(self) -> None:
        """服务器推送“对局列表变了”：有新对局开始或已有对局结束。"""
        if self.panel.stack.currentWidget() is not self.panel.plaza_page:
            return
        self.panel._fetch_live_games()

    def on_live_ready(self, data: object) -> None:
        """收到进行中的对局列表并渲染到对战广场。"""
        if self.panel._auth_session is None or self.panel._logging_out:
            return
        if not isinstance(data, Mapping):
            return
        raw_entries = data.get("entries")
        entries = raw_entries if isinstance(raw_entries, list) else []
        self.panel._live_games_entries = [
            entry for entry in entries if isinstance(entry, Mapping)
        ]
        total = data.get("total", len(self.panel._live_games_entries))
        if not isinstance(total, int) or isinstance(total, bool):
            total = len(self.panel._live_games_entries)
        self.panel._fill_live_games(self.panel._live_games_entries, total)
        self.panel._refresh_plaza_controls()

    def on_live_failed(self, message: str) -> None:
        """读取进行中对局失败：提示在广场页，不影响其它区块。"""
        logger.warning("读取进行中对局失败：%s", message)
        if self.panel.stack.currentWidget() is self.panel.plaza_page:
            self.panel.live_status_label.setText(
                message or "无法加载进行中的对局"
            )
        self.panel._refresh_plaza_controls()

    def fill_live_games(self, entries: list, total: int) -> None:
        """把进行中对局列表交给广场页渲染。"""
        self.panel.plaza_page.set_entries(entries, total)

    def on_watch_game(self, game_id: str) -> None:
        """开始观战指定的进行中对局。"""
        session = self.panel._session
        if session is None or not game_id:
            return
        if session.in_room or session.is_spectator:
            self.panel.plaza_status_label.setText(
                "你已经在对局中，无法开始观战"
            )
            return
        if session.watch_game(game_id):
            # 真正的切换由服务器下发的观战快照触发（_sync_online_ui）。
            self.panel._watching_from_plaza = True
            self.panel.plaza_status_label.setText("正在进入观战…")

    def clear_profile_ui(self) -> None:
        self.panel._profile_user = None
        self.panel._season_info = None
        self.panel._season_rank = None
        self.panel._leaderboard_entries = []
        self.panel.leaderboard_table.setRowCount(0)
        self.panel.leaderboard_status_label.setText("登录后自动加载排行榜")
        self.panel._update_rating_summary()
        self.panel._recent_games_entries = []
        self.panel._live_games_entries = []
        if hasattr(self.panel, "live_games_layout"):
            self.panel._fill_live_games([], 0)
        if hasattr(self.panel, "recent_games_table"):
            self.panel.recent_games_table.setRowCount(0)
            self.panel.history_status_label.setText(
                "登录后自动加载最近对局"
            )
            self.panel.replay_history_button.setEnabled(False)
