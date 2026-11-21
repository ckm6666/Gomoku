"""从残局图片提取 15×15 棋子坐标，生成题库候选与调试图。

依赖仅 PySide6（QImage 负责解码 WebP）。默认读取：

    C:\\Users\\a1325\\Downloads\\残局收录

用法：

    .\\.venv\\Scripts\\python.exe tools\\extract_puzzle_images.py

输出：

    tools/puzzle_extract_candidate.json   可直接替换 assets/puzzles/puzzles.json
    tools/puzzle_extract_report.txt       每张图的 ASCII 棋盘与识别统计
    tools/puzzle_extract_debug/*.png      在原图上标出识别到的棋子，便于人工核对

说明：
* 棋盘固定 15×15，图片坐标按 5.56% 边距、14 格等分推算；
* 只识别黑白棋子；图片 8/9/11 的褐色/红色标记会被忽略；
* 图片上的大号数字若是走子序号，不影响棋子集合的识别；
* 生成 moves 时黑白交替任意排列——只要最终局面没有五连，任何中间顺序
  都不会提前五连（五连是棋子集合的性质，加子不会消除五连）。
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.shared.game_model import GameModel  # noqa: E402
from gomoku.shared.rules import BOARD_SIZE, Move, Player, find_winning_line  # noqa: E402

IMAGE_DIR = Path(r"C:\Users\a1325\Downloads\残局收录")
OUTPUT_DIR = Path(__file__).resolve().parent
CANDIDATE_PATH = OUTPUT_DIR / "puzzle_extract_candidate.json"
REPORT_PATH = OUTPUT_DIR / "puzzle_extract_report.txt"
DEBUG_DIR = OUTPUT_DIR / "puzzle_extract_debug"

# 实测图片为 550×547，15 条网格线约在 x=7..542、y=10.5..544.5；
# 使用比例以便不同尺寸的同类图片也能对齐。
GRID_X0_RATIO = 7.0 / 550.0
GRID_X1_RATIO = 542.0 / 550.0
GRID_Y0_RATIO = 10.5 / 547.0
GRID_Y1_RATIO = 544.5 / 547.0
MAX_PLAYER_MOVES = 5
DEFENDER = "hard"

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


def _cluster_positions(values: list[int]) -> list[float]:
    groups: list[list[int]] = []
    for value in values:
        if groups and value - groups[-1][-1] <= 2:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _scan_line_positions(
    image: QImage, *, horizontal: bool, ratio: float
) -> list[float]:
    width, height = image.width(), image.height()
    if horizontal:
        y = max(1, min(height - 2, int(round(height * ratio))))
        hits = []
        for x in range(width):
            color = image.pixelColor(x, y)
            r, g, b = color.red(), color.green(), color.blue()
            brightness = (r + g + b) / 3.0
            saturation = max(r, g, b) - min(r, g, b)
            if brightness < 150 and saturation > 25:
                hits.append(x)
        return _cluster_positions(hits)

    x = max(1, min(width - 2, int(round(width * ratio))))
    hits = []
    for y in range(height):
        color = image.pixelColor(x, y)
        r, g, b = color.red(), color.green(), color.blue()
        brightness = (r + g + b) / 3.0
        saturation = max(r, g, b) - min(r, g, b)
        if brightness < 150 and saturation > 25:
            hits.append(y)
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
        if min(diffs) < 25.0 or max(diffs) > 50.0:
            continue
        mean = sum(diffs) / len(diffs)
        variance = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        cv = (variance**0.5) / mean
        key = (cv, -(window[-1] - window[0]))
        if best_key is None or key < best_key:
            best_key = key
            best = window
    if best is None or best_key is None or best_key[0] > 0.12:
        return None
    return best


def detect_margin_grid(image: QImage) -> tuple[list[float], list[float]] | None:
    """从图片边缘的空白扫描出 15 条网格线；带数字标签的图可能失败。"""
    for ratio in (0.035, 0.05, 0.07, 0.09):
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
        if not (0.85 <= x_span / max(1.0, y_span) <= 1.15):
            continue
        return xs, ys
    return None


def board_geometry(
    width: int, height: int
) -> tuple[float, float, float, float, float, float]:
    x0 = width * GRID_X0_RATIO
    x1 = width * GRID_X1_RATIO
    y0 = height * GRID_Y0_RATIO
    y1 = height * GRID_Y1_RATIO
    spacing_x = (x1 - x0) / (BOARD_SIZE - 1)
    spacing_y = (y1 - y0) / (BOARD_SIZE - 1)
    return x0, y0, spacing_x, spacing_y, x1, y1


def classify_intersection(
    image: QImage,
    cx: float,
    cy: float,
    spacing: float,
    bounds: tuple[float, float, float, float],
    black_max: float = 110.0,
    white_min: float = 115.0,
    white_sat: float = 35.0,
) -> str | None:
    """返回 'black' / 'white' / None（空位或标记）。

    以交叉点为中心取半径约 0.28 格的圆盘，用中位数颜色判断。圆盘完全落在
    棋子内部，既能避开黑子外侧的木纹，也能让中心的大号数字被棋子面积稀释。
    """
    radius = max(4, int(round(spacing * 0.28)))
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
            if not (0 <= x < width and 0 <= y < height):
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
            if brightness < black_max and saturation < 45:
                dark_pixels += 1
            elif brightness > white_min and saturation < white_sat:
                white_pixels += 1

    if total_pixels == 0:
        return None
    dark_fraction = dark_pixels / total_pixels
    white_fraction = white_pixels / total_pixels

    # 黑子上的白色数字/白子上的黑色数字只占圆盘一小部分，用占比判定
    # 比中位数更稳；木纹和褐色标记既不够暗也不够白。
    if dark_fraction >= 0.42 and dark_fraction >= white_fraction:
        return "black"
    if white_fraction >= 0.50:
        return "white"
    return None


def detect_stones(
    image: QImage,
    black_max: float = 110.0,
    white_min: float = 195.0,
    white_sat: float = 40.0,
    grid_override: tuple[list[float], list[float]] | None = None,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    if grid_override is not None:
        xs, ys = grid_override
        x0, x1 = xs[0], xs[-1]
        y0, y1 = ys[0], ys[-1]
        x_positions = xs
        y_positions = ys
    else:
        margin_grid = detect_margin_grid(image)
        if margin_grid is not None:
            xs, ys = margin_grid
            x0, x1 = xs[0], xs[-1]
            y0, y1 = ys[0], ys[-1]
            x_positions = xs
            y_positions = ys
        else:
            x0, y0, spacing_x, spacing_y, x1, y1 = board_geometry(
                image.width(), image.height()
            )
            spacing_x = (x1 - x0) / (BOARD_SIZE - 1)
            spacing_y = (y1 - y0) / (BOARD_SIZE - 1)
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
            if row >= 13 and col >= 12:
                continue
            cx = x_positions[col]
            cy = y_positions[row]
            color = classify_intersection(
                image,
                cx,
                cy,
                spacing,
                bounds,
                black_max=black_max,
                white_min=white_min,
                white_sat=white_sat,
            )
            if color is not None:
                stones[(row, col)] = color
    return stones, (x0, y0, spacing_x, spacing_y, x1, y1)


_THRESHOLD_PROFILES = (
    (110.0, 115.0, 35.0),  # 白子约 120-250，黑子 <100，木纹饱和度高
)


def detect_with_count_check(
    image: QImage,
    side: Player,
    grid_override: tuple[list[float], list[float]] | None = None,
) -> tuple[dict[tuple[int, int], str], tuple[float, float, float, float, float, float]]:
    """尝试多组阈值，取识别棋子最多的一组；数量合法时优先。"""
    best: tuple[
        dict[tuple[int, int], str],
        tuple[float, float, float, float, float, float],
    ] | None = None
    best_total = -1
    best_valid = False
    for black_max, white_min, white_sat in _THRESHOLD_PROFILES:
        stones, geometry = detect_stones(
            image,
            black_max=black_max,
            white_min=white_min,
            white_sat=white_sat,
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
    assert best is not None
    return best


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


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app

    if not IMAGE_DIR.is_dir():
        print(f"找不到图片目录：{IMAGE_DIR}")
        return 1

    paths = sorted(IMAGE_DIR.glob("*.*"), key=natural_key)
    paths = [path for path in paths if path.suffix.lower() in {".webp", ".png", ".jpg", ".jpeg"}]
    if not paths:
        print(f"目录中没有图片：{IMAGE_DIR}")
        return 1

    puzzles: list[dict] = []
    report: list[str] = []

    for index, path in enumerate(paths):
        stem = path.stem
        side = side_from_name(stem)
        image = QImage(str(path))
        if image.isNull():
            report.append(f"{stem}: 图片解码失败")
            continue

        stones, geometry = detect_with_count_check(
            image, side, GRID_OVERRIDES.get(stem)
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
        draw_debug(
            image,
            stones,
            geometry,
            DEBUG_DIR / f"{index + 1:02d}_{stem}.png",
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

    candidate = {"version": 1, "puzzles": puzzles}
    CANDIDATE_PATH.write_text(
        json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")

    print(f"识别到 {len(puzzles)} / {len(paths)} 个可用残局")
    print(f"候选题库：{CANDIDATE_PATH}")
    print(f"识别报告：{REPORT_PATH}")
    print(f"调试图目录：{DEBUG_DIR}")
    if len(puzzles) != len(paths):
        print("有图片未通过数量/五连检查，请查看报告和调试图。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
