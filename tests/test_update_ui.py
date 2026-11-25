"""「检查更新」界面的离屏回归测试。

覆盖主菜单入口、版本比较、GitHub 发布页跳转、下载替换流程与失败提示；
网络层用假客户端替换，因此不会产生真实的 GitHub 请求。

运行方式：
    python -m unittest tests.test_update_ui -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

# 必须在导入 PySide6 之前设置，保证 CI/无桌面环境也能构造控件。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gomoku.client.confirm_dialog import ConfirmDialog  # noqa: E402
from gomoku.client.constants import (  # noqa: E402
    APP_VERSION,
    GITHUB_RELEASES_PAGE_URL,
)
from gomoku.client.main_window import MainWindow  # noqa: E402
from gomoku.client.modal_overlay import ModalOverlay, NoticeOverlay  # noqa: E402
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


class FakeDownloadClient(QObject):
    """替代真实下载客户端的假对象：直接产出本地更新包。"""

    progress = Signal(int, int)
    finished = Signal(str)
    failed = Signal(str)
    busy_changed = Signal(bool)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(kwargs.get("parent"))
        self.urls: list[str] = []
        self.targets: list[Path] = []
        self.aborted = False
        #: 置为 ``"fail"`` 时直接报下载失败。
        self.mode = "ok"

    def start(self, url: str, target: Path) -> bool:
        self.urls.append(url)
        self.targets.append(Path(target))
        if self.mode == "fail":
            self.failed.emit("下载更新包失败，请检查网络")
            return False
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, "w") as bundle:
            bundle.writestr("Gomoku.exe", b"MZfake")
        self.progress.emit(10, 10)
        self.finished.emit(str(target))
        return True

    def abort(self) -> None:
        self.aborted = True


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
        workspace = getattr(self.window, "_update_workspace", None)
        if workspace is not None:
            from gomoku.client import auto_update

            auto_update.cleanup_workspace(workspace)
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
        """不支持原地替换时，点按钮会用浏览器打开 GitHub 发布页。"""
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

    # ------------------------------------------------------------------
    # 下载并替换
    # ------------------------------------------------------------------
    def _run_update_flow(self, client: FakeDownloadClient) -> list[str]:
        """走一遍「检查更新 → 下载 → 重启」流程，返回途中出现的提示。

        弹窗都是窗口内的模态层，这里用一个定时器代替用户逐个点按钮。
        """
        seen: list[str] = []
        clients: list[FakeDownloadClient] = [client]

        def fake_client(*args, **kwargs):
            return clients[0]

        def inspector() -> None:
            modal = self.window.active_modal()
            if isinstance(modal, UpdateAvailableDialog):
                modal.download_button.click()
            elif isinstance(modal, ConfirmDialog):
                seen.append("confirm")
                modal.confirm_button.click()
            elif isinstance(modal, NoticeOverlay):
                seen.append("notice")
                modal.ok_button.click()

        timer = QTimer()
        timer.setInterval(20)
        timer.timeout.connect(inspector)
        timer.start()
        try:
            with mock.patch(
                "gomoku.client.main_window.UpdateDownloadClient", fake_client
            ), mock.patch(
                "gomoku.client.main_window.auto_update.is_supported",
                return_value=True,
            ):
                self.window._on_release_ready(  # noqa: SLF001
                    make_release("9.9.9")
                )
                self.app.processEvents()
        finally:
            timer.stop()
        return seen

    def test_update_button_offers_inplace_download(self) -> None:
        """支持原地替换时，按钮变成「下载并更新」而不是「前往下载页」。"""
        opened: list[str] = []

        def fake_open(url) -> bool:
            opened.append(url.toString())
            return True

        with mock.patch(
            "gomoku.client.main_window.auto_update.is_supported",
            return_value=True,
        ), mock.patch(
            "gomoku.client.main_window.QDesktopServices.openUrl",
            side_effect=fake_open,
        ):
            captured: dict = {}

            def inspect() -> None:
                modal = self.window.active_modal()
                if isinstance(modal, UpdateAvailableDialog):
                    captured["dialog"] = modal
                    modal.later_button.click()

            QTimer.singleShot(20, inspect)
            self.window._on_release_ready(  # noqa: SLF001
                make_release("9.9.9")
            )
            self.app.processEvents()

        dialog = captured.get("dialog")
        self.assertIsInstance(dialog, UpdateAvailableDialog)
        self.assertEqual(dialog.download_button.text(), "下载并更新")
        self.assertEqual(opened, [])

    def test_update_flow_downloads_package_and_asks_for_restart(self) -> None:
        """点「下载并更新」会真的下载安装包，并询问是否重启。"""
        client = FakeDownloadClient()
        seen = self._run_update_flow(client)

        self.assertEqual(
            client.urls,
            [
                "https://github.com/ckm6666/Gomoku/releases/download/"
                "v9.9.9/Gomoku-9.9.9-windows-x64.zip"
            ],
        )
        # 下载完成后应当出现「立即重启」的确认框。
        self.assertIn("confirm", seen)

    def test_update_flow_reports_download_failure(self) -> None:
        """下载失败时给出原因，并提示条恢复可用。"""
        client = FakeDownloadClient()
        client.mode = "fail"
        seen = self._run_update_flow(client)

        self.assertIn("notice", seen)
        self.assertNotIn("confirm", seen)

    def test_update_flow_extracts_package(self) -> None:
        """下载完成后会把安装包解压到暂存目录。"""
        client = FakeDownloadClient()
        self._run_update_flow(client)

        staging = self.window._update_staging  # noqa: SLF001
        self.assertIsNotNone(staging)
        self.assertTrue((staging / "Gomoku.exe").is_file())

    def test_update_flow_deletes_downloaded_archive(self) -> None:
        """解压完成后要删掉压缩包，别把几十兆的安装包留在临时目录里。"""
        client = FakeDownloadClient()
        self._run_update_flow(client)

        archives = list(client.targets)
        self.assertEqual(len(archives), 1)
        self.assertFalse(archives[0].exists())

    def test_stale_download_failure_is_ignored(self) -> None:
        """过期客户端迟到的失败通知不能影响当前下载。"""
        stale = FakeDownloadClient()
        current = FakeDownloadClient()
        self.window._update_download_client = current  # noqa: SLF001

        self.window._on_update_download_failed(  # noqa: SLF001
            "无法写入更新包，请检查磁盘空间与权限", stale
        )

        self.assertEqual(self.window._update_state, "idle")  # noqa: SLF001
        self.assertEqual(self.window._update_error_message, "")  # noqa: SLF001


if __name__ == "__main__":  # pragma: no cover - 手动运行入口
    unittest.main()
