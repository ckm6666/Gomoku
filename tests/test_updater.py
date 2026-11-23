"""更新模块测试：版本号比较、清单解析、文件校验与安装脚本生成。

覆盖范围：

* 版本号解析（合法形式与非法形式）与版本大小比较（含预发布版本规则）；
* 服务端更新清单结构的解析（含字段别名与 ``sha256:`` 前缀）；
* 默认更新清单地址的推导；
* 非法清单（缺少版本号 / 地址、URL 协议错误、非对象载荷）抛出 ``ValueError``；
* 文件 SHA-256 计算与校验；
* 体积格式化；
* 自更新批处理脚本内容与暂存文件路径。
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from gomoku.client.update_installer import (
    build_update_script,
    staged_download_path,
)
from gomoku.client.updater import (
    SERVER_UPDATE_PATH,
    UpdateInfo,
    compare_versions,
    default_manifest_url,
    format_size,
    is_newer_version,
    parse_manifest,
    parse_version,
    sha256_of_file,
    verify_file,
)

_HEX = "a" * 64


class VersionParsingTests(unittest.TestCase):
    """版本号解析。"""

    def test_valid_forms(self) -> None:
        self.assertEqual(parse_version("1"), (1, 0, 0, ""))
        self.assertEqual(parse_version("1.2"), (1, 2, 0, ""))
        self.assertEqual(parse_version("1.2.3"), (1, 2, 3, ""))
        self.assertEqual(parse_version("v1.2.3"), (1, 2, 3, ""))
        self.assertEqual(parse_version("  v1.2.3  "), (1, 2, 3, ""))
        self.assertEqual(parse_version("1.2.3-beta.1"), (1, 2, 3, "beta.1"))
        self.assertEqual(parse_version("v2.0"), (2, 0, 0, ""))

    def test_invalid_forms(self) -> None:
        for text in ("", "   ", "abc", "1.2.3.4", "1.x.3", "1..3", "1.2.3-"):
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    parse_version(text)


class VersionComparisonTests(unittest.TestCase):
    """版本号大小比较。"""

    def test_ordering(self) -> None:
        self.assertEqual(compare_versions("1.0.0", "1.0.0"), 0)
        self.assertEqual(compare_versions("1.2.0", "1.1.9"), 1)
        self.assertEqual(compare_versions("1.1.9", "1.2.0"), -1)
        self.assertEqual(compare_versions("2.0.0", "1.99.99"), 1)
        self.assertEqual(compare_versions("v1.2.3", "1.2.3"), 0)

    def test_prerelease_sorts_before_release(self) -> None:
        self.assertEqual(compare_versions("1.2.0-beta.1", "1.2.0"), -1)
        self.assertEqual(compare_versions("1.2.0", "1.2.0-beta.1"), 1)

    def test_prerelease_vs_prerelease(self) -> None:
        self.assertEqual(compare_versions("1.2.0-beta.1", "1.2.0-beta.2"), -1)
        self.assertEqual(compare_versions("1.2.0-alpha.3", "1.2.0-beta.1"), -1)
        self.assertEqual(compare_versions("1.2.0-beta.1", "1.2.0-beta.1"), 0)

    def test_is_newer_version(self) -> None:
        self.assertTrue(is_newer_version("1.2.0", "1.1.9"))
        self.assertTrue(is_newer_version("1.12.0", "1.2.0"))
        self.assertFalse(is_newer_version("1.2.0", "1.2.0"))
        self.assertFalse(is_newer_version("1.1.0", "1.2.0"))
        self.assertFalse(is_newer_version("1.2.0-beta.1", "1.2.0"))


class ServerManifestTests(unittest.TestCase):
    """服务端更新清单解析。"""

    def test_full_manifest(self) -> None:
        payload = {
            "version": "1.12.0",
            "url": "https://example.com/Gomoku-1.12.0.exe",
            "sha256": _HEX,
            "size": 12345678,
            "notes": "更新说明",
            "published_at": "2026-01-01T00:00:00Z",
        }
        info = parse_manifest(payload)
        self.assertIsInstance(info, UpdateInfo)
        self.assertEqual(info.version, "1.12.0")
        self.assertEqual(info.url, "https://example.com/Gomoku-1.12.0.exe")
        self.assertEqual(info.sha256, _HEX)
        self.assertEqual(info.size_bytes, 12345678)
        self.assertEqual(info.notes, "更新说明")
        self.assertEqual(info.published_at, "2026-01-01T00:00:00Z")

    def test_aliases(self) -> None:
        payload = {
            "version": "1.9.0",
            "download_url": "http://example.com/app.exe",
            "size_bytes": 42,
            "release_notes": "别名说明",
        }
        info = parse_manifest(payload)
        self.assertEqual(info.version, "1.9.0")
        self.assertEqual(info.url, "http://example.com/app.exe")
        self.assertEqual(info.size_bytes, 42)
        self.assertEqual(info.notes, "别名说明")

    def test_sha256_prefix_is_stripped(self) -> None:
        payload = {
            "version": "1.12.0",
            "url": "https://example.com/app.exe",
            "sha256": f"sha256:{_HEX}",
        }
        info = parse_manifest(payload)
        self.assertEqual(info.sha256, _HEX)

    def test_sha256_is_lowercased(self) -> None:
        payload = {
            "version": "1.12.0",
            "url": "https://example.com/app.exe",
            "sha256": ("A" * 64),
        }
        info = parse_manifest(payload)
        self.assertEqual(info.sha256, "a" * 64)

    def test_optional_fields_default_to_empty(self) -> None:
        payload = {
            "version": "1.12.0",
            "url": "https://example.com/app.exe",
        }
        info = parse_manifest(payload)
        self.assertEqual(info.sha256, "")
        self.assertEqual(info.size_bytes, 0)
        self.assertEqual(info.notes, "")
        self.assertEqual(info.published_at, "")

    def test_tag_name_is_not_accepted(self) -> None:
        """服务端始终下发 version；旧版的 tag_name 结构不再支持。"""
        with self.assertRaises(ValueError):
            parse_manifest(
                {
                    "tag_name": "v1.12.0",
                    "url": "https://example.com/app.exe",
                }
            )

    def test_assets_are_not_parsed(self) -> None:
        """GitHub Release 风格的 assets 列表不再被识别。"""
        with self.assertRaises(ValueError):
            parse_manifest(
                {
                    "version": "1.12.0",
                    "assets": [
                        {
                            "name": "Gomoku-1.12.0.exe",
                            "browser_download_url": "https://x/app.exe",
                        }
                    ],
                }
            )


class ManifestErrorTests(unittest.TestCase):
    """非法清单的处理。"""

    def test_missing_version(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"url": "https://example.com/app.exe"})

    def test_empty_version(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"version": "  ", "url": "https://example.com/a.exe"})

    def test_missing_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"version": "1.0.0"})

    def test_empty_url(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"version": "1.0.0", "url": ""})

    def test_bad_url_scheme(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"version": "1.0.0", "url": "ftp://example.com/app.exe"})

    def test_url_without_netloc(self) -> None:
        with self.assertRaises(ValueError):
            parse_manifest({"version": "1.0.0", "url": "https:///app.exe"})

    def test_non_mapping_payload(self) -> None:
        for payload in ([], "not a mapping", 42, None):
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    parse_manifest(payload)  # type: ignore[arg-type]


class DigestTests(unittest.TestCase):
    """文件摘要计算与校验。"""

    def test_sha256_and_verify(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "payload.bin"
            content = b"gomoku updater test payload"
            path.write_bytes(content)
            expected = hashlib.sha256(content).hexdigest()

            self.assertEqual(sha256_of_file(path), expected)
            self.assertEqual(sha256_of_file(str(path)), expected)
            self.assertTrue(verify_file(path, expected))
            self.assertTrue(verify_file(path, expected.upper()))
            self.assertTrue(verify_file(path, ""))
            self.assertFalse(verify_file(path, "b" * 64))

    def test_missing_file(self) -> None:
        missing = Path(tempfile.gettempdir()) / "definitely-missing-updater-file.bin"
        self.assertFalse(verify_file(missing, _HEX))
        self.assertFalse(verify_file(missing, ""))


class FormatSizeTests(unittest.TestCase):
    """体积格式化。"""

    def test_format_size(self) -> None:
        self.assertEqual(format_size(0), "0 B")
        self.assertEqual(format_size(-10), "0 B")
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(860 * 1024), "860 KB")
        self.assertEqual(format_size(12 * 1024**2 + 300 * 1024), "12.3 MB")
        self.assertEqual(format_size(int(1.02 * 1024**3)), "1.0 GB")


class UpdateScriptTests(unittest.TestCase):
    """自更新脚本生成。"""

    def setUp(self) -> None:
        self.new_exe = Path(r"C:\Temp\Gomoku-1.12.0-setup.exe")
        self.target_exe = Path(r"C:\Program Files\Gomoku\Gomoku.exe")
        self.pid = 4242

    def test_script_contains_required_commands(self) -> None:
        script = build_update_script(self.new_exe, self.target_exe, self.pid)
        self.assertIn(str(self.pid), script)
        self.assertIn("tasklist", script)
        # timeout 在没有控制台输入时会直接报错退化成忙等，因此改用 ping 等待。
        self.assertNotIn("timeout /t", script)
        self.assertIn("ping -n 2 127.0.0.1", script)
        self.assertIn("copy /Y", script)
        self.assertIn(str(self.new_exe), script)
        self.assertIn(str(self.target_exe), script)
        self.assertIn('start ""', script)

    def test_script_waits_for_extra_pids(self) -> None:
        script = build_update_script(
            self.new_exe, self.target_exe, self.pid, extra_pids=(777, 777)
        )
        self.assertIn('PID eq 777', script)
        # 重复的 pid 只等待一次。
        self.assertEqual(script.count('PID eq 777'), 1)

    def test_script_retries_copy_when_target_is_locked(self) -> None:
        script = build_update_script(self.new_exe, self.target_exe, self.pid)
        self.assertIn(":COPY_RETRY", script)
        self.assertIn("goto COPY_RETRY", script)
        self.assertIn("set /a ATTEMPT=0", script)

    def test_script_without_restart(self) -> None:
        script = build_update_script(
            self.new_exe, self.target_exe, self.pid, restart=False
        )
        self.assertIn("copy /Y", script)
        self.assertNotIn('start ""', script)

    def test_rejects_quote_in_path(self) -> None:
        with self.assertRaises(ValueError):
            build_update_script(Path('C:\\bad"name.exe'), self.target_exe, self.pid)
        with self.assertRaises(ValueError):
            build_update_script(self.new_exe, Path('C:\\bad"name.exe'), self.pid)


class StagedPathTests(unittest.TestCase):
    """暂存安装包路径。"""

    def test_staged_download_path(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder) / "nested" / "downloads"
            result = staged_download_path(directory, "1.12.0")
            self.assertEqual(result, directory / "Gomoku-1.12.0-setup.exe")
            self.assertTrue(directory.is_dir())


class DefaultManifestUrlTests(unittest.TestCase):
    """由联机服务地址推导更新清单地址。"""

    def test_append_server_update_path(self) -> None:
        self.assertEqual(
            default_manifest_url("https://s.example"),
            "https://s.example/api/v1/updates/latest",
        )

    def test_trailing_slash_is_trimmed(self) -> None:
        self.assertEqual(
            default_manifest_url("https://s.example/"),
            "https://s.example/api/v1/updates/latest",
        )

    def test_empty_base_returns_none(self) -> None:
        self.assertIsNone(default_manifest_url(""))

    def test_none_base_returns_none(self) -> None:
        self.assertIsNone(default_manifest_url(None))

    def test_blank_base_returns_none(self) -> None:
        self.assertIsNone(default_manifest_url("   "))

    def test_server_update_path_constant(self) -> None:
        self.assertEqual(SERVER_UPDATE_PATH, "/api/v1/updates/latest")


if __name__ == "__main__":
    unittest.main()
