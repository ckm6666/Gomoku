"""构建期固化配置的单元测试（纯标准库，不需要 Qt）。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from gomoku.client import build_info  # noqa: E402
from gomoku.client.build_info import BuildInfo  # noqa: E402


class BuildInfoTests(unittest.TestCase):
    """build_info.json 的读取与容错。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self._dir = Path(self._tmpdir.name)
        self._path = self._dir / build_info.BUILD_INFO_FILENAME

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def _write(self, text: str) -> None:
        self._path.write_text(text, encoding="utf-8")

    def _load_with_patched_path(self) -> BuildInfo:
        """把读取路径指向临时目录，保证测试可重复。"""
        with mock.patch.object(
            build_info, "build_info_path", return_value=self._path
        ):
            return build_info.load_build_info()

    def test_missing_file_returns_defaults(self) -> None:
        self.assertFalse(self._path.exists())
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertEqual(info.update_manifest_url, "")
        self.assertIsNone(info.auto_update_check)

    def test_dev_mode_reads_file_next_to_module(self) -> None:
        """开发模式下读取模块同目录的 build_info.json。"""
        self._write(
            json.dumps(
                {
                    "api_base_url": "https://s.example",
                    "update_manifest_url": "https://s.example/update.json",
                    "auto_update_check": False,
                },
                ensure_ascii=False,
            )
        )
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "https://s.example")
        self.assertEqual(
            info.update_manifest_url, "https://s.example/update.json"
        )
        self.assertIs(info.auto_update_check, False)

    def test_default_path_is_next_to_module_in_dev_mode(self) -> None:
        """未打包时默认路径就是模块所在目录。"""
        with mock.patch.object(sys, "frozen", False, create=True):
            path = build_info.build_info_path()
        self.assertEqual(path.parent, Path(build_info.__file__).parent)
        self.assertEqual(path.name, build_info.BUILD_INFO_FILENAME)

    def test_frozen_mode_uses_meipass(self) -> None:
        """打包后从解包目录（sys._MEIPASS）读取。"""
        with mock.patch.object(sys, "frozen", True, create=True), (
            mock.patch.object(sys, "_MEIPASS", str(self._dir), create=True)
        ):
            path = build_info.build_info_path()
        self.assertEqual(path, self._dir / build_info.BUILD_INFO_FILENAME)

    def test_invalid_json_degrades_to_defaults(self) -> None:
        self._write("{ this is not json ")
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertEqual(info.update_manifest_url, "")
        self.assertIsNone(info.auto_update_check)

    def test_non_object_json_degrades_to_defaults(self) -> None:
        self._write('["https://s.example"]')
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertEqual(info.update_manifest_url, "")
        self.assertIsNone(info.auto_update_check)

    def test_non_bool_auto_update_check_is_ignored(self) -> None:
        self._write(
            json.dumps(
                {
                    "api_base_url": "https://s.example",
                    "auto_update_check": "false",
                }
            )
        )
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "https://s.example")
        self.assertIsNone(info.auto_update_check)

    def test_non_string_values_are_ignored(self) -> None:
        self._write(
            json.dumps({"api_base_url": 123, "update_manifest_url": None})
        )
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertEqual(info.update_manifest_url, "")

    def test_empty_strings_are_normalised(self) -> None:
        self._write(
            json.dumps({"api_base_url": "", "update_manifest_url": "  "})
        )
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertEqual(info.update_manifest_url, "")

    def test_unreadable_file_degrades_to_defaults(self) -> None:
        """文件不可读（这里是目录）时也不能抛异常。"""
        self._dir.joinpath(build_info.BUILD_INFO_FILENAME).mkdir()
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "")
        self.assertIsNone(info.auto_update_check)

    def test_bom_is_tolerated(self) -> None:
        """带 BOM 的 UTF-8 文件也能正常解析。"""
        self._path.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps({"api_base_url": "https://s.example"}).encode("utf-8")
        )
        info = self._load_with_patched_path()
        self.assertEqual(info.api_base_url, "https://s.example")


if __name__ == "__main__":
    unittest.main(verbosity=2)
