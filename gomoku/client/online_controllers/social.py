"""好友、私聊、邀请与举报审计控制器。

面板里的社交浮层、私聊窗口、好友事件、房间邀请和举报提交/审计展示
都由这里编排；HTTP 客户端仍由 ``OnlinePanel`` 构造并注入。
"""

from __future__ import annotations

import logging
from typing import Mapping

from gomoku.client.friends_panel import FriendsPanel
from gomoku.client.online_controllers.common import (
    PanelController,
    entry_avatar_version,
    host_module,
)
from gomoku.client.private_chat_panel import PrivateChatPanel
from gomoku.client.report_panel import ReportPanel
from gomoku.shared.social import DirectMessage, FriendEvent

logger = logging.getLogger(__name__)


class SocialController(PanelController):
    """好友、私聊、房间邀请与举报界面。"""

    def refresh_friends(self) -> None:
        if self.panel._auth_session is None:
            return
        self.panel._social_client.fetch_friends(
            self.panel._auth_session.access_token
        )

    def update_friends_badge(self) -> None:
        incoming = self.panel._friends_data.get("incoming", [])
        count = len(incoming) if isinstance(incoming, list) else 0
        self.panel.friends_button.setText(
            f"好友 ({count})" if count else "好友"
        )

    def open_friends_panel(self, invite_mode: bool = False) -> None:
        if self.panel._auth_session is None:
            return
        if self.panel._friends_panel is not None:
            self.panel._friends_panel.set_invite_mode(invite_mode)
            self.panel._friends_panel.show()
            return
        window = FriendsPanel(self.panel, invite_mode=invite_mode)
        window.profile_requested.connect(self.panel._open_user_profile)
        window.chat_requested.connect(self.panel._open_private_chat)
        window.invite_requested.connect(self.panel._invite_friend)
        window.add_requested.connect(self.panel._send_friend_request)
        window.accept_requested.connect(self.panel._accept_friend_request)
        window.decline_requested.connect(self.panel._decline_friend_request)
        window.remove_requested.connect(self.panel._remove_friend)
        window.search_requested.connect(self.panel._search_users)
        window.refresh_requested.connect(self.panel._refresh_friends)
        window.destroyed.connect(
            lambda *_, panel=self.panel: setattr(
                panel, "_friends_panel", None
            )
        )
        self.panel._friends_panel = window
        if self.panel._friends_data:
            window.set_data(self.panel._friends_data)
        window.show()
        self.panel._refresh_friends()

    def send_friend_request(self, user_id: str) -> None:
        if self.panel._auth_session is None or not user_id:
            return
        self.panel._social_client.send_friend_request(
            self.panel._auth_session.access_token, user_id
        )
        self.panel._show_status("正在发送好友申请…")

    def accept_friend_request(self, request_id: str) -> None:
        if self.panel._auth_session is None or not request_id:
            return
        self.panel._social_client.accept_friend_request(
            self.panel._auth_session.access_token, request_id
        )

    def decline_friend_request(self, request_id: str) -> None:
        if self.panel._auth_session is None or not request_id:
            return
        self.panel._social_client.decline_friend_request(
            self.panel._auth_session.access_token, request_id
        )

    def remove_friend(self, user_id: str) -> None:
        if self.panel._auth_session is None or not user_id:
            return
        username, _avatar, _version = self.panel._username_avatar(user_id)
        dialog = host_module().ConfirmDialog(
            "删除好友",
            f"确定删除好友「{username}」吗？",
            confirm_text="删除",
            cancel_text="取消",
            seal_text="友",
            parent=self.panel,
        )
        dialog.exec()
        if not dialog.confirm_requested:
            # 取消删除：这一行不能锁，聊天和邀请对局还得能点。
            return
        # 确认之后再锁行。锁早了的话，用户一取消，这一行就再也点不动。
        self.panel._pending_removal_user_id = user_id
        if self.panel._friends_panel is not None:
            self.panel._friends_panel.lock_friend_row(user_id)
        self.panel._social_client.remove_friend(
            self.panel._auth_session.access_token, user_id
        )

    def search_users(self, query: str) -> None:
        if self.panel._auth_session is None:
            return
        if self.panel._friends_panel is not None:
            self.panel._friends_panel.set_status("正在搜索…")
        self.panel._social_client.search_users(
            self.panel._auth_session.access_token, query
        )

    def username_avatar(
        self, user_id: str, fallback: str = ""
    ) -> tuple[str, str, int]:
        current = getattr(self.panel, "_current_profile", None)
        if isinstance(current, Mapping):
            user = current.get("user")
            if isinstance(user, Mapping) and user.get("id") == user_id:
                return (
                    str(user.get("username", fallback or "好友")),
                    str(user.get("avatar", "")),
                    entry_avatar_version(user),
                )
        for section in ("friends", "incoming", "outgoing"):
            entries = self.panel._friends_data.get(section, [])
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                entry_id = entry.get("user_id") or entry.get("id")
                if entry_id == user_id:
                    return (
                        str(entry.get("username", fallback or "好友")),
                        str(entry.get("avatar", "")),
                        entry_avatar_version(entry),
                    )
        return fallback or "好友", "", 0

    def open_private_chat(
        self, user_id: str, username: str = ""
    ) -> None:
        if not user_id:
            return
        resolved_name, avatar, avatar_version = self.panel._username_avatar(
            user_id, username
        )
        existing = self.panel._chat_panels.get(user_id)
        if existing is not None:
            # 聊天窗可以被关闭，重新点“聊天”时把旧窗拉回来：刷新一下
            # 好友资料，再补拉一次历史（关闭期间对方可能发过消息）。
            existing.set_friend(
                resolved_name, avatar, avatar_version=avatar_version
            )
            self.panel._sync_chat_self_avatar(existing)
            existing.show()
            if self.panel._auth_session is not None:
                self.panel._social_client.fetch_messages(
                    self.panel._auth_session.access_token, user_id, limit=200
                )
            return
        window = PrivateChatPanel(
            user_id, resolved_name, avatar, self.panel, avatar_version
        )
        window.send_requested.connect(
            lambda text, uid=user_id, panel=self.panel: (
                panel._send_private_message(uid, text)
            )
        )
        window.destroyed.connect(
            lambda *_, panel=self.panel: panel._chat_panels.pop(
                user_id, None
            )
        )
        self.panel._chat_panels[user_id] = window
        self.panel._sync_chat_self_avatar(window)
        window.set_send_enabled(True)
        window.show()
        if self.panel._auth_session is not None:
            self.panel._social_client.fetch_messages(
                self.panel._auth_session.access_token, user_id, limit=200
            )

    def sync_chat_self_avatar(self, window: PrivateChatPanel) -> None:
        """把自己的头像告诉聊天窗：自己发的消息旁边也要挂头像。"""
        if self.panel._auth_session is None:
            return
        user = self.panel._auth_session.user
        window.set_self_avatar(
            user.avatar, avatar_version=user.avatar_version
        )

    def send_private_message(self, user_id: str, text: str) -> None:
        if self.panel._auth_session is None or not text.strip():
            return
        window = self.panel._chat_panels.get(user_id)
        if window is not None:
            window.set_status("发送中…")
        self.panel._social_client.send_message(
            self.panel._auth_session.access_token, user_id, text
        )

    def on_direct_message(self, message: object) -> None:
        if not isinstance(message, DirectMessage):
            return
        payload = {
            "id": message.id,
            "sender_id": message.sender_id,
            "sender_name": message.sender_name,
            "sender_avatar": message.sender_avatar,
            "sender_avatar_version": message.sender_avatar_version,
            "content": message.content,
            "sent_at_ms": message.sent_at_ms,
        }
        self_id = (
            self.panel._auth_session.user.id if self.panel._auth_session else ""
        )
        window = self.panel._chat_panels.get(message.sender_id)
        if window is not None:
            window.append_message(payload, self_id)
            window.set_status("")
        if window is not None and window.isVisible():
            # 聊天窗正开着，这条消息当场就读完了：让服务端销账，
            # 免得好友列表里的未读数一直挂着。
            self.panel._report_messages_read(message.sender_id, window)
        else:
            # 没有聊天窗，或者窗被关掉了：这条消息算未读，好友列表
            # 上要冒出未读标记来。
            self.panel._show_status(f"收到 {message.sender_name} 的新消息")
            self.panel._refresh_friends()

    def report_messages_read(
        self, user_id: str, window: PrivateChatPanel
    ) -> None:
        """把窗口里已读的消息报给服务端，并清掉本地未读数。"""
        message_ids = window.pending_read_ids()
        if message_ids:
            window.mark_reported(message_ids)
            if self.panel._auth_session is not None:
                self.panel._social_client.mark_messages_read(
                    self.panel._auth_session.access_token, user_id, message_ids
                )
        self.panel._clear_friend_unread(user_id)

    def clear_friend_unread(self, user_id: str) -> None:
        """把某位好友的未读数清零。

        先在本地改一次，好友列表立刻不再显示未读；再向服务端要一次
        好友列表校准——本地只剩“已经显示过”的消息才算读完，服务端
        才知道最终数字。
        """
        friends = self.panel._friends_data.get("friends", [])
        changed = False
        if isinstance(friends, list):
            for entry in friends:
                if not isinstance(entry, Mapping):
                    continue
                if str(entry.get("user_id", "")) != user_id:
                    continue
                if entry.get("unread"):
                    entry["unread"] = 0
                    changed = True
        if changed and self.panel._friends_panel is not None:
            self.panel._friends_panel.set_data(self.panel._friends_data)
        self.panel._refresh_friends()

    def on_friend_event(self, event: object) -> None:
        if not isinstance(event, FriendEvent):
            return
        if event.event == "request":
            self.panel._show_status(f"{event.username} 想加你为好友")
            if (
                self.panel.stack.currentWidget() is self.panel.lobby_page
                and event.request_id
            ):
                dialog = host_module().ConfirmDialog(
                    "好友申请",
                    f"{event.username} 想加你为好友，是否接受？",
                    confirm_text="接受",
                    cancel_text="稍后处理",
                    seal_text="友",
                    parent=self.panel,
                )
                dialog.exec()
                if dialog.confirm_requested:
                    self.panel._accept_friend_request(event.request_id)
        elif event.event == "accepted":
            self.panel._show_status(f"你和 {event.username} 已成为好友")
        elif event.event == "removed":
            self.panel._show_status(f"{event.username} 已从好友列表中移除")
        elif event.event == "declined":
            self.panel._show_status(f"{event.username} 拒绝了你的好友申请")
        self.panel._refresh_friends()

    def on_room_invited(self, payload: object) -> None:
        if (
            self.panel._session is None
            or not isinstance(payload, Mapping)
        ):
            return
        invite_id = payload.get("invite_id")
        inviter = payload.get("inviter")
        if not isinstance(invite_id, str) or not isinstance(
            inviter, Mapping
        ):
            return
        name = str(inviter.get("username", "好友"))
        if self.panel._session.in_room:
            self.panel._session.respond_room_invite(invite_id, False)
            self.panel._show_status(
                "你正在对局中，已拒绝好友邀请", error=True
            )
            return
        dialog = host_module().ConfirmDialog(
            "对局邀请",
            f"{name} 邀请你进行一局自建房对局，是否接受？",
            confirm_text="接受",
            cancel_text="拒绝",
            seal_text="邀",
            parent=self.panel,
        )
        dialog.exec()
        accept = dialog.confirm_requested
        self.panel._session.respond_room_invite(invite_id, accept)
        self.panel._show_status(
            "已接受邀请，正在进入房间…"
            if accept
            else "已拒绝好友邀请"
        )

    def on_room_invite_result(self, payload: object) -> None:
        if not isinstance(payload, Mapping):
            return
        status = payload.get("status")
        user = payload.get("user")
        name = (
            str(user.get("username", "好友"))
            if isinstance(user, Mapping)
            else "好友"
        )
        reason = str(payload.get("reason", ""))
        if status == "sent":
            self.panel._show_status(f"已向 {name} 发送对局邀请")
        elif status == "accepted":
            self.panel._show_status(f"{name} 已接受邀请，正在开始对局")
        elif status == "declined":
            self.panel._show_status(f"{name} 拒绝了邀请", error=True)
        elif status == "cancelled":
            self.panel._show_status(
                f"对局邀请已失效{('：' + reason) if reason else ''}",
                error=True,
            )

    def on_social_ready(self, kind: str, data: object) -> None:
        if not isinstance(kind, str):
            return
        if kind.startswith("user:"):
            requested_id = kind.split(":", 1)[1]
            if requested_id != self.panel._pending_profile_user_id:
                # 已经切到别的用户了，过期响应直接丢掉。
                return
            if isinstance(data, Mapping):
                user = data.get("user")
                if isinstance(user, Mapping):
                    returned_id = str(user.get("id", ""))
                    if returned_id and returned_id != requested_id:
                        return
            self.panel._show_profile_panel(data)
            return
        if kind == "avatar":
            self.panel._apply_avatar_update(data)
            return
        if kind == "search":
            entries = (
                data.get("entries", [])
                if isinstance(data, Mapping)
                else []
            )
            if self.panel._friends_panel is not None:
                self.panel._friends_panel.set_search_results(entries)
                self.panel._friends_panel.set_status(
                    f"找到 {len(entries)} 位玩家"
                )
            return
        if kind == "friends":
            if isinstance(data, Mapping):
                self.panel._friends_data = dict(data)
            self.panel._update_friends_badge()
            if self.panel._friends_panel is not None:
                self.panel._friends_panel.set_data(self.panel._friends_data)
            return
        if kind == "friend_request":
            self.panel._show_status("好友申请已发送")
            self.panel._refresh_friends()
            self.panel._refresh_open_profile()
            return
        if kind == "friend_accept":
            self.panel._show_status("已添加好友")
            self.panel._refresh_friends()
            self.panel._refresh_open_profile()
            return
        if kind in ("friend_decline", "friend_remove"):
            self.panel._pending_removal_user_id = ""
            self.panel._refresh_friends()
            self.panel._refresh_open_profile()
            return
        if kind.startswith("messages:"):
            user_id = kind.split(":", 1)[1]
            window = self.panel._chat_panels.get(user_id)
            if window is None:
                return
            messages = (
                data.get("messages", [])
                if isinstance(data, Mapping)
                else []
            )
            self_id = (
                self.panel._auth_session.user.id
                if self.panel._auth_session
                else ""
            )
            window.set_history(messages, self_id)
            window.set_status("")
            window.set_send_enabled(True)
            # 历史接口已经把返回的消息标成已读，好友列表上的未读数要
            # 跟着归零，否则会出现"消息读完了还显示未读"。
            if window.isVisible():
                # 实时推送先到、还没被历史覆盖的那几条，仍然要销账。
                self.panel._report_messages_read(user_id, window)
            else:
                self.panel._clear_friend_unread(user_id)
            return
        if kind.startswith("messages_read:"):
            # 销账成功：好友列表的未读数已经本地清零，这里等下一次
            # 好友列表刷新校准即可，不必再发一次请求。
            return
        if kind.startswith("message_send:"):
            user_id = kind.split(":", 1)[1]
            window = self.panel._chat_panels.get(user_id)
            if window is not None and isinstance(data, Mapping):
                self_id = (
                    self.panel._auth_session.user.id
                    if self.panel._auth_session
                    else ""
                )
                window.append_message(data, self_id)
                window.clear_draft()
                window.set_status("")

    def on_social_failed(self, kind: str, message: str) -> None:
        logger.warning("社交请求失败 kind=%s：%s", kind, message)
        if kind.startswith("messages_read:"):
            # 销账请求是后台动作，失败了下次刷新好友列表自然会把
            # 未读数补回来，不用拿红色报错打扰用户。
            return
        if kind == "friend_remove":
            # 删除失败：对方仍是好友，把锁住的那一行解开。
            pending = self.panel._pending_removal_user_id
            self.panel._pending_removal_user_id = ""
            if self.panel._friends_panel is not None and pending:
                self.panel._friends_panel.unlock_friend_row(pending)
        elif kind in ("friend_accept", "friend_decline"):
            # 接受/拒绝失败：申请还在列表里，重新拉一次把行按钮解锁。
            self.panel._refresh_friends()
        if kind.startswith("messages:") or kind.startswith(
            "message_send:"
        ):
            user_id = kind.split(":", 1)[1]
            window = self.panel._chat_panels.get(user_id)
            if window is not None:
                window.set_status(message or "发送失败")
                if kind.startswith("message_send:"):
                    # 发送失败：把文本放回输入框，别让用户白打一遍。
                    window.restore_draft()
                return
        if self.panel._friends_panel is not None and kind in (
            "friends",
            "friend_request",
            "friend_accept",
            "friend_decline",
            "friend_remove",
            "search",
        ):
            self.panel._friends_panel.set_status(message)
        self.panel._show_status(message or "操作失败", error=True)

    def on_social_unauthorized(self, _kind: str) -> None:
        if self.panel._auth_session is None or self.panel._auth_client.busy:
            return
        self.panel._refresh_after_auth_refresh = True
        if not self.panel._auth_client.refresh():
            self.panel._refresh_after_auth_refresh = False
            self.panel._force_relogin("登录已过期，请重新登录")

    def close_social_windows(self) -> None:
        for panel in (
            self.panel._friends_panel,
            self.panel._profile_panel,
            self.panel._achievements_panel,
        ):
            if panel is not None:
                try:
                    panel.close()
                    panel.deleteLater()
                except RuntimeError:
                    pass
        for panel in list(self.panel._chat_panels.values()):
            try:
                panel.close()
                panel.deleteLater()
            except RuntimeError:
                pass
        self.panel._friends_panel = None
        self.panel._profile_panel = None
        self.panel._achievements_panel = None
        self.panel._chat_panels.clear()
        self.panel._friends_data = {}
        self.panel._pending_profile_user_id = ""

    def on_report_clicked(self) -> None:
        if (
            self.panel._session is None
            or self.panel._auth_session is None
            or self.panel._session.snapshot is None
            or not self.panel._session.snapshot.is_game_over
        ):
            return
        snapshot = self.panel._session.snapshot
        if self.panel._report_panel is not None:
            self.panel._report_panel.close()
        panel = ReportPanel(
            snapshot.game_id,
            self.panel._session.opponent_username,
            parent=self.panel,
        )
        panel.submitted.connect(self.panel._on_report_submit)
        panel.closed.connect(self.panel._on_report_panel_closed)
        self.panel._report_panel = panel
        self.panel._report_game_id = snapshot.game_id
        self.panel._report_awaiting_response = False
        panel.show()
        panel.raise_()
        token = self.panel._auth_session.access_token
        if self.panel._api_base_url:
            self.panel._report_client.fetch_audit(token, snapshot.game_id)
        else:
            panel.set_audit(0, ok=False)

    def on_report_submit(self, category: str, description: str) -> None:
        if (
            self.panel._report_panel is None
            or self.panel._auth_session is None
            or not self.panel._report_game_id
        ):
            return
        self.panel._report_awaiting_response = True
        if not self.panel._report_client.submit_report(
            self.panel._auth_session.access_token,
            self.panel._report_game_id,
            category,
            description,
        ):
            self.panel._report_awaiting_response = False
            self.panel._report_panel.set_result(
                "无法提交举报，请检查连接", success=False
            )

    def on_report_submitted(self, _data: object) -> None:
        self.panel._report_awaiting_response = False
        if self.panel._report_panel is None:
            return
        self.panel._report_panel.set_result(
            "举报已提交，管理员会结合回放审计处理。", success=True
        )

    def on_report_audit_ready(self, data: object) -> None:
        if self.panel._report_panel is None or not isinstance(data, Mapping):
            return
        game_id = data.get("game_id")
        if isinstance(game_id, str) and game_id != self.panel._report_game_id:
            return
        raw_findings = data.get("findings")
        findings = (
            raw_findings if isinstance(raw_findings, list) else []
        )
        details = "；".join(
            str(item.get("message", ""))
            for item in findings[:3]
            if isinstance(item, Mapping)
        )
        self.panel._report_panel.set_audit(
            len(findings),
            ok=bool(data.get("ok", not findings)),
            details=details,
        )

    def on_report_failed(self, message: str) -> None:
        if self.panel._report_panel is None:
            return
        if self.panel._report_awaiting_response:
            self.panel._report_awaiting_response = False
            self.panel._report_panel.set_result(
                message or "举报提交失败", success=False
            )
        else:
            self.panel._report_panel.audit_label.setText(
                "回放审计暂时不可用，举报仍可提交。"
            )

    def on_report_panel_closed(self) -> None:
        panel = self.panel._report_panel
        self.panel._report_panel = None
        self.panel._report_game_id = ""
        if panel is not None:
            panel.deleteLater()
