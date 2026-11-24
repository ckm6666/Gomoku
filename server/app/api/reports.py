"""对局举报与管理员复核接口。

玩家只能举报自己参与、已经结束的对局，且目标必须是实时对手；管理员
（``GOMOKU_ADMIN_USERNAMES`` 白名单）可以列出举报、查看关联对局审计并
标记处理结果。账号处罚不在本接口内自动执行，避免误伤。
"""

from __future__ import annotations

from collections import defaultdict
from typing import Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from server.app.audit import audit_game, audit_moves_from_rows
from server.app.db import utcnow_naive
from server.app.dependencies import get_admin_user, get_current_user, get_db
from server.app.models import Game, GameReport, Move as MoveRow, User
from server.app.schemas import (
    AdminReportEntry,
    AdminReportListResponse,
    ReportCreateRequest,
    ReportEntry,
    ReportListResponse,
    ReportReviewRequest,
)

router = APIRouter(tags=["reports"])

_COMPLETED_STATUSES = ("black_won", "white_won", "draw")
_REVIEW_STATUSES = frozenset({"pending", "resolved", "dismissed"})


def _report_entry(report: GameReport) -> ReportEntry:
    return ReportEntry(
        id=report.id,
        game_id=report.game_id,
        reporter_id=report.reporter_id,
        reported_user_id=report.reported_user_id,
        category=report.category,
        description=report.description,
        status=report.status,
        created_at=report.created_at,
        reviewed_at=report.reviewed_at,
        resolution=report.resolution,
    )


def _audit_count_for_game(
    game: Game, rows: Sequence[MoveRow]
) -> int:
    """用已加载的落子行计算审计异常数量。"""
    result = audit_game(
        game_id=game.id,
        black_user_id=game.black_user_id,
        white_user_id=game.white_user_id,
        status=game.status,
        winner_user_id=game.winner_user_id,
        result_reason=game.result_reason,
        move_count=game.move_no,
        moves=audit_moves_from_rows(rows),
        started_at=game.started_at,
        ended_at=game.ended_at,
        rule_mode=game.rule_mode or "freestyle",
    )
    return len(result.findings)


async def _load_moves_by_game(
    session: AsyncSession, game_ids: Sequence[str]
) -> dict[str, list[MoveRow]]:
    """一次性取出所有被举报对局的落子，并按对局分组。

    举报列表可能有几十上百条、涉及几十局对局；逐局查询会让 SQL 次数
    随对局数线性增长（N+1），长棋谱还要重复重放，延迟可以到秒级。
    这里合并成一条 ``IN`` 查询，查询次数与对局数无关。
    """
    grouped: dict[str, list[MoveRow]] = defaultdict(list)
    unique_ids = list(dict.fromkeys(game_ids))
    if not unique_ids:
        return grouped
    rows = (
        await session.scalars(
            select(MoveRow)
            .where(MoveRow.game_id.in_(unique_ids))
            .order_by(MoveRow.game_id.asc(), MoveRow.move_no.asc())
        )
    ).all()
    for row in rows:
        grouped[row.game_id].append(row)
    return grouped


@router.post(
    "/reports",
    response_model=ReportEntry,
    status_code=status.HTTP_201_CREATED,
)
async def create_report(
    payload: ReportCreateRequest,
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ReportEntry:
    """举报一局已结束对局中的对手。"""
    game = await session.get(Game, payload.game_id)
    if game is None or current_user.id not in (
        game.black_user_id,
        game.white_user_id,
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="对局不存在"
        )
    if game.status not in _COMPLETED_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="只能举报已经结束的对局",
        )
    opponent_id = (
        game.white_user_id
        if game.black_user_id == current_user.id
        else game.black_user_id
    )
    if not opponent_id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="该对局没有对手"
        )
    existing = await session.scalar(
        select(GameReport.id).where(
            GameReport.game_id == game.id,
            GameReport.reporter_id == current_user.id,
        )
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="你已经举报过这一局",
        )

    report = GameReport(
        game_id=game.id,
        reporter_id=current_user.id,
        reported_user_id=opponent_id,
        category=payload.category,
        description=payload.description.strip(),
        status="pending",
    )
    session.add(report)
    try:
        await session.commit()
    except IntegrityError:
        # 并发重复提交时由唯一约束兜底。
        await session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="你已经举报过这一局",
        ) from None
    await session.refresh(report)
    return _report_entry(report)


@router.get("/reports/mine", response_model=ReportListResponse)
async def my_reports(
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> ReportListResponse:
    rows = (
        await session.scalars(
            select(GameReport)
            .where(GameReport.reporter_id == current_user.id)
            .order_by(GameReport.created_at.desc())
            .limit(50)
        )
    ).all()
    return ReportListResponse(
        reports=[_report_entry(report) for report in rows]
    )


@router.get(
    "/admin/reports", response_model=AdminReportListResponse
)
async def admin_reports(
    report_status: Optional[str] = Query(default="pending", alias="status"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
    _admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db),
) -> AdminReportListResponse:
    conditions = []
    if report_status:
        if report_status not in _REVIEW_STATUSES:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="举报状态必须是 pending/resolved/dismissed",
            )
        conditions.append(GameReport.status == report_status)
    total = await session.scalar(
        select(func.count(GameReport.id)).where(*conditions)
    )
    reports = (
        await session.scalars(
            select(GameReport)
            .where(*conditions)
            .order_by(GameReport.created_at.asc())
            .offset(offset)
            .limit(limit)
        )
    ).all()

    user_ids = {
        user_id
        for report in reports
        for user_id in (report.reporter_id, report.reported_user_id)
    }
    names: dict[str, str] = {}
    if user_ids:
        rows = await session.execute(
            select(User.id, User.username).where(User.id.in_(user_ids))
        )
        names = {user_id: name for user_id, name in rows.all()}
    games: dict[str, Game] = {}
    game_ids = {report.game_id for report in reports}
    if game_ids:
        game_rows = (
            await session.scalars(
                select(Game).where(Game.id.in_(game_ids))
            )
        ).all()
        games = {game.id: game for game in game_rows}
    # 所有被举报对局的落子一次查完，再逐局在内存里重放审计，
    # 避免每局一次查询（N+1）。
    moves_by_game = await _load_moves_by_game(
        session, list(games.keys())
    )
    audit_counts: dict[str, int] = {}
    entries: list[AdminReportEntry] = []
    for report in reports:
        base = _report_entry(report)
        game = games.get(report.game_id)
        if game is not None and game.id not in audit_counts:
            audit_counts[game.id] = _audit_count_for_game(
                game, moves_by_game.get(game.id, [])
            )
        entries.append(
            AdminReportEntry(
                **base.model_dump(),
                reporter_username=names.get(
                    report.reporter_id, "已注销玩家"
                ),
                reported_username=names.get(
                    report.reported_user_id, "已注销玩家"
                ),
                game_status=game.status if game else "",
                game_rated=bool(game.rated) if game else False,
                audit_findings=audit_counts.get(report.game_id, 0),
            )
        )
    return AdminReportListResponse(
        reports=entries, total=int(total or 0)
    )


@router.post(
    "/admin/reports/{report_id}/review", response_model=ReportEntry
)
async def review_report(
    report_id: str,
    payload: ReportReviewRequest,
    admin: User = Depends(get_admin_user),
    session: AsyncSession = Depends(get_db),
) -> ReportEntry:
    report = await session.get(GameReport, report_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="举报不存在"
        )
    if report.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="该举报已经处理过",
        )
    default_note = {
        "dismiss": "未发现违规",
        "warn": "已记录并警告被举报玩家",
        "uphold": "举报成立，已记录处罚依据",
    }[payload.action]
    report.status = (
        "dismissed" if payload.action == "dismiss" else "resolved"
    )
    report.resolution = payload.note.strip() or default_note
    report.reviewed_at = utcnow_naive()
    report.reviewer_id = admin.id
    await session.commit()
    await session.refresh(report)
    return _report_entry(report)


__all__ = ["router"]
