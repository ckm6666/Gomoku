"""更新检查（版本比较 + Release 解析）的纯 Python 测试（不依赖 Qt）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.shared.update_check import (  # noqa: E402
    ELLIPSIS,
    ReleaseAsset,
    ReleaseInfo,
    compare_versions,
    is_newer_version,
    normalize_digest,
    parse_release_payload,
    parse_version,
    select_windows_asset,
    summarize_notes,
)

FULL_PAYLOAD = {
    "tag_name": "v1.13.0",
    "name": "1.13.0",
    "html_url": "https://github.com/ckm6666/Gomoku/releases/tag/v1.13.0",
    "body": "**版本 1.13.0**\n\n- 修复联机断线重连\n- 新增每日一题",
    "published_at": "2026-11-20T10:00:00Z",
    "prerelease": False,
    "draft": False,
    "assets": [
        {
            "name": "Gomoku-1.13.0-windows-x64.zip",
            "browser_download_url": "https://example.com/Gomoku-1.13.0-windows-x64.zip",
            "size": 25165824,
            "digest": "sha256:" + "ab" * 32,
        },
        {
            "name": "Gomoku-1.13.0-linux-x64.zip",
            "browser_download_url": "https://example.com/Gomoku-1.13.0-linux-x64.zip",
            "size": 24117248,
        },
    ],
}


class ParseVersionTests(unittest.TestCase):
    """版本号解析应容忍前缀、缺省修订号与预发布后缀。"""

    def test_plain_version(self) -> None:
        self.assertEqual(parse_version("1.13.0"), (1, 13, 0))

    def test_v_prefix_is_ignored(self) -> None:
        self.assertEqual(parse_version("v1.13.0"), (1, 13, 0))

    def test_missing_patch_defaults_to_zero(self) -> None:
        self.assertEqual(parse_version("1.13"), (1, 13, 0))

    def test_prerelease_suffix_is_ignored(self) -> None:
        self.assertEqual(parse_version("v1.13.0-rc.1"), (1, 13, 0))

    def test_invalid_inputs_return_none(self) -> None:
        for text in ("", "   ", "latest", "abc", "v", "-", "nightly"):
            with self.subTest(text=text):
                self.assertIsNone(parse_version(text))

    def test_never_raises(self) -> None:
        self.assertIsNone(parse_version(None))  # type: ignore[arg-type]


class CompareVersionsTests(unittest.TestCase):
    """版本比较应区分大小、忽略 v 前缀并让预发布版排在正式版之前。"""

    def test_equal_versions(self) -> None:
        self.assertEqual(compare_versions("1.13.0", "1.13.0"), 0)

    def test_greater_version(self) -> None:
        self.assertEqual(compare_versions("1.14.0", "1.13.0"), 1)

    def test_lesser_version(self) -> None:
        self.assertEqual(compare_versions("1.12.9", "1.13.0"), -1)

    def test_v_prefix_is_equivalent(self) -> None:
        self.assertEqual(compare_versions("v1.13.0", "1.13.0"), 0)

    def test_prerelease_sorts_before_release(self) -> None:
        self.assertEqual(compare_versions("1.13.0-rc.1", "1.13.0"), -1)
        self.assertEqual(compare_versions("1.13.0", "1.13.0-rc.1"), 1)

    def test_different_prerelease_suffixes_are_equal(self) -> None:
        self.assertEqual(compare_versions("1.13.0-rc.1", "1.13.0-beta"), 0)

    def test_garbage_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            compare_versions("latest", "1.13.0")
        with self.assertRaises(ValueError):
            compare_versions("1.13.0", "")


class IsNewerVersionTests(unittest.TestCase):
    """只有确实更新时才提示玩家。"""

    def test_newer_is_true(self) -> None:
        self.assertTrue(is_newer_version("1.13.0", "1.12.0"))

    def test_older_is_false(self) -> None:
        self.assertFalse(is_newer_version("1.12.0", "1.13.0"))

    def test_same_is_false(self) -> None:
        self.assertFalse(is_newer_version("1.13.0", "1.13.0"))

    def test_unparsable_is_false(self) -> None:
        self.assertFalse(is_newer_version("latest", "1.13.0"))
        self.assertFalse(is_newer_version("1.13.0", "nightly"))
        self.assertFalse(is_newer_version("", ""))


class ParseReleasePayloadTests(unittest.TestCase):
    """GitHub Release JSON 应被解析成结构化数据，畸形条目要被过滤。"""

    def test_full_payload(self) -> None:
        release = parse_release_payload(FULL_PAYLOAD)
        self.assertEqual(release.tag, "v1.13.0")
        self.assertEqual(release.version, "1.13.0")
        self.assertEqual(release.title, "1.13.0")
        self.assertEqual(
            release.url,
            "https://github.com/ckm6666/Gomoku/releases/tag/v1.13.0",
        )
        self.assertIn("修复联机断线重连", release.notes)
        self.assertEqual(release.published_at, "2026-11-20T10:00:00Z")
        self.assertFalse(release.prerelease)
        self.assertEqual(len(release.assets), 2)
        self.assertEqual(release.assets[0].name, "Gomoku-1.13.0-windows-x64.zip")
        self.assertEqual(release.assets[0].size, 25165824)

    def test_asset_digest_is_parsed(self) -> None:
        """发布里的摘要要被带出来，客户端靠它校验下载到的安装包。"""
        release = parse_release_payload(FULL_PAYLOAD)
        self.assertEqual(
            release.assets[0].digest, "sha256:" + "ab" * 32
        )
        # 没有摘要字段的产物留空串，不影响其它字段。
        self.assertEqual(release.assets[1].digest, "")

    def test_non_string_digest_is_ignored(self) -> None:
        """摘要不是字符串时按「没有摘要」处理。"""
        payload = dict(FULL_PAYLOAD)
        payload["assets"] = [
            {
                "name": "a.zip",
                "browser_download_url": "https://example.com/a.zip",
                "size": 1,
                "digest": 12345,
            }
        ]
        release = parse_release_payload(payload)
        self.assertEqual(release.assets[0].digest, "")

    def test_missing_tag_name_raises(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload.pop("tag_name")
        with self.assertRaises(ValueError):
            parse_release_payload(payload)

    def test_empty_or_invalid_tag_name_raises(self) -> None:
        for tag in ("", "   ", 123, None):
            with self.subTest(tag=tag):
                payload = dict(FULL_PAYLOAD)
                payload["tag_name"] = tag
                with self.assertRaises(ValueError):
                    parse_release_payload(payload)

    def test_malformed_assets_are_filtered(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload["assets"] = [
            "not-a-mapping",
            {"name": "", "browser_download_url": "https://example.com/a.zip"},
            {"name": "missing-url.zip"},
            {"browser_download_url": "https://example.com/b.zip"},
            {"name": "ok.zip", "browser_download_url": "https://example.com/c.zip"},
        ]
        release = parse_release_payload(payload)
        self.assertEqual(len(release.assets), 1)
        self.assertEqual(release.assets[0].name, "ok.zip")
        self.assertEqual(release.assets[0].size, 0)

    def test_negative_size_falls_back_to_zero(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload["assets"] = [
            {
                "name": "Gomoku-1.13.0-windows-x64.zip",
                "browser_download_url": "https://example.com/a.zip",
                "size": -5,
            }
        ]
        release = parse_release_payload(payload)
        self.assertEqual(release.assets[0].size, 0)

    def test_missing_name_and_body_fall_back(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload.pop("name")
        payload.pop("body")
        payload.pop("html_url")
        payload.pop("published_at")
        release = parse_release_payload(payload)
        self.assertEqual(release.title, "v1.13.0")
        self.assertEqual(release.notes, "")
        self.assertEqual(release.url, "")
        self.assertEqual(release.published_at, "")

    def test_non_string_body_falls_back_to_empty(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload["body"] = 42
        payload["prerelease"] = "yes"
        release = parse_release_payload(payload)
        self.assertEqual(release.notes, "")
        self.assertTrue(release.prerelease)

    def test_tag_without_version_falls_back_to_tag(self) -> None:
        payload = dict(FULL_PAYLOAD)
        payload["tag_name"] = "vnightly"
        release = parse_release_payload(payload)
        self.assertEqual(release.version, "nightly")
        self.assertEqual(release.tag, "vnightly")


class SelectWindowsAssetTests(unittest.TestCase):
    """应优先选中发布流水线约定的 Windows 压缩包。"""

    def test_exact_name_match(self) -> None:
        release = parse_release_payload(FULL_PAYLOAD)
        asset = select_windows_asset(release)
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.name, "Gomoku-1.13.0-windows-x64.zip")

    def test_suffix_match(self) -> None:
        release = ReleaseInfo(
            tag="v1.13.0",
            version="1.13.0",
            title="",
            url="",
            notes="",
            published_at="",
            assets=(
                ReleaseAsset("other.zip", "https://example.com/o.zip", 1),
                ReleaseAsset(
                    "Gomoku-1.13.0-WINDOWS-X64.zip", "https://example.com/w.zip", 2
                ),
            ),
            prerelease=False,
        )
        asset = select_windows_asset(release)
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.name, "Gomoku-1.13.0-WINDOWS-X64.zip")

    def test_zip_with_windows_in_name(self) -> None:
        release = ReleaseInfo(
            tag="v1.13.0",
            version="1.13.0",
            title="",
            url="",
            notes="",
            published_at="",
            assets=(
                ReleaseAsset("src.tar.gz", "https://example.com/s.tar.gz", 1),
                ReleaseAsset(
                    "Gomoku-Windows-Bundle.zip", "https://example.com/b.zip", 2
                ),
            ),
            prerelease=False,
        )
        asset = select_windows_asset(release)
        self.assertIsNotNone(asset)
        assert asset is not None
        self.assertEqual(asset.name, "Gomoku-Windows-Bundle.zip")

    def test_no_match_returns_none(self) -> None:
        release = ReleaseInfo(
            tag="v1.13.0",
            version="1.13.0",
            title="",
            url="",
            notes="",
            published_at="",
            assets=(ReleaseAsset("src.tar.gz", "https://example.com/s.tar.gz", 1),),
            prerelease=False,
        )
        self.assertIsNone(select_windows_asset(release))

    def test_release_without_assets_returns_none(self) -> None:
        release = ReleaseInfo(
            tag="v1.13.0",
            version="1.13.0",
            title="",
            url="",
            notes="",
            published_at="",
            assets=(),
            prerelease=False,
        )
        self.assertIsNone(select_windows_asset(release))


class SummarizeNotesTests(unittest.TestCase):
    """发布说明应折叠成单行并在超长时截断。"""

    def test_whitespace_is_collapsed(self) -> None:
        summary = summarize_notes("  **版本 1.13.0**\n\n- 修复 A\n- 修复 B  ")
        self.assertEqual(summary, "**版本 1.13.0** - 修复 A - 修复 B")

    def test_long_notes_are_truncated(self) -> None:
        summary = summarize_notes("x" * 500, limit=10)
        self.assertEqual(summary, "x" * 10 + ELLIPSIS)
        self.assertEqual(len(summary), 11)

    def test_exact_length_is_not_truncated(self) -> None:
        self.assertEqual(summarize_notes("x" * 10, limit=10), "x" * 10)

    def test_empty_input(self) -> None:
        self.assertEqual(summarize_notes(""), "")
        self.assertEqual(summarize_notes("   \n\t "), "")


class ReleasePipelineTests(unittest.TestCase):
    """端到端：GitHub 响应 -> ReleaseInfo -> 是否需要提示更新。

    客户端真正走的是这条链路，单测 ``compare_versions`` 覆盖不到这里的
    坑（标签解析、预发布后缀、非版本标签）。
    """

    def _payload(self, tag: str) -> dict:
        payload = dict(FULL_PAYLOAD)
        payload["tag_name"] = tag
        return payload

    def test_stable_release_newer_than_current(self) -> None:
        release = parse_release_payload(self._payload("v1.13.0"))
        self.assertEqual(release.version, "1.13.0")
        self.assertTrue(is_newer_version(release.version, "1.12.0"))

    def test_prerelease_tag_keeps_its_suffix(self) -> None:
        """RC 标签不能被截成正式版号，否则会指向并不存在的安装包。"""
        release = parse_release_payload(self._payload("v1.13.0-rc.1"))
        self.assertEqual(release.version, "1.13.0-rc.1")
        # RC 比 1.12.0 新，但比同版本正式版旧。
        self.assertTrue(is_newer_version(release.version, "1.12.0"))
        self.assertFalse(is_newer_version(release.version, "1.13.0"))

    def test_non_version_tag_is_not_a_version(self) -> None:
        """``nightly-20241120`` 不是版本号，不能被读成 20241120.0.0。"""
        self.assertIsNone(parse_version("nightly-20241120"))
        self.assertIsNone(parse_version("release-1.13.0"))

        release = parse_release_payload(self._payload("nightly-20241120"))
        self.assertEqual(release.version, "nightly-20241120")
        # 版本无法识别时不得提示有更新。
        self.assertFalse(is_newer_version(release.version, "1.12.0"))

    def test_same_version_is_not_an_update(self) -> None:
        release = parse_release_payload(self._payload("v1.12.0"))
        self.assertEqual(release.version, "1.12.0")
        self.assertFalse(is_newer_version(release.version, "1.12.0"))

    def test_prerelease_asset_is_still_found(self) -> None:
        """RC 版本的压缩包名不同，但后缀匹配仍能找到产物。"""
        payload = self._payload("v1.13.0-rc.1")
        payload["assets"] = [
            {
                "name": "Gomoku-1.13.0-rc.1-windows-x64.zip",
                "browser_download_url": (
                    "https://github.com/ckm6666/Gomoku/releases/download/"
                    "v1.13.0-rc.1/Gomoku-1.13.0-rc.1-windows-x64.zip"
                ),
                "size": 1024,
            },
        ]
        release = parse_release_payload(payload)
        asset = select_windows_asset(release)
        self.assertIsNotNone(asset)
        self.assertEqual(asset.name, "Gomoku-1.13.0-rc.1-windows-x64.zip")


class NormalizeDigestTests(unittest.TestCase):
    """GitHub 的 ``sha256:<hex>`` 摘要应被归一成裸十六进制。"""

    def test_valid_digest_is_lowercased(self) -> None:
        digest = "sha256:" + "AB" * 32
        self.assertEqual(normalize_digest(digest), "ab" * 32)

    def test_surrounding_whitespace_is_ignored(self) -> None:
        digest = "  sha256:" + "cd" * 32 + "\n"
        self.assertEqual(normalize_digest(digest), "cd" * 32)

    def test_invalid_digests_are_rejected(self) -> None:
        for bad in (
            "",
            "   ",
            "md5:" + "0" * 32,
            "sha256:",
            "sha256:" + "0" * 63,
            "sha256:" + "0" * 65,
            "sha256:" + "z" * 64,
            "0" * 64,
            "sha256:ABC",
        ):
            with self.subTest(digest=bad):
                self.assertIsNone(normalize_digest(bad))


if __name__ == "__main__":
    unittest.main()
