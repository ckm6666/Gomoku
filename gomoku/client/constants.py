"""桌面客户端专用的应用、窗口、棋盘绘制与主题常量。

棋盘规则常量（棋盘大小、连子数、方向）位于
:mod:`gomoku.shared.rules`，供未来服务端复用；本文件只放 PySide6 界面
使用的尺寸、颜色和绘制比例。
"""

from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# 应用信息
# ---------------------------------------------------------------------------
APP_NAME: Final[str] = "五子棋"
APP_TITLE: Final[str] = "五子棋 · 本地对弈与联机排位"
APP_VERSION: Final[str] = "1.16.0"
APP_ORGANIZATION: Final[str] = "Gomoku"
APP_STORAGE_NAME: Final[str] = "Gomoku"

# ---------------------------------------------------------------------------
# 更新检查（GitHub Releases）
# ---------------------------------------------------------------------------
# 版本信息与安装包都从 GitHub Releases 获取：客户端不经过游戏服务端分发
# 新版本，服务端挂了也不影响更新。安装包由客户端直接下载、校验并就地替换。
#: GitHub Releases「最新版本」API，匿名访问有频率限制（60 次/小时/IP）。
GITHUB_RELEASE_API_URL: Final[str] = (
    "https://api.github.com/repos/ckm6666/Gomoku/releases/latest"
)

# ---------------------------------------------------------------------------
# 棋盘绘制（15×15 棋盘常用的五个星位，使用 0 起始行列坐标）
# ---------------------------------------------------------------------------
STAR_POINTS: Final[tuple[tuple[int, int], ...]] = (
    (3, 3),
    (3, 11),
    (7, 7),
    (11, 3),
    (11, 11),
)

# ---------------------------------------------------------------------------
# 窗口尺寸（逻辑像素，Qt 会在高 DPI 屏幕上自动缩放）
# ---------------------------------------------------------------------------
WINDOW_DEFAULT_WIDTH: Final[int] = 720
WINDOW_DEFAULT_HEIGHT: Final[int] = 860
# 设置栏增加了一行高度；最小尺寸仍要兼容 1366×768 笔记本在 125% 缩放下运行。
WINDOW_MIN_WIDTH: Final[int] = 520
WINDOW_MIN_HEIGHT: Final[int] = 540
WINDOW_SCREEN_MARGIN: Final[int] = 32
BOARD_SUGGESTED_SIZE: Final[int] = 620
BOARD_MIN_SIZE: Final[int] = 250

# ---------------------------------------------------------------------------
# 棋盘绘制参数
# ---------------------------------------------------------------------------
BOARD_OUTER_PADDING: Final[float] = 12.0
BOARD_FRAME_THICKNESS_RATIO: Final[float] = 0.038
BOARD_SURFACE_CORNER_RATIO: Final[float] = 0.012
BOARD_GRID_MARGIN_RATIO: Final[float] = 0.066
STONE_RADIUS_RATIO: Final[float] = 0.43
CLICK_TOLERANCE_RATIO: Final[float] = 0.46
BOARD_CORNER_RADIUS_RATIO: Final[float] = 0.024
BOARD_GRAIN_LINE_COUNT: Final[int] = 14

# ---------------------------------------------------------------------------
# 棋盘配色
# ---------------------------------------------------------------------------
WOOD_TOP_COLOR: Final[str] = "#E8C99B"
WOOD_MIDDLE_COLOR: Final[str] = "#D9B17B"
WOOD_BOTTOM_COLOR: Final[str] = "#C99A62"
WOOD_BORDER_COLOR: Final[str] = "#8A6336"
WOOD_GRAIN_COLOR: Final[str] = "#6B4526"
GRID_COLOR: Final[str] = "#5A4129"
STAR_COLOR: Final[str] = "#3E2C1A"

# 深色漆框与金线
BOARD_LACQUER_TOP: Final[str] = "#332C24"
BOARD_LACQUER_BOTTOM: Final[str] = "#1E1A16"
BOARD_FRAME_GOLD: Final[str] = "#B8934E"
BOARD_FRAME_INK: Final[str] = "#26221C"

STONE_BLACK_HIGHLIGHT: Final[str] = "#6D6D6D"
STONE_BLACK_MIDDLE: Final[str] = "#262626"
STONE_BLACK_EDGE: Final[str] = "#050505"
STONE_BLACK_BORDER: Final[str] = "#000000"

STONE_WHITE_HIGHLIGHT: Final[str] = "#FFFFFF"
STONE_WHITE_MIDDLE: Final[str] = "#F4F0E7"
STONE_WHITE_EDGE: Final[str] = "#C9C2B4"
STONE_WHITE_BORDER: Final[str] = "#A9A194"

LAST_MOVE_COLOR: Final[str] = "#C7442E"
HINT_MARKER_COLOR: Final[str] = "#C7442E"
WIN_HIGHLIGHT_COLOR: Final[str] = "#C0392B"
