"""PySide6 桌面客户端初始化与启动入口。"""

from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from gomoku.client.audio import initialize_audio
from gomoku.client.config import AppConfig
from gomoku.client.constants import (
    APP_NAME,
    APP_ORGANIZATION,
    APP_STORAGE_NAME,
    APP_VERSION,
)
from gomoku.client.font_settings import load_font_scale
from gomoku.client.logging_setup import (
    initialize_logging,
    install_exception_hook,
)
from gomoku.client.main_window import MainWindow
from gomoku.client.theme_palette import (
    GOLD,
    GOLD_DARK,
    INK,
    INK_FAINT,
    PAPER_LIGHT,
)
from gomoku.client.ui_theme import (
    apply_app_theme,
    make_seal_pixmap,
    ui_assets_directory,
)
from gomoku.shared.errors import ConfigurationError


def create_app_icon() -> QIcon:
    """程序化生成水墨朱印风格的应用图标。"""
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    painter.setPen(QPen(QColor(GOLD_DARK), 1.4))
    painter.setBrush(QColor(PAPER_LIGHT))
    painter.drawRoundedRect(QRectF(3.0, 3.0, 58.0, 58.0), 12.0, 12.0)
    painter.setPen(QPen(QColor(GOLD), 0.9))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(QRectF(7.0, 7.0, 50.0, 50.0), 9.0, 9.0)

    painter.setPen(QPen(QColor(INK), 1.2))
    painter.setBrush(QColor(INK))
    painter.drawEllipse(QPointF(24.0, 26.0), 12.0, 12.0)

    painter.setPen(QPen(QColor(INK_FAINT), 1.0))
    painter.setBrush(QColor(PAPER_LIGHT))
    painter.drawEllipse(QPointF(38.0, 38.0), 12.0, 12.0)

    seal = make_seal_pixmap("弈", 17, dpr=1.0)
    painter.drawPixmap(QPointF(42.0, 42.0), seal)

    painter.end()
    return QIcon(pixmap)


def main() -> int:
    """创建 QApplication、应用全局样式并启动主窗口。"""
    app = QApplication(sys.argv)

    # 应用元信息用于窗口标题、任务栏和后续 QStandardPaths 数据目录。
    app.setApplicationName(APP_STORAGE_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(APP_ORGANIZATION)

    # Fusion 风格可避免 Windows 原生主题干扰自定义 QSS 的圆角和配色。
    app.setStyle("Fusion")

    # 加载内置 OFL 字体并应用全局古风 QSS；失败时回退系统字体。
    # 字号比例从 QSettings 读取，用户上次的选择在启动后继续生效。
    font_scale = load_font_scale()
    theme_families = apply_app_theme(app, font_scale=font_scale)
    app.setWindowIcon(create_app_icon())

    # 配置只从环境变量读取非敏感项；坏配置不应阻止离线/AI 模式启动。
    config_error: Exception | None = None
    try:
        config = AppConfig.from_env()
    except ConfigurationError as exc:
        config = AppConfig()
        config_error = exc

    log_path = initialize_logging(config)
    install_exception_hook()
    logger = logging.getLogger("gomoku.client.app")
    if config_error is not None:
        logger.warning("网络配置无效，回退到默认离线配置：%s", config_error)
    logger.info(
        "应用启动 version=%s env=%s online_configured=%s log=%s",
        APP_VERSION,
        config.environment,
        config.online_configured,
        log_path,
    )
    if config.online_configured:
        # 记录实际连向的入口，便于排查「朋友拿到 exe 连不上」这类问题。
        logger.info(
            "联机入口 api=%s ws=%s",
            config.api_base_url,
            config.ws_base_url,
        )
    logger.info(
        "主题字体 families=%s body_fonts=%s assets=%s",
        theme_families,
        app.font().families(),
        ui_assets_directory(),
    )

    # 在窗口创建前初始化音频，让 QSettings/QStandardPaths 使用正确的应用元信息。
    # 内置 MP3 素材的解码在后台线程进行，首次运行也不会把启动卡住。
    audio_manager = initialize_audio()
    audio_manager.install_button_click_sounds(app)
    logger.info("音频初始化 available=%s", audio_manager.available)

    window = MainWindow(config=config)
    window.show()
    return app.exec()


__all__ = ["create_app_icon", "main"]
