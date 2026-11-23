"""「检查更新」界面的离屏回归测试。

覆盖主菜单入口、版本比较、GitHub 发布页跳转与失败提示；网络层用假客户端
替换，因此不会产生真实的 GitHub 请求。

运行方式：
    python -m unittest tests.test_update_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 必须在导入 PySide6 之前设置，保证 CI/无桌面环境也能构造控件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.constants import (  # noqa: E402
    APP_VERSION,
    GITHUB_RELEASES_PAGE_URL,
)
from gomoku.client.main_window import MainWindow  # noqa: E402
from gomoku.client.modal_overlay import ModalOverlay  # noqa: E402
from gomoku.client.records import LocalRecordStore  # noqa: E402
from gomoku.client.ui_theme import apply_app_theme  # noqa: E402
from gomoku.client.update_dialog import UpdateAvailableDialog  # noqa: E402
from gomoku.shared.update_check import (  # noqa: E402
    ReleaseAsset,
    ReleaseInfo,
)


def wait_until(
    predicate,
    timeout: float = 6.0,
    app: QApplication | None = None,
) -> bool:
    """处理 Qt 事件直到条件成立或超时。"""
    application = app or QApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        application.processEvents()
        if predicate():
            return True
        time.sleep(0.005)
    application.processEvents()
    return bool(predicate())


def make_release(
    version: str,
    *,
    url: str = "https://github.com/ckm6666/Gomoku/releases/tag/v9.9.9",
    notes: str = "新增功能若干",
    asset_name: str = "Gomoku-9.9.9-windows-x64.zip",
) -> ReleaseInfo:
    """构造一条发布信息，模拟 GitHub ``/releases/latest`` 的解析结果。"""
    assets: tuple[ReleaseAsset, ...] = ()
    if asset_name:
        assets = (
            ReleaseAsset(
                name=asset_name,
                download_url=(
                    "https://github.com/ckm6666/Gomoku/releases/download/"
                    f"v{version}/{asset_name}"
                ),
                size=28 * 1024 * 1024,
            ),
        )
    return ReleaseInfo(
        tag=f"v{version}",
        version=version,
        title=version,
        url=url,
        notes=notes,
        published_at="2026-01-01T00:00:00Z",
        assets=assets,
        prerelease=False,
    )


class FakeUpdateClient(QObject):
    """替代真实 HTTP 客户端的假对象，只记录调用并回放预置结果。"""

    release_ready = Signal(object)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self) -> None:
        super().__init__()
        self.checks = 0
        self.busy = False
        #: 置位后 ``check()`` 直接拒绝发起请求（模拟地址无效等情况）。
        self.refuse = False

    def check(self) -> bool:
        self.checks += 1
        if self.refuse:
            self.failed.emit("更新地址无效")
            return False
        self.busy = True
        self.busy_changed.emit(True)
        return True

    def abort(self) -> None:
        self.busy = False
        self.busy_changed.emit(False)


class UpdateUiTests(unittest.TestCase):
    """主菜单「检查更新」入口的行为。"""

    @classmethod
    def setUpClass(cls) -> None:
        app = QApplication.instance() or QApplication([])
        cls.app = app
        app.setStyle("Fusion")
        apply_app_theme(app)

    def setUp(self) -> None:
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
        self.app.processEvents()

        # 用假客户端替换真实 HTTP 客户端，并重新接上信号。
        self.client = FakeUpdateClient()
        self.window._update_client = self.client  # noqa: SLF001
        self.client.release_ready.connect(self.window._on_release_ready)
        self.client.failed.connect(self.window._on_update_failed)

    def tearDown(self) -> None:
        self.window.close()
        self.app.processEvents()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _run_modal(self, action) -> dict:
        """执行会弹出模态层的动作，并返回抓取到的弹窗信息。"""
        captured: dict = {}

        def inspect() -> None:
            modal = self.window.active_modal()
            captured["modal"] = modal
            if modal is None:
                return
            ok_button = getattr(modal, "ok_button", None)
            if ok_button is not None:
                ok_button.click()

        QTimer.singleShot(20, inspect)
        action()
        self.app.processEvents()
        return captured

    # ------------------------------------------------------------------
    # 用例
    # ------------------------------------------------------------------
    def test_menu_exposes_check_update_entry(self) -> None:
        """主菜单底部提供「检查更新」入口。"""
        self.assertEqual(self.window.update_button.text(), "检查更新")
        self.assertTrue(self.window.update_button.isEnabled())

    def test_click_starts_check_and_shows_busy_state(self) -> None:
        """点击入口会发起一次检查，并进入忙碌状态。"""
        self.window.update_button.click()
        self.app.processEvents()
        self.assertEqual(self.client.checks, 1)
        self.assertEqual(self.window.update_button.text(), "检查中…")
        self.assertFalse(self.window.update_button.isEnabled())

    def test_same_version_reports_already_latest(self) -> None:
        """版本相同时提示已是最新版本。"""
        captured = self._run_modal(
            lambda: self.window._on_release_ready(  # noqa: SLF001
                make_release(APP_VERSION)
            )
        )
        modal = captured.get("modal")
        self.assertIsNotNone(modal)
        self.assertIn(APP_VERSION, modal.message_label.text())
        self.assertEqual(self.window.update_button.text(), "检查更新")
        self.assertTrue(self.window.update_button.isEnabled())

    def test_newer_version_shows_update_dialog(self) -> None:
        """有新版时弹出更新弹窗，展示版本号与安装包名。"""
        captured: dict = {}

        def inspect() -> None:
            modal = self.window.active_modal()
            captured["modal"] = modal
            if isinstance(modal, UpdateAvailableDialog):
                modal.later_button.click()

        QTimer.singleShot(20, inspect)
        self.window._on_release_ready(  # noqa: SLF001
            make_release("9.9.9", notes="新增沙盘复盘")
        )
        self.app.processEvents()

        modal = captured.get("modal")
        self.assertIsInstance(modal, UpdateAvailableDialog)
        self.assertIn("9.9.9", modal.version_label.text())
        self.assertIn(
            "Gomoku-9.9.9-windows-x64.zip", modal.asset_label.text()
        )
        self.assertIn("新增沙盘复盘", modal.notes_label.text())

    def test_download_button_opens_github_release_page(self) -> None:
        """点「前往下载」会用浏览器打开 GitHub 发布页。"""
        opened: list[str] = []

        def fake_open(url) -> bool:
            opened.append(url.toString())
            return True

        def click_download() -> None:
            modal = self.window.active_modal()
            if isinstance(modal, UpdateAvailableDialog):
                modal.download_button.click()

        with mock.patch(
            "gomoku.client.main_window.QDesktopServices.openUrl",
            side_effect=fake_open,
        ):
            QTimer.singleShot(20, click_download)
            self.window._on_release_ready(  # noqa: SLF001
                make_release("9.9.9")
            )
            self.app.processEvents()

        self.assertEqual(
            opened,
            ["https://github.com/ckm6666/Gomoku/releases/tag/v9.9.9"],
        )

    def test_foreign_release_url_falls_back_to_releases_page(self) -> None:
        """非 GitHub 域名的发布地址会被替换成官方发布页。"""
        opened: list[str] = []

        def fake_open(url) -> bool:
            opened.append(url.toString())
            return True

        def click_download() -> None:
            modal = self.window.active_modal()
            if isinstance(modal, UpdateAvailableDialog):
                modal.download_button.click()

        with mock.patch(
            "gomoku.client.main_window.QDesktopServices.openUrl",
            side_effect=fake_open,
        ):
            QTimer.singleShot(20, click_download)
            self.window._on_release_ready(  # noqa: SLF001
                make_release(
                    "9.9.9",
                    url="https://example.invalid/malicious",
                )
            )
            self.app.processEvents()

        self.assertEqual(opened, [GITHUB_RELEASES_PAGE_URL])

    def test_failure_shows_reason(self) -> None:
        """检查失败时提示原因并恢复按钮。"""
        captured = self._run_modal(
            lambda: self.window._on_update_failed(  # noqa: SLF001
                "无法连接 GitHub，请检查网络"
            )
        )
        modal = captured.get("modal")
        self.assertIsNotNone(modal)
        self.assertIn("无法连接 GitHub", modal.message_label.text())
        self.assertEqual(self.window.update_button.text(), "检查更新")
        self.assertTrue(self.window.update_button.isEnabled())

    def test_refused_check_restores_button(self) -> None:
        """客户端直接拒绝发起请求时，按钮不能停在“检查中…”。"""
        self.client.refuse = True
        # 拒绝时会同步弹出失败提示，_run_modal 负责把它关掉。
        captured = self._run_modal(
            lambda: self.window.update_button.click()
        )
        self.assertIsNotNone(captured.get("modal"))
        self.assertEqual(self.window.update_button.text(), "检查更新")
        self.assertTrue(self.window.update_button.isEnabled())

    def test_unparsable_remote_version_is_reported(self) -> None:
        """远端版本号无法识别时不能说“已是最新版本”。"""
        captured = self._run_modal(
            lambda: self.window._on_release_ready(  # noqa: SLF001
                make_release("nightly-20241120")
            )
        )
        modal = captured.get("modal")
        self.assertIsNotNone(modal)
        self.assertIn("无法识别", modal.message_label.text())

    def test_update_defers_while_another_modal_is_open(self) -> None:
        """已有模态层时不叠加第二个弹窗，改为稍后重试。"""
        blocker = ModalOverlay(self.window._overlay_host())
        self.window._active_modal = blocker

        captured: dict = {}

        def inspect() -> None:
            modal = self.window.active_modal()
            if isinstance(modal, UpdateAvailableDialog):
                captured["dialog"] = modal
                modal.later_button.click()

        QTimer.singleShot(300, inspect)
        self.window._on_release_ready(  # noqa: SLF001
            make_release("9.9.9")
        )
        # 立即返回，没有抢走已经打开的模态层。
        self.assertIs(self.window.active_modal(), blocker)

        # 阻塞解除后，延迟的调用才会弹出更新弹窗。
        self.window._active_modal = None
        self.assertTrue(
            wait_until(lambda: "dialog" in captured, 3.0, self.app)
        )

    def test_closing_window_ignores_late_response(self) -> None:
        """窗口正在关闭时，迟到的响应不能再弹窗。"""
        self.window._closing = True
        self.window._on_release_ready(  # noqa: SLF001
            make_release("9.9.9")
        )
        self.app.processEvents()
        self.assertIsNone(self.window.active_modal())

    def test_release_url_allow_list(self) -> None:
        """只有本仓库的发布页地址会被放行。"""
        allowed = "https://github.com/ckm6666/Gomoku/releases/tag/v1.13.0"
        self.assertEqual(
            self.window._safe_release_url(allowed),  # noqa: SLF001
            allowed,
        )
        for hostile in (
            "https://github.com.evil.com/ckm6666/Gomoku/releases",
            "https://github.com@evil.com/ckm6666/Gomoku/releases",
            "https://attacker.github.com/x",
            "http://github.com/ckm6666/Gomoku/releases",
            "javascript:alert(1)",
            "https://github.com/attacker/phish",
            "https://github.com/ckm6666/Other/releases",
            "",
        ):
            self.assertEqual(
                self.window._safe_release_url(hostile),  # noqa: SLF001
                GITHUB_RELEASES_PAGE_URL,
                msg=f"不应放行：{hostile!r}",
            )


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    unittest.main()
