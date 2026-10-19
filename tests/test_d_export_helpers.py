"""Synthetic D-only rules; not API07 or full product acceptance tests."""

import json
from pathlib import Path
import unittest

from pmkb.application.export.naming import default_export_name, selected_export_name
from pmkb.application.export.rendering import (
    ExportReference,
    ReferenceKind,
    render_export_markdown,
)
from pmkb.domain.text_spans import to_utf16_offset
from pmkb.infrastructure.file_io.windows_names import validate_windows_basename


COLLECTION = "0123456789abcdef0123456789abcdef"
NOTE = "abcdef0123456789abcdef0123456789"
MARKER = f"[[note:{COLLECTION}:{NOTE}]]"
FIXTURES = Path(__file__).parent / "test-data" / "d-export"


def reference(body, marker=MARKER, kind=ReferenceKind.VALID, name="数据结构复习"):
    start = body.index(marker)
    return ExportReference(
        start_utf16=to_utf16_offset(body, start),
        length_utf16=to_utf16_offset(marker, len(marker)),
        marker_text=marker,
        kind=kind,
        target_name=name,
    )


class WindowsBasenameTests(unittest.TestCase):
    def test_legal_names_are_not_normalized(self):
        for name in ["课程.md", "note-CON.md", "输出.MD", ".md", "COM10.md", "😀.txt"]:
            with self.subTest(name=name):
                self.assertEqual(validate_windows_basename(name), name)

    def test_path_ads_controls_and_trailing_characters_are_rejected(self):
        for name in ["", ".", "..", "a/b", "a\\b", "a:b", "<a>", 'a"', "a|b",
                     "a?b", "a*b", "a\x00", "a\n", "a\x1f", "a\x7f", "a ", "a.",
                     "x\ud800", "x\udfff", None, 42]:
            with self.subTest(name=repr(name)):
                with self.assertRaises(ValueError):
                    validate_windows_basename(name)

    def test_reserved_names_and_extension_variants_are_rejected(self):
        devices = ["CON", "NUL", "PRN", "AUX", "CONIN$", "CONOUT$"]
        devices += [f"{prefix}{digit}" for prefix in ("COM", "LPT")
                    for digit in "123456789¹²³"]
        for device in devices:
            for suffix in ("", ".md", ".extra.md", " .md"):
                with self.subTest(device=device, suffix=suffix):
                    with self.assertRaises(ValueError):
                        validate_windows_basename(device.lower() + suffix)


class ExportNamingTests(unittest.TestCase):
    def test_confirmed_default_examples_and_no_implicit_title_edit(self):
        examples = {"课程/笔记": "note-课程_笔记.md", "CON": "note-CON.md",
                    "复习.md": "note-复习.md.md", 'a<>:"/\\|?*b': "note-a_________b.md",
                    "课程.  ": "note-课程.md", "... ": "note.md", "": "note.md"}
        for title, expected in examples.items():
            with self.subTest(title=title):
                self.assertEqual(default_export_name(title), expected)

    def test_default_rejects_invalid_unicode_or_controls_not_a_title_validator(self):
        for title in (None, 42, "a\x00", "a\n", "a\x7f", "a\ud800"):
            with self.subTest(title=repr(title)):
                with self.assertRaises(ValueError):
                    default_export_name(title)

    def test_user_choice_only_appends_missing_extension(self):
        for name, expected in [("输出", "输出.md"), ("输出.MD", "输出.MD"),
                               ("输出.md", "输出.md"), (".hidden", ".hidden.md")]:
            with self.subTest(name=name):
                self.assertEqual(selected_export_name(name), expected)

    def test_user_choice_is_never_silently_sanitized(self):
        for name in ["a.txt", "a.md.exe", "a.", "a ", "课程/笔记", "CON", "NUL.md", "a:b", ""]:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    selected_export_name(name)


class ExportRenderingTests(unittest.TestCase):
    def test_exact_four_fr09_texts(self):
        cases = [
            (ReferenceKind.VALID, "数据结构复习", "内部引用：数据结构复习（仅供识别，导出后不支持应用内跳转）"),
            (ReferenceKind.DELETED, "数据结构复习", "失效内部引用：数据结构复习（目标已删除，无法跳转）"),
            (ReferenceKind.MISSING, None, "失效内部引用：未知目标（目标标识不存在或不可用，无法跳转）"),
            (ReferenceKind.FOREIGN_COLLECTION, None, "失效内部引用：未知目标（引用来自其他集合，无法跳转）"),
        ]
        for kind, name, expected in cases:
            with self.subTest(kind=kind):
                self.assertEqual(render_export_markdown(MARKER, [reference(MARKER, kind=kind, name=name)]), expected)

    def test_missing_deleted_history_is_explicit_unknown(self):
        self.assertEqual(
            render_export_markdown(MARKER, [reference(MARKER, kind=ReferenceKind.DELETED, name=None)]),
            "失效内部引用：未知目标（目标已删除，无法跳转）",
        )

    def test_supplied_trusted_history_is_preserved_for_missing_and_foreign(self):
        cases = [(ReferenceKind.MISSING, "目标标识不存在或不可用，无法跳转"),
                 (ReferenceKind.FOREIGN_COLLECTION, "引用来自其他集合，无法跳转")]
        for kind, reason in cases:
            with self.subTest(kind=kind):
                self.assertEqual(
                    render_export_markdown(MARKER, [reference(MARKER, kind=kind, name="旧[标题]")]),
                    f"失效内部引用：旧\\[标题\\]（{reason}）",
                )

    def test_names_cannot_inject_markdown_links_or_html(self):
        name = "[a](https://example.test) <b> &copy; `x` *y* _z_ ![i] [[note:x]] " + "\\"
        result = render_export_markdown(MARKER, [reference(MARKER, name=name)])
        expected_name = r"\[a\]\(https\:\/\/example\.test\) \<b\> \&copy\; \`x\` \*y\* \_z\_ \!\[i\] \[\[note\:x\]\] \\"
        self.assertEqual(result, f"内部引用：{expected_name}（仅供识别，导出后不支持应用内跳转）")

    def test_only_supplied_span_is_replaced_not_identical_code_or_links(self):
        body = f"😀正文\r\n{MARKER}\r\n`{MARKER}`\r\n```md\r\n{MARKER}\r\n```\r\n[外链](https://example.test) ![图](../图片.png)\r\n"
        expected = body.replace(MARKER, "内部引用：数据结构复习（仅供识别，导出后不支持应用内跳转）", 1)
        self.assertEqual(render_export_markdown(body, [reference(body)]), expected)

    def test_no_spans_means_no_discovery_or_reformatting(self):
        body = f"\ufeff前文\r\n{MARKER}\n  尾部  \n"
        self.assertEqual(render_export_markdown(body, []), body)

    def test_adjacent_spans_and_unsorted_input_are_supported(self):
        other = f"[[note:{COLLECTION}:{'1' * 32}]]"
        body = MARKER + other
        refs = [reference(body, marker=other, name="乙"), reference(body, name="甲")]
        self.assertEqual(render_export_markdown(body, refs),
                         "内部引用：甲（仅供识别，导出后不支持应用内跳转）内部引用：乙（仅供识别，导出后不支持应用内跳转）")

    def test_bad_spans_marker_mismatch_or_kind_fail_closed(self):
        body = "😀" + MARKER
        valid = reference(body)
        bad = [
            ExportReference(-1, len(MARKER), MARKER, ReferenceKind.VALID, "甲"),
            ExportReference(1, len(MARKER), MARKER, ReferenceKind.VALID, "甲"),
            ExportReference(2, len(MARKER) + 1, MARKER, ReferenceKind.VALID, "甲"),
            ExportReference(2, 0, MARKER, ReferenceKind.VALID, "甲"),
            ExportReference(True, len(MARKER), MARKER, ReferenceKind.VALID, "甲"),
            ExportReference(2, len(MARKER), "wrong", ReferenceKind.VALID, "甲"),
            ExportReference(2, len(MARKER), MARKER, "VALID", "甲"),
        ]
        for item in bad:
            with self.subTest(item=repr(item)):
                with self.assertRaises(ValueError):
                    render_export_markdown(body, [item])
        with self.assertRaises(ValueError):
            render_export_markdown(body, [valid, valid])

    def test_invalid_marker_literal_is_not_accepted_as_an_authorized_reference(self):
        for marker in ["[[note:x]]", MARKER.upper(), "not a marker"]:
            with self.subTest(marker=marker):
                with self.assertRaises(ValueError):
                    render_export_markdown(marker, [reference(marker, marker=marker)])

    def test_invalid_source_or_display_name_is_rejected(self):
        for body in (None, 42, "x\ud800"):
            with self.subTest(body=repr(body)):
                with self.assertRaises(ValueError):
                    render_export_markdown(body, [])
        for name in (None, "", "a\n", "a\r", "a\x00", "x\ud800", "x\u2028"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(ValueError):
                    render_export_markdown(MARKER, [reference(MARKER, name=name)])

    def test_checked_in_synthetic_expected_output(self):
        body = (FIXTURES / "source.md").read_bytes().decode("utf-8")
        expected = (FIXTURES / "expected.md").read_bytes().decode("utf-8")
        records = json.loads((FIXTURES / "references.json").read_text(encoding="utf-8"))
        refs = [ExportReference(item["start_utf16"], item["length_utf16"], item["marker_text"],
                                ReferenceKind(item["kind"]), item["target_name"])
                for item in records]
        self.assertEqual(render_export_markdown(body, refs), expected)


if __name__ == "__main__":
    unittest.main()
