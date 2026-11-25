"""WSS/WS 联机网关：认证、消息分发、好友房与排位匹配命令。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import time
import uuid
from typing import Any, Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from gomoku.shared.achievements import (
    AchievementStats,
    unlocked_achievement_keys,
)
from gomoku.shared.avatar import normalize_avatar
from gomoku.shared.capabilities import (
    SERVER_CAPABILITIES,
    normalize_capabilities,
)
from gomoku.shared.errors import ProtocolError
from gomoku.shared.protocol import (
    CLIENT_AUTH,
    CLIENT_CANCEL_MATCH,
    CLIENT_CHAT,
    CLIENT_CREATE_ROOM,
    CLIENT_INVITE_TO_ROOM,
    CLIENT_JOIN_ROOM,
    CLIENT_MOVE_INTENT,
    CLIENT_PING,
    CLIENT_QUICK_MATCH,
    CLIENT_REMATCH,
    CLIENT_RESPOND_ROOM_INVITE,
    CLIENT_RESIGN,
    CLIENT_SPECTATOR_CHAT,
    CLIENT_LEAVE_ROOM,
    CLIENT_UNDO_REQUEST,
    CLIENT_UNDO_RESPONSE,
    CLIENT_WATCH_GAME,
    SERVER_AUTHENTICATED,
    SERVER_ERROR,
    SERVER_PONG,
    make_message,
    parse_message,
    rule_mode_from_payload,
)
from gomoku.shared.time_control import (
    PER_MOVE_MAX_SECONDS,
    PER_MOVE_MIN_SECONDS,
    TimeControl,
)
from server.app.models import User
from server.app.ratings import rank_for_rating
from server.app.security import decode_access_token
from server.app.ws.entities import ClientConnection
from server.app.ws.limits import CommandRateLimiter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["game-websocket"])

#: 心跳（ping）单独限流。ping 是保活机制，不能按命令的额度限，否则
#: 空闲连接会被误伤；但也不能完全豁免——每条 ping 都要服务端回一个
#: pong，无限制的 ping 等于让客户端无限占用服务端往返。
_PING_BURST = 5
_PING_WINDOW_SECONDS = 5.0


class _EnvelopeError(ProtocolError):
    """协议错误，附带原始帧的 request_id（若能取到）。

    网关解析失败时也回 request_id，客户端才能把这条错误归到对应的
    请求上（例如"发言被拒"），而不是笼统地弹进红色错误条。
    """

    def __init__(self, message: str, request_id: Optional[str] = None) -> None:
        super().__init__(message)
        self.request_id = request_id


async def _receive_envelope(
    websocket: WebSocket,
    timeout: float,
    max_chars: int,
) -> dict[str, Any]:
    """读取一条文本帧并严格限制大小/JSON/协议 envelope。"""
    text = await asyncio.wait_for(websocket.receive_text(), timeout=timeout)
    if len(text) > max_chars:
        await websocket.close(code=1009, reason="消息过大")
        raise ProtocolError("消息过大")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("消息不是合法 JSON") from exc
    try:
        return parse_message(raw)
    except ProtocolError as exc:
        # 尽量带上原始 request_id，客户端才能精确定位是哪条请求被拒。
        request_id: Optional[str] = None
        if isinstance(raw, dict):
            candidate = raw.get("request_id")
            if isinstance(candidate, str) and candidate:
                request_id = candidate
        raise _EnvelopeError(str(exc), request_id) from exc


async def _safe_close(websocket: WebSocket, code: int, reason: str) -> None:
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:
        # 关闭失败也要留痕：否则「连接没断开」这类问题无从排查。
        logger.debug("关闭 WebSocket 失败", exc_info=True)


def _requested_time_control(
    payload: dict,
) -> tuple[Optional[TimeControl], str]:
    """解析创建房间/快速匹配的时限设置。

    兼容旧的 ``turn_seconds`` 字段；新的 ``time_control`` 对象优先。
    返回 ``(time_control, error)``，未指定时 time_control 为 None，
    由 RoomManager 使用服务端默认每手时间。
    """
    raw = payload.get("time_control")
    if raw is not None:
        try:
            return TimeControl.from_payload(raw), ""
        except ProtocolError as exc:
            return None, str(exc)
    turn_seconds = payload.get("turn_seconds")
    if turn_seconds is None:
        return None, ""
    if (
        not isinstance(turn_seconds, int)
        or isinstance(turn_seconds, bool)
        or not PER_MOVE_MIN_SECONDS
        <= turn_seconds
        <= PER_MOVE_MAX_SECONDS
    ):
        return None, (
            f"每手时间必须在 {PER_MOVE_MIN_SECONDS}～"
            f"{PER_MOVE_MAX_SECONDS} 秒"
        )
    return TimeControl.per_move(turn_seconds), ""


def _client_ip(websocket: WebSocket, settings: Any) -> str:
    """解析客户端 IP；只有明确信任代理时才读取 X-Forwarded-For。"""
    direct = websocket.client.host if websocket.client else ""
    if not settings.trust_proxy_headers:
        return direct or "unknown"

    candidates = []
    forwarded = websocket.headers.get("x-forwarded-for", "")
    if forwarded:
        candidates.append(forwarded.split(",")[0].strip())
    real_ip = websocket.headers.get("x-real-ip", "").strip()
    if real_ip:
        candidates.append(real_ip)
    for candidate in candidates:
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            continue
    return direct or "unknown"


@router.websocket("/ws")
async def game_websocket(websocket: WebSocket) -> None:
    """认证后进入好友房/排位匹配/对局事件循环。

    第一条消息必须是：
    ``{"protocol":1,"type":"auth","payload":{"access_token":"..."}}``
    """
    await websocket.accept()
    app = websocket.app
    settings = app.state.settings
    session_factory = app.state.session_factory
    room_manager = app.state.room_manager

    # ------------------------------------------------------------------
    # 首帧认证
    # ------------------------------------------------------------------
    try:
        message = await _receive_envelope(
            websocket,
            timeout=settings.ws_auth_timeout_seconds,
            max_chars=settings.max_ws_message_chars,
        )
    except asyncio.TimeoutError:
        await _safe_close(websocket, 1008, "认证超时")
        return
    except ProtocolError:
        await _safe_close(websocket, 1008, "认证消息非法")
        return
    except WebSocketDisconnect:
        return

    if message.get("type") != CLIENT_AUTH:
        await _safe_close(websocket, 1008, "首条消息必须是认证")
        return
    payload = message.get("payload", {})
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token:
        await _safe_close(websocket, 1008, "缺少访问令牌")
        return
    try:
        client_capabilities = normalize_capabilities(
            payload.get("capabilities", ())
        )
    except ProtocolError:
        await _safe_close(websocket, 1008, "能力声明非法")
        return

    try:
        user_id = decode_access_token(token, settings)
    except ProtocolError:
        await _safe_close(websocket, 1008, "认证失败")
        return

    async with session_factory() as session:
        user = await session.scalar(
            select(User).where(User.id == user_id, User.is_active.is_(True))
        )
        if user is None:
            await _safe_close(websocket, 1008, "认证失败")
            return
        user_id = user.id
        username = user.username
        rating = user.rating
        avatar = normalize_avatar(user.avatar, user.id)
        avatar_version = user.public_avatar_version
        achievements = list(
            unlocked_achievement_keys(
                AchievementStats(
                    wins=user.wins,
                    rated_games=user.rated_games,
                    current_streak=user.current_win_streak,
                    best_streak=user.best_win_streak,
                    rating=user.rating,
                    peak_rating=user.peak_rating,
                )
            )
        )
        current_streak = user.current_win_streak
        best_streak = user.best_win_streak
        rating_rd = round(float(user.rating_rd), 1)

    client_ip = _client_ip(websocket, settings)
    conn = ClientConnection(
        connection_id=uuid.uuid4().hex,
        user_id=user_id,
        username=username,
        websocket=websocket,
        client_ip=client_ip,
        capabilities=client_capabilities,
        avatar=avatar,
        rating=rating,
        avatar_version=avatar_version,
        achievements=tuple(achievements),
    )
    if not await room_manager.reserve_connection(conn):
        metrics = getattr(app.state, "metrics", None)
        if metrics is not None:
            metrics.connection_limited.inc()
        logger.warning(
            "ws_connection_limited",
            extra={
                "event": "ws_connection_limited",
                "user_id": user_id,
                "client_ip": client_ip,
            },
        )
        await _safe_close(websocket, 4009, "连接数过多")
        return

    connected_at = time.monotonic()
    logger.info(
        "ws_connected",
        extra={
            "event": "ws_connected",
            "user_id": user_id,
            "client_ip": client_ip,
            "connection_id": conn.connection_id,
        },
    )
    season = await room_manager.current_season()
    # 兜底的启动补结算：把"已终局但没结算"的排位对局补上。真正的启动
    # 钩子在 main.py 的 lifespan 里，这里保证只要有连接进来就会补一次。
    await room_manager.repair_unsettled_games_once()
    authenticated = await conn.send(
        make_message(
            SERVER_AUTHENTICATED,
            {
                "user": {
                    "id": user_id,
                    "username": username,
                    "avatar": avatar,
                    "avatar_version": avatar_version,
                    "achievements": achievements,
                    "current_win_streak": current_streak,
                    "best_win_streak": best_streak,
                    "rating": rating,
                    "rating_rd": rating_rd,
                    "rank_name": rank_for_rating(rating).name,
                },
                "season": (
                    season.payload() if season is not None else None
                ),
                "capabilities": sorted(SERVER_CAPABILITIES),
            },
        )
    )
    if not authenticated:
        await room_manager.release_connection(conn.connection_id)
        return

    try:
        await room_manager.attach(conn)
    except Exception:
        logger.exception("恢复联机房间失败")
        await conn.send(
            make_message(SERVER_ERROR, {"reason": "恢复房间状态失败"})
        )

    # ------------------------------------------------------------------
    # 命令循环
    # ------------------------------------------------------------------
    command_limiter = CommandRateLimiter(
        settings.ws_command_burst,
        settings.ws_command_window_seconds,
    )
    # 心跳单独一套额度：比命令宽松，但同样有限。
    ping_limiter = CommandRateLimiter(
        _PING_BURST, _PING_WINDOW_SECONDS
    )
    try:
        while True:
            try:
                message = await _receive_envelope(
                    websocket,
                    timeout=settings.ws_idle_timeout_seconds,
                    max_chars=settings.max_ws_message_chars,
                )
            except asyncio.TimeoutError:
                await _safe_close(websocket, 1001, "连接空闲超时")
                break
            except ProtocolError as exc:
                await conn.send(
                    make_message(
                        SERVER_ERROR,
                        {"reason": str(exc)},
                        request_id=getattr(exc, "request_id", None),
                    )
                )
                continue

            message_type = message["type"]
            request_id = message.get("request_id")
            payload = message.get("payload", {})

            if message_type == CLIENT_PING:
                # 心跳有自己的额度：不占命令配额，但同样有限。
                if not ping_limiter.allow(time.monotonic()):
                    metrics = getattr(app.state, "metrics", None)
                    if metrics is not None:
                        metrics.ws_rate_limited.inc()
                    logger.warning(
                        "ws_ping_rate_limited",
                        extra={
                            "event": "ws_ping_rate_limited",
                            "user_id": user_id,
                            "message_type": message_type,
                            "request_id": request_id,
                        },
                    )
                    await conn.send(
                        make_message(
                            SERVER_ERROR,
                            {"reason": "心跳过于频繁，请稍后再试"},
                            request_id=request_id,
                        )
                    )
                    continue
            elif not command_limiter.allow(time.monotonic()):
                metrics = getattr(app.state, "metrics", None)
                if metrics is not None:
                    metrics.ws_rate_limited.inc()
                logger.warning(
                    "ws_rate_limited",
                    extra={
                        "event": "ws_rate_limited",
                        "user_id": user_id,
                        "message_type": message_type,
                        "request_id": request_id,
                    },
                )
                await conn.send(
                    make_message(
                        SERVER_ERROR,
                        {"reason": "操作过于频繁，请稍后再试"},
                        request_id=request_id,
                    )
                )
                continue

            try:
                if message_type == CLIENT_PING:
                    await conn.send(
                        make_message(
                            SERVER_PONG,
                            {"server_now_ms": int(time.time() * 1000)},
                        )
                    )
                elif message_type == CLIENT_CREATE_ROOM:
                    allow_spectators = payload.get(
                        "allow_spectators", True
                    )
                    time_control, time_error = _requested_time_control(
                        payload
                    )
                    # 缺省视为自由规则；显式给出非法值时沿用协议错误
                    # 语义，回具体的 request_id，客户端才能归因。
                    try:
                        rule_mode = rule_mode_from_payload(
                            payload.get("rule_mode")
                        )
                    except ProtocolError:
                        rule_mode = None
                    if not isinstance(allow_spectators, bool):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "观战设置格式非法"},
                                request_id=request_id,
                            )
                        )
                    elif time_error:
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": time_error},
                                request_id=request_id,
                            )
                        )
                    elif rule_mode is None:
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "规则模式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.create_room(
                            conn,
                            request_id,
                            allow_spectators,
                            time_control=time_control,
                            rule_mode=rule_mode,
                        )
                elif message_type == CLIENT_JOIN_ROOM:
                    code = payload.get("room_code")
                    if not isinstance(code, str) or not code.strip():
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "房间码不能为空"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.join_room(conn, code, request_id)
                elif message_type == CLIENT_MOVE_INTENT:
                    row = payload.get("row")
                    col = payload.get("col")
                    if (
                        not isinstance(row, int)
                        or isinstance(row, bool)
                        or not isinstance(col, int)
                        or isinstance(col, bool)
                    ):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "落子坐标格式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.move(conn, request_id, row, col)
                elif message_type == CLIENT_RESIGN:
                    await room_manager.resign(conn, request_id)
                elif message_type == CLIENT_UNDO_REQUEST:
                    await room_manager.undo_request(conn, request_id)
                elif message_type == CLIENT_UNDO_RESPONSE:
                    accept = payload.get("accept")
                    if not isinstance(accept, bool):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "悔棋回应格式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.undo_response(
                            conn, accept, request_id
                        )
                elif message_type == CLIENT_LEAVE_ROOM:
                    await room_manager.leave_room(conn, request_id)
                elif message_type == CLIENT_WATCH_GAME:
                    # 观战：旁观者按对局编号进入别人正在进行的一局。
                    game_id = payload.get("game_id")
                    if not isinstance(game_id, str):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "对局编号无效"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.watch_game(
                            conn, game_id, request_id
                        )
                elif message_type == CLIENT_REMATCH:
                    await room_manager.rematch(conn, request_id)
                elif message_type == CLIENT_QUICK_MATCH:
                    # 排位固定每手 30 秒；这里仍解析一次时限字段，
                    # 只为了拒绝畸形请求，实际对局不采用客户端的设置。
                    _ignored, time_error = _requested_time_control(
                        payload
                    )
                    if time_error:
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": time_error},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.quick_match(conn, request_id)
                elif message_type == CLIENT_CANCEL_MATCH:
                    await room_manager.cancel_match(conn, request_id)
                elif message_type == CLIENT_CHAT:
                    kind = payload.get("kind")
                    content = payload.get("content")
                    if (
                        not isinstance(kind, str)
                        or not isinstance(content, str)
                    ):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "聊天消息格式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        # 白名单、长度、限流与规范化都在 RoomManager 内完成，
                        # 网关只负责挡下非字符串的畸形负载。
                        await room_manager.send_chat(
                            conn, kind, content, request_id
                        )
                elif message_type == CLIENT_SPECTATOR_CHAT:
                    kind = payload.get("kind")
                    content = payload.get("content")
                    if (
                        not isinstance(kind, str)
                        or not isinstance(content, str)
                    ):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "聊天消息格式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        # 同观频道的校验与广播同样在 RoomManager 内完成，
                        # 网关只挡下非字符串的畸形负载。
                        await room_manager.send_spectator_chat(
                            conn, kind, content, request_id
                        )
                elif message_type == CLIENT_INVITE_TO_ROOM:
                    invitee_id = payload.get("user_id")
                    if not isinstance(invitee_id, str) or not invitee_id:
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "邀请对象非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.invite_to_room(
                            conn, invitee_id, request_id
                        )
                elif message_type == CLIENT_RESPOND_ROOM_INVITE:
                    invite_id = payload.get("invite_id")
                    accept = payload.get("accept")
                    if (
                        not isinstance(invite_id, str)
                        or not invite_id
                        or not isinstance(accept, bool)
                    ):
                        await conn.send(
                            make_message(
                                SERVER_ERROR,
                                {"reason": "邀请响应格式非法"},
                                request_id=request_id,
                            )
                        )
                    else:
                        await room_manager.respond_room_invite(
                            conn, invite_id, accept, request_id
                        )
                elif message_type == CLIENT_AUTH:
                    await conn.send(
                        make_message(
                            SERVER_ERROR,
                            {"reason": "连接已经认证"},
                            request_id=request_id,
                        )
                    )
                else:
                    await conn.send(
                        make_message(
                            SERVER_ERROR,
                            {"reason": "服务端不接受该消息类型"},
                            request_id=request_id,
                        )
                    )
            except Exception:
                metrics = getattr(app.state, "metrics", None)
                if metrics is not None:
                    metrics.ws_errors.inc()
                logger.exception(
                    "处理 WebSocket 消息失败 type=%s",
                    message_type,
                    extra={
                        "event": "ws_message_error",
                        "user_id": user_id,
                        "message_type": message_type,
                        "request_id": request_id,
                    },
                )
                await conn.send(
                    make_message(
                        SERVER_ERROR,
                        {"reason": "服务器处理消息失败"},
                        request_id=request_id,
                    )
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket 会话异常 user=%s", user_id)
        await _safe_close(websocket, 1011, "服务器内部错误")
    finally:
        try:
            await room_manager.detach(conn)
        except Exception:
            logger.exception("WebSocket 清理房间失败 user=%s", user_id)
        await _safe_close(websocket, 1000, "")
        logger.info(
            "ws_disconnected",
            extra={
                "event": "ws_disconnected",
                "user_id": user_id,
                "connection_id": conn.connection_id,
                "duration_ms": round(
                    (time.monotonic() - connected_at) * 1000, 2
                ),
            },
        )


__all__ = ["router"]
