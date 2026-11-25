"""自动更新核心逻辑的单元测试（无 Qt、无网络）。

覆盖：下载地址白名单、压缩包解压与路径穿越防护、替换脚本生成，以及
「源码运行时不支持原地替换」这一约束。

运行方式：
    python -m unittest tests.test_auto_update -v
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client import auto_update  # noqa: E402

#: 合法的产物下载地址。
VALID_URL = (
    "https://github.com/ckm6666/Gomoku/releases/download/"
    "v1.14.0/Gomoku-1.14.0-windows-x64.zip"
)


def make_zip(path: Path, members: dict[str, bytes]) -> Path:
    """按「名字 → 内容」写出一个压缩包。"""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, payload in members.items():
            bundle.writestr(name, payload)
    return path


class AssetDownloadUrlTests(unittest.TestCase):
    """下载地址白名单。"""

    def test_accepts_official_release_asset(self) -> None:
        self.assertEqual(auto_update.asset_download_url(VALID_URL), VALID_URL)

    def test_rejects_foreign_or_insecure_urls(self) -> None:
        hostile = (
            "",
            "   ",
            "http://github.com/ckm6666/Gomoku/releases/download/v1/a.zip",
            "https://example.com/ckm6666/Gomoku/releases/download/v1/a.zip",
            "https://github.com.evil.com/ckm6666/Gomoku/releases/download/v1/a.zip",
            "https://github.com@evil.com/ckm6666/Gomoku/releases/download/v1/a.zip",
            "https://github.com/attacker/evil/releases/download/v1/a.zip",
            "https://github.com/ckm6666/Gomoku/releases/download/v1/a.exe",
            "javascript:alert(1)",
        )
        for url in hostile:
            self.assertEqual(
                auto_update.asset_download_url(url),
                "",
                msg=f"不应放行：{url!r}",
            )

    def test_rejects_other_github_hosts(self) -> None:
        """只有 github.com 才放行。

        ``objects.githubusercontent.com`` 是 GitHub 302 的落点，由 Qt 内部
        跟随即可；把它加进白名单会让「只下载本仓库产物」出现缺口。
        """
        hostile = (
            "https://objects.githubusercontent.com/anything/evil.zip",
            "https://raw.githubusercontent.com/ckm6666/Gomoku/main/a.zip",
            "https://gist.githubusercontent.com/ckm6666/a.zip",
        )
        for url in hostile:
            self.assertEqual(
                auto_update.asset_download_url(url),
                "",
                msg=f"不应放行：{url!r}",
            )

    def test_rejects_non_default_ports(self) -> None:
        """端口必须还是 HTTPS 的默认端口。"""
        hostile = (
            "https://github.com:8443/ckm6666/Gomoku/releases/download/v1/a.zip",
            "https://github.com:22/ckm6666/Gomoku/releases/download/v1/a.zip",
        )
        for url in hostile:
            self.assertEqual(
                auto_update.asset_download_url(url),
                "",
                msg=f"不应放行：{url!r}",
            )

    def test_accepts_explicit_default_port(self) -> None:
        """显式写出 :443 仍然是同一个合法地址。"""
        url = (
            "https://github.com:443/ckm6666/Gomoku/releases/download/"
            "v1.14.0/Gomoku-1.14.0-windows-x64.zip"
        )
        self.assertEqual(auto_update.asset_download_url(url), url)


class ExtractArchiveTests(unittest.TestCase):
    """解压校验。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.archive = self.root / "update.zip"
        self.staging = self.root / "stage"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extracts_valid_bundle(self) -> None:
        make_zip(
            self.archive,
            {
                "Gomoku.exe": b"MZ fake executable",
                "THIRD_PARTY_NOTICES.md": b"notices",
                "fonts/OFL-MaShanZheng.txt": b"font license",
            },
        )
        staging = auto_update.extract_archive(self.archive, self.staging)

        self.assertTrue((staging / "Gomoku.exe").is_file())
        self.assertTrue((staging / "fonts" / "OFL-MaShanZheng.txt").is_file())

    def test_rejects_path_traversal(self) -> None:
        """压缩包里的 ``..`` 绝不能写到暂存目录之外。"""
        make_zip(
            self.archive,
            {
                "Gomoku.exe": b"MZ",
                "../escaped.txt": b"nope",
            },
        )
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.archive, self.staging)
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_rejects_absolute_member_path(self) -> None:
        make_zip(
            self.archive,
            {"Gomoku.exe": b"MZ", "/etc/passwd": b"nope"},
        )
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.archive, self.staging)

    def test_rejects_bundle_without_executable(self) -> None:
        make_zip(self.archive, {"THIRD_PARTY_NOTICES.md": b"notices"})
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.archive, self.staging)

    def test_rejects_non_executable_payload(self) -> None:
        """主程序不是 PE 文件时必须终止安装。"""
        make_zip(self.archive, {"Gomoku.exe": b"not an executable"})
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.archive, self.staging)

    def test_rejects_corrupted_archive(self) -> None:
        self.archive.write_bytes(b"not a zip file")
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.archive, self.staging)

    def test_rejects_missing_archive(self) -> None:
        with self.assertRaises(auto_update.UpdateError):
            auto_update.extract_archive(self.root / "missing.zip", self.staging)


class HelperScriptTests(unittest.TestCase):
    """替换脚本与工作目录。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_workspace_is_unique(self) -> None:
        first = auto_update.create_workspace("1.14.0")
        second = auto_update.create_workspace("1.14.0")
        try:
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_dir())
        finally:
            auto_update.cleanup_workspace(first)
            auto_update.cleanup_workspace(second)

    def test_cleanup_workspace_removes_directory(self) -> None:
        workspace = auto_update.create_workspace()
        auto_update.cleanup_workspace(workspace)
        self.assertFalse(workspace.exists())

    def test_prepare_update_requires_frozen_client(self) -> None:
        """源码运行时不能替换安装目录，必须明确报错。"""
        staging = self.root / "stage"
        staging.mkdir()
        if auto_update.is_frozen():  # pragma: no cover - 打包产物下不适用
            self.skipTest("打包产物环境下另行验证")
        with self.assertRaises(auto_update.UpdateError):
            auto_update.prepare_update(staging, self.root)

    def test_helper_script_waits_and_replaces(self) -> None:
        """生成的脚本必须等待客户端退出后再替换并重启。"""
        staging = self.root / "stage"
        staging.mkdir()
        script = self.root / "apply-update.ps1"
        plan = auto_update.UpdatePlan(
            staging_dir=staging,
            install_dir=self.root,
            exe_path=self.root / "Gomoku.exe",
            script_path=script,
            log_path=self.root / "apply-update.log",
        )
        auto_update.write_helper_script(plan)
        text = script.read_text(encoding="utf-8-sig")

        # 等待客户端退出：自己轮询并设超时，不能用没有超时的
        # Wait-Process——客户端退不出来就会永远卡住。
        self.assertIn("Get-Process -Id $WaitPid", text)
        self.assertIn("AddSeconds(120)", text)
        self.assertIn("Start-Process", text)
        self.assertIn("$WaitPid", text)
        self.assertIn("Copy-Item", text)
        # 收尾时清掉整个工作目录，别把临时文件越攒越多。
        self.assertIn("$Workspace", text)

    def test_helper_script_passes_workspace_to_cleanup(self) -> None:
        """启动脚本时要把工作目录告诉它，替换完才能一并清掉。"""
        staging = self.root / "stage"
        staging.mkdir()
        script = self.root / "apply-update.ps1"
        plan = auto_update.UpdatePlan(
            staging_dir=staging,
            install_dir=self.root,
            exe_path=self.root / "Gomoku.exe",
            script_path=script,
            log_path=self.root / "apply-update.log",
        )
        auto_update.write_helper_script(plan)
        text = script.read_text(encoding="utf-8-sig")

        self.assertIn("[string]$Workspace", text)

    def test_install_directory_is_none_when_not_frozen(self) -> None:
        if auto_update.is_frozen():  # pragma: no cover - 打包产物下不适用
            self.skipTest("打包产物环境下另行验证")
        self.assertIsNone(auto_update.install_directory())
        self.assertFalse(auto_update.is_supported())

    def test_clean_environment_drops_pyinstaller_variables(self) -> None:
        """辅助脚本不能把解包目录变量传给新客户端。

        打包产物的子进程带着 ``_PYI_PARENT_PROCESS_LEVEL`` 运行；新客户端
        若继承它，会误以为自己是子进程，从而卡在已经失效的旧临时目录上。
        """
        injected = {
            "_PYI_PARENT_PROCESS_LEVEL": "1",
            "_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI123",
            "_PYI_ARCHIVE_FILE": r"C:\Temp\_MEI123\base.zip",
            "_MEIPASS2": r"C:\Temp\_MEI123",
        }
        saved = {key: os.environ.get(key) for key in injected}
        os.environ.update(injected)
        try:
            env = auto_update._clean_environment()  # noqa: SLF001
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        for key in injected:
            self.assertNotIn(key, env, msg=f"不应传给辅助脚本：{key}")
        # 其余环境变量照常保留，辅助脚本离不开 PATH。
        self.assertIn("PATH", env)


class VerifyArchiveTests(unittest.TestCase):
    """下载到的更新包必须与发布时的摘要一致才能安装。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.archive = make_zip(
            self.root / "update.zip", {"Gomoku.exe": b"MZfake"}
        )
        self.digest = auto_update.compute_sha256(self.archive)

    def test_compute_sha256_matches_hashlib(self) -> None:
        """摘要要与 hashlib 算出来的一致。"""
        import hashlib

        self.assertEqual(
            self.digest, hashlib.sha256(self.archive.read_bytes()).hexdigest()
        )

    def test_matching_digest_passes(self) -> None:
        """摘要一致时校验通过。"""
        auto_update.verify_archive(self.archive, self.digest)

    def test_mismatching_digest_is_rejected(self) -> None:
        """摘要对不上必须报错，绝不能继续安装。"""
        with self.assertRaises(auto_update.UpdateError):
            auto_update.verify_archive(self.archive, "0" * 64)

    def test_missing_archive_is_rejected(self) -> None:
        """文件不存在时报错。"""
        with self.assertRaises(auto_update.UpdateError):
            auto_update.verify_archive(self.root / "missing.zip", self.digest)

    def test_empty_expected_digest_skips_verification(self) -> None:
        """发布没给摘要时跳过校验，但不让流程卡住。"""
        auto_update.verify_archive(self.archive, "")
        auto_update.verify_archive(self.archive, "   ")


if __name__ == "__main__":
    unittest.main()
