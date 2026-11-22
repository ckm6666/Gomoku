"""成就与勋章定义，客户端与服务端共用。

成就**不单独存表**，而是由账号已有的排位统计（胜场、最高连胜、
最高积分）实时推导。这样可以避免"解锁事件写一半失败"之类的不一致，
也方便客户端在资料页展示未解锁成就的进度。

段位成就按**历史最高积分**计算，取得过的段位不会因为掉分而失去；
排位胜场/连胜只统计参与积分结算的快速匹配对局，好友房不计分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional

#: 胜场成就的 metric 名。
METRIC_WINS = "wins"
#: 最高连胜成就的 metric 名。
METRIC_STREAK = "streak"
#: 段位成就的 metric 名（历史最高积分）。
METRIC_PEAK_RATING = "peak_rating"

#: 勋章品级 -> (底色, 前景色)。客户端绘制勋章时使用。
TIER_COLORS: dict[str, tuple[str, str]] = {
    "bronze": ("#A8794A", "#FBF3E7"),
    "silver": ("#8C939B", "#FCFAF6"),
    "gold": ("#B8934E", "#2B2118"),
    "platinum": ("#5E8A7D", "#F3FBF7"),
    "diamond": ("#4F6FA8", "#F2F6FC"),
    "master": ("#7E3D5A", "#FBF0F4"),
    "legend": ("#9E3B2E", "#FCF0EC"),
}


@dataclass(frozen=True)
class AchievementSpec:
    """一项成就的展示与达成条件定义。"""

    key: str
    name: str
    description: str
    glyph: str
    category: str  # wins / streak / rank
    tier: str  # bronze/silver/gold/platinum/diamond/master/legend
    rarity: int  # 1～5，对局中优先展示稀有的勋章
    metric: str
    target: int

    @property
    def colors(self) -> tuple[str, str]:
        """返回 (底色, 前景色)。"""
        return TIER_COLORS.get(self.tier, TIER_COLORS["bronze"])


#: 全部成就；顺序也会用作成就墙与结算提示的展示顺序。
ACHIEVEMENTS: tuple[AchievementSpec, ...] = (
    # 排位胜场
    AchievementSpec(
        "first_win", "初捷", "排位赛取得首场胜利", "初",
        "wins", "bronze", 1, METRIC_WINS, 1,
    ),
    AchievementSpec(
        "wins_10", "十胜", "排位赛累计取胜 10 局", "十",
        "wins", "silver", 1, METRIC_WINS, 10,
    ),
    AchievementSpec(
        "wins_25", "廿五胜", "排位赛累计取胜 25 局", "廿",
        "wins", "gold", 2, METRIC_WINS, 25,
    ),
    AchievementSpec(
        "wins_50", "五十胜", "排位赛累计取胜 50 局", "五",
        "wins", "diamond", 3, METRIC_WINS, 50,
    ),
    AchievementSpec(
        "wins_100", "百胜", "排位赛累计取胜 100 局", "百",
        "wins", "legend", 4, METRIC_WINS, 100,
    ),
    # 最高连胜
    AchievementSpec(
        "streak_3", "三连捷", "排位赛最高连胜达到 3 局", "连",
        "streak", "silver", 2, METRIC_STREAK, 3,
    ),
    AchievementSpec(
        "streak_5", "五连捷", "排位赛最高连胜达到 5 局", "五",
        "streak", "gold", 3, METRIC_STREAK, 5,
    ),
    AchievementSpec(
        "streak_10", "十连捷", "排位赛最高连胜达到 10 局", "十",
        "streak", "legend", 5, METRIC_STREAK, 10,
    ),
    # 历史最高段位
    AchievementSpec(
        "rank_gold", "金榜", "历史最高段位达到黄金", "金",
        "rank", "gold", 1, METRIC_PEAK_RATING, 1200,
    ),
    AchievementSpec(
        "rank_platinum", "铂金", "历史最高段位达到铂金", "铂",
        "rank", "platinum", 2, METRIC_PEAK_RATING, 1350,
    ),
    AchievementSpec(
        "rank_diamond", "钻石", "历史最高段位达到钻石", "钻",
        "rank", "diamond", 3, METRIC_PEAK_RATING, 1500,
    ),
    AchievementSpec(
        "rank_master", "大师", "历史最高段位达到大师", "宗",
        "rank", "master", 4, METRIC_PEAK_RATING, 1650,
    ),
    AchievementSpec(
        "rank_legend", "王者", "历史最高段位达到王者", "王",
        "rank", "legend", 5, METRIC_PEAK_RATING, 1800,
    ),
)

ACHIEVEMENT_BY_KEY: dict[str, AchievementSpec] = {
    spec.key: spec for spec in ACHIEVEMENTS
}
ACHIEVEMENT_KEYS = frozenset(ACHIEVEMENT_BY_KEY)
ACHIEVEMENT_TOTAL = len(ACHIEVEMENTS)

#: 进行中的对局最多展示几枚勋章，避免玩家卡片被挤满。
MATCH_MEDAL_LIMIT = 3


@dataclass(frozen=True)
class AchievementStats:
    """推导成就所需的账号统计快照。"""

    wins: int = 0
    rated_games: int = 0
    current_streak: int = 0
    best_streak: int = 0
    rating: int = 1200
    peak_rating: int = 1200

    def metric_value(self, metric: str) -> int:
        if metric == METRIC_WINS:
            return max(0, int(self.wins))
        if metric == METRIC_STREAK:
            return max(0, int(self.best_streak))
        if metric == METRIC_PEAK_RATING:
            return max(0, int(self.peak_rating))
        return 0


def _safe_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    return default


def stats_from_mapping(user: Mapping[str, object]) -> AchievementStats:
    """从资料接口返回的 user 字典构造统计快照。"""
    rating = _safe_int(user.get("rating"), 1200)
    return AchievementStats(
        wins=_safe_int(user.get("wins"), 0),
        rated_games=_safe_int(user.get("rated_games"), 0),
        current_streak=_safe_int(user.get("current_win_streak"), 0),
        best_streak=_safe_int(user.get("best_win_streak"), 0),
        rating=rating,
        peak_rating=_safe_int(user.get("peak_rating"), rating),
    )


def achievement_value(
    spec: AchievementSpec, stats: AchievementStats
) -> int:
    """返回该成就当前指标值（未截断）。"""
    return stats.metric_value(spec.metric)


def achievement_unlocked(
    spec: AchievementSpec, stats: AchievementStats
) -> bool:
    return achievement_value(spec, stats) >= spec.target


def achievement_progress(
    spec: AchievementSpec, stats: AchievementStats
) -> tuple[int, int, int]:
    """返回 (当前值, 目标值, 百分比)；百分比用于成就墙进度条。"""
    current = max(0, achievement_value(spec, stats))
    target = max(1, spec.target)
    percent = max(0, min(100, round(current * 100 / target)))
    return current, spec.target, percent


def unlocked_achievements(
    stats: AchievementStats,
) -> tuple[AchievementSpec, ...]:
    """按目录顺序返回已解锁成就。"""
    return tuple(
        spec for spec in ACHIEVEMENTS if achievement_unlocked(spec, stats)
    )


def unlocked_achievement_keys(stats: AchievementStats) -> tuple[str, ...]:
    """按目录顺序返回已解锁成就 key。"""
    return tuple(
        spec.key for spec in ACHIEVEMENTS if achievement_unlocked(spec, stats)
    )


def normalize_achievement_keys(value: object) -> tuple[str, ...]:
    """把任意输入过滤成已知成就 key，保持目录顺序且去重。"""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return ()
    provided = {item for item in value if isinstance(item, str)}
    return tuple(spec.key for spec in ACHIEVEMENTS if spec.key in provided)


def new_achievement_keys(
    before: Iterable[str], after: Iterable[str]
) -> tuple[str, ...]:
    """返回 after 相比 before 新增的成就 key（按目录顺序）。"""
    old = set(before)
    return tuple(
        key
        for key in normalize_achievement_keys(list(after))
        if key not in old
    )


def top_achievement_keys(
    keys: Iterable[str], limit: int = MATCH_MEDAL_LIMIT
) -> tuple[str, ...]:
    """挑选最稀有、且目录顺序靠后的成就用于对局展示。"""
    known = [
        ACHIEVEMENT_BY_KEY[key]
        for key in normalize_achievement_keys(list(keys))
    ]
    ordered = sorted(
        enumerate(known),
        key=lambda pair: (-pair[1].rarity, -pair[0]),
    )
    return tuple(spec.key for _index, spec in ordered[: max(0, limit)])


def achievement_by_key(value: object) -> Optional[AchievementSpec]:
    if isinstance(value, str):
        return ACHIEVEMENT_BY_KEY.get(value)
    return None


__all__ = [
    "METRIC_WINS",
    "METRIC_STREAK",
    "METRIC_PEAK_RATING",
    "TIER_COLORS",
    "AchievementSpec",
    "AchievementStats",
    "ACHIEVEMENTS",
    "ACHIEVEMENT_BY_KEY",
    "ACHIEVEMENT_KEYS",
    "ACHIEVEMENT_TOTAL",
    "MATCH_MEDAL_LIMIT",
    "stats_from_mapping",
    "achievement_value",
    "achievement_unlocked",
    "achievement_progress",
    "unlocked_achievements",
    "unlocked_achievement_keys",
    "normalize_achievement_keys",
    "new_achievement_keys",
    "top_achievement_keys",
    "achievement_by_key",
]
