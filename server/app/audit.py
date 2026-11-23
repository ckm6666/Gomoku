"""对局回放审计：校验落子序列、终局结果与可疑时间特征。

审计不替代人工判断，只把“数据库里的棋谱是否自洽、是否明显像脚本”
这类客观问题列出来，供举报复核和管理员排查：

* 落子编号连续、黑白交替、执子人与座位一致、坐标合法且不重复；
* 重放后的局面胜者与 ``games`` 表记录一致；
* 落子时间单调递增，且不存在大量“秒回”式响应；
* 终局时间之后仍有落子等明显异常。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Sequence

from gomoku.shared.game_model import GameModel
from gomoku.shared.rules import BOARD_SIZE, Player

#: 被判定为“秒回”的单步响应时间上限。
FAST_RESPONSE_SECONDS = 0.3
#: 至少这么多手秒回才给出警告，避免把正常快棋误判成脚本。
FAST_MOVE_WARNING_COUNT = 10
#: 开始时间之前允许的时钟误差。
START_CLOCK_TOLERANCE_SECONDS = 2.0

_COMPLETED_STATUSES = frozenset({"black_won", "white_won", "draw"})


@dataclass(frozen=True)
class AuditMove:
    move_no: int
    user_id: str
    color: str
    row: int
    col: int
    created_at: Optional[datetime] = None


@dataclass(frozen=True)
class AuditFinding:
    code: str
    severity: str  # error/warning/info
    message: str


@dataclass(frozen=True)
class AuditResult:
    game_id: str
    move_count: int
    findings: tuple[AuditFinding, ...] = ()
    fast_moves: int = 0

    @property
    def ok(self) -> bool:
        return not any(item.severity == "error" for item in self.findings)

    @property
    def summary(self) -> dict[str, int]:
        counts = {"error": 0, "warning": 0, "info": 0}
        for item in self.findings:
            counts[item.severity] = counts.get(item.severity, 0) + 1
        counts["moves_checked"] = self.move_count
        counts["fast_moves"] = self.fast_moves
        return counts


def audit_game(
    *,
    game_id: str,
    black_user_id: str,
    white_user_id: Optional[str],
    status: str,
    winner_user_id: Optional[str],
    result_reason: Optional[str],
    move_count: int,
    moves: Sequence[AuditMove],
    started_at: Optional[datetime] = None,
    ended_at: Optional[datetime] = None,
) -> AuditResult:
    """审计一局；返回按发现顺序排列的异常列表。"""
    findings: list[AuditFinding] = []
    model = GameModel()
    seen: set[tuple[int, int]] = set()
    fast_moves = 0
    previous_at: Optional[datetime] = None
    previous_user: Optional[str] = None

    if move_count != len(moves):
        findings.append(
            AuditFinding(
                "move_count_mismatch",
                "error",
                f"games.move_no={move_count}，实际落子 {len(moves)} 手",
            )
        )

    for index, move in enumerate(moves, start=1):
        if move.move_no != index:
            findings.append(
                AuditFinding(
                    "move_number_gap",
                    "error",
                    f"第 {index} 条落子编号为 {move.move_no}，编号不连续",
                )
            )
        expected_color = "black" if index % 2 == 1 else "white"
        if move.color != expected_color:
            findings.append(
                AuditFinding(
                    "color_order",
                    "error",
                    f"第 {index} 手颜色 {move.color}，应为 {expected_color}",
                )
            )
        seat_user = (
            black_user_id if move.color == "black" else white_user_id
        )
        if seat_user is None or move.user_id != seat_user:
            findings.append(
                AuditFinding(
                    "seat_mismatch",
                    "error",
                    f"第 {index} 手执子人与 {move.color} 方座位不一致",
                )
            )
        if not (
            0 <= move.row < BOARD_SIZE and 0 <= move.col < BOARD_SIZE
        ):
            findings.append(
                AuditFinding(
                    "coordinate_out_of_range",
                    "error",
                    f"第 {index} 手坐标越界 ({move.row}, {move.col})",
                )
            )
            continue
        if (move.row, move.col) in seen:
            findings.append(
                AuditFinding(
                    "duplicate_coordinate",
                    "error",
                    f"第 {index} 手重复落在 ({move.row}, {move.col})",
                )
            )
            continue
        seen.add((move.row, move.col))
        if not model.place_stone(move.row, move.col):
            findings.append(
                AuditFinding(
                    "illegal_move",
                    "error",
                    f"第 {index} 手无法在重放中合法落子",
                )
            )
            continue

        if move.created_at is not None:
            if (
                started_at is not None
                and move.created_at
                < started_at - timedelta(
                    seconds=START_CLOCK_TOLERANCE_SECONDS
                )
            ):
                findings.append(
                    AuditFinding(
                        "before_start",
                        "warning",
                        f"第 {index} 手时间早于开局时间",
                    )
                )
            if previous_at is not None:
                delta = (move.created_at - previous_at).total_seconds()
                if delta < 0:
                    findings.append(
                        AuditFinding(
                            "time_reversed",
                            "error",
                            f"第 {index} 手时间早于上一手",
                        )
                    )
                elif (
                    previous_user is not None
                    and previous_user != move.user_id
                    and delta < FAST_RESPONSE_SECONDS
                ):
                    fast_moves += 1
            previous_at = move.created_at
            previous_user = move.user_id
        if ended_at is not None and move.created_at is not None:
            if move.created_at > ended_at + timedelta(seconds=2):
                findings.append(
                    AuditFinding(
                        "after_end",
                        "warning",
                        f"第 {index} 手时间晚于终局时间",
                    )
                )

    if fast_moves >= FAST_MOVE_WARNING_COUNT:
        findings.append(
            AuditFinding(
                "suspicious_fast_moves",
                "warning",
                f"{fast_moves} 手响应快于 {FAST_RESPONSE_SECONDS} 秒，"
                "建议结合举报内容复核",
            )
        )

    if status in _COMPLETED_STATUSES:
        board_winner = model.winner
        non_board_ending = result_reason in {
            "resign",
            "timeout",
            "escape",
        }
        if status == "draw":
            if board_winner is not None:
                findings.append(
                    AuditFinding(
                        "result_mismatch",
                        "error",
                        "记录为和棋，但重放棋盘已有五连胜者",
                    )
                )
        elif winner_user_id is None:
            findings.append(
                AuditFinding(
                    "result_mismatch",
                    "error",
                    "对局有胜负结果但缺少 winner_user_id",
                )
            )
        elif non_board_ending:
            # 认输/超时/逃跑可以由服务器在棋盘没有五连时判负；
            # 只需确认胜者确实是本局玩家。
            if winner_user_id not in {black_user_id, white_user_id}:
                findings.append(
                    AuditFinding(
                        "winner_mismatch",
                        "error",
                        "胜者不属于本局玩家",
                    )
                )
            elif board_winner is not None:
                expected_winner = (
                    black_user_id
                    if board_winner is Player.BLACK
                    else white_user_id
                )
                if expected_winner != winner_user_id:
                    findings.append(
                        AuditFinding(
                            "result_board_conflict",
                            "warning",
                            "终局原因为认输/超时/逃跑，"
                            "但棋盘五连方向与胜者不一致",
                        )
                    )
                else:
                    findings.append(
                        AuditFinding(
                            "result_board_conflict",
                            "warning",
                            "终局原因为认输/超时/逃跑，"
                            "但棋盘已经形成五连",
                        )
                    )
        elif status == "black_won" and board_winner is not Player.BLACK:
            findings.append(
                AuditFinding(
                    "result_mismatch",
                    "error",
                    "记录为黑胜，但重放棋盘不成立",
                )
            )
        elif status == "white_won" and board_winner is not Player.WHITE:
            findings.append(
                AuditFinding(
                    "result_mismatch",
                    "error",
                    "记录为白胜，但重放棋盘不成立",
                )
            )
        elif board_winner is not None:
            expected_winner = (
                black_user_id
                if board_winner is Player.BLACK
                else white_user_id
            )
            if expected_winner != winner_user_id:
                findings.append(
                    AuditFinding(
                        "winner_mismatch",
                        "error",
                        "胜者与重放棋盘的执子方不一致",
                    )
                )

    return AuditResult(
        game_id=game_id,
        move_count=len(moves),
        findings=tuple(findings),
        fast_moves=fast_moves,
    )


def audit_moves_from_rows(rows: Sequence[object]) -> list[AuditMove]:
    """把数据库 Move 行转换成审计输入；兼容 ORM 对象。"""
    return [
        AuditMove(
            move_no=int(getattr(row, "move_no")),
            user_id=str(getattr(row, "user_id")),
            color=str(getattr(row, "color")),
            row=int(getattr(row, "row")),
            col=int(getattr(row, "col")),
            created_at=getattr(row, "created_at", None),
        )
        for row in rows
    ]


__all__ = [
    "AuditFinding",
    "AuditMove",
    "AuditResult",
    "audit_game",
    "audit_moves_from_rows",
]
