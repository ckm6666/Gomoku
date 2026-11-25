"""发布说明生成脚本的测试。

覆盖 README「当前版本」滚动写法的解析：只取本版内容、丢弃历史版本、
按分号拆成列表，并限制超长文本。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from tools.compose_release_notes import (  # noqa: E402
    MAX_NOTES_CHARS,
    build_release_notes,
    cut_at_next_version,
    extract_bracket,
    split_sentences,
    strip_leading_version,
    version_notes,
)

README_SAMPLE = """# 五子棋

- 可打包为单个 `Gomoku.exe`。

当前版本：**1.14.0**（完成 issue #33 的 51 项代码审查整改：自建房新增可选连珠规则、落子与积分结算单事务；客户端 737 项测试通过；1.13.0 新增主菜单「检查更新」入口；1.12.0 新增登录页“记住密码”）

> 联机功能需要同时运行本项目的服务端。
"""


class ExtractBracketTests(unittest.TestCase):
    def test_chinese_parentheses(self) -> None:
        self.assertEqual(extract_bracket("**1.14.0**（内容）"), "内容")

    def test_english_parentheses(self) -> None:
        self.assertEqual(extract_bracket("**1.14.0** (content)"), "content")

    def test_nested_parentheses_are_kept(self) -> None:
        self.assertEqual(extract_bracket("（外层（内层））"), "外层（内层）")

    def test_without_parentheses(self) -> None:
        self.assertEqual(extract_bracket("没有括号"), "")


class StripLeadingVersionTests(unittest.TestCase):
    def test_strips_bold_version(self) -> None:
        self.assertEqual(strip_leading_version("**1.14.0** 后续内容"), "后续内容")

    def test_strips_plain_version(self) -> None:
        self.assertEqual(strip_leading_version("1.14.0 后续内容"), "后续内容")

    def test_keeps_text_without_version(self) -> None:
        self.assertEqual(strip_leading_version("直接是内容"), "直接是内容")


class CutAtNextVersionTests(unittest.TestCase):
    def test_cuts_at_next_version(self) -> None:
        text = "本版内容；1.13.0 的历史内容；1.12.0 更早的内容"
        self.assertEqual(cut_at_next_version(text), "本版内容")

    def test_keeps_text_without_version(self) -> None:
        self.assertEqual(cut_at_next_version("只有本版内容"), "只有本版内容")

    def test_ignores_issue_numbers(self) -> None:
        text = "完成 issue #33 的整改；客户端 737 项测试通过"
        self.assertEqual(cut_at_next_version(text), text)

    def test_ignores_decimal_numbers(self) -> None:
        text = "兼容 Windows 11.2.1 客户端"
        self.assertEqual(cut_at_next_version(text), text)


class SplitSentencesTests(unittest.TestCase):
    def test_splits_on_chinese_semicolon(self) -> None:
        self.assertEqual(split_sentences("第一条；第二条"), ["第一条", "第二条"])

    def test_splits_on_english_semicolon(self) -> None:
        self.assertEqual(split_sentences("first; second"), ["first", "second"])

    def test_drops_empty_parts(self) -> None:
        self.assertEqual(split_sentences("第一条；；"), ["第一条"])


class VersionNotesTests(unittest.TestCase):
    def test_only_current_version_is_kept(self) -> None:
        notes = version_notes(README_SAMPLE, "1.14.0")
        self.assertIn("完成 issue #33 的 51 项代码审查整改", notes)
        self.assertNotIn("1.13.0", notes)
        self.assertNotIn("1.12.0", notes)

    def test_notes_are_rendered_as_list(self) -> None:
        notes = version_notes(README_SAMPLE, "1.14.0")
        self.assertTrue(notes.startswith("- "))
        self.assertIn("\n- ", notes)

    def test_no_line_is_a_single_run(self) -> None:
        notes = version_notes(README_SAMPLE, "1.14.0")
        for line in notes.splitlines():
            self.assertTrue(line.startswith("- "), line)

    def test_missing_summary_line(self) -> None:
        self.assertEqual(version_notes("# 标题\n没有版本行\n", "1.14.0"), "")

    def test_summary_without_parentheses(self) -> None:
        readme = "当前版本：**1.14.0** 只有本版内容\n"
        self.assertEqual(version_notes(readme, "1.14.0"), "- 只有本版内容")

    def test_version_without_any_text(self) -> None:
        readme = "当前版本：**1.14.0**\n"
        self.assertEqual(version_notes(readme, "1.14.0"), "")

    def test_overlong_notes_are_truncated(self) -> None:
        readme = "当前版本：**1.14.0**（" + "很长的内容" * 500 + "）\n"
        notes = version_notes(readme, "1.14.0")
        self.assertLessEqual(len(notes), MAX_NOTES_CHARS + 32)
        self.assertIn("已截断", notes)


class BuildReleaseNotesTests(unittest.TestCase):
    def test_contains_version_and_artifacts(self) -> None:
        notes = build_release_notes(
            "1.14.0", "v1.14.0", "ckm6666/gomoku", README_SAMPLE
        )
        self.assertIn("**版本 1.14.0**", notes)
        self.assertIn("Gomoku-1.14.0-windows-x64.zip", notes)
        self.assertIn("ghcr.io/ckm6666/gomoku/gomoku-server:1.14.0", notes)

    def test_history_versions_are_absent(self) -> None:
        notes = build_release_notes(
            "1.14.0", "v1.14.0", "ckm6666/gomoku", README_SAMPLE
        )
        self.assertNotIn("1.13.0 新增", notes)
        self.assertNotIn("1.12.0 新增", notes)

    def test_fallback_text_when_notes_missing(self) -> None:
        notes = build_release_notes("1.14.0", "v1.14.0", "ckm6666/gomoku", "# 空\n")
        self.assertIn("本次更新未附带说明。", notes)


class ScriptEntryPointTests(unittest.TestCase):
    def test_import_has_no_side_effects(self) -> None:
        result = os.system(
            f'{sys.executable} -c "import tools.compose_release_notes as m; '
            'print(m.__name__)"'
        )
        self.assertEqual(result, 0)


if __name__ == "__main__":
    unittest.main()
