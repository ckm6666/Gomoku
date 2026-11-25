"""残局图片提取工具的测试。

用合成的 QImage 覆盖网格检测、黑白分类与候选 JSON 生成，
不依赖任何私有图片素材，因此换机器也能跑。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter, QPen  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from tools import extract_puzzle_images as tool  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])

# 合成棋盘图的尺寸与网格几何：15 条线、间距 35 像素，全部落在图内。
IMAGE_SIZE = 570
GRID_ORIGIN = 40
GRID_SPACING = 35

# 木纹底色：亮度高但饱和度高，既不会被当成网格线，也不会被当成白子。
WOOD_COLOR = QColor(222, 190, 140)
# 网格线：亮度低、饱和度高，正好符合"深色木纹"的判定条件。
LINE_COLOR = QColor(90, 60, 20)
BLACK_STONE_COLOR = QColor(0, 0, 0)
WHITE_STONE_COLOR = QColor(255, 255, 255)

# 白先局面：黑子比白子多一颗。
WHITE_FIRST_STONES: dict[tuple[int, int], str] = {
    (2, 2): "black",
    (2, 8): "white",
    (5, 5): "black",
    (7, 3): "white",
    (8, 8): "black",
}

# 新版渲染图：尺寸大、格距宽、网格线是浅灰细线。
# 边缘扫描只认"深色木纹"网格线，对这类图会失效，需要走投影拟合。
WIDE_IMAGE_SIZE = 1280
WIDE_GRID_ORIGIN = 70
WIDE_GRID_SPACING = 82
WIDE_LINE_COLOR = QColor(100, 100, 100)
WIDE_BOARD_COLOR = QColor(204, 148, 74)


def build_wide_board_image(
    stones: dict[tuple[int, int], str]
) -> QImage:
    """生成一张新版风格的棋盘图：浅灰细网格 + 大格距。"""
    image = QImage(WIDE_IMAGE_SIZE, WIDE_IMAGE_SIZE, QImage.Format.Format_RGB32)
    image.fill(WIDE_BOARD_COLOR)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(WIDE_LINE_COLOR, 1))
    for index in range(tool.BOARD_SIZE):
        offset = WIDE_GRID_ORIGIN + index * WIDE_GRID_SPACING
        painter.drawLine(offset, 0, offset, WIDE_IMAGE_SIZE - 1)
        painter.drawLine(0, offset, WIDE_IMAGE_SIZE - 1, offset)
    painter.setPen(QPen(Qt.PenStyle.NoPen))
    radius = WIDE_GRID_SPACING * 0.42
    for (row, col), color in stones.items():
        cx = WIDE_GRID_ORIGIN + col * WIDE_GRID_SPACING
        cy = WIDE_GRID_ORIGIN + row * WIDE_GRID_SPACING
        painter.setBrush(
            BLACK_STONE_COLOR if color == "black" else WHITE_STONE_COLOR
        )
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
    painter.end()
    return image


def build_board_image(stones: dict[tuple[int, int], str]) -> QImage:
    """生成一张 15×15 的合成棋盘图，stones 中的坐标会被画成对应颜色的棋子。"""
    image = QImage(IMAGE_SIZE, IMAGE_SIZE, QImage.Format.Format_RGB32)
    image.fill(WOOD_COLOR)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(LINE_COLOR, 1))
    for index in range(tool.BOARD_SIZE):
        offset = GRID_ORIGIN + index * GRID_SPACING
        painter.drawLine(offset, 0, offset, IMAGE_SIZE - 1)
        painter.drawLine(0, offset, IMAGE_SIZE - 1, offset)
    painter.setPen(QPen(Qt.PenStyle.NoPen))
    radius = GRID_SPACING * 0.42
    for (row, col), color in stones.items():
        cx = GRID_ORIGIN + col * GRID_SPACING
        cy = GRID_ORIGIN + row * GRID_SPACING
        painter.setBrush(
            BLACK_STONE_COLOR if color == "black" else WHITE_STONE_COLOR
        )
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
    painter.end()
    return image


class ThresholdProfileTests(unittest.TestCase):
    """阈值配置应当是"名称 → 配置"的映射，且名字与内容一致。"""

    def test_profiles_are_named_mapping(self) -> None:
        profiles = tool.threshold_profiles()
        self.assertGreaterEqual(len(profiles), 2)
        for name, profile in profiles.items():
            self.assertEqual(profile.name, name)
            self.assertLess(profile.black_max, profile.white_min)

    def test_resolve_auto_uses_every_profile(self) -> None:
        resolved = tool.resolve_profiles("auto")
        self.assertEqual(len(resolved), len(tool.threshold_profiles()))

    def test_resolve_single_profile(self) -> None:
        resolved = tool.resolve_profiles("standard")
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0].name, "standard")

    def test_resolve_unknown_profile_raises(self) -> None:
        with self.assertRaises(ValueError):
            tool.resolve_profiles("no-such-profile")


class GridDetectionTests(unittest.TestCase):
    """网格检测：合成图上的 15 条线应当被完整识别出来。"""

    def test_detect_margin_grid_finds_known_lines(self) -> None:
        image = build_board_image({})
        grid = tool.detect_margin_grid(image)
        self.assertIsNotNone(grid)
        xs, ys = grid
        expected = [
            float(GRID_ORIGIN + index * GRID_SPACING)
            for index in range(tool.BOARD_SIZE)
        ]
        self.assertEqual(len(xs), tool.BOARD_SIZE)
        self.assertEqual(len(ys), tool.BOARD_SIZE)
        for found, want in zip(xs, expected):
            self.assertAlmostEqual(found, want, delta=1.5)
        for found, want in zip(ys, expected):
            self.assertAlmostEqual(found, want, delta=1.5)

    def test_detect_stones_reports_expected_geometry(self) -> None:
        image = build_board_image({})
        _, geometry = tool.detect_stones(image, tool.resolve_profiles("standard")[0])
        x0, y0, spacing_x, spacing_y, x1, y1 = geometry
        self.assertAlmostEqual(spacing_x, GRID_SPACING, delta=1.5)
        self.assertAlmostEqual(spacing_y, GRID_SPACING, delta=1.5)
        self.assertAlmostEqual(x1 - x0, GRID_SPACING * (tool.BOARD_SIZE - 1), delta=3.0)
        self.assertAlmostEqual(y1 - y0, GRID_SPACING * (tool.BOARD_SIZE - 1), delta=3.0)

    def test_grid_override_must_have_full_board_size(self) -> None:
        image = build_board_image({})
        with self.assertRaises(ValueError):
            tool.detect_stones(
                image,
                tool.resolve_profiles("standard")[0],
                grid_override=([1.0, 2.0], [1.0, 2.0]),
            )


class StoneClassificationTests(unittest.TestCase):
    """黑白分类：已知颜色的棋子必须被识别成同样的颜色，空位不得误判。"""

    def test_classifies_known_stones(self) -> None:
        image = build_board_image(WHITE_FIRST_STONES)
        stones, _ = tool.detect_stones(image, tool.resolve_profiles("standard")[0])
        for cell, color in WHITE_FIRST_STONES.items():
            self.assertEqual(stones.get(cell), color, f"{cell} 颜色识别错误")

    def test_empty_intersections_are_ignored(self) -> None:
        image = build_board_image(WHITE_FIRST_STONES)
        stones, _ = tool.detect_stones(image, tool.resolve_profiles("standard")[0])
        for cell in ((0, 0), (4, 4), (12, 6), (1, 13)):
            self.assertNotIn(cell, stones, f"{cell} 是空位，不应识别成棋子")

    def test_every_profile_finds_the_same_stones(self) -> None:
        image = build_board_image(WHITE_FIRST_STONES)
        for profile in tool.resolve_profiles("auto"):
            stones, _ = tool.detect_stones(image, profile)
            for cell, color in WHITE_FIRST_STONES.items():
                self.assertEqual(
                    stones.get(cell), color, f"{profile.name} 配置下 {cell} 识别错误"
                )

    def test_detect_with_count_check_requires_a_profile(self) -> None:
        """没有可用阈值时必须显式报错，而不是依赖会被 -O 优化掉的 assert。"""
        image = build_board_image({})
        with self.assertRaises(RuntimeError):
            tool.detect_with_count_check(image, tool.Player.BLACK, None, [])


class CandidateJsonTests(unittest.TestCase):
    """候选题库 JSON：结构完整、可反序列化、坐标正确。"""

    def _run_extraction(self, tmp: str) -> Path:
        input_dir = Path(tmp) / "images"
        output_dir = Path(tmp) / "out"
        input_dir.mkdir(parents=True)
        image = build_board_image(WHITE_FIRST_STONES)
        self.assertTrue(image.save(str(input_dir / "白先1.png")))
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = tool.main(
                ["--input", str(input_dir), "--output", str(output_dir)]
            )
        self.assertEqual(code, 0)
        self.assertTrue((output_dir / tool.REPORT_FILENAME).exists())
        return output_dir / tool.CANDIDATE_FILENAME

    def test_candidate_json_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = self._run_extraction(tmp)
            self.assertTrue(candidate_path.exists())
            data = json.loads(candidate_path.read_text(encoding="utf-8"))

        self.assertEqual(data["version"], 1)
        self.assertEqual(len(data["puzzles"]), 1)
        puzzle = data["puzzles"][0]
        for key in (
            "id",
            "title",
            "difficulty",
            "description",
            "hint",
            "moves",
            "to_move",
            "max_player_moves",
            "defender",
        ):
            self.assertIn(key, puzzle)
        self.assertEqual(puzzle["to_move"], "white")
        self.assertEqual(puzzle["id"], "p001")
        self.assertIsInstance(puzzle["max_player_moves"], int)
        self.assertEqual(puzzle["moves"], [[2, 2], [2, 8], [5, 5], [7, 3], [8, 8]])

    def test_candidate_moves_are_coordinate_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = self._run_extraction(tmp)
            data = json.loads(candidate_path.read_text(encoding="utf-8"))
        moves = data["puzzles"][0]["moves"]
        self.assertTrue(moves)
        for move in moves:
            self.assertEqual(len(move), 2)
            row, col = move
            self.assertIsInstance(row, int)
            self.assertIsInstance(col, int)
            self.assertTrue(0 <= row < tool.BOARD_SIZE)
            self.assertTrue(0 <= col < tool.BOARD_SIZE)

    def test_debug_images_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "out"
            input_dir = Path(tmp) / "images"
            input_dir.mkdir(parents=True)
            build_board_image(WHITE_FIRST_STONES).save(str(input_dir / "白先1.png"))
            with redirect_stdout(io.StringIO()):
                tool.main(["--input", str(input_dir), "--output", str(output_dir)])
            debug_files = list((output_dir / tool.DEBUG_DIRNAME).glob("*.png"))
        self.assertEqual(len(debug_files), 1)


class ProjectionGridTests(unittest.TestCase):
    """投影拟合：浅灰细线、大格距的新版渲染图也要能识别出 15 条线。"""

    def test_projection_grid_finds_wide_spaced_lines(self) -> None:
        image = build_wide_board_image({})
        grid = tool.detect_projection_grid(image)
        self.assertIsNotNone(grid)
        xs, ys = grid
        expected = [
            float(WIDE_GRID_ORIGIN + index * WIDE_GRID_SPACING)
            for index in range(tool.BOARD_SIZE)
        ]
        for found, want in zip(xs, expected):
            self.assertAlmostEqual(found, want, delta=1.5)
        for found, want in zip(ys, expected):
            self.assertAlmostEqual(found, want, delta=1.5)

    def test_margin_scan_cannot_read_grey_lines(self) -> None:
        """浅灰网格线不是深色木纹，边缘扫描应当失败，从而只能走投影拟合。"""
        image = build_wide_board_image({})
        self.assertIsNone(tool.detect_margin_grid(image))

    def test_count_check_reads_stones_on_wide_grid(self) -> None:
        stones = {
            (5, 5): "black",
            (5, 6): "white",
            (6, 7): "black",
            (7, 8): "white",
            (8, 9): "black",
            (9, 10): "white",
        }
        image = build_wide_board_image(stones)
        detected, _ = tool.detect_with_count_check(
            image, tool.Player.BLACK, None, tool.resolve_profiles("auto")
        )
        for cell, color in stones.items():
            self.assertEqual(detected.get(cell), color, f"{cell} 颜色识别错误")


class PuzzleTitleTests(unittest.TestCase):
    """标题表：图片与成语标题一一对应，标题互不重复。"""

    def test_titles_are_unique_idioms(self) -> None:
        titles = list(tool.PUZZLE_TITLES.values())
        self.assertEqual(len(set(titles)), len(titles))
        for title in titles:
            self.assertEqual(len(title), 4, f"{title} 不是四字成语")

    def test_every_puzzle_has_a_limit(self) -> None:
        """每关的限手数要么显式给出，要么落到默认值，且都合法。"""
        for title in tool.PUZZLE_TITLES.values():
            limit = tool.PUZZLE_LIMITS.get(title, tool.MAX_PLAYER_MOVES)
            self.assertTrue(1 <= limit <= 9, f"{title} 限手数非法")


class BankMergeTests(unittest.TestCase):
    """合并题库：同名就地更新、源图缺失的关卡原样保留。"""

    def test_merge_updates_same_title_and_keeps_others(self) -> None:
        base = [
            {"id": "p001", "title": "白虹贯日", "moves": [[1, 1]]},
            {"id": "p002", "title": "白璧无瑕", "moves": [[2, 2]]},
        ]
        generated = [
            {"id": "p001", "title": "白虹贯日", "moves": [[3, 3]]},
            {"id": "p003", "title": "势如破竹", "moves": [[4, 4]]},
        ]
        merged = tool.merge_bank(base, generated)
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["id"], "p001")
        self.assertEqual(merged[0]["moves"], [[3, 3]])
        self.assertEqual(merged[1]["moves"], [[2, 2]], "源图缺失的关卡应原样保留")
        self.assertEqual(merged[2]["id"], "p003", "新条目应接在现有 id 之后")
        self.assertEqual(merged[2]["title"], "势如破竹")

    def test_merge_with_empty_base_starts_from_generated(self) -> None:
        generated = [{"id": "p001", "title": "势如破竹", "moves": [[4, 4]]}]
        merged = tool.merge_bank([], generated)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["title"], "势如破竹")

    def test_load_bank_entries_reads_puzzle_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bank.json"
            path.write_text(
                json.dumps({"version": 1, "puzzles": [{"id": "p001"}]}),
                encoding="utf-8",
            )
            self.assertEqual(len(tool.load_bank_entries(path)), 1)
            self.assertEqual(tool.load_bank_entries(Path(tmp) / "none.json"), [])


class CommandLineTests(unittest.TestCase):
    """命令行接口：参数齐全、输入目录必填、导入无副作用。"""

    def test_parser_exposes_documented_options(self) -> None:
        help_text = tool.build_parser().format_help()
        for option in ("--input", "--output", "--threshold-profile"):
            self.assertIn(option, help_text)

    def test_input_is_required(self) -> None:
        with redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                tool.build_parser().parse_args([])

    def test_parser_reads_options(self) -> None:
        args = tool.build_parser().parse_args(
            ["--input", "imgs", "--output", "out", "--threshold-profile", "standard"]
        )
        self.assertEqual(args.input, Path("imgs"))
        self.assertEqual(args.output, Path("out"))
        self.assertEqual(args.threshold_profile, "standard")

    def test_missing_input_directory_returns_error_code(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with redirect_stdout(io.StringIO()):
                code = tool.main(
                    ["--input", str(Path(tmp) / "missing"), "--output", str(Path(tmp))]
                )
        self.assertEqual(code, 1)

    def test_module_has_no_hardcoded_author_path(self) -> None:
        source = Path(tool.__file__).read_text(encoding="utf-8")
        self.assertNotIn("a1325", source)
        self.assertNotIn("Downloads", source)

    def test_import_has_no_side_effects(self) -> None:
        """导入模块不应触发提取流程，也不应要求任何特定目录存在。"""
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import tools.extract_puzzle_images as m; print(m.__name__)",
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("extract_puzzle_images", completed.stdout)


if __name__ == "__main__":
    unittest.main()
