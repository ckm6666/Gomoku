"""从残局图片提取 15×15 棋子坐标，生成题库候选与调试图。

依赖仅 PySide6（QImage 负责解码 WebP）与仓库内的共享规则模块。
输入目录、输出目录、识别阈值与合并基线全部通过命令行参数指定，
换机器也能直接运行：

    .\\.venv\\Scripts\\python.exe tools\\extract_puzzle_images.py --input <图片目录> --output <输出目录>

输出（默认写在脚本所在目录）：

    puzzle_extract_candidate.json   可直接替换 assets/puzzles/puzzles.json
    puzzle_extract_report.txt       每张图的 ASCII 棋盘与识别统计
    puzzle_extract_debug/*.png      在原图上标出识别到的棋子，便于人工核对

带 ``--merge <现有题库>`` 时，候选 JSON 会与现有题库按标题合并：同名标题
就地更新、其余条目原样保留，源图已缺失的关卡不会被新一次提取抹掉。

说明：
* 棋盘固定 15×15，网格有两条来源：边缘扫描（深色木纹网格线）与暗像素投影
  拟合（浅灰细线、格距大的新版渲染图）；两者都试，取黑白数量对得上的那组；
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

# 投影拟合：暗像素阈值 = 整图灰度中位数 × 该系数。棋盘底色明显高于阈值，
# 网格线与棋子低于阈值，于是"每列（行）有多少暗像素"就会在网格线上形成尖峰。
DARK_THRESHOLD_RATIO = 0.72

# 等距拟合允许的格距区间（相对图片宽/高）以及步长精度（0.1 像素）。
FIT_MIN_SPACING_RATIO = 1.0 / 22.0
FIT_MAX_SPACING_RATIO = 1.0 / 11.0
FIT_STEP_PRECISION = 10

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

# 图片文件名 → 成语标题。标题与图片绑定，目录里增删图片不会让已有标题错位。
PUZZLE_TITLES: dict[str, str] = {
    "白先1": "白虹贯日",
    "白先2": "双管齐下",
    "白先3": "暗度陈仓",
    "白先4": "声东击西",
    "黑先5": "黑云压城",
    "白先6": "白驹过隙",
    "黑先7": "一鸣惊人",
    "白先8": "柳暗花明",
    "白先9": "峰回路转",
    "黑先10": "势如破竹",
    "黑先11": "围魏救赵",
    "黑先12": "釜底抽薪",
    "黑先13": "调虎离山",
    "黑先14": "顺手牵羊",
    "黑先15": "欲擒故纵",
    "黑先16": "瞒天过海",
    "黑先17": "笑里藏刀",
    "黑先18": "以逸待劳",
    "黑先19": "各个击破",
    "黑先20": "步步为营",
    "黑先21": "草船借箭",
    "黑先22": "偷梁换柱",
    "黑先23": "关门捉贼",
    "黑先24": "破釜沉舟",
}

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

# 每关限手数：先手方须在限手数内连成五子。
# 旧关卡由用户实测确定；新关卡按"大师 AI 执先手对阵困难 AI 防守"实测出的
# 成五手数与强制胜线路长度取较宽的一档再加一手余量，并夹在 4～6 手之间。
# 调整限手数只需改这里，重新提取即可。
PUZZLE_LIMITS: dict[str, int] = {
    "白虹贯日": 4,
    "双管齐下": 6,
    "黑云压城": 6,
    "一鸣惊人": 6,
    "柳暗花明": 5,
    "峰回路转": 6,
    "白璧无瑕": 5,
    "势如破竹": 5,
    "围魏救赵": 6,
    "釜底抽薪": 5,
    "调虎离山": 5,
    "顺手牵羊": 5,
    "欲擒故纵": 6,
    "瞒天过海": 6,
    "笑里藏刀": 5,
    "以逸待劳": 5,
    "各个击破": 5,
    "步步为营": 5,
    "草船借箭": 6,
    "偷梁换柱": 5,
    "关门捉贼": 6,
    "破釜沉舟": 6,
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
# 投影拟合网格
# ---------------------------------------------------------------------------


def grayscale_plane(image: QImage) -> tuple[bytes, int, int, int]:
    """把图片转成 8 位灰度，返回 (灰度数据, 宽, 高, 每行字节数)。

    直接取灰度字节比逐像素读 QColor 快一个数量级，1280×1280 的图只需几十毫秒。
    """
    gray = image.convertToFormat(QImage.Format.Format_Grayscale8)
    width, height = gray.width(), gray.height()
    stride = gray.bytesPerLine()
    return bytes(gray.constBits()), width, height, stride


def median_brightness(data: bytes) -> float:
    """灰度数据的中位数亮度（用 256 级直方图求，避免整体排序）。"""
    histogram = [0] * 256
    for value in data:
        histogram[value] += 1
    accumulated = 0
    half = len(data) // 2
    for value, count in enumerate(histogram):
        accumulated += count
        if accumulated > half:
            return float(value)
    return 127.5


def dark_table(threshold: float) -> bytes:
    """构造查表：暗于阈值的像素记 1，其余记 0。"""
    return bytes(1 if value < threshold else 0 for value in range(256))


def dark_counts(
    data: bytes,
    width: int,
    height: int,
    stride: int,
    table: bytes,
    *,
    horizontal: bool,
) -> list[int]:
    """统计每行（horizontal=True）或每列中的暗像素数量。"""
    counts = [0] * (height if horizontal else width)
    for y in range(height):
        row = data[y * stride : y * stride + width].translate(table)
        if horizontal:
            counts[y] = sum(row)
        else:
            for x, value in enumerate(row):
                if value:
                    counts[x] += 1
    return counts


def fit_uniform_lines(counts: Sequence[int], dimension: int) -> list[float]:
    """在暗像素投影上拟合 :data:`BOARD_SIZE` 条等距网格线。

    网格线贯穿整块棋盘，对应的投影计数远高于棋子（棋子只占一格高），
    因此"偏移 + 格距"的等距模型能稳定命中真实网格；搜索时取计数总和最大的一组。
    """
    low = max(
        4, int(dimension * FIT_MIN_SPACING_RATIO * FIT_STEP_PRECISION)
    )
    high = max(
        low + 1, int(dimension * FIT_MAX_SPACING_RATIO * FIT_STEP_PRECISION)
    )
    best_positions: list[float] = []
    best_score = -1.0
    for step_tenths in range(low, high + 1):
        step = step_tenths / FIT_STEP_PRECISION
        for offset in range(int(step)):
            last = offset + (BOARD_SIZE - 1) * step
            if last >= dimension:
                break
            score = 0.0
            for index in range(BOARD_SIZE):
                position = int(round(offset + index * step))
                if 0 <= position < len(counts):
                    score += counts[position]
            if score > best_score:
                best_score = score
                best_positions = [
                    offset + index * step for index in range(BOARD_SIZE)
                ]
    return best_positions


def detect_projection_grid(image: QImage) -> tuple[list[float], list[float]] | None:
    """用暗像素投影拟合网格，适用于网格线细、格距大的新版渲染图。

    边缘扫描依赖深棕色木纹网格线，对浅灰细线的渲染图会失效；
    投影拟合只看"哪一列（行）整条都是暗的"，与线的颜色无关。
    """
    data, width, height, stride = grayscale_plane(image)
    if not data:
        return None
    threshold = median_brightness(data) * DARK_THRESHOLD_RATIO
    table = dark_table(threshold)
    columns = dark_counts(
        data, width, height, stride, table, horizontal=False
    )
    rows = dark_counts(data, width, height, stride, table, horizontal=True)
    xs = fit_uniform_lines(columns, width)
    ys = fit_uniform_lines(rows, height)
    if not xs or not ys:
        return None
    x_span = xs[-1] - xs[0]
    y_span = ys[-1] - ys[0]
    if not (
        MIN_GRID_SPAN_RATIO <= x_span / max(1.0, y_span) <= MAX_GRID_SPAN_RATIO
    ):
        return None
    return xs, ys



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
    grid: tuple[list[float], list[float]] | None = None,
    exclude_watermark: bool = True,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """识别图片中的黑白棋子，返回棋子表与棋盘几何信息。

    网格来源按优先级取：``grid_override``（人工指定的 15 条线）> ``grid``
    （调用方给的候选网格）> 边缘扫描 > 按固定边距推算。
    ``exclude_watermark`` 控制是否跳过右下角水印区域。
    """
    if grid_override is not None:
        xs, ys = grid_override
    elif grid is not None:
        xs, ys = grid
    else:
        margin_grid = detect_margin_grid(image)
        if margin_grid is not None:
            xs, ys = margin_grid
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
            return _classify_board(
                image,
                profile,
                x_positions,
                y_positions,
                (x0, x1),
                (y0, y1),
                exclude_watermark,
            )

    if len(xs) != BOARD_SIZE or len(ys) != BOARD_SIZE:
        raise ValueError(
            f"网格覆盖必须各提供 {BOARD_SIZE} 条线，"
            f"实际为 {len(xs)} / {len(ys)}"
        )
    x_positions = list(xs)
    y_positions = list(ys)
    return _classify_board(
        image,
        profile,
        x_positions,
        y_positions,
        (xs[0], xs[-1]),
        (ys[0], ys[-1]),
        exclude_watermark,
    )


def _classify_board(
    image: QImage,
    profile: ThresholdProfile,
    x_positions: list[float],
    y_positions: list[float],
    x_bounds: tuple[float, float],
    y_bounds: tuple[float, float],
    exclude_watermark: bool = True,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """按给定网格线逐点判定黑白棋子。"""
    x0, x1 = x_bounds
    y0, y1 = y_bounds
    spacing_x = (x1 - x0) / (BOARD_SIZE - 1)
    spacing_y = (y1 - y0) / (BOARD_SIZE - 1)
    spacing = (spacing_x + spacing_y) / 2.0
    bounds = (x0 + 2.0, y0 + 2.0, x1 - 2.0, y1 - 2.0)
    stones: dict[tuple[int, int], str] = {}
    for row in range(BOARD_SIZE):
        for col in range(BOARD_SIZE):
            # 右下角水印会污染边线样本；旧版题库图的水印在这一小块。
            if (
                exclude_watermark
                and row >= WATERMARK_MIN_ROW
                and col >= WATERMARK_MIN_COL
            ):
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


def grid_candidates(
    image: QImage,
    grid_override: tuple[list[float], list[float]] | None = None,
) -> list[tuple[list[float], list[float]]]:
    """列出所有可能的网格：人工指定的线、边缘扫描结果与投影拟合结果。"""
    if grid_override is not None:
        return [grid_override]
    candidates: list[tuple[list[float], list[float]]] = []
    margin_grid = detect_margin_grid(image)
    if margin_grid is not None:
        candidates.append(margin_grid)
    projection_grid = detect_projection_grid(image)
    if projection_grid is not None:
        candidates.append(projection_grid)
    return candidates


def _best_detection(
    image: QImage,
    side: Player,
    grids: Sequence[tuple[list[float], list[float]]],
    profiles: Sequence[ThresholdProfile],
    exclude_watermark: bool,
) -> tuple[bool, dict[tuple[int, int], str], tuple[float, ...]] | None:
    """在给定网格与阈值组合里挑出黑白数量最合法的一组。"""
    best: tuple[
        bool,
        dict[tuple[int, int], str],
        tuple[float, float, float, float, float, float],
    ] | None = None
    best_key: tuple[bool, int] | None = None
    for grid in grids:
        for profile in profiles:
            stones, geometry = detect_stones(
                image,
                profile,
                grid=grid,
                exclude_watermark=exclude_watermark,
            )
            blacks = sum(1 for color in stones.values() if color == "black")
            whites = sum(1 for color in stones.values() if color == "white")
            expected_black = whites + (1 if side is Player.WHITE else 0)
            key = (blacks == expected_black, len(stones))
            if best_key is None or key > best_key:
                best = (key[0], stones, geometry)
                best_key = key
    if best is None:
        return None
    return best


def detect_with_count_check(
    image: QImage,
    side: Player,
    grid_override: tuple[list[float], list[float]] | None = None,
    profiles: Sequence[ThresholdProfile] | None = None,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """尝试多组网格与多组阈值，取黑白数量合法、且识别棋子最多的一组。

    数量合法性优先于棋子数量：数量对得上说明网格与图片对上了，
    比"多认出几颗子"更可靠。右下角水印区域先按"排除"处理；只有在排除后
    数量仍对不上时，才把该区域算进来再试一次——新版渲染图没有水印，
    那一格里可能放着真正的棋子。

    没有任何阈值配置可用时抛出 :class:`RuntimeError`——不能用 ``assert``，
    否则 ``python -O`` 会把这条校验直接优化掉。
    """
    candidates = list(profiles) if profiles is not None else list(
        _THRESHOLD_PROFILES.values()
    )
    grids = grid_candidates(image, grid_override)
    best = _best_detection(image, side, grids, candidates, True)
    if best is None:
        raise RuntimeError("没有可用的黑白识别阈值配置，无法完成识别")
    if best[0]:
        return best[1], best[2]

    fallback = _best_detection(image, side, grids, candidates, False)
    if fallback is not None and fallback[0]:
        return fallback[1], fallback[2]
    return best[1], best[2]



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

        title = PUZZLE_TITLES.get(stem) or f"残局{index + 1}"
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


# ---------------------------------------------------------------------------
# 题库合并
# ---------------------------------------------------------------------------


def load_bank_entries(path: Path) -> list[dict[str, Any]]:
    """读取现有题库；文件缺失或结构不对时返回空列表。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return []
    if not isinstance(raw, dict):
        return []
    items = raw.get("puzzles")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def next_puzzle_id(entries: Sequence[dict[str, Any]]) -> str:
    """按现有 id 推断下一个可用 id（形如 ``p012``）。"""
    highest = 0
    for entry in entries:
        raw = str(entry.get("id", ""))
        if raw.startswith("p") and raw[1:].isdigit():
            highest = max(highest, int(raw[1:]))
    return f"p{highest + 1:03d}"


def merge_bank(
    base_entries: Sequence[dict[str, Any]],
    generated_entries: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    """按标题合并题库。

    同名标题就地更新（保留原有 id），新标题追加到末尾并分配新 id，
    基线里没能重新识别出来的关卡（例如源图已缺失）原样保留。
    """
    merged = [dict(entry) for entry in base_entries]
    position_by_title: dict[str, int] = {}
    for position, entry in enumerate(merged):
        title = entry.get("title")
        if isinstance(title, str) and title not in position_by_title:
            position_by_title[title] = position

    for generated in generated_entries:
        title = generated.get("title")
        entry = dict(generated)
        if not isinstance(title, str) or title not in position_by_title:
            entry["id"] = next_puzzle_id(merged)
            merged.append(entry)
            if isinstance(title, str):
                position_by_title[title] = len(merged) - 1
            continue
        position = position_by_title[title]
        entry["id"] = merged[position].get("id", entry.get("id"))
        merged[position] = entry
    return merged


def write_outputs(
    output_dir: Path,
    result: ExtractionResult,
    base_entries: Sequence[dict[str, Any]] | None = None,
) -> ExtractionOutputs:
    """把候选题库与识别报告写入输出目录。

    给了 ``base_entries`` 就先按标题合并，这样源图已缺失的关卡不会丢失。
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = output_dir / CANDIDATE_FILENAME
    report_path = output_dir / REPORT_FILENAME
    debug_path = output_dir / DEBUG_DIRNAME
    puzzles = (
        merge_bank(base_entries, result.puzzles)
        if base_entries is not None
        else result.puzzles
    )
    candidate_path.write_text(
        json.dumps(
            {"version": 1, "puzzles": puzzles},
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
    parser.add_argument(
        "--merge",
        type=Path,
        default=None,
        help="现有题库 JSON；给出后按标题合并，源图缺失的关卡会被保留",
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

    base_entries: list[dict[str, Any]] | None = None
    if args.merge is not None:
        merge_path = args.merge.expanduser()
        if not merge_path.is_file():
            print(f"找不到题库文件：{merge_path}")
            return 1
        base_entries = load_bank_entries(merge_path)

    debug_dir = output_dir / DEBUG_DIRNAME
    result = extract_puzzles(paths, profiles, debug_dir)
    outputs = write_outputs(output_dir, result, base_entries)

    print(f"识别到 {len(result.puzzles)} / {result.processed} 个可用残局")
    if base_entries is not None:
        merged = len(base_entries) + len(result.puzzles) - len(
            {entry.get("title") for entry in result.puzzles}
            & {entry.get("title") for entry in base_entries}
        )
        print(f"合并后题库共 {merged} 关")
    print(f"候选题库：{outputs.candidate}")
    print(f"识别报告：{outputs.report}")
    print(f"调试图目录：{outputs.debug}")
    if len(result.puzzles) != result.processed:
        print("有图片未通过数量/五连检查，请查看报告和调试图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
