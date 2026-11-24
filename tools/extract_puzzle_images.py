"""从残局图片提取 15×15 棋子坐标，生成题库候选与调试图。

依赖仅 PySide6（QImage 负责解码 WebP）与仓库内的共享规则模块。
输入目录、输出目录与识别阈值全部通过命令行参数指定，换机器也能直接运行：

    .\\.venv\\Scripts\\python.exe tools\\extract_puzzle_images.py --input <图片目录> --output <输出目录>

输出（默认写在脚本所在目录）：

    puzzle_extract_candidate.json   可直接替换 assets/puzzles/puzzles.json
    puzzle_extract_report.txt       每张图的 ASCII 棋盘与识别统计
    puzzle_extract_debug/*.png      在原图上标出识别到的棋子，便于人工核对

说明：
* 棋盘固定 15×15，图片坐标按 5.56% 边距、14 格等分推算；
* 只识别黑白棋子；褐色/红色标记会被忽略；
* 图像阈值与网格比例都收拢在具名 dataclass 中，阈值可用 ``--threshold-profile`` 切换；
* 生成 moves 时黑白交替任意排列——只要最终局面没有五连，任何中间顺序
  都不会提前五连（五连是棋子集合的性质，加子不会消除五连）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 共享规则统一从 game_model 取，避免与正在并行调整的 shared.rules 形成跨模块耦合。
from gomoku.shared.game_model import (  # noqa: E402
    BOARD_SIZE,
    GameModel,
    Move,
    Player,
    find_winning_line,
)

CANDIDATE_FILENAME = "puzzle_extract_candidate.json"
REPORT_FILENAME = "puzzle_extract_report.txt"
DEBUG_DIRNAME = "puzzle_extract_debug"

MAX_PLAYER_MOVES = 5
DEFENDER = "hard"

# 支持的图片后缀。
IMAGE_SUFFIXES = frozenset({".webp", ".png", ".jpg", ".jpeg"})


# ---------------------------------------------------------------------------
# 具名配置：原先散落在代码里的魔法数字都收拢到这里
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThresholdProfile:
    """识别黑白棋子时使用的亮度与饱和度阈值。

    字段：
        name: 配置名，同时作为命令行 ``--threshold-profile`` 的取值。
        black_max: 判定为黑子的亮度上限（越小越严格）。
        black_sat: 判定为黑子的饱和度上限（用于排除褐色木纹）。
        white_min: 判定为白子的亮度下限（越大越严格）。
        white_sat: 判定为白子的饱和度上限（用于排除褐色木纹）。
    """

    name: str
    black_max: float
    black_sat: float
    white_min: float
    white_sat: float


@dataclass(frozen=True)
class GridSpec:
    """棋盘网格线在图片中的相对位置。

    字段是相对图片宽/高的比例，因此不同分辨率的同类图片也能对齐。
    """

    x0_ratio: float
    x1_ratio: float
    y0_ratio: float
    y1_ratio: float


# 实测图片为 550×547，15 条网格线约在 x=7..542、y=10.5..544.5。
GRID_SPEC = GridSpec(
    x0_ratio=7.0 / 550.0,
    x1_ratio=542.0 / 550.0,
    y0_ratio=10.5 / 547.0,
    y1_ratio=544.5 / 547.0,
)

# 网格线是深棕色木纹：亮度低、饱和度高。
LINE_BRIGHTNESS_MAX = 150.0
LINE_SATURATION_MIN = 25.0

# 相邻网格线的合法像素间距区间，以及间距稳定度（变异系数）上限。
MIN_LINE_SPACING = 25.0
MAX_LINE_SPACING = 50.0
MAX_SPACING_CV = 0.12

# 扫描网格线时尝试的取样比例；这些比例落在相邻网格线之间，避免扫到线本身。
GRID_SCAN_RATIOS = (0.035, 0.05, 0.07, 0.09)

# 网格横纵跨度之比的合法区间（棋盘应当近似正方形）。
MIN_GRID_SPAN_RATIO = 0.85
MAX_GRID_SPAN_RATIO = 1.15

# 判定为空交叉点时，采样圆盘里允许的最大深色占比与最小白色占比。
BLACK_FRACTION_MIN = 0.42
WHITE_FRACTION_MIN = 0.50

# 采样圆盘半径相对格距的比例。
SAMPLE_RADIUS_RATIO = 0.28

# 右下角水印区域不参与识别。
WATERMARK_MIN_ROW = 13
WATERMARK_MIN_COL = 12

# 黑白棋子的阈值配置：名称 → 配置。
_THRESHOLD_PROFILES: dict[str, ThresholdProfile] = {
    # 与原始实现一致：白子约 120-250，黑子 <100，木纹饱和度高。
    "standard": ThresholdProfile(
        name="standard",
        black_max=110.0,
        black_sat=45.0,
        white_min=115.0,
        white_sat=35.0,
    ),
    # 更严格：要求白子更亮、黑子更暗，适合黑白分明的扫描件。
    "high_contrast": ThresholdProfile(
        name="high_contrast",
        black_max=95.0,
        black_sat=40.0,
        white_min=180.0,
        white_sat=25.0,
    ),
    # 更宽松：容忍发灰/泛黄的棋子，适合对比度低的手机拍照。
    "low_contrast": ThresholdProfile(
        name="low_contrast",
        black_max=140.0,
        black_sat=60.0,
        white_min=150.0,
        white_sat=55.0,
    ),
}

# ``--threshold-profile auto`` 表示依次尝试全部配置，取识别棋子最多的一组。
AUTO_PROFILE_NAME = "auto"

IDIOM_NAMES = [
    "白虹贯日",
    "双管齐下",
    "暗度陈仓",
    "声东击西",
    "黑云压城",
    "白驹过隙",
    "一鸣惊人",
    "柳暗花明",
    "峰回路转",
    "白璧无瑕",
    "黑马当先",
]

# 自动识别后按调试图人工核对出的少量补正（坐标已对照原图）。
MANUAL_CORRECTIONS: dict[str, dict[str, list[tuple[int, int]]]] = {
    # 右边缘两颗黑子的大号白字导致自动识别为白子。
    "黑先5": {"black": [(6, 14), (7, 14)]},
}

# 带数字标签的题库图无法用边缘扫描得到网格；这里记录人工核对的 15 条线。
GRID_OVERRIDES: dict[str, tuple[list[float], list[float]]] = {
    "白先9": (
        [30.0, 65.5, 100.0, 135.5, 170.5, 205.5, 240.0, 275.5, 310.0, 345.5, 381.0, 416.0, 450.5, 485.5, 520.5],
        [27.5, 61.5, 95.5, 129.0, 162.0, 196.0, 230.5, 265.0, 298.0, 331.0, 365.0, 398.5, 432.5, 466.5, 500.0],
    ),
}

# 用户实测后指定的每关限手数；未列出的关卡使用默认值。
PUZZLE_LIMITS: dict[str, int] = {
    "白虹贯日": 4,
    "双管齐下": 6,
    "黑云压城": 6,
    "一鸣惊人": 6,
    "柳暗花明": 5,
    "峰回路转": 6,
    "白璧无瑕": 5,
}


# ---------------------------------------------------------------------------
# 结果容器
# ---------------------------------------------------------------------------


@dataclass
class ExtractionResult:
    """一次批量识别的结果：可用残局、逐图报告与已处理图片数。"""

    puzzles: list[dict[str, Any]]
    report: list[str]
    processed: int


@dataclass(frozen=True)
class ExtractionOutputs:
    """一次批量识别写出的文件位置。"""

    candidate: Path
    report: Path
    debug: Path


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def in_bounds(x: float, y: float, width: int, height: int) -> bool:
    """判断像素坐标是否落在图片范围内。

    模块内所有越界判断都走这里，避免同一套坐标校验散落在多处。
    """
    return 0 <= x < width and 0 <= y < height


def threshold_profiles() -> dict[str, ThresholdProfile]:
    """返回全部黑白识别阈值配置（名称 → 配置）。"""
    return dict(_THRESHOLD_PROFILES)


def resolve_profiles(name: str) -> list[ThresholdProfile]:
    """把 ``--threshold-profile`` 的取值解析成待尝试的阈值配置列表。

    ``auto`` 表示依次尝试全部配置；其余取值表示只使用指定的那一组。
    """
    if name == AUTO_PROFILE_NAME:
        return list(_THRESHOLD_PROFILES.values())
    profile = _THRESHOLD_PROFILES.get(name)
    if profile is None:
        raise ValueError(f"未知的阈值配置：{name}")
    return [profile]


def apply_corrections(
    stem: str, stones: dict[tuple[int, int], str]
) -> dict[tuple[int, int], str]:
    corrected = dict(stones)
    spec = MANUAL_CORRECTIONS.get(stem, {})
    for cell in spec.get("remove_black", []):
        if corrected.get(cell) == "black":
            corrected.pop(cell, None)
    for color in ("black", "white"):
        for cell in spec.get(color, []):
            corrected[cell] = color
    return corrected


def natural_key(path: Path) -> int:
    match = re.search(r"(\d+)", path.stem)
    return int(match.group(1)) if match else 10**6


def side_from_name(stem: str) -> Player:
    if "白先" in stem:
        return Player.WHITE
    if "黑先" in stem or "黑线" in stem:
        return Player.BLACK
    raise ValueError(f"无法从文件名判断先后手：{stem}")


# ---------------------------------------------------------------------------
# 网格识别
# ---------------------------------------------------------------------------


def _cluster_positions(values: list[int]) -> list[float]:
    groups: list[list[int]] = []
    for value in values:
        if groups and value - groups[-1][-1] <= 2:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _clamp_scan_index(value: int, limit: int) -> int:
    """把扫描线位置限制在图片内部（避开最外侧一两像素）。"""
    if limit <= 3:
        return max(0, limit - 1)
    return max(1, min(limit - 2, value))


def _is_grid_line_color(color: QColor) -> bool:
    """判断颜色是否是深色木纹（网格线）。"""
    r, g, b = color.red(), color.green(), color.blue()
    brightness = (r + g + b) / 3.0
    saturation = max(r, g, b) - min(r, g, b)
    return brightness < LINE_BRIGHTNESS_MAX and saturation > LINE_SATURATION_MIN


def _scan_line_positions(
    image: QImage, *, horizontal: bool, ratio: float
) -> list[float]:
    """沿一条水平或垂直扫描线找出网格线的候选位置。

    命中的像素按 2 像素的间隔聚类，得到一组近似等距的候选线位置。
    """
    width, height = image.width(), image.height()
    if horizontal:
        fixed = _clamp_scan_index(int(round(height * ratio)), height)
    else:
        fixed = _clamp_scan_index(int(round(width * ratio)), width)
    hits: list[int] = []
    for index in range(width if horizontal else height):
        x, y = (index, fixed) if horizontal else (fixed, index)
        if not in_bounds(x, y, width, height):
            continue
        if _is_grid_line_color(image.pixelColor(x, y)):
            hits.append(index)
    return _cluster_positions(hits)


def _best_grid_sequence(positions: list[float]) -> list[float] | None:
    """从扫描到的候选线中取间距最稳定的连续 15 条。"""
    if len(positions) < BOARD_SIZE:
        return None
    best: list[float] | None = None
    best_key: tuple[float, float] | None = None
    for start in range(len(positions) - BOARD_SIZE + 1):
        window = positions[start : start + BOARD_SIZE]
        diffs = [b - a for a, b in zip(window, window[1:])]
        if min(diffs) < MIN_LINE_SPACING or max(diffs) > MAX_LINE_SPACING:
            continue
        mean = sum(diffs) / len(diffs)
        variance = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        cv = (variance**0.5) / mean
        key = (cv, -(window[-1] - window[0]))
        if best_key is None or key < best_key:
            best_key = key
            best = window
    if best is None or best_key is None or best_key[0] > MAX_SPACING_CV:
        return None
    return best


def detect_margin_grid(image: QImage) -> tuple[list[float], list[float]] | None:
    """从图片边缘的空白扫描出 15 条网格线；带数字标签的图可能失败。"""
    for ratio in GRID_SCAN_RATIOS:
        xs = _best_grid_sequence(
            _scan_line_positions(image, horizontal=True, ratio=ratio)
        )
        if xs is None:
            continue
        ys = _best_grid_sequence(
            _scan_line_positions(image, horizontal=False, ratio=ratio)
        )
        if ys is None:
            continue
        x_span = xs[-1] - xs[0]
        y_span = ys[-1] - ys[0]
        if not (
            MIN_GRID_SPAN_RATIO
            <= x_span / max(1.0, y_span)
            <= MAX_GRID_SPAN_RATIO
        ):
            continue
        return xs, ys
    return None


def board_geometry(
    width: int, height: int, spec: GridSpec = GRID_SPEC
) -> tuple[float, float, float, float, float, float]:
    """按图片尺寸与网格比例推算棋盘的几何信息。"""
    x0 = width * spec.x0_ratio
    x1 = width * spec.x1_ratio
    y0 = height * spec.y0_ratio
    y1 = height * spec.y1_ratio
    spacing_x = (x1 - x0) / (BOARD_SIZE - 1)
    spacing_y = (y1 - y0) / (BOARD_SIZE - 1)
    return x0, y0, spacing_x, spacing_y, x1, y1


# ---------------------------------------------------------------------------
# 棋子识别
# ---------------------------------------------------------------------------


def classify_intersection(
    image: QImage,
    cx: float,
    cy: float,
    spacing: float,
    bounds: tuple[float, float, float, float],
    profile: ThresholdProfile,
) -> str | None:
    """返回 'black' / 'white' / None（空位或标记）。

    以交叉点为中心取半径约 0.28 格的圆盘，用像素占比判断颜色。圆盘完全落在
    棋子内部，既能避开黑子外侧的木纹，也能让中心的大号数字被棋子面积稀释。
    """
    radius = max(4, int(round(spacing * SAMPLE_RADIUS_RATIO)))
    dark_pixels = 0
    white_pixels = 0
    total_pixels = 0
    width, height = image.width(), image.height()
    left, top, right, bottom = bounds

    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            x = int(round(cx + dx))
            y = int(round(cy + dy))
            if not in_bounds(x, y, width, height):
                continue
            # 外框线是深棕色，若把边线样本算进来会被误判为黑子；
            # 只在棋盘内侧采样，边线上的棋子仍能命中其内侧一半。
            if not (left <= x <= right and top <= y <= bottom):
                continue
            color = image.pixelColor(x, y)
            r, g, b = color.red(), color.green(), color.blue()
            brightness = (r + g + b) / 3.0
            saturation = max(r, g, b) - min(r, g, b)
            total_pixels += 1
            if brightness < profile.black_max and saturation < profile.black_sat:
                dark_pixels += 1
            elif brightness > profile.white_min and saturation < profile.white_sat:
                white_pixels += 1

    if total_pixels == 0:
        return None
    dark_fraction = dark_pixels / total_pixels
    white_fraction = white_pixels / total_pixels

    # 黑子上的白色数字/白子上的黑色数字只占圆盘一小部分，用占比判定
    # 比中位数更稳；木纹和褐色标记既不够暗也不够白。
    if dark_fraction >= BLACK_FRACTION_MIN and dark_fraction >= white_fraction:
        return "black"
    if white_fraction >= WHITE_FRACTION_MIN:
        return "white"
    return None


def detect_stones(
    image: QImage,
    profile: ThresholdProfile,
    grid_override: tuple[list[float], list[float]] | None = None,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """识别图片中的黑白棋子，返回棋子表与棋盘几何信息。"""
    if grid_override is not None:
        xs, ys = grid_override
        if len(xs) != BOARD_SIZE or len(ys) != BOARD_SIZE:
            raise ValueError(
                f"网格覆盖必须各提供 {BOARD_SIZE} 条线，"
                f"实际为 {len(xs)} / {len(ys)}"
            )
        x_positions = list(xs)
        y_positions = list(ys)
        x0, x1 = xs[0], xs[-1]
        y0, y1 = ys[0], ys[-1]
    else:
        margin_grid = detect_margin_grid(image)
        if margin_grid is not None:
            xs, ys = margin_grid
            x_positions = list(xs)
            y_positions = list(ys)
            x0, x1 = xs[0], xs[-1]
            y0, y1 = ys[0], ys[-1]
        else:
            x0, y0, spacing_x, spacing_y, x1, y1 = board_geometry(
                image.width(), image.height()
            )
            x_positions = [
                x0 + col * spacing_x for col in range(BOARD_SIZE)
            ]
            y_positions = [
                y0 + row * spacing_y for row in range(BOARD_SIZE)
            ]

    spacing_x = (x1 - x0) / (BOARD_SIZE - 1)
    spacing_y = (y1 - y0) / (BOARD_SIZE - 1)
    spacing = (spacing_x + spacing_y) / 2.0
    bounds = (x0 + 2.0, y0 + 2.0, x1 - 2.0, y1 - 2.0)
    stones: dict[tuple[int, int], str] = {}
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            # 右下角水印会污染边线样本；残局棋子不会落在这一小块。
            if row >= WATERMARK_MIN_ROW and col >= WATERMARK_MIN_COL:
                continue
            cx = x_positions[col]
            cy = y_positions[row]
            color = classify_intersection(
                image,
                cx,
                cy,
                spacing,
                bounds,
                profile,
            )
            if color is not None:
                stones[(row, col)] = color
    return stones, (x0, y0, spacing_x, spacing_y, x1, y1)


def detect_with_count_check(
    image: QImage,
    side: Player,
    grid_override: tuple[list[float], list[float]] | None = None,
    profiles: Sequence[ThresholdProfile] | None = None,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """尝试多组阈值，取识别棋子最多的一组；数量合法时优先。

    没有任何阈值配置可用时抛出 :class:`RuntimeError`——不能用 ``assert``，
    否则 ``python -O`` 会把这条校验直接优化掉。
    """
    candidates = list(profiles) if profiles is not None else list(
        _THRESHOLD_PROFILES.values()
    )
    best: tuple[
        dict[tuple[int, int], str],
        tuple[float, float, float, float, float, float],
    ] | None = None
    best_total = -1
    best_valid = False
    for profile in candidates:
        stones, geometry = detect_stones(
            image,
            profile,
            grid_override=grid_override,
        )
        blacks = sum(1 for color in stones.values() if color == "black")
        whites = sum(1 for color in stones.values() if color == "white")
        expected_black = whites + (1 if side is Player.WHITE else 0)
        valid = blacks == expected_black
        total = len(stones)
        if total > best_total or (
            total == best_total and valid and not best_valid
        ):
            best = (stones, geometry)
            best_total = total
            best_valid = valid
    if best is None:
        raise RuntimeError("没有可用的黑白识别阈值配置，无法完成识别")
    return best


# ---------------------------------------------------------------------------
# 结果生成
# ---------------------------------------------------------------------------


def ascii_board(stones: dict[tuple[int, int], str]) -> str:
    lines = ["    " + " ".join(f"{col:2d}" for col in range(BOARD_SIZE))]
    for row in range(BOARD_SIZE):
        cells = []
        for col in range(BOARD_SIZE):
            color = stones.get((row, col))
            cells.append(" B" if color == "black" else " W" if color == "white" else " .")
        lines.append(f"{row:2d}  " + " ".join(cells))
    return "\n".join(lines)


def has_five(stones: dict[tuple[int, int], str], color: str) -> bool:
    board = [
        [Player.BLACK if stones.get((r, c)) == "black"
         else Player.WHITE if stones.get((r, c)) == "white"
         else None for c in range(BOARD_SIZE)]
        for r in range(BOARD_SIZE)
    ]
    for (row, col), value in stones.items():
        if value != color:
            continue
        if find_winning_line(
            board,
            row,
            col,
            Player.BLACK if color == "black" else Player.WHITE,
        ):
            return True
    return False


def build_moves(
    blacks: list[tuple[int, int]],
    whites: list[tuple[int, int]],
) -> list[list[int]]:
    moves: list[list[int]] = []
    for index in range(max(len(blacks), len(whites))):
        if index < len(blacks):
            moves.append([blacks[index][0], blacks[index][1]])
        if index < len(whites):
            moves.append([whites[index][0], whites[index][1]])
    return moves


def draw_debug(
    image: QImage,
    stones: dict[tuple[int, int], str],
    geometry: tuple[float, float, float, float, float, float],
    output: Path,
) -> None:
    canvas = image.convertToFormat(QImage.Format.Format_RGB32)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    x0, y0, spacing_x, spacing_y, _x1, _y1 = geometry
    for (row, col), color in stones.items():
        cx = x0 + col * spacing_x
        cy = y0 + row * spacing_y
        pen = QPen(QColor(0, 210, 90) if color == "black" else QColor(30, 90, 255), 2)
        painter.setPen(pen)
        painter.drawEllipse(QPointF(cx, cy), spacing_x * 0.42, spacing_y * 0.42)
        painter.drawText(
            QPointF(cx + spacing_x * 0.32, cy - spacing_y * 0.32),
            f"{row},{col}",
        )
    painter.end()
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(str(output))


# ---------------------------------------------------------------------------
# 批量提取
# ---------------------------------------------------------------------------


def collect_images(input_dir: Path) -> list[Path]:
    """列出目录下所有受支持的图片，按文件名里的数字排序。"""
    paths = sorted(input_dir.glob("*.*"), key=natural_key)
    return [path for path in paths if path.suffix.lower() in IMAGE_SUFFIXES]


def extract_puzzles(
    paths: Sequence[Path],
    profiles: Sequence[ThresholdProfile],
    debug_dir: Path | None = None,
) -> ExtractionResult:
    """逐张识别图片，生成可用的残局题库条目与逐图报告。"""
    puzzles: list[dict[str, Any]] = []
    report: list[str] = []
    processed = 0

    for index, path in enumerate(paths):
        stem = path.stem
        processed += 1
        try:
            side = side_from_name(stem)
        except ValueError as exc:
            report.append(f"{stem}: {exc}，已跳过。")
            continue
        image = QImage(str(path))
        if image.isNull():
            report.append(f"{stem}: 图片解码失败")
            continue

        stones, geometry = detect_with_count_check(
            image, side, GRID_OVERRIDES.get(stem), profiles
        )
        stones = apply_corrections(stem, stones)
        blacks = sorted(pos for pos, color in stones.items() if color == "black")
        whites = sorted(pos for pos, color in stones.items() if color == "white")
        side_text = "白先" if side is Player.WHITE else "黑先"
        expected_black = len(whites) + (1 if side is Player.WHITE else 0)
        count_ok = len(blacks) == expected_black

        report.append(
            f"\n=== {stem} ({side_text}) ===\n"
            f"black={len(blacks)} white={len(whites)} "
            f"expected_black={expected_black} {'OK' if count_ok else 'COUNT-MISMATCH'}\n"
            f"{ascii_board(stones)}\n"
        )
        if debug_dir is not None:
            draw_debug(
                image,
                stones,
                geometry,
                debug_dir / f"{index + 1:02d}_{stem}.png",
            )

        if not count_ok:
            report.append("跳过：黑白数量与先后手不一致，需调整识别或原图。")
            continue
        if has_five(stones, "black") or has_five(stones, "white"):
            report.append("跳过：最终局面已有五连。")
            continue

        moves = build_moves(blacks, whites)
        try:
            model = GameModel.from_moves(
                [Move(row, col, Player.BLACK if i % 2 == 0 else Player.WHITE)
                 for i, (row, col) in enumerate(moves)]
            )
        except ValueError as exc:
            report.append(f"跳过：无法生成合法历史（{exc}）。")
            continue
        if model.is_game_over:
            report.append("跳过：生成历史提前终局。")
            continue

        title = IDIOM_NAMES[index] if index < len(IDIOM_NAMES) else f"残局{index + 1}"
        limit = PUZZLE_LIMITS.get(title, MAX_PLAYER_MOVES)
        puzzles.append(
            {
                "id": f"p{index + 1:03d}",
                "title": title,
                "difficulty": "medium",
                "description": f"{side_text}残局，限{limit}手内连成五子。",
                "hint": "寻找连续冲四或双活三的次序。",
                "moves": moves,
                "to_move": "white" if side is Player.WHITE else "black",
                "max_player_moves": limit,
                "defender": DEFENDER,
            }
        )

    return ExtractionResult(puzzles=puzzles, report=report, processed=processed)


def write_outputs(
    output_dir: Path, result: ExtractionResult
) -> ExtractionOutputs:
    """把候选题库与识别报告写入输出目录。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / CANDIDATE_FILENAME
    report_path = output_dir / REPORT_FILENAME
    debug_path = output_dir / DEBUG_DIRNAME
    candidate_path.write_text(
        json.dumps(
            {"version": 1, "puzzles": result.puzzles},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    report_path.write_text("\n".join(result.report), encoding="utf-8")
    return ExtractionOutputs(
        candidate=candidate_path, report=report_path, debug=debug_path
    )


# ---------------------------------------------------------------------------
# 命令行入口
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        prog="extract_puzzle_images",
        description="从残局图片提取 15×15 棋子坐标，生成题库候选与调试图。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="存放残局图片的目录（支持 png/jpg/jpeg/webp）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="候选题库、识别报告与调试图的输出目录",
    )
    parser.add_argument(
        "--threshold-profile",
        choices=[AUTO_PROFILE_NAME, *sorted(_THRESHOLD_PROFILES)],
        default=AUTO_PROFILE_NAME,
        help="黑白识别阈值配置；auto 表示依次尝试全部配置并取识别棋子最多的一组",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行入口：识别残局图片并生成候选题库。"""
    args = build_parser().parse_args(argv)

    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app

    input_dir = args.input.expanduser()
    output_dir = args.output.expanduser()
    profiles = resolve_profiles(args.threshold_profile)

    if not input_dir.is_dir():
        print(f"找不到图片目录：{input_dir}")
        return 1

    paths = collect_images(input_dir)
    if not paths:
        print(f"目录中没有图片：{input_dir}")
        return 1

    debug_dir = output_dir / DEBUG_DIRNAME
    result = extract_puzzles(paths, profiles, debug_dir)
    outputs = write_outputs(output_dir, result)

    print(f"识别到 {len(result.puzzles)} / {result.processed} 个可用残局")
    print(f"候选题库：{outputs.candidate}")
    print(f"识别报告：{outputs.report}")
    print(f"调试图目录：{outputs.debug}")
    if len(result.puzzles) != result.processed:
        print("有图片未通过数量/五连检查，请查看报告和调试图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
