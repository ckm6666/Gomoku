"""更新功能界面测试：更新弹窗、下载进度与配置项（离屏，无真实网络）。"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from gomoku.client.build_info import BuildInfo  # noqa: E402
from gomoku.client.config import AppConfig  # noqa: E402
from gomoku.client.main_window import MainWindow  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.client.update_dialog import (  # noqa: E402
    DownloadProgressDialog,
    UpdateDialog,
)
from gomoku.client.update_service import UpdateService  # noqa: E402
from gomoku.client.updater import UpdateInfo  # noqa: E402
from gomoku.shared.errors import ConfigurationError  # noqa: E402

QT_APP = QApplication.instance() or QApplication([])


class FakeUpdateService(QObject):
    """替身更新服务：立即返回指定结果，不触碰网络。"""

    check_started = Signal()
    update_available = Signal(object)
    up_to_date = Signal(str)
    check_failed = Signal(str)
    download_progress = Signal(int, int)
    download_ready = Signal(object, str)
    download_failed = Signal(str)

    def __init__(self, message: str = "", parent=None) -> None:
        super().__init__(parent)
        self.message = message
        self.checks = 0
        self.downloads = 0
        self.cancels = 0

    def check_for_update(self) -> None:
        self.checks += 1
        self.check_started.emit()
        if self.message:
            self.check_failed.emit(self.message)
        else:
            self.up_to_date.emit("1.0.0")

    def download(self, info, target_dir=None) -> None:
        self.downloads += 1

    def cancel(self) -> None:
        self.cancels += 1


class FakeSettings:
    """内存版设置存储，避免测试污染真实 QSettings。"""

    def __init__(self, values=None) -> None:
        self.values = dict(values or {})
        self.sync_count = 0

    def value(self, key, default=""):
        return self.values.get(key, default)

    def setValue(self, key, value) -> None:
        self.values[key] = value

    def sync(self) -> None:
        self.sync_count += 1


class NoNetworkManager:
    """一旦被调用就让测试失败，用来证明没有真实网络访问。"""

    def get(self, request):
        raise AssertionError("不应发起真实网络请求")


def make_info(version: str = "9.9.9") -> UpdateInfo:
    """构造一条用于测试的版本信息。"""
    return UpdateInfo(
        version=version,
        url="https://example.invalid/gomoku.zip",
        sha256="",
        size_bytes=5 * 1024 * 1024,
        notes="修复若干问题",
        published_at="2026-01-01T00:00:00",
    )


class UpdateDialogTests(unittest.TestCase):
    """更新提示弹窗的文案、按钮与信号。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")

    def test_dialog_shows_versions_and_three_buttons(self) -> None:
        dialog = UpdateDialog(make_info("9.9.9"), "1.0.0")
        try:
            self.assertIn("1.0.0", dialog.version_label.text())
            self.assertIn("9.9.9", dialog.version_label.text())
            self.assertIn("安装包大小", dialog.size_label.text())
            self.assertTrue(dialog.title_label.text())
            self.assertEqual(
                dialog.download_button.text(), "下载并安装"
            )
            self.assertEqual(dialog.later_button.text(), "稍后提醒")
            self.assertEqual(dialog.ignore_button.text(), "忽略此版本")
            self.assertTrue(dialog.notes_browser.isReadOnly())
            self.assertIn("修复若干问题", dialog.notes_browser.toPlainText())
        finally:
            dialog.deleteLater()

    def test_download_requested_carries_update_info(self) -> None:
        dialog = UpdateDialog(make_info("9.9.9"), "1.0.0")
        received = []
        dialog.download_requested.connect(received.append)
        try:
            dialog.download_button.click()
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].version, "9.9.9")
            self.assertEqual(
                received[0].url, "https://example.invalid/gomoku.zip"
            )
        finally:
            dialog.deleteLater()

    def test_ignore_requested_carries_update_info(self) -> None:
        dialog = UpdateDialog(make_info("9.9.9"), "1.0.0")
        received = []
        dialog.ignore_requested.connect(received.append)
        try:
            dialog.ignore_button.click()
            self.assertEqual(len(received), 1)
            self.assertEqual(received[0].version, "9.9.9")
        finally:
            dialog.deleteLater()

    def test_dialog_is_overlay_not_native_window(self) -> None:
        """更新弹窗必须是窗口内遮罩，不能创建第二个原生窗口。"""
        dialog = UpdateDialog(make_info("9.9.9"), "1.0.0")
        try:
            self.assertNotIsInstance(dialog, QDialog)
            self.assertTrue(dialog.isModal())
        finally:
            dialog.deleteLater()

    def test_exec_returns_when_later_clicked(self) -> None:
        dialog = UpdateDialog(make_info("9.9.9"), "1.0.0")
        try:
            QTimer.singleShot(0, dialog.later_button.click)
            self.assertEqual(dialog.exec(), 0)
        finally:
            dialog.deleteLater()


class DownloadProgressDialogTests(unittest.TestCase):
    """下载进度弹窗的进度条更新。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")

    def test_known_total_updates_bar(self) -> None:
        dialog = DownloadProgressDialog()
        try:
            dialog.set_progress(50, 100)
            self.assertEqual(dialog.progress_bar.maximum(), 100)
            self.assertEqual(dialog.progress_bar.value(), 50)
            self.assertIn("50%", dialog.progress_bar.format())
            self.assertTrue(dialog.status_label.text())
        finally:
            dialog.deleteLater()

    def test_unknown_total_uses_indeterminate_bar(self) -> None:
        dialog = DownloadProgressDialog()
        try:
            dialog.set_progress(1024, -1)
            self.assertEqual(dialog.progress_bar.minimum(), 0)
            self.assertEqual(dialog.progress_bar.maximum(), 0)
            self.assertTrue(dialog.status_label.text())
        finally:
            dialog.deleteLater()

    def test_cancel_button_emits_signal(self) -> None:
        dialog = DownloadProgressDialog()
        called = []
        dialog.cancel_requested.connect(lambda: called.append(True))
        try:
            dialog.cancel_button.click()
            self.assertEqual(called, [True])
        finally:
            dialog.deleteLater()


class AppConfigUpdateTests(unittest.TestCase):
    """更新相关配置项的环境变量解析。"""

    def test_defaults(self) -> None:
        """没有配置服务器时不存在更新来源。"""
        config = AppConfig.from_env({})
        self.assertIsNone(config.update_manifest_url)
        self.assertTrue(config.auto_check_updates)

    def test_manifest_url_derived_from_api_base(self) -> None:
        config = AppConfig.from_env(
            {"GOMOKU_API_BASE_URL": "https://s.example"}
        )
        self.assertEqual(
            config.update_manifest_url,
            "https://s.example/api/v1/updates/latest",
        )

    def test_manifest_url_derived_from_api_base_with_trailing_slash(
        self,
    ) -> None:
        config = AppConfig.from_env(
            {"GOMOKU_API_BASE_URL": "https://s.example/"}
        )
        self.assertEqual(
            config.update_manifest_url,
            "https://s.example/api/v1/updates/latest",
        )

    def test_explicit_manifest_url_wins_over_api_base(self) -> None:
        config = AppConfig.from_env(
            {
                "GOMOKU_API_BASE_URL": "https://s.example",
                "GOMOKU_UPDATE_MANIFEST_URL": (
                    "https://mirror.example/api/v1/updates/latest"
                ),
            }
        )
        self.assertEqual(
            config.update_manifest_url,
            "https://mirror.example/api/v1/updates/latest",
        )

    def test_env_overrides(self) -> None:
        config = AppConfig.from_env(
            {
                "GOMOKU_UPDATE_MANIFEST_URL": "https://example.com/update.json",
                "GOMOKU_AUTO_UPDATE_CHECK": "0",
            }
        )
        self.assertEqual(
            config.update_manifest_url, "https://example.com/update.json"
        )
        self.assertFalse(config.auto_check_updates)

    def test_boolean_accepts_common_spellings(self) -> None:
        for value, expected in (
            ("1", True),
            ("TRUE", True),
            ("Yes", True),
            ("on", True),
            ("0", False),
            ("false", False),
            ("No", False),
            ("off", False),
        ):
            with self.subTest(value=value):
                config = AppConfig.from_env(
                    {"GOMOKU_AUTO_UPDATE_CHECK": value}
                )
                self.assertEqual(config.auto_check_updates, expected)

    def test_invalid_url_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env(
                {"GOMOKU_UPDATE_MANIFEST_URL": "ftp://example.com/update.json"}
            )

    def test_invalid_boolean_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            AppConfig.from_env({"GOMOKU_AUTO_UPDATE_CHECK": "maybe"})


class AppConfigBakedInfoTests(unittest.TestCase):
    """环境变量与打包时固化配置的优先级。"""

    def _baked(self, info: BuildInfo) -> None:
        """把内置配置替换成指定值。"""
        self._patcher = mock.patch(
            "gomoku.client.config.load_build_info", return_value=info
        )
        self._patcher.start()
        self.addCleanup(self._patcher.stop)

    def test_env_api_base_url_wins_over_baked(self) -> None:
        self._baked(BuildInfo(api_base_url="https://baked.example"))
        config = AppConfig.from_env(
            {"GOMOKU_API_BASE_URL": "https://env.example"}
        )
        self.assertEqual(config.api_base_url, "https://env.example")

    def test_env_manifest_url_wins_over_baked(self) -> None:
        self._baked(
            BuildInfo(
                api_base_url="https://baked.example",
                update_manifest_url="https://baked.example/update.json",
            )
        )
        config = AppConfig.from_env(
            {"GOMOKU_UPDATE_MANIFEST_URL": "https://env.example/update.json"}
        )
        self.assertEqual(
            config.update_manifest_url, "https://env.example/update.json"
        )

    def test_baked_values_used_without_env(self) -> None:
        self._baked(
            BuildInfo(
                api_base_url="https://baked.example",
                update_manifest_url="https://baked.example/update.json",
            )
        )
        config = AppConfig.from_env({})
        self.assertEqual(config.api_base_url, "https://baked.example")
        self.assertEqual(
            config.update_manifest_url, "https://baked.example/update.json"
        )

    def test_baked_api_base_derives_manifest_url(self) -> None:
        """只固化服务地址时，清单地址按服务地址推导。"""
        self._baked(BuildInfo(api_base_url="https://baked.example"))
        config = AppConfig.from_env({})
        self.assertEqual(
            config.update_manifest_url,
            "https://baked.example/api/v1/updates/latest",
        )

    def test_env_auto_update_check_wins_over_baked(self) -> None:
        self._baked(BuildInfo(auto_update_check=False))
        config = AppConfig.from_env({"GOMOKU_AUTO_UPDATE_CHECK": "1"})
        self.assertTrue(config.auto_check_updates)

    def test_baked_auto_update_check_wins_over_default(self) -> None:
        self._baked(BuildInfo(auto_update_check=False))
        config = AppConfig.from_env({})
        self.assertFalse(config.auto_check_updates)

    def test_baked_auto_update_check_true_keeps_default(self) -> None:
        self._baked(BuildInfo(auto_update_check=True))
        config = AppConfig.from_env({})
        self.assertTrue(config.auto_check_updates)

    def test_default_without_baked_info(self) -> None:
        """没有内置配置时维持原有默认行为。"""
        self._baked(BuildInfo())
        config = AppConfig.from_env({})
        self.assertIsNone(config.api_base_url)
        self.assertIsNone(config.update_manifest_url)
        self.assertTrue(config.auto_check_updates)


class UpdateServiceOfflineTests(unittest.TestCase):
    """离线环境下更新服务不得抛出异常，也不得发起真实请求。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")
        cls._logger = logging.getLogger("gomoku.client.update_service")
        cls._level = cls._logger.level
        cls._logger.setLevel(logging.CRITICAL)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._logger.setLevel(cls._level)

    def test_check_failure_is_reported_as_message(self) -> None:
        service = UpdateService(
            "https://example.invalid/update.json", "1.0.0"
        )
        service._manager = NoNetworkManager()
        messages = []
        service.check_failed.connect(messages.append)
        try:
            service.check_for_update()
            self.assertEqual(len(messages), 1)
            self.assertTrue(messages[0])
        finally:
            service.deleteLater()

    def test_invalid_manifest_url_is_rejected(self) -> None:
        service = UpdateService("not-a-url", "1.0.0")
        messages = []
        service.check_failed.connect(messages.append)
        try:
            service.check_for_update()
            self.assertEqual(len(messages), 1)
        finally:
            service.deleteLater()

    def test_empty_manifest_url_reports_missing_server(self) -> None:
        service = UpdateService("", "1.0.0")
        messages = []
        service.check_failed.connect(messages.append)
        try:
            service.check_for_update()
            self.assertEqual(messages, ["未配置更新服务器地址"])
        finally:
            service.deleteLater()


class MainWindowUpdateTests(unittest.TestCase):
    """主窗口里的更新入口与检查流程。"""

    @classmethod
    def setUpClass(cls) -> None:
        QT_APP.setStyle("Fusion")

    def setUp(self) -> None:
        self._logger = logging.getLogger("gomoku.client.main_window")
        self._level = self._logger.level
        self._logger.setLevel(logging.CRITICAL)
        self._tmpdir = tempfile.TemporaryDirectory()
        self.window = MainWindow(
            record_store=LocalRecordStore(
                Path(self._tmpdir.name) / "records"
            ),
        )
        self.window.setAttribute(
            Qt.WidgetAttribute.WA_DontShowOnScreen, True
        )
        self.window.show()
        QT_APP.processEvents()
        # 用内存设置替换真实 QSettings，避免污染用户环境。
        self.window._settings = FakeSettings()
        self.notices = []
        self.window._show_notice = (
            lambda title, message: self.notices.append((title, message))
        )

    def tearDown(self) -> None:
        self.window.close()
        QT_APP.processEvents()
        self._tmpdir.cleanup()
        self._logger.setLevel(self._level)

    def _use_service(self, service: FakeUpdateService) -> None:
        """换上一个替身更新服务，并按主窗口的方式接好信号。"""
        self.window.update_service = service
        self.window._connect_update_service(service)

    def test_menu_has_update_button(self) -> None:
        button = self.window.menu_update_button
        self.assertEqual(button.objectName(), "menuUpdateButton")
        self.assertEqual(button.accessibleName(), "检查更新")
        self.assertEqual(button.toolTip(), "检查是否有新版本")
        self.assertGreaterEqual(button.minimumHeight(), 30)

    def test_manual_check_failure_does_not_raise(self) -> None:
        service = FakeUpdateService("无法连接更新服务器")
        self._use_service(service)
        self.window._check_for_updates(manual=True)
        self.assertEqual(service.checks, 1)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("无法连接更新服务器", self.notices[0][1])

    def test_manual_check_failure_shows_tried_address(self) -> None:
        """手动检查失败时提示里要带上实际访问过的地址。"""
        address = "https://s.example/api/v1/updates/latest"
        self.window.config = AppConfig(update_manifest_url=address)
        service = FakeUpdateService("无法连接更新服务器，请检查网络")
        self._use_service(service)
        self.window._check_for_updates(manual=True)
        self.assertEqual(len(self.notices), 1)
        message = self.notices[0][1]
        self.assertIn(address, message)
        self.assertIn("无法连接更新服务器，请检查网络", message)

    def test_manual_check_failure_without_address_mentions_missing(self) -> None:
        """没有配置地址时，提示里要说明未配置服务器地址。"""
        self.window.config = AppConfig()
        service = FakeUpdateService("网络请求失败，无法获取更新信息")
        self._use_service(service)
        self.window._check_for_updates(manual=True)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("未配置服务器地址", self.notices[0][1])

    def test_manual_check_without_update_source_shows_notice(self) -> None:
        """没有配置服务器时手动检查只提示，不抛异常也不发请求。"""
        self.window.config = AppConfig()
        self.assertIsNone(self.window.update_service)
        self.window._check_for_updates(manual=True)
        self.assertEqual(len(self.notices), 1)
        self.assertIn("未配置服务器地址，无法检查更新", self.notices[0][1])

    def test_automatic_check_without_update_source_is_silent(self) -> None:
        self.window.config = AppConfig()
        self.window._check_for_updates(manual=False)
        self.assertEqual(self.notices, [])

    def test_manual_check_always_runs(self) -> None:
        self.window.config = AppConfig(auto_check_updates=False)
        service = FakeUpdateService()
        self._use_service(service)
        self.window._check_for_updates(manual=True)
        self.assertEqual(service.checks, 1)

    def test_automatic_check_skipped_when_disabled(self) -> None:
        self.window.config = AppConfig(auto_check_updates=False)
        service = FakeUpdateService()
        self._use_service(service)
        self.window._check_for_updates(manual=False)
        self.assertEqual(service.checks, 0)

    def test_automatic_check_throttled_after_recent_check(self) -> None:
        self.window._settings = FakeSettings(
            {
                "update/last_check": (
                    "2999-01-01T00:00:00+00:00"
                ),
            }
        )
        service = FakeUpdateService()
        self._use_service(service)
        self.window._check_for_updates(manual=False)
        self.assertEqual(service.checks, 0)

    def test_ignored_version_is_not_prompted(self) -> None:
        self.window._settings = FakeSettings(
            {"update/ignored_version": "9.9.9"}
        )
        self._use_service(FakeUpdateService())
        self.window._update_check_is_manual = True
        self.window._on_update_available(make_info("9.9.9"))
        self.assertEqual(self.notices, [])

    def test_ignored_version_does_not_block_automatic_check(self) -> None:
        """忽略某个版本后仍要继续自动检查，否则后续新版本永远发现不了。"""
        self.window._settings = FakeSettings(
            {"update/ignored_version": "9.9.9"}
        )
        service = FakeUpdateService()
        self._use_service(service)
        self.window._check_for_updates(manual=False)
        self.assertEqual(service.checks, 1)

    def test_startup_check_uses_single_shot_timer(self) -> None:
        self.window.schedule_startup_update_check()
        timer = self.window._startup_check_timer
        self.assertIsNotNone(timer)
        self.assertTrue(timer.isSingleShot())
        self.assertEqual(timer.interval(), 3000)
        # 重复调用不会叠加定时器。
        self.window.schedule_startup_update_check()
        self.assertIs(self.window._startup_check_timer, timer)

    def test_download_ready_records_path_for_install(self) -> None:
        """下载完成后先记录路径，等进度遮罩收起再进入安装流程。"""
        self.window._on_update_download_ready(make_info("9.9.9"), "/tmp/x.exe")
        self.assertEqual(self.window._download_ready_path, "/tmp/x.exe")


if __name__ == "__main__":
    unittest.main(verbosity=2)
