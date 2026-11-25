"""服务器权威的好友房、排位房与快速匹配管理器。

设计要点：
* 单进程内存房间/匹配队列 + 每手数据库写穿；每房一个 asyncio.Lock 串行处理；
* 客户端只提交意图，服务器校验座位/回合/步序/坐标/时钟后广播完整快照；
* `(game_id, move_no)` 和 `(game_id, client_move_id)` 唯一，支持幂等重发；
* 断线不立刻判负：保留房间和时钟，玩家可在剩余时间内重连；
* 服务重启不伪造恢复：启动时未终局对局标记 abandoned，客户端回大厅；
* 好友房不计分；快速匹配创建排位房，终局由服务器按 Elo 结算一次，
  并把两名玩家的积分变化写入 rating_events 审计；
* 排位房不能通过房间码加入、不支持同一对手直接再战，降低刷分面。
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections import OrderedDict
from typing import Optional, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy import delete, select, update

from gomoku.shared.avatar import DEFAULT_AVATAR
from gomoku.shared.chat import (
    CHAT_BURST,
    CHAT_WINDOW_SECONDS,
    normalize_chat_content,
)
from gomoku.shared.errors import ProtocolError
from gomoku.shared.game_model import GameModel
from gomoku.shared.protocol import (
    SERVER_ERROR,
    SERVER_LIVE_GAMES_CHANGED,
    SERVER_MATCH_FOUND,
    SERVER_MOVE_REJECTED,
    SERVER_QUEUE_STATUS,
    SERVER_ROOM_CLOSED,
    SERVER_ROOM_INVITATION,
    SERVER_ROOM_INVITE_RESULT,
    make_message,
)
from gomoku.shared.rules import (
    Player,
    RuleMode,
    in_bounds,
    is_forbidden_move,
    parse_rule_mode,
)
from gomoku.shared.time_control import DEFAULT_TIME_CONTROL, TimeControl
from server.app.config import Settings
from server.app.db import rowcount_of, utcnow_naive
from server.app.metrics import ServerMetrics
from server.app.models import (
    Friendship,
    Game,
    Move,
    User,
    new_id,
)
from server.app.seasons import SeasonInfo, ensure_current_season
from server.app.ws import payloads
from server.app.ws.clock_service import ClockService
from server.app.ws.connection_registry import ConnectionRegistry
from server.app.ws.entities import (
    ClientConnection,
    QueuedPlayer,
    Room,
    RoomInvite,
)
from server.app.ws.matchmaker import Matchmaker
from server.app.ws.settlement import RatingSettlement, SettlementResult

logger = logging.getLogger(__name__)

_ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_INVITE_TTL_SECONDS = 60.0
#: 单个连接发送一条广播消息的最长时间；超过就放弃这一条。
_BROADCAST_SEND_TIMEOUT_SECONDS = 5.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _consume_task_result(task: asyncio.Future) -> None:
    """取走后台发送任务的异常，避免 asyncio 报“异常未被检索”。"""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.warning("后台发送失败: %s", exc)


class RoomManager:
    """单进程内所有连接、房间与计时器的管理者。"""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        metrics: ServerMetrics,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._metrics = metrics
        self._rooms_by_code: dict[str, Room] = {}
        self._room_by_user: dict[str, Room] = {}
        # 观战者 → 正在观战的房间。观战者绝不进 _room_by_user：
        # 那张表表示“这个人是本局玩家”，混进去会让离开房间/断线/重连
        # 逻辑把观战者当成对局参与者处理。
        self._spectator_by_user: dict[str, Room] = {}
        self._invites: dict[str, RoomInvite] = {}
        self._state_lock = asyncio.Lock()
        # 赛季推进与积分结算共用一把锁，避免同一进程内重复归档/重置。
        self._season_lock = asyncio.Lock()
        self._current_season: Optional[SeasonInfo] = None
        # 领域服务：连接、匹配、计时与积分结算各自持有独立状态。
        self._connections_registry = ConnectionRegistry(
            settings, session_factory, self._state_lock, self._room_by_user
        )
        self._matchmaker = Matchmaker(
            self._state_lock,
            self._connections_registry,
            self._room_by_user,
        )
        # 注入 lambda 而不是函数对象：测试 monkeypatch
        # ``server.app.ws.rooms._now_ms`` 后，服务内取值仍然生效。
        self._clocks = ClockService(settings, lambda: _now_ms(), self)
        self._settlement = RatingSettlement(
            session_factory, self.current_season
        )

    @property
    def _connections(self) -> dict[str, ClientConnection]:
        """连接表视图；实际状态由 ConnectionRegistry 持有。"""
        return self._connections_registry.connections

    @_connections.setter
    def _connections(self, value: dict[str, ClientConnection]) -> None:
        self._connections_registry.connections = value

    @property
    def _friend_cache(self) -> "OrderedDict[str, frozenset[str]]":
        """好友缓存视图；实际状态由 ConnectionRegistry 持有。"""
        return self._connections_registry.friend_cache

    @_friend_cache.setter
    def _friend_cache(
        self, value: "OrderedDict[str, frozenset[str]]"
    ) -> None:
        self._connections_registry.friend_cache = value

    # ------------------------------------------------------------------
    # 连接/发送辅助
    # ------------------------------------------------------------------
    def _is_current_connection(self, conn: ClientConnection) -> bool:
        return self._connections_registry.is_current_connection(conn)

    def is_online(self, user_id: str) -> bool:
        """用户当前是否有一条活动连接。"""
        return self._connections_registry.is_online(user_id)

    def _release_connection_locked(self, connection_id: str) -> None:
        """调用方必须持有 ``_state_lock``。"""
        self._connections_registry.release_locked(connection_id)

    async def reserve_connection(self, conn: ClientConnection) -> bool:
        """认证后登记 IP 配额；同一 connection_id 重复调用是幂等的。"""
        return await self._connections_registry.reserve_connection(conn)

    async def release_connection(self, connection_id: str) -> None:
        await self._connections_registry.release_connection(connection_id)

    async def update_connection_avatar(
        self, user_id: str, avatar: str, avatar_version: int
    ) -> None:
        """头像在 REST 接口更新后，同步到已认证连接与所在房间。

        WebSocket 连接在认证时缓存了头像；如果用户在大厅里换了头像再
        开局，不刷新缓存会让对手看到旧头像。这里只更新展示字段，不广播，
        下一份对局快照自然带上新头像。
        """
        await self._connections_registry.update_connection_avatar(
            user_id, avatar, avatar_version
        )

    async def send_to_user(self, user_id: str, message: dict) -> bool:
        """向指定用户的当前连接发送一条消息。"""
        return await self._connections_registry.send_to_user(
            user_id, message
        )

    def _connection_user_payload(self, conn: ClientConnection) -> dict:
        """连接用户的好友/邀请摘要。"""
        return payloads.connection_user_payload(conn)

    async def _send_error(
        self,
        conn: ClientConnection,
        reason: str,
        request_id: Optional[str] = None,
    ) -> None:
        await conn.send(
            make_message(SERVER_ERROR, {"reason": reason}, request_id=request_id)
        )

    async def _send_all(
        self, sends: list[tuple[ClientConnection, dict]]
    ) -> None:
        """并发给一批连接发送各自的（同一类）消息。

        逐个 await 会让总延迟变成各连接发送时间之和：2 名玩家加最多
        50 名观战者里只要有一个慢客户端，整局广播（连同它持有的房间
        锁）就被拖住，超时判定也会跟着延迟。这里改成并发发送，并给
        每条发送单独设上限；某条失败或卡住都不影响其他人收到消息。

        ``ClientConnection.send`` 自带每连接一把发送锁，保证同一条连接
        上的消息不会交错；这里用 shield 包一层，超时时只是不再等待它，
        不打断已经开始的 WebSocket 写。
        """
        if not sends:
            return

        async def _send_one(conn: ClientConnection, message: dict) -> None:
            task = asyncio.ensure_future(conn.send(message))
            task.add_done_callback(_consume_task_result)
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=_BROADCAST_SEND_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "广播发送超时 user=%s", conn.user_id
                )
            except Exception:
                logger.warning(
                    "广播发送失败 user=%s", conn.user_id
                )

        await asyncio.gather(
            *(_send_one(conn, message) for conn, message in sends),
            return_exceptions=True,
        )

    async def current_season(self) -> Optional[SeasonInfo]:
        """返回当前赛季；数据库异常时退回上一次缓存，不阻塞对局。"""
        async with self._season_lock:
            now = utcnow_naive()
            cached = self._current_season
            if cached is not None and now < cached.ends_at:
                return cached
            try:
                season = await ensure_current_season(
                    self._session_factory, self._settings
                )
            except Exception:
                logger.exception("推进排位赛季失败")
                return cached
            self._current_season = season
            return season

    def _requested_time_control(
        self,
        turn_seconds: Optional[int] = None,
        time_control: Optional[TimeControl] = None,
    ) -> TimeControl:
        if time_control is not None:
            return time_control
        if turn_seconds is not None:
            return TimeControl.per_move(turn_seconds)
        return TimeControl.per_move(self._settings.turn_seconds)

    async def _send_room_closed(self, room: Room, reason: str) -> None:
        message = make_message(SERVER_ROOM_CLOSED, {"reason": reason})
        sends: list[tuple[ClientConnection, dict]] = [
            (conn, message) for conn in room.connections.values()
        ]
        # 观战者不是玩家，但对局结束/房间关闭时也要收到通知，
        # 否则客户端会一直以为还在观战。
        sends.extend(
            (conn, message) for conn in room.spectators.values()
        )
        await self._send_all(sends)

    async def _send_opponent_status(
        self,
        room: Room,
        target_user_id: str,
        online: bool,
        who_name: str,
    ) -> None:
        conn = room.connections.get(target_user_id)
        if conn is not None:
            await conn.send(
                payloads.opponent_status_message(who_name, online)
            )

    async def _send_room_created(self, conn: ClientConnection, room: Room) -> None:
        await conn.send(payloads.room_created_message(room))

    async def _notify_live_games_changed(self) -> None:
        """通知空闲客户端：进行中的对局列表发生了变化。

        对战广场据此即时增删对局条目，不必让客户端自己轮询。通知只说
        “变了”，不带列表内容，客户端再按需拉一次接口。

        只通知没有在对局中的连接：正在下棋或正在观战的人不需要这个广播，
        也不用让多余的消息插进对局的消息流里。
        """
        message = make_message(SERVER_LIVE_GAMES_CHANGED, {})
        sends: list[tuple[ClientConnection, dict]] = []
        for user_id, conn in list(self._connections.items()):
            if (
                user_id in self._room_by_user
                or user_id in self._spectator_by_user
            ):
                continue
            sends.append((conn, message))
        await self._send_all(sends)

    # ------------------------------------------------------------------
    # 匹配队列辅助
    # ------------------------------------------------------------------
    def _queue_state_payload(self, user_id: str) -> dict:
        return self._matchmaker.state_payload(user_id)

    async def _send_queue_status(self, conn: ClientConnection) -> None:
        await conn.send(
            make_message(
                SERVER_QUEUE_STATUS,
                self._queue_state_payload(conn.user_id),
            )
        )

    def _match_found_payload(
        self, room: Room, user_id: str, opponent_id: Optional[str]
    ) -> dict:
        return payloads.match_found_payload(room, user_id, opponent_id)

    def _prune_queue_locked(self) -> None:
        self._matchmaker.prune_locked()

    async def _fetch_user_rating(self, user_id: str) -> Optional[int]:
        async with self._session_factory() as session:
            return await session.scalar(
                select(User.rating).where(
                    User.id == user_id, User.is_active.is_(True)
                )
            )

    def _room_state_payload(self, room: Room, user_id: str) -> dict:
        return payloads.room_state_payload(
            room,
            user_id,
            settings=self._settings,
            remaining_ms=self._clocks.snapshot_remaining_locked,
            now_ms=_now_ms,
        )

    def _game_state_message(
        self,
        room: Room,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> Optional[dict]:
        """构造某条连接的权威局面消息；不属于该房间时返回 None。"""
        return payloads.game_state_message(
            room,
            conn,
            settings=self._settings,
            remaining_ms=self._clocks.snapshot_remaining_locked,
            now_ms=_now_ms,
            request_id=request_id,
        )

    async def _send_game_state(
        self,
        room: Room,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        message = self._game_state_message(room, conn, request_id)
        if message is None:
            return
        await conn.send(message)

    async def _broadcast_game_state(
        self,
        room: Room,
        request_user_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> None:
        sends: list[tuple[ClientConnection, dict]] = []
        for user_id, conn in list(room.connections.items()):
            message = self._game_state_message(
                room,
                conn,
                request_id if user_id == request_user_id else None,
            )
            if message is not None:
                sends.append((conn, message))
        # 观战者也接收同一份权威快照（观战没有待回应的落子请求，
        # 所以不携带 request_id）。
        for conn in list(room.spectators.values()):
            message = self._game_state_message(room, conn)
            if message is not None:
                sends.append((conn, message))
        await self._send_all(sends)

    async def _send_rematch_status(self, room: Room) -> None:
        await self._send_all(
            [
                (conn, payloads.rematch_status_message(room, user_id))
                for user_id, conn in list(room.connections.items())
            ]
        )

    def _undo_status_message(
        self,
        room: Room,
        state: str,
        requester_id: Optional[str],
        reason: str = "",
    ) -> dict:
        return payloads.undo_status_message(
            room, state, requester_id, reason
        )

    async def _broadcast_undo_status(
        self,
        room: Room,
        state: str,
        requester_id: Optional[str],
        reason: str = "",
    ) -> None:
        message = self._undo_status_message(
            room, state, requester_id, reason
        )
        await self._send_all(
            [(conn, message) for conn in room.connections.values()]
        )

    # ------------------------------------------------------------------
    # 连接生命周期
    # ------------------------------------------------------------------
    def _friend_cache_get(self, user_id: str) -> frozenset[str]:
        """读取好友缓存并刷新 LRU 顺序。"""
        return self._connections_registry.friend_cache_get(user_id)

    def _friend_cache_put(
        self, user_id: str, friend_ids: frozenset[str]
    ) -> None:
        """写入好友缓存；超过容量上限就淘汰最久未使用的一条。"""
        self._connections_registry.friend_cache_put(user_id, friend_ids)

    async def _load_friend_ids(self, user_id: str) -> list[str]:
        """查询已接受好友的 id 列表并刷新缓存；数据库异常时返回空表。"""
        return await self._connections_registry.load_friend_ids(user_id)

    async def refresh_friend_cache(self, user_id: str) -> None:
        """好友关系变化后刷新缓存（供 REST 接口调用）。"""
        await self._connections_registry.refresh_friend_cache(user_id)

    async def _notify_friends_presence(
        self, conn: ClientConnection, online: bool
    ) -> None:
        """向在线好友推送上下线事件。"""
        await self._connections_registry.notify_friends_presence(
            conn, online
        )

    async def attach(self, conn: ClientConnection) -> None:
        """认证后的连接注册；若用户在房间中则重连并发送权威快照。"""
        old_conn: Optional[ClientConnection] = None
        queued: Optional[QueuedPlayer] = None
        async with self._state_lock:
            old_conn = self._connections.get(conn.user_id)
            if old_conn is not None and old_conn is not conn:
                self._release_connection_locked(old_conn.connection_id)
            self._connections[conn.user_id] = conn
            queued = self._matchmaker.get(conn.user_id)
            if queued is not None:
                self._matchmaker.rebind_connection(conn)
            watched = self._spectator_by_user.get(conn.user_id)
        if old_conn is not None and old_conn is not conn:
            await old_conn.close(4001, "账号已在另一处连接")

        try:
            room = self._room_by_user.get(conn.user_id)
            if room is None:
                if watched is not None:
                    # 观战者重连：重新挂上新连接并补发一份快照，
                    # 否则棋盘会是空白的。
                    async with watched.lock:
                        was_watching = conn.user_id not in watched.spectators
                        watched.spectators[conn.user_id] = conn
                        if was_watching:
                            self._metrics.reconnects.inc()
                            logger.info(
                                "spectator_reconnected",
                                extra={
                                    "event": "spectator_reconnected",
                                    "game_id": watched.game_id,
                                },
                            )
                        await self._send_game_state(watched, conn)
                if queued is not None:
                    await self._send_queue_status(conn)
                return

            async with room.lock:
                if conn.user_id not in room.participant_ids():
                    return
                is_reconnect = conn.user_id not in room.connections
                room.connections[conn.user_id] = conn
                self._cancel_escape_timer_locked(room, conn.user_id)
                if is_reconnect:
                    self._metrics.reconnects.inc()
                    logger.info(
                        "ws_reconnected",
                        extra={
                            "event": "ws_reconnected",
                            "game_id": room.game_id,
                        },
                    )
                if room.status == "waiting":
                    if conn.user_id == room.black_user_id:
                        await self._send_room_created(conn, room)
                else:
                    await self._send_game_state(room, conn)
                    if room.undo_requester_id:
                        # 重连后恢复待回应的悔棋请求。
                        await conn.send(
                            self._undo_status_message(
                                room,
                                "requested",
                                room.undo_requester_id,
                            )
                        )
                    opponent = room.opponent_id(conn.user_id)
                    if opponent:
                        await self._send_opponent_status(
                            room, opponent, True, conn.username
                        )
        finally:
            await self._notify_friends_presence(conn, True)

    async def detach(self, conn: ClientConnection) -> None:
        """连接断开；旧连接被新连接替换时不做任何房间变更。"""
        async with self._state_lock:
            self._release_connection_locked(conn.connection_id)
            if self._connections.get(conn.user_id) is not conn:
                return
            self._connections.pop(conn.user_id, None)
            self._matchmaker.remove(conn.user_id)
            # 观战者断线只是停止接收快照：不能触发逃跑计时、
            # 不能判作弃局，更不能把房间关掉。
            # 观战登记本身保留，重连时 attach 才能把棋盘补回来
            # （和玩家断线后仍保留 _room_by_user 是同一个道理）。
            watched = self._spectator_by_user.get(conn.user_id)
            if watched is not None:
                watched.spectators.pop(conn.user_id, None)

        await self._notify_friends_presence(conn, False)

        room = self._room_by_user.get(conn.user_id)
        if room is None:
            return

        cleanup = False
        room_closed_reason = ""
        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            room.connections.pop(conn.user_id, None)
            room.rematch_votes.discard(conn.user_id)

            if room.status == "waiting":
                # 等待中的房主断线：房间没有对手，直接作废。
                cleanup = True
                room_closed_reason = "房主已离开，房间已关闭"
                await self._cancel_room_invites(room, "房主已离开")
                await self._mark_waiting_game_abandoned(room, "host_disconnected")
            else:
                if room.undo_requester_id:
                    requester_id = room.undo_requester_id
                    self._clear_undo_locked(room)
                    room.state_version += 1
                    if room.status == "playing":
                        self._start_timeout_locked(room)
                    await self._broadcast_undo_status(
                        room,
                        "cancelled",
                        requester_id,
                        (
                            "请求方已离线，悔棋请求已取消"
                            if requester_id == conn.user_id
                            else "对方已离线，悔棋请求已取消"
                        ),
                    )
                    await self._broadcast_game_state(room)
                if room.status == "playing":
                    # 掉线不立刻判负；宽限期内重连即可继续对局。
                    self._start_escape_timer_locked(room, conn.user_id)
                opponent = room.opponent_id(conn.user_id)
                if opponent and opponent in room.connections:
                    await self._send_opponent_status(
                        room, opponent, False, conn.username
                    )
                if room.status != "playing" and not room.connections:
                    cleanup = True

        if cleanup:
            if room_closed_reason:
                await self._send_room_closed(room, room_closed_reason)
            self._remove_room_locked(room)

    # ------------------------------------------------------------------
    # 房间创建/加入/离开
    # ------------------------------------------------------------------
    def _new_room_code(self) -> str:
        for _ in range(50):
            code = "".join(
                secrets.choice(_ROOM_ALPHABET)
                for _ in range(self._settings.room_code_length)
            )
            if code not in self._rooms_by_code:
                return code
        raise RuntimeError("无法生成唯一房间码")

    async def _create_game_row(
        self,
        room_code: str,
        black_id: str,
        white_id: Optional[str],
        status: str,
        started: bool,
        rated: bool = False,
        time_control: Optional[TimeControl] = None,
        turn_seconds: Optional[int] = None,
        rule_mode: RuleMode = RuleMode.FREESTYLE,
    ) -> Game:
        control = time_control or TimeControl.per_move(
            turn_seconds or self._settings.turn_seconds
        )
        game_row = Game(
            id=new_id(),
            room_code=room_code,
            black_user_id=black_id,
            white_user_id=white_id,
            status=status,
            turn_seconds=(
                control.turn_seconds
                if not control.is_fischer
                else control.main_seconds
            ),
            time_mode=control.mode,
            main_seconds=control.main_seconds,
            increment_seconds=control.increment_seconds,
            rated=rated,
            rule_mode=rule_mode.value,
            started_at=utcnow_naive() if started else None,
        )
        async with self._session_factory() as session:
            session.add(game_row)
            await session.commit()
        return game_row

    async def create_room(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
        allow_spectators: bool = True,
        turn_seconds: Optional[int] = None,
        time_control: Optional[TimeControl] = None,
        rule_mode: RuleMode | str = RuleMode.FREESTYLE,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        # 兼容直接调用管理器的内部入口：接受 RuleMode 或字符串，非法值
        # 一律拒绝，不能悄悄退回自由规则造成规则分歧。
        try:
            mode = parse_rule_mode(rule_mode)
        except ValueError:
            await self._send_error(conn, "规则模式非法", request_id)
            return
        if turn_seconds is not None and (
            not isinstance(turn_seconds, int)
            or isinstance(turn_seconds, bool)
            or not 5 <= turn_seconds <= 600
        ):
            await self._send_error(
                conn, "每手时间必须在 5～600 秒", request_id
            )
            return
        control = self._requested_time_control(turn_seconds, time_control)
        room: Optional[Room] = None
        error: Optional[str] = None
        code = ""

        # 第一阶段：锁内只做内存校验并预留房间码。数据库写入放到锁外，
        # 否则一次慢查询会把所有用户的建房/匹配都堵在全局锁上。
        async with self._state_lock:
            self._matchmaker.remove(conn.user_id)
            if conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            else:
                code = self._new_room_code()

        if error is None:
            # 第二阶段：锁外写库。
            try:
                game_row = await self._create_game_row(
                    code,
                    conn.user_id,
                    None,
                    "waiting",
                    False,
                    time_control=control,
                    rule_mode=mode,
                )
            except Exception:
                logger.exception("创建房间数据库写入失败")
                error = "服务器无法创建房间"
            else:
                # 第三阶段：重新拿锁并二次校验——等数据库的这段时间里，
                # 用户可能已经进了别的房间，这时不能把房间登记到他名下。
                async with self._state_lock:
                    if conn.user_id in self._room_by_user:
                        error = "你已经在一个房间中"
                    else:
                        room = Room(
                            code=code,
                            game_id=game_row.id,
                            black_user_id=conn.user_id,
                            white_user_id=None,
                            black_name=conn.username,
                            white_name="",
                            time_control=control,
                            black_avatar=conn.avatar,
                            black_avatar_version=conn.avatar_version,
                            black_achievements=conn.achievements,
                            connections={conn.user_id: conn},
                            allow_spectators=allow_spectators,
                            rule_mode=mode,
                            game=GameModel(mode),
                        )
                        self._rooms_by_code[code] = room
                        self._room_by_user[conn.user_id] = room
                if error is not None:
                    # 房间没能用上：把刚写入的对局行作废，别留下"等待中"
                    # 的孤儿记录。
                    await self._abandon_game_row(game_row.id, "left")

        if error:
            await self._send_error(conn, error, request_id)
        elif room is not None:
            await self._send_room_created(conn, room)

    async def _join_waiting_room(
        self,
        conn: ClientConnection,
        room: Room,
    ) -> Optional[str]:
        """把连接加入等待中的普通房间；返回错误文本或 None。

        调用方负责在锁外广播新的 game_state，并且负责把玩家登记进
        ``_room_by_user``（那张表由全局锁保护）。

        数据库写入在房间锁之外做：写库期间的延迟不该卡住这一局的超时
        判定。抢白座用条件更新——房间必须仍是"等待中且白座空着"才
        写得进去，两个人同时抢也只会成功一个。
        """
        # 第一阶段：房间锁内只做校验，不碰数据库。
        error: Optional[str] = None
        host_offline = False
        async with room.lock:
            if room.matchmade:
                error = "排位对局不能通过房间码加入"
            elif room.status != "waiting" or room.white_user_id is not None:
                error = "房间已经开始或已满"
            elif room.black_user_id == conn.user_id:
                error = "不能加入自己创建的房间"
            elif room.black_user_id not in room.connections:
                error = "房主已离线，房间已关闭"
                host_offline = True
        if host_offline:
            async with self._state_lock:
                self._remove_room_locked(room)
            await self._mark_waiting_game_abandoned(room, "host_offline")
            return error
        if error is not None:
            return error

        # 第二阶段：锁外写库，条件更新抢白座。
        try:
            async with self._session_factory() as session:
                result = await session.execute(
                    update(Game)
                    .where(
                        Game.id == room.game_id,
                        Game.status == "waiting",
                        Game.white_user_id.is_(None),
                    )
                    .values(
                        white_user_id=conn.user_id,
                        status="playing",
                        started_at=utcnow_naive(),
                    )
                )
                if rowcount_of(result) == 0:
                    # 白座已经被别人抢走（或者房间已经开打）。
                    await session.rollback()
                    return "房间已经开始或已满"
                await session.commit()
        except Exception:
            logger.exception("加入房间数据库写入失败")
            return "服务器无法开始对局"

        # 第三阶段：回到房间锁二次校验座位，再落内存状态。
        async with room.lock:
            if room.status != "waiting" or room.white_user_id is not None:
                seat_taken = True
            else:
                seat_taken = False
                room.white_user_id = conn.user_id
                room.white_name = conn.username
                room.white_avatar = conn.avatar
                room.white_avatar_version = conn.avatar_version
                room.white_achievements = conn.achievements
                room.status = "playing"
                room.connections[conn.user_id] = conn
                self._reset_clocks_locked(room)
                self._start_timeout_locked(room)
        if seat_taken:
            # 写库期间房间被别人接手了：把刚才的写入撤回。
            await self._revert_join(room, conn.user_id)
            return "房间已经开始或已满"
        return None

    async def _revert_join(self, room: Room, user_id: str) -> None:
        """撤销一次已经写库的加入：白座、状态和数据库行一起改回去。"""
        async with room.lock:
            if room.white_user_id != user_id:
                return
            room.white_user_id = None
            room.white_name = ""
            room.white_avatar = DEFAULT_AVATAR
            room.white_avatar_version = 0
            room.white_achievements = ()
            room.status = "waiting"
            room.connections.pop(user_id, None)
            self._cancel_timeout_locked(room)
            self._reset_clocks_locked(room)
        try:
            async with self._session_factory() as session:
                await session.execute(
                    update(Game)
                    .where(
                        Game.id == room.game_id,
                        Game.white_user_id == user_id,
                    )
                    .values(
                        white_user_id=None,
                        status="waiting",
                        started_at=None,
                    )
                )
                await session.commit()
        except Exception:
            logger.exception("撤销加入房间数据库写入失败")

    async def join_room(
        self,
        conn: ClientConnection,
        room_code: str,
        request_id: Optional[str] = None,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        code = room_code.strip().upper()
        error: Optional[str] = None
        target: Optional[Room] = None

        # 第一阶段：锁内校验并找到目标房间，数据库写入放到锁外。
        async with self._state_lock:
            self._matchmaker.remove(conn.user_id)
            if conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            else:
                room = self._rooms_by_code.get(code)
                if room is None:
                    error = "房间不存在或已关闭"
                else:
                    target = room

        if error is not None:
            await self._send_error(conn, error, request_id)
            return
        joined_room = cast(Room, target)

        # 第二阶段：锁外写库并占下白座。
        error = await self._join_waiting_room(conn, joined_room)
        if error is not None:
            await self._send_error(conn, error, request_id)
            return

        # 第三阶段：重新拿锁，二次校验后登记全局索引。
        async with self._state_lock:
            if conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            else:
                self._room_by_user[conn.user_id] = joined_room
        if error is not None:
            await self._revert_join(joined_room, conn.user_id)
            await self._send_error(conn, error, request_id)
            return

        async with joined_room.lock:
            await self._broadcast_game_state(joined_room)
        self._record_game_started(joined_room)
        # 对局开始，进行中的对局列表多了一条。
        await self._notify_live_games_changed()

    # ------------------------------------------------------------------
    # 自建房邀请
    # ------------------------------------------------------------------
    def _invite_payload(self, invite: RoomInvite) -> dict:
        return payloads.invite_payload(
            invite, self._connections.get(invite.inviter_id)
        )

    @staticmethod
    def _invite_result_payload(
        invite: RoomInvite,
        status: str,
        accepted: Optional[bool],
        user: dict,
        reason: str = "",
    ) -> dict:
        return payloads.invite_result_payload(
            invite, status, accepted, user, reason
        )

    async def _are_friends(self, user_a: str, user_b: str) -> bool:
        low, high = (
            (user_a, user_b) if user_a <= user_b else (user_b, user_a)
        )
        try:
            async with self._session_factory() as session:
                status = await session.scalar(
                    select(Friendship.status).where(
                        Friendship.user_a == low,
                        Friendship.user_b == high,
                    )
                )
        except Exception:
            logger.exception("查询好友关系失败")
            return False
        return status == "accepted"

    def _purge_expired_invites(self, except_id: Optional[str] = None) -> int:
        """清理过期且无人回应的邀请，返回清理数量。

        邀请的 TTL 是 60 秒，但过期之后没有任何地方真正把它删掉：没人
        回应的邀请会一直留在 ``_invites`` 里，房间开得越多这张表就越
        大。这里做惰性清理——每次碰到邀请表时顺手扫一遍，不必再起一个
        后台任务。``except_id`` 用来保护正在处理的那条邀请，避免把
        “已过期”的具体原因换成笼统的“已失效”。
        """
        now = time.monotonic()
        expired = [
            invite_id
            for invite_id, invite in self._invites.items()
            if invite_id != except_id
            and now - invite.created_at > _INVITE_TTL_SECONDS
        ]
        for invite_id in expired:
            self._invites.pop(invite_id, None)
        return len(expired)

    async def invite_to_room(
        self,
        conn: ClientConnection,
        invitee_id: str,
        request_id: Optional[str] = None,
    ) -> None:
        """房主邀请一位在线好友加入自己的等待房间。"""
        if not self._is_current_connection(conn):
            return
        if not isinstance(invitee_id, str) or not invitee_id:
            await self._send_error(conn, "邀请对象非法", request_id)
            return
        if invitee_id == conn.user_id:
            await self._send_error(conn, "不能邀请自己", request_id)
            return

        room = self._room_by_user.get(conn.user_id)
        if (
            room is None
            or room.black_user_id != conn.user_id
            or room.status != "waiting"
            or room.white_user_id is not None
        ):
            await self._send_error(conn, "请先创建自建房", request_id)
            return

        target = self._connections.get(invitee_id)
        if target is None or not target.connected:
            await self._send_error(conn, "好友不在线", request_id)
            return
        if not await self._are_friends(conn.user_id, invitee_id):
            await self._send_error(conn, "只能邀请好友", request_id)
            return

        # 发新邀请前先清掉过期邀请，邀请表不会随着房间数量无限增长。
        self._purge_expired_invites()
        for existing in self._invites.values():
            if (
                existing.room is room
                and existing.invitee_id == invitee_id
            ):
                await self._send_error(conn, "已向该好友发送邀请", request_id)
                return

        invite = RoomInvite(
            invite_id=new_id(),
            room=room,
            inviter_id=conn.user_id,
            invitee_id=invitee_id,
            created_at=time.monotonic(),
        )
        self._invites[invite.invite_id] = invite
        sent = await target.send(
            make_message(SERVER_ROOM_INVITATION, self._invite_payload(invite))
        )
        if not sent:
            self._invites.pop(invite.invite_id, None)
            await self._send_error(conn, "好友不在线", request_id)
            return

        await conn.send(
            make_message(
                SERVER_ROOM_INVITE_RESULT,
                self._invite_result_payload(
                    invite,
                    "sent",
                    None,
                    self._connection_user_payload(target),
                ),
            )
        )

    async def _cancel_room_invites(self, room: Room, reason: str) -> None:
        """房间关闭/已满时通知其余被邀请者。"""
        invites = [
            invite
            for invite in self._invites.values()
            if invite.room is room
        ]
        for invite in invites:
            self._invites.pop(invite.invite_id, None)
            target = self._connections.get(invite.invitee_id)
            if target is None:
                continue
            inviter = self._connections.get(invite.inviter_id)
            user = payloads.invite_user_payload(
                invite.inviter_id, inviter
            )
            await target.send(
                make_message(
                    SERVER_ROOM_INVITE_RESULT,
                    self._invite_result_payload(
                        invite, "cancelled", False, user, reason
                    ),
                )
            )

    async def respond_room_invite(
        self,
        conn: ClientConnection,
        invite_id: str,
        accept: bool,
        request_id: Optional[str] = None,
    ) -> None:
        """被邀请者接受/拒绝自建房邀请。"""
        if not self._is_current_connection(conn):
            return
        invite = (
            self._invites.get(invite_id)
            if isinstance(invite_id, str)
            else None
        )
        if invite is None:
            # 顺手清掉过期邀请：失效的邀请不应该一直占着位置。
            self._purge_expired_invites()
            await self._send_error(conn, "邀请已失效", request_id)
            return
        if invite.invitee_id != conn.user_id:
            await self._send_error(conn, "无权处理该邀请", request_id)
            return
        if time.monotonic() - invite.created_at > _INVITE_TTL_SECONDS:
            self._invites.pop(invite.invite_id, None)
            await self._send_error(conn, "邀请已过期", request_id)
            return
        # 处理这条邀请的同时清掉其它过期邀请。
        self._purge_expired_invites(except_id=invite.invite_id)
        # 邀请发出后、对方回应前，好友关系可能已经被解除（删好友）。这里
        # 重新校验一次，否则“不是好友也能被拉进自建房”，绕过了发邀请时
        # 的好友限制。
        if not await self._are_friends(invite.inviter_id, conn.user_id):
            self._invites.pop(invite.invite_id, None)
            await self._send_error(conn, "你们已不是好友，无法接受邀请", request_id)
            return

        self._invites.pop(invite.invite_id, None)
        host = self._connections.get(invite.inviter_id)
        invitee_user = self._connection_user_payload(conn)

        if not accept:
            if host is not None:
                await host.send(
                    make_message(
                        SERVER_ROOM_INVITE_RESULT,
                        self._invite_result_payload(
                            invite,
                            "declined",
                            False,
                            invitee_user,
                        ),
                    )
                )
            return

        room = invite.room
        error: Optional[str] = None
        joined_room: Optional[Room] = None
        host_offline = False
        # 第一阶段：锁内只做内存校验；数据库写入放到锁外，避免一次慢
        # 查询把所有人的建房/匹配都堵在全局锁上。
        async with self._state_lock:
            if conn.user_id in self._room_by_user:
                error = "你已经在房间中"
            elif room.status != "waiting" or room.white_user_id is not None:
                error = "房间已满或已关闭"
            elif room.black_user_id not in room.connections:
                error = "房主已离开"
                self._remove_room_locked(room)
                host_offline = True
        if host_offline:
            await self._mark_waiting_game_abandoned(room, "host_offline")
        if error is None:
            # 第二阶段：锁外抢白座。
            error = await self._join_waiting_room(conn, room)
        if error is None:
            # 第三阶段：重新拿锁，二次校验后登记全局索引。
            async with self._state_lock:
                if conn.user_id in self._room_by_user:
                    error = "你已经在房间中"
                else:
                    self._room_by_user[conn.user_id] = room
                    joined_room = room
            if error is not None:
                await self._revert_join(room, conn.user_id)

        if error:
            await self._send_error(conn, error, request_id)
            if host is not None:
                await host.send(
                    make_message(
                        SERVER_ROOM_INVITE_RESULT,
                        self._invite_result_payload(
                            invite, "declined", False, invitee_user
                        ),
                    )
                )
            await self._cancel_room_invites(room, "邀请已失效")
            return

        if host is not None:
            await host.send(
                make_message(
                    SERVER_ROOM_INVITE_RESULT,
                    self._invite_result_payload(
                        invite, "accepted", True, invitee_user
                    ),
                )
            )
        await self._cancel_room_invites(room, "房间已满")
        if joined_room is not None:
            async with joined_room.lock:
                await self._broadcast_game_state(joined_room)
            self._record_game_started(joined_room)
            # 对局开始，进行中的对局列表多了一条。
            await self._notify_live_games_changed()

    # ------------------------------------------------------------------
    # 公开排位匹配
    # ------------------------------------------------------------------
    async def quick_match(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        """加入排位队列；排位赛固定每手 30 秒，不接受自定义时限。

        客户端仍可能带着旧的 ``turn_seconds``/``time_control`` 字段：
        WebSocket 端点会先校验它们的格式（畸形协议直接报错），但实际
        对局一律使用固定的 ``DEFAULT_TIME_CONTROL``。
        """
        if not self._is_current_connection(conn):
            return
        # 排位固定每手 30 秒：刻意忽略客户端的自定义选择。
        control = DEFAULT_TIME_CONTROL
        if conn.user_id in self._room_by_user:
            await self._send_error(conn, "你已经在房间中", request_id)
            return
        # 匹配前推进赛季，避免跨赛季时仍在旧积分池里配对。
        await self.current_season()

        # 第一阶段：锁内只做内存校验并记录是否已在队列；数据库查询在锁外。
        async with self._state_lock:
            self._matchmaker.prune_locked()
            queued = self._matchmaker.get(conn.user_id)
            if queued is not None:
                self._matchmaker.refresh(conn, control)

        rating: Optional[int] = (
            queued.rating if queued is not None else None
        )
        if queued is None:
            # 第二阶段：锁外读取积分。慢查询不该占着全局锁，把其他用户
            # 的建房/入队也一起堵住。
            rating = await self._fetch_user_rating(conn.user_id)
            if rating is None:
                await self._send_error(
                    conn, "账号不存在或已停用", request_id
                )
                return

        room: Optional[Room] = None
        outgoing: list[tuple[ClientConnection, dict]] = []
        error: Optional[str] = None
        pair: Optional[tuple[QueuedPlayer, QueuedPlayer]] = None
        code = ""

        async with self._state_lock:
            self._matchmaker.prune_locked()
            if conn.user_id in self._room_by_user:
                # 已经在房间里的玩家不能继续留在匹配队列中。
                self._matchmaker.remove(conn.user_id)
                error = "你已经在房间中"
            else:
                current = self._matchmaker.get(conn.user_id)
                if current is None:
                    self._matchmaker.put(
                        QueuedPlayer(
                            conn=conn,
                            rating=cast(int, rating),
                            queued_at=time.monotonic(),
                            time_control=control,
                        )
                    )
                else:
                    self._matchmaker.refresh(conn, control)
                pair = self._matchmaker.pop_match_locked(conn)
                if pair is None:
                    outgoing.append(
                        (
                            conn,
                            make_message(
                                SERVER_QUEUE_STATUS,
                                self._queue_state_payload(conn.user_id),
                            ),
                        )
                    )
                else:
                    # 房间码提前在锁内生成，避免两个并发匹配拿到同一个码。
                    code = self._new_room_code()

        if error is not None:
            await self._send_error(conn, error, request_id)
            return

        if pair is not None:
            black, white = pair
            # 第三阶段：锁外写库创建对局行。
            try:
                game_row = await self._create_game_row(
                    code,
                    black.conn.user_id,
                    white.conn.user_id,
                    "playing",
                    True,
                    rated=True,
                    time_control=black.time_control,
                    # 排位赛本迭代固定自由规则，不接受连珠。
                    rule_mode=RuleMode.FREESTYLE,
                )
            except Exception:
                logger.exception("创建排位对局数据库写入失败")
                await self._matchmaker.requeue_opponent_after_failure(
                    conn, pair
                )
                await self._send_error(
                    conn,
                    "服务器无法创建排位对局，请稍后重试",
                    request_id,
                )
                return

            # 第四阶段：重新拿锁，二次校验后登记房间。写库期间两人可能
            # 已经进了别的房间（例如并发建房），这时不能覆盖已有归属。
            conflict = False
            async with self._state_lock:
                if (
                    black.conn.user_id in self._room_by_user
                    or white.conn.user_id in self._room_by_user
                ):
                    conflict = True
                else:
                    # 写库期间他们可能重新入队：以本次配对为准清掉残留项。
                    self._matchmaker.remove(black.conn.user_id)
                    self._matchmaker.remove(white.conn.user_id)
                    room = Room(
                        code=code,
                        game_id=game_row.id,
                        black_user_id=black.conn.user_id,
                        white_user_id=white.conn.user_id,
                        black_name=black.conn.username,
                        white_name=white.conn.username,
                        time_control=black.time_control,
                        black_avatar=black.conn.avatar,
                        white_avatar=white.conn.avatar,
                        black_avatar_version=black.conn.avatar_version,
                        white_avatar_version=white.conn.avatar_version,
                        black_achievements=black.conn.achievements,
                        white_achievements=white.conn.achievements,
                        status="playing",
                        connections={
                            black.conn.user_id: black.conn,
                            white.conn.user_id: white.conn,
                        },
                        rated=True,
                        matchmade=True,
                        rating_before={
                            black.conn.user_id: black.rating,
                            white.conn.user_id: white.rating,
                        },
                        # 排位房固定自由规则。
                        rule_mode=RuleMode.FREESTYLE,
                        game=GameModel(RuleMode.FREESTYLE),
                    )
                    self._rooms_by_code[code] = room
                    self._room_by_user[black.conn.user_id] = room
                    self._room_by_user[white.conn.user_id] = room
                    self._record_game_started(room)
                    self._reset_clocks_locked(room)
                    self._start_timeout_locked(room)
                    outgoing = self._match_found_outgoing(room, pair)
            if conflict:
                # 刚写入的对局行没人用得上，作废掉，别留下"进行中"孤儿。
                await self._abandon_game_row(game_row.id, "left")
                await self._matchmaker.requeue_opponent_after_failure(
                    conn, pair
                )
                await self._send_error(
                    conn, "匹配状态已变化，请重试", request_id
                )
                return

        for target, message in outgoing:
            await target.send(message)
        if room is not None:
            await self._broadcast_game_state(room)
            # 排位匹配成功，进行中的对局列表多了一条。
            await self._notify_live_games_changed()

    def _match_found_outgoing(
        self,
        room: Room,
        pair: tuple[QueuedPlayer, QueuedPlayer],
    ) -> list[tuple[ClientConnection, dict]]:
        """构造配对成功时要发给双方的消息（队列状态 + 匹配结果）。"""
        black, white = pair
        outgoing: list[tuple[ClientConnection, dict]] = []
        for player, rival in ((black, white), (white, black)):
            outgoing.append(
                (
                    player.conn,
                    make_message(
                        SERVER_QUEUE_STATUS,
                        self._queue_state_payload(player.conn.user_id),
                    ),
                )
            )
            outgoing.append(
                (
                    player.conn,
                    make_message(
                        SERVER_MATCH_FOUND,
                        self._match_found_payload(
                            room,
                            player.conn.user_id,
                            rival.conn.user_id,
                        ),
                    ),
                )
            )
        return outgoing

    async def cancel_match(
        self, conn: ClientConnection, request_id: Optional[str] = None
    ) -> None:
        """退出匹配队列；不在队列中时也返回 idle，供客户端恢复状态。"""
        if not self._is_current_connection(conn):
            return
        async with self._state_lock:
            self._matchmaker.remove(conn.user_id)
            payload = self._queue_state_payload(conn.user_id)
        await conn.send(make_message(SERVER_QUEUE_STATUS, payload))

    async def leave_room(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        """等待中或终局后可离开；进行中的对局请使用认输。"""
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._leave_spectating(conn)
            return

        error: Optional[str] = None
        message = make_message(SERVER_ROOM_CLOSED, {"reason": "对局房间已关闭"})
        async with room.lock:
            # 只有房间里登记的那条连接能离房：旧连接（已被重连替换）
            # 不能把房间关掉，否则对手和观战者会收到莫名其妙的
            # ROOM_CLOSED。检查放在关闭房间之前。
            if room.connections.get(conn.user_id) is not conn:
                return
            if room.status == "playing":
                error = "进行中的对局请先认输"
            else:
                if room.status == "waiting":
                    await self._cancel_room_invites(room, "房主已离开")
                    await self._mark_waiting_game_abandoned(room, "left")
                for other in list(room.connections.values()):
                    await other.send(message)
                # 观战者也要知道房间没了，否则会一直停在一局早已结束的棋盘上。
                for watcher in list(room.spectators.values()):
                    await watcher.send(message)
                self._remove_room_locked(room)

        if error:
            await self._send_error(conn, error, request_id)

    # ------------------------------------------------------------------
    # 观战
    # ------------------------------------------------------------------
    async def _leave_spectating(self, conn: ClientConnection) -> None:
        """观战者退出观战：只结束自己的观战，不影响对局双方。"""
        async with self._state_lock:
            watched = self._spectator_by_user.pop(conn.user_id, None)
            if watched is not None:
                watched.spectators.pop(conn.user_id, None)
        if watched is None:
            return
        # 只通知观战者本人；房间和玩家的状态一律不动。
        await conn.send(
            make_message(SERVER_ROOM_CLOSED, {"reason": "已退出观战"})
        )

    def _find_playing_room(self, game_id: str) -> Optional[Room]:
        """按对局编号找到正在进行中的房间。"""
        for room in self._rooms_by_code.values():
            if room.game_id == game_id and room.status == "playing":
                return room
        return None

    async def watch_game(
        self,
        conn: ClientConnection,
        game_id: str,
        request_id: Optional[str] = None,
    ) -> None:
        """观战：旁观者以对局编号进入别人正在进行的一局。

        观战者只接收权威快照，不能落子、认输、悔棋、再战、聊天或邀请；
        这些入口都不会把观战者当成房间成员。
        """
        if not self._is_current_connection(conn):
            return
        if not isinstance(game_id, str) or not game_id.strip():
            await self._send_error(conn, "对局编号无效", request_id)
            return

        error: Optional[str] = None
        target: Optional[Room] = None
        async with self._state_lock:
            # 观战和匹配不能同时存在，否则被匹配进新房间后身份会打架。
            self._matchmaker.remove(conn.user_id)
            target = self._find_playing_room(game_id)
            if target is None:
                error = "对局不存在或已结束"
            elif not target.allow_spectators:
                error = "该对局未开启观战"
            elif target.color_for(conn.user_id) is not None:
                # 自己下的棋不能观摩，只能专心下。
                error = "你不能观摩自己的对局"
            elif conn.user_id in self._room_by_user:
                error = "你已经在一个房间中"
            elif self._rooms_by_code.get(target.code) is not target:
                # 房间在查找或登记时被清理掉了。
                error = "对局不存在或已结束"
            elif (
                conn.user_id not in target.spectators
                and len(target.spectators)
                >= self._settings.max_spectators_per_room
            ):
                error = "观战人数已满"
            else:
                previous = self._spectator_by_user.get(conn.user_id)
                if previous is not None and previous is not target:
                    # 换局观战：先退出上一局，避免同时挂在两局上。
                    previous.spectators.pop(conn.user_id, None)
                    self._spectator_by_user.pop(conn.user_id, None)
                # 幂等：重复观战同一局只是重发一份快照。
                target.spectators[conn.user_id] = conn
                self._spectator_by_user[conn.user_id] = target

        if error is not None:
            await self._send_error(conn, error, request_id)
            return
        if target is not None:
            await self._send_game_state(target, conn)

    def list_live_games(self) -> list[dict]:
        """列出内存中正在进行中的对局，供观战入口挑选。"""
        entries = [
            {
                "game_id": room.game_id,
                "rated": room.rated,
                "black_username": room.black_name,
                "white_username": room.white_name,
                "black_user_id": room.black_user_id,
                "white_user_id": room.white_user_id or "",
                "move_no": room.game.move_count,
                "status": room.status,
                "rule_mode": room.rule_mode.value,
                # 观战人数：让广场上的人知道"这场有人看"，
                # 才可能进来看同观频道。
                "spectator_count": len(room.spectators),
            }
            for room in self._rooms_by_code.values()
            if room.status == "playing" and room.allow_spectators
        ]
        # 手数多的对局更“热”，排在前面；game_id 兜底保证顺序稳定。
        entries.sort(
            key=lambda entry: (
                -cast(int, entry["move_no"]),
                cast(str, entry["game_id"]),
            )
        )
        return entries

    def metrics_snapshot(self) -> dict[str, int]:
        """给 Prometheus collector 读取的即时房间/连接状态。"""
        rooms = list(self._rooms_by_code.values())
        return {
            "connections": len(self._connections),
            "rooms_active": len(rooms),
            "rooms_playing": sum(
                1 for room in rooms if room.status == "playing"
            ),
            "rooms_waiting": sum(
                1 for room in rooms if room.status == "waiting"
            ),
            "spectators": sum(
                len(room.spectators) for room in rooms
            ),
            "match_queue": len(self._matchmaker),
        }

    def _record_game_started(self, room: Room) -> None:
        self._metrics.games_started.inc()
        logger.info(
            "game_started",
            extra={
                "event": "game_started",
                "game_id": room.game_id,
                "rated": room.rated,
            },
        )

    def _record_game_finished(self, room: Room) -> None:
        reason = room.result_reason or "unknown"
        self._metrics.games_finished.labels(reason=reason).inc()
        logger.info(
            "game_ended",
            extra={
                "event": "game_ended",
                "game_id": room.game_id,
                "reason": reason,
                "move_no": room.game.move_count,
                "status": room.status,
            },
        )

    # ------------------------------------------------------------------
    # 落子/认输/再战
    # ------------------------------------------------------------------
    async def move(
        self,
        conn: ClientConnection,
        request_id: Optional[str],
        row: int,
        col: int,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        if not request_id or len(request_id) > 64:
            await self._send_error(conn, "落子请求缺少有效的 request_id")
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._send_error(conn, "你不在房间中", request_id)
            return

        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            # 幂等重发：同一 request_id 已经落子成功时，直接回当前权威
            # 状态，不重复落子。
            #
            # 这个分支必须排在所有业务校验（终局 / 超时补判 / 回合 /
            # 坐标 / 位置占用）之前：客户端丢了 ACK 后重发时，对手可能
            # 已经落子、或者对局已经结束，此时若先做业务校验，就会把
            # "已经生效的落子"误报成"该位置已有棋子/还没有轮到你/
            # 当前对局已结束"，客户端会以为自己的落子失败了。
            #
            # 也要排在超时补判之前：重发的是"已经走过的那一手"，玩家
            # 在当时就已经消耗了自己的时间；因为一次重发再判一次超时，
            # 等于让玩家为同一手付两次账。
            if request_id in room.processed_moves:
                await self._send_game_state(
                    room, conn, request_id=request_id
                )
                return
            if room.status != "playing" or room.game.is_game_over:
                await self._send_error(conn, "当前对局已结束", request_id)
                return
            move_received_ms = _now_ms()
            if (
                room.turn_deadline_ms is not None
                and move_received_ms >= room.turn_deadline_ms
            ):
                # 时钟已经耗尽但计时任务还没来得及执行：由服务器在落子
                # 路径上补判超时，防止“卡点落子”绕过超时判负。
                loser_id = self._current_clock_user_id(room)
                if loser_id is not None:
                    timeout_winner_id = room.opponent_id(loser_id)
                    if timeout_winner_id is not None:
                        await self._end_game_locked(
                            room,
                            timeout_winner_id,
                            self._timeout_reason_locked(room, loser_id),
                        )
                        return
            if room.undo_requester_id:
                await self._send_move_rejected(
                    conn, request_id, "请先处理悔棋请求"
                )
                return
            color = room.color_for(conn.user_id)
            if color is None:
                await self._send_error(conn, "你不是本局玩家", request_id)
                return
            if room.game.current_player is not color:
                await self._send_move_rejected(conn, request_id, "还没有轮到你")
                return
            if not in_bounds(row, col):
                await self._send_move_rejected(conn, request_id, "落子坐标非法")
                return
            if room.game.board[row][col] is not None:
                await self._send_move_rejected(conn, request_id, "该位置已有棋子")
                return

            # 连珠模式下黑棋的长连、四四、三三禁手在服务器侧直接拒绝，
            # 客户端无法通过伪造落子绕过；``place_stone`` 内的同一判定
            # 保留作最后防线。白棋与自由规则不受影响。
            if (
                room.rule_mode is RuleMode.RENJU
                and is_forbidden_move(
                    room.game.board, row, col, color, room.rule_mode
                )
            ):
                await self._send_move_rejected(conn, request_id, "连珠禁手")
                return

            if not room.game.place_stone(row, col):
                await self._send_move_rejected(conn, request_id, "落子不合法")
                return

            new_move_no = room.game.move_count
            status = payloads.status_for_game(room.game)
            winner_id: Optional[str] = None
            if room.game.winner is Player.BLACK:
                winner_id = room.black_user_id
            elif room.game.winner is Player.WHITE:
                winner_id = room.white_user_id
            result_reason: Optional[str] = None
            if status != "playing":
                result_reason = "five" if winner_id is not None else "draw"
            # 终局写入与排位结算必须原子提交：分成两次提交时，进程在
            # 中间被杀会留下"已终局但没结算"的对局，只有启动补结算能
            # 兜底。这里把落子、终局状态与结算放进同一个事务。
            settle_rating = (
                status != "playing"
                and room.rated
                and not room.rating_applied
            )
            settlement: Optional[SettlementResult] = None

            db_started = time.perf_counter()
            try:
                # 赛季 id 必须在事务外取：current_season() 自己会开一个
                # 会话，不能把会话嵌套进下面的事务里。这一步放进 try 是
                # 有意的：取赛季失败时也要撤销上面的内存落子，否则棋盘
                # 上会多出一颗数据库里并不存在的棋子。
                season_id = (
                    await self._settlement.settlement_season_id()
                    if settle_rating
                    else None
                )
                async with self._session_factory() as session:
                    game_row = await session.get(Game, room.game_id)
                    if game_row is None:
                        raise RuntimeError("game row missing")
                    session.add(
                        Move(
                            game_id=room.game_id,
                            move_no=new_move_no,
                            user_id=conn.user_id,
                            color=payloads.color_text(color),
                            row=row,
                            col=col,
                            client_move_id=request_id,
                        )
                    )
                    game_row.move_no = new_move_no
                    game_row.status = status
                    game_row.winner_user_id = winner_id
                    if status != "playing":
                        game_row.result_reason = result_reason
                        game_row.ended_at = utcnow_naive()
                        if settle_rating:
                            settlement = await self._settlement.settle_rated_game_in_session(
                                session,
                                game_row,
                                black_id=room.black_user_id,
                                white_id=room.white_user_id,
                                winner_user_id=winner_id,
                                status=status,
                                result_reason=result_reason or "",
                                move_no=new_move_no,
                                season_id=season_id,
                            )
                    # 任何一步失败都整体回滚：下面 except 会撤销内存
                    # 落子并让客户端重试。
                    await session.commit()
            except Exception:
                logger.exception("落子数据库写入失败，撤销内存落子")
                room.game.undo()
                await self._send_error(
                    conn, "服务器保存落子失败，请重试", request_id
                )
                return
            db_seconds = time.perf_counter() - db_started
            self._metrics.record_move(db_seconds)
            logger.debug(
                "move_confirmed",
                extra={
                    "event": "move_confirmed",
                    "game_id": room.game_id,
                    "move_no": new_move_no,
                    "duration_ms": round(db_seconds * 1000, 2),
                },
            )

            # 结算结果必须在广播前落到内存房间：快照里的积分与成就
            # 字段直接读 room，晚一步客户端就会收到"已终局但无积分"。
            if settlement is not None:
                self._settlement.apply_settlement(room, settlement)

            room.processed_moves[request_id] = new_move_no
            room.status = status
            room.winner_user_id = winner_id
            room.result_reason = None
            room.state_version += 1
            self._consume_clock_locked(
                room, conn.user_id, at_ms=move_received_ms
            )

            if status == "playing":
                self._start_timeout_locked(room)
            else:
                room.result_reason = result_reason
                room.turn_deadline_ms = None
                self._record_game_finished(room)

            await self._broadcast_game_state(
                room,
                request_user_id=conn.user_id,
                request_id=request_id,
            )
            if room.status != "playing" and not room.connections:
                self._remove_room_locked(room)

    async def _send_move_rejected(
        self,
        conn: ClientConnection,
        request_id: str,
        reason: str,
    ) -> None:
        await conn.send(
            make_message(
                SERVER_MOVE_REJECTED,
                {"reason": reason},
                request_id=request_id,
            )
        )

    async def resign(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            # 观战者没有执子资格，明确拒绝而不是静默忽略，
            # 否则客户端会把请求当成已受理。
            if conn.user_id in self._spectator_by_user:
                await self._send_error(conn, "观战者不能认输", request_id)
            return
        async with room.lock:
            # 只有房间里登记的那条连接能认输：重连替换过的旧连接
            # 不再代表玩家本人，不能替它终结对局。
            if room.connections.get(conn.user_id) is not conn:
                return
            if room.status != "playing":
                await self._send_error(
                    conn, "当前没有进行中的对局", request_id
                )
                return
            opponent = room.opponent_id(conn.user_id)
            if opponent is None:
                return
            await self._end_game_locked(room, opponent, "resign")

    async def undo_request(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        """请求悔棋：只能在自己回合发起，等待对手同意或拒绝。"""
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._send_error(conn, "你不在房间中", request_id)
            return

        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            if room.status != "playing" or room.game.is_game_over:
                await self._send_error(
                    conn, "当前没有进行中的对局", request_id
                )
                return
            color = room.color_for(conn.user_id)
            if color is None:
                await self._send_error(conn, "你不是本局玩家", request_id)
                return
            if room.undo_requester_id:
                await self._send_error(
                    conn, "已有待回应的悔棋请求", request_id
                )
                return
            if room.game.current_player is not color:
                await self._send_error(
                    conn, "只能在自己回合请求悔棋", request_id
                )
                return
            if not any(
                move.player is color for move in room.game.moves
            ):
                await self._send_error(conn, "还没有可悔的落子", request_id)
                return
            opponent = room.opponent_id(conn.user_id)
            if opponent is None or opponent not in room.connections:
                await self._send_error(
                    conn, "对手已离线，无法请求悔棋", request_id
                )
                return

            limit = self._settings.undo_limit_per_game
            if limit <= 0:
                await self._send_error(conn, "本局未开启悔棋", request_id)
                return
            used = room.undo_counts.get(conn.user_id, 0)
            if used >= limit:
                await self._send_error(
                    conn,
                    f"本局悔棋次数已用完（每方 {limit} 次）",
                    request_id,
                )
                return
            cooldown = self._settings.undo_cooldown_seconds
            now = time.time()
            last_request = room.undo_last_request.get(conn.user_id)
            if cooldown > 0 and last_request is not None:
                wait = last_request + cooldown - now
                if wait > 0:
                    await self._send_error(
                        conn,
                        f"悔棋请求过于频繁，请 {max(1, int(wait) + 1)} 秒后再试",
                        request_id,
                    )
                    return

            room.undo_counts[conn.user_id] = used + 1
            room.undo_last_request[conn.user_id] = now
            room.undo_requester_id = conn.user_id
            room.state_version += 1
            # 等待回应期间暂停本回合计时，避免对手故意拖延。
            self._pause_clock_locked(room)
            self._start_undo_expiry_locked(room)
            await self._broadcast_undo_status(
                room, "requested", conn.user_id
            )
            await self._broadcast_game_state(room)

    async def undo_response(
        self,
        conn: ClientConnection,
        accept: bool,
        request_id: Optional[str] = None,
    ) -> None:
        """回应对手的悔棋请求；同意后回滚到请求者上一手之前。"""
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._send_error(conn, "你不在房间中", request_id)
            return

        async with room.lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            requester_id = room.undo_requester_id
            if requester_id is None:
                await self._send_error(
                    conn, "没有待回应的悔棋请求", request_id
                )
                return
            if requester_id == conn.user_id:
                await self._send_error(
                    conn, "不能回应自己的悔棋请求", request_id
                )
                return
            if room.opponent_id(requester_id) != conn.user_id:
                await self._send_error(
                    conn, "你不是该悔棋请求的对手", request_id
                )
                return
            if room.status != "playing" or room.game.is_game_over:
                self._clear_undo_locked(room)
                room.state_version += 1
                await self._broadcast_undo_status(
                    room, "cancelled", requester_id, "对局已结束"
                )
                return

            requester_color = room.color_for(requester_id)
            if requester_color is None:
                self._clear_undo_locked(room)
                await self._send_error(
                    conn, "悔棋请求状态异常", request_id
                )
                return

            if not accept:
                self._clear_undo_locked(room)
                room.state_version += 1
                self._start_timeout_locked(room)
                await self._broadcast_undo_status(
                    room, "declined", requester_id, "对手拒绝了悔棋请求"
                )
                await self._broadcast_game_state(room)
                return

            # 目标手数 = 请求者最后一手之前的手数；其后的对手应手一并撤销。
            target_move_no = 0
            for index, move in enumerate(room.game.moves):
                if move.player is requester_color:
                    target_move_no = index

            try:
                async with self._session_factory() as session:
                    game_row = await session.get(Game, room.game_id)
                    if game_row is None:
                        raise RuntimeError("game row missing")
                    await session.execute(
                        delete(Move).where(
                            Move.game_id == room.game_id,
                            Move.move_no > target_move_no,
                        )
                    )
                    game_row.move_no = target_move_no
                    game_row.status = "playing"
                    game_row.winner_user_id = None
                    game_row.result_reason = None
                    game_row.ended_at = None
                    await session.commit()
            except Exception:
                logger.exception("悔棋数据库写入失败 room=%s", room.code)
                # 写库失败必须把待回应的悔棋请求收干净：否则
                # room.undo_requester_id 一直挂着、回合计时一直暂停，
                # 对手可以反复点“同意”，直到 30 秒过期任务自己跑完。
                # 这里照搬“拒绝”路径的收尾动作，再把状态广播给双方。
                self._clear_undo_locked(room)
                room.state_version += 1
                self._start_timeout_locked(room)
                await self._send_error(
                    conn, "服务器保存悔棋失败，请重试", request_id
                )
                await self._broadcast_undo_status(
                    room,
                    "cancelled",
                    requester_id,
                    "服务器保存悔棋失败，悔棋请求已取消",
                )
                await self._broadcast_game_state(room)
                return

            while room.game.move_count > target_move_no:
                room.game.undo()
            self._clear_undo_locked(room)
            room.status = "playing"
            room.winner_user_id = None
            room.result_reason = None
            room.processed_moves = {
                move_id: move_no
                for move_id, move_no in room.processed_moves.items()
                if move_no <= target_move_no
            }
            room.state_version += 1
            self._start_timeout_locked(room)
            logger.info(
                "undo_accepted",
                extra={
                    "event": "undo_accepted",
                    "game_id": room.game_id,
                    "move_no": target_move_no,
                },
            )
            await self._broadcast_undo_status(
                room, "accepted", requester_id, "对手已同意悔棋"
            )
            await self._broadcast_game_state(room)

    async def rematch(
        self,
        conn: ClientConnection,
        request_id: Optional[str] = None,
    ) -> None:
        if not self._is_current_connection(conn):
            return
        room = self._room_by_user.get(conn.user_id)
        if room is None:
            # 观战者不是本局玩家，不能发起再战。
            if conn.user_id in self._spectator_by_user:
                await self._send_error(conn, "观战者不能发起再战", request_id)
            return
        error: Optional[str] = None
        async with room.lock:
            if room.rated:
                error = "排位赛不支持再战，请重新匹配"
            elif room.status == "playing" or room.status == "waiting":
                error = "当前对局还没有结束"
            elif conn.user_id not in room.connections:
                error = "请先重新连接到房间"
            else:
                room.rematch_votes.add(conn.user_id)
                participants = set(room.participant_ids())
                if participants and participants.issubset(room.rematch_votes):
                    if not all(uid in room.connections for uid in participants):
                        await self._send_rematch_status(room)
                    else:
                        try:
                            # 能进入再战说明对局已结束且双方都在房间，
                            # 白方座位必然存在；cast 仅用于满足类型检查。
                            new_game = await self._create_game_row(
                                room.code,
                                cast(str, room.white_user_id),  # 交换黑白
                                room.black_user_id,
                                "playing",
                                True,
                                time_control=room.time_control,
                                # 再战沿用本房规则模式，不能退回默认值。
                                rule_mode=room.rule_mode,
                            )
                        except Exception:
                            logger.exception("再战新对局写入失败")
                            error = "服务器无法开始下一局"
                        else:
                            old_black = room.black_user_id
                            old_white = cast(str, room.white_user_id)
                            old_black_name = room.black_name
                            old_white_name = room.white_name
                            old_black_avatar = room.black_avatar
                            old_white_avatar = room.white_avatar
                            old_black_version = room.black_avatar_version
                            old_white_version = room.white_avatar_version
                            old_black_achievements = room.black_achievements
                            old_white_achievements = room.white_achievements
                            room.game_id = new_game.id
                            room.black_user_id = old_white
                            room.white_user_id = old_black
                            room.black_name = old_white_name
                            room.white_name = old_black_name
                            room.black_avatar = old_white_avatar
                            room.white_avatar = old_black_avatar
                            room.black_avatar_version = old_white_version
                            room.white_avatar_version = old_black_version
                            room.black_achievements = old_white_achievements
                            room.white_achievements = old_black_achievements
                            # 这是明确的新局重置：棋盘清空但仍沿用原规则
                            # 模式，不能把连珠房悄悄变回自由规则。
                            room.game = GameModel(room.rule_mode)
                            room.status = "playing"
                            room.winner_user_id = None
                            room.result_reason = None
                            room.new_achievements.clear()
                            room.rematch_votes.clear()
                            room.processed_moves.clear()
                            room.undo_counts.clear()
                            room.undo_last_request.clear()
                            self._clear_undo_locked(room)
                            self._cancel_all_escape_timers_locked(room)
                            room.state_version = 0
                            self._reset_clocks_locked(room)
                            self._start_timeout_locked(room)
                            await self._broadcast_game_state(room)
                            self._record_game_started(room)
                            # 再战开新局：列表里的对局编号换了一个。
                            await self._notify_live_games_changed()
                else:
                    await self._send_rematch_status(room)

        if error:
            await self._send_error(conn, error, request_id)

    # ------------------------------------------------------------------
    # 表情/评论
    # ------------------------------------------------------------------
    async def send_chat(
        self,
        conn: ClientConnection,
        kind: str,
        content: str,
        request_id: Optional[str] = None,
    ) -> None:
        """处理房间内的表情/评论：校验、限流后广播给房间内所有人。

        校验放在这里而不是网关层，保证任何调用方（未来的机器人或管理
        工具）都无法绕过白名单和长度限制；广播出去的也是规范化之后的
        文本，而不是客户端原样送上来的内容。
        """
        if not self._is_current_connection(conn):
            return

        room = self._room_by_user.get(conn.user_id)
        if room is None:
            await self._send_error(conn, "你不在房间中", request_id)
            return

        # 规范化在锁外完成，失败直接回错误，不占用任何房间锁。
        try:
            normalized = normalize_chat_content(kind, content)
        except ProtocolError as exc:
            await self._send_error(conn, str(exc), request_id)
            return

        # 只用 chat_lock 串行化发言顺序；绝不碰 room.lock，
        # 否则聊天的网络写会拖慢落子写库与超时判定，造成误判超时负。
        async with room.chat_lock:
            if room.connections.get(conn.user_id) is not conn:
                return
            if room.status == "waiting":
                await self._send_error(
                    conn, "对手尚未加入，暂时无法发言", request_id
                )
                return

            # 限流：CHAT_WINDOW_SECONDS 窗口内最多 CHAT_BURST 条。
            # 窗口比客户端的发言间隔宽一倍，网络抖动时也不会误判。
            now = time.monotonic()
            times = [
                stamp
                for stamp in room.chat_sent_times.get(conn.user_id, [])
                if now - stamp < CHAT_WINDOW_SECONDS
            ]
            if len(times) >= CHAT_BURST:
                await self._send_error(conn, "发言太快，请稍候", request_id)
                return
            times.append(now)
            room.chat_sent_times[conn.user_id] = times

            # 广播给房间内所有连接（含发送者本人，双方看到同样的弹幕）。
            message = payloads.chat_message(
                conn, kind, normalized, _now_ms()
            )
            await self._send_all(
                [(other, message) for other in room.connections.values()]
            )

    async def send_spectator_chat(
        self,
        conn: ClientConnection,
        kind: str,
        content: str,
        request_id: Optional[str] = None,
    ) -> None:
        """处理同观频道的一条发言：只发给同一房间的观战者。

        与玩家聊天刻意分开成一条独立的频道：观战席是观众自己的
        角落，棋手既收不到、也看不到这里的任何内容。

        频道不落库，房间一关就没了——它是"此刻"的陪伴而不是留言簿，
        所以这里既不建表也不写数据库。
        """
        if not self._is_current_connection(conn):
            return

        room = self._spectator_by_user.get(conn.user_id)
        # 连接可能已经换过（重连后旧连接不再有效），两种情况都视为
        # 不在观战席。
        if room is None or room.spectators.get(conn.user_id) is not conn:
            await self._send_error(conn, "你不在观战席中", request_id)
            return

        if room.rated:
            # 排位赛不开观战席交流：观战者能实时看到棋盘，场外传话
            # 会绕过现有的举报与回放审计，段位就不可信了。
            await self._send_error(
                conn, "排位对局不支持观战席聊天", request_id
            )
            return

        try:
            normalized = normalize_chat_content(kind, content)
        except ProtocolError as exc:
            await self._send_error(conn, str(exc), request_id)
            return

        # 与玩家聊天共用房间的发言锁：它只用来串行化广播顺序，
        # 两种频道各限各的流，互不消耗对方的额度。
        async with room.chat_lock:
            now = time.monotonic()
            times = [
                stamp
                for stamp in room.spectator_chat_sent_times.get(
                    conn.user_id, []
                )
                if now - stamp < CHAT_WINDOW_SECONDS
            ]
            if len(times) >= CHAT_BURST:
                await self._send_error(conn, "发言太快，请稍候", request_id)
                return
            times.append(now)
            room.spectator_chat_sent_times[conn.user_id] = times

            message = payloads.spectator_chat_message(
                conn, kind, normalized, _now_ms()
            )
            # 只发给观战者。棋手永远收不到这个频道的任何内容。
            await self._send_all(
                [(other, message) for other in room.spectators.values()]
            )

    # ------------------------------------------------------------------
    # 时钟（实现见 clock_service.ClockService）
    # ------------------------------------------------------------------
    def _start_undo_expiry_locked(self, room: Room) -> None:
        self._clocks.start_undo_expiry_locked(room)

    def _cancel_undo_timer_locked(self, room: Room) -> None:
        self._clocks.cancel_undo_timer_locked(room)

    def _clear_undo_locked(self, room: Room) -> None:
        self._clocks.clear_undo_locked(room)

    def _start_escape_timer_locked(
        self, room: Room, user_id: str
    ) -> None:
        self._clocks.start_escape_timer_locked(room, user_id)

    def _cancel_escape_timer_locked(
        self, room: Room, user_id: str
    ) -> None:
        self._clocks.cancel_escape_timer_locked(room, user_id)

    def _cancel_all_escape_timers_locked(self, room: Room) -> None:
        self._clocks.cancel_all_escape_timers_locked(room)

    def _current_clock_user_id(self, room: Room) -> Optional[str]:
        return self._clocks.current_clock_user_id(room)

    def _cancel_timeout_locked(self, room: Room) -> None:
        self._clocks.cancel_timeout_locked(room)

    def _reset_clocks_locked(self, room: Room) -> None:
        """新对局/再战开始时重置双方余量。"""
        self._clocks.reset_clocks_locked(room)

    def _pause_clock_locked(self, room: Room) -> None:
        """暂停计时（悔棋等待期间），把已用时间从当前余量中扣除。"""
        self._clocks.pause_clock_locked(room)

    def _consume_clock_locked(
        self, room: Room, user_id: str, at_ms: Optional[int] = None
    ) -> None:
        """落子成功后结算走子方本手耗时。"""
        self._clocks.consume_clock_locked(room, user_id, at_ms)

    def _snapshot_remaining_locked(
        self, room: Room, user_id: str
    ) -> Optional[int]:
        """快照里展示的余量；每手模式不使用双方余量字段。"""
        return self._clocks.snapshot_remaining_locked(room, user_id)

    def _start_timeout_locked(self, room: Room) -> None:
        self._clocks.start_timeout_locked(room)

    def _timeout_reason_locked(self, room: Room, loser_id: str) -> str:
        """超时判负的原因：掉线方按“逃跑”，在线方按普通“超时”。"""
        return self._clocks.timeout_reason_locked(room, loser_id)

    async def _end_game_locked(
        self,
        room: Room,
        winner_user_id: str,
        reason: str,
    ) -> None:
        """终局：终局状态与排位积分在同一个事务里提交。

        两件事必须原子：写成两次提交时，进程在中间被杀会留下
        "已终局但没结算"的对局（``rating_applied`` 仍为假），而服务
        启动时的作废扫描只看 waiting/playing，扫不到它——积分就永久
        丢了。写库失败时把内存状态回滚，不能让客户端看到"对局已结束"
        而数据库里这局还停在"进行中"。
        """
        color = room.color_for(winner_user_id)
        if color is None:
            return
        previous_status = room.status
        previous_winner = room.winner_user_id
        previous_reason = room.result_reason
        previous_deadline = room.turn_deadline_ms
        previous_started = room.clock_started_ms

        room.status = "black_won" if color is Player.BLACK else "white_won"
        room.winner_user_id = winner_user_id
        room.result_reason = reason
        room.turn_deadline_ms = None
        room.clock_started_ms = None
        room.rematch_votes.clear()
        room.state_version += 1
        self._clear_undo_locked(room)
        self._cancel_all_escape_timers_locked(room)
        self._cancel_timeout_locked(room)

        settlement: Optional[SettlementResult] = None
        try:
            # 赛季 id 必须在事务外取：current_season() 自己要开一个会话，
            # 不能把会话嵌套进调用方的事务里。放在 try 内是为了让查询
            # 自身失败时同样回滚上面的内存终局状态，而不是把房间卡在
            # “内存已终局、数据库仍在进行中”的矛盾状态。
            season_id = await self._settlement.settlement_season_id()
            async with self._session_factory() as session:
                game_row = await session.get(Game, room.game_id)
                if game_row is not None:
                    game_row.status = room.status
                    game_row.winner_user_id = winner_user_id
                    game_row.result_reason = reason
                    game_row.ended_at = utcnow_naive()
                    game_row.move_no = room.game.move_count
                    if room.rated and not room.rating_applied:
                        settlement = await self._settlement.settle_rated_game_in_session(
                            session,
                            game_row,
                            black_id=room.black_user_id,
                            white_id=room.white_user_id,
                            winner_user_id=room.winner_user_id,
                            status=room.status,
                            result_reason=room.result_reason or "",
                            move_no=room.game.move_count,
                            season_id=season_id,
                        )
                # 终局状态与积分结算一起提交：要么都成功，要么都回滚。
                await session.commit()
        except Exception:
            logger.exception("终局结果写入失败 room=%s", room.code)
            room.status = previous_status
            room.winner_user_id = previous_winner
            room.result_reason = previous_reason
            room.turn_deadline_ms = previous_deadline
            room.clock_started_ms = previous_started
            room.state_version += 1
            if previous_status == "playing":
                self._start_timeout_locked(room)
            await self._broadcast_game_state(room)
            return

        if settlement is not None:
            self._settlement.apply_settlement(room, settlement)
        self._record_game_finished(room)
        await self._broadcast_game_state(room)
        # 对局结束，进行中的对局列表少了一条。
        await self._notify_live_games_changed()
        if not room.connections:
            self._remove_room_locked(room)

    async def _settlement_season_id(self) -> Optional[str]:
        """结算要用的赛季 id。"""
        return await self._settlement.settlement_season_id()

    async def _settle_rated_game_locked(self, room: Room) -> None:
        """独立事务结算排位积分；实现见 settlement.RatingSettlement。"""
        await self._settlement.settle_rated_game_locked(room)

    async def _settle_rated_game_in_session(
        self,
        session: AsyncSession,
        game_row: Game,
        *,
        black_id: Optional[str],
        white_id: Optional[str],
        winner_user_id: Optional[str],
        status: str,
        result_reason: str,
        move_no: int,
        season_id: Optional[str],
    ) -> Optional[SettlementResult]:
        """在给定会话里完成排位结算的数据库部分。"""
        return await self._settlement.settle_rated_game_in_session(
            session,
            game_row,
            black_id=black_id,
            white_id=white_id,
            winner_user_id=winner_user_id,
            status=status,
            result_reason=result_reason,
            move_no=move_no,
            season_id=season_id,
        )

    def _apply_settlement(
        self, room: Room, result: SettlementResult
    ) -> None:
        """把已经提交的结算结果落到内存房间。"""
        self._settlement.apply_settlement(room, result)

    async def repair_unsettled_games(self) -> int:
        """补结算"已终局但没结算"的排位对局，返回补结算的对局数。"""
        return await self._settlement.repair_unsettled_games()

    async def repair_unsettled_games_once(self) -> int:
        """整个进程只补结算一次；重复调用直接返回 0。"""
        return await self._settlement.repair_unsettled_games_once()

    # ------------------------------------------------------------------
    # 持久化清理/关闭
    # ------------------------------------------------------------------
    async def _abandon_game_row(self, game_id: str, reason: str) -> None:
        """把一个还没用上的对局行标记为作废。

        覆盖 waiting（建房后没能入座）与 playing（匹配写库成功、但二次
        校验发现玩家已进别的房间）两种状态；终局行不属于“没用上”，
        不会被这里改写。
        """
        try:
            async with self._session_factory() as session:
                game_row = await session.get(Game, game_id)
                if game_row is not None and game_row.status in (
                    "waiting",
                    "playing",
                ):
                    game_row.status = "abandoned"
                    game_row.result_reason = reason
                    game_row.ended_at = utcnow_naive()
                    await session.commit()
        except Exception:
            logger.exception("作废未使用对局数据库写入失败")

    async def _mark_waiting_game_abandoned(
        self, room: Room, reason: str
    ) -> None:
        if room.status != "waiting":
            return
        await self._abandon_game_row(room.game_id, reason)

    def _remove_room_locked(self, room: Room) -> None:
        self._cancel_timeout_locked(room)
        self._clear_undo_locked(room)
        self._cancel_all_escape_timers_locked(room)
        self._rooms_by_code.pop(room.code, None)
        for uid in room.participant_ids():
            if self._room_by_user.get(uid) is room:
                self._room_by_user.pop(uid, None)
        room.connections.clear()
        # 房间没了，观战登记也要一并清掉，避免留下悬空的观战者。
        for uid in list(room.spectators):
            if self._spectator_by_user.get(uid) is room:
                self._spectator_by_user.pop(uid, None)
        room.spectators.clear()

    async def shutdown(self) -> None:
        """停止时作废未完成对局并关闭连接，避免数据库残留 playing。"""
        rooms = list(self._rooms_by_code.values())
        for room in rooms:
            self._cancel_timeout_locked(room)
            self._clear_undo_locked(room)
            self._cancel_all_escape_timers_locked(room)
        active_game_ids = [
            room.game_id
            for room in rooms
            if room.status in ("waiting", "playing")
        ]
        if active_game_ids:
            try:
                async with self._session_factory() as session:
                    await session.execute(
                        update(Game)
                        .where(
                            Game.id.in_(active_game_ids),
                            Game.status.in_(("waiting", "playing")),
                        )
                        .values(
                            status="abandoned",
                            result_reason="server_shutdown",
                            ended_at=utcnow_naive(),
                        )
                    )
                    await session.commit()
            except Exception:
                logger.exception("服务关闭时作废对局失败")
        for conn in list(self._connections.values()):
            await conn.close(1001, "服务器正在关闭")
        self._rooms_by_code.clear()
        self._room_by_user.clear()
        self._spectator_by_user.clear()
        self._connections_registry.clear()
        self._matchmaker.clear()


__all__ = [
    "ClientConnection",
    "QueuedPlayer",
    "Room",
    "RoomInvite",
    "RoomManager",
]
