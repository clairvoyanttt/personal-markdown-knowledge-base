"""C shared Markdown adapter: protected spans, reference occurrences, blocks.

The scanner must be the single source for preview, references, export and
restore. Tests below pin the behaviour that the other modules are allowed to
rely on: code/link/image/escape protection, complete-marker-only recognition,
UTF-16 half-open spans, and structural blocks for the six preview categories.
"""
from __future__ import annotations

import unittest

from pmkb.application.parsing import (
    BlockKind,
    Ownership,
    ProtectedKind,
    parse_markdown,
)
from pmkb.domain.rules import RuleViolation
from pmkb.domain.text_spans import to_utf16_offset

COLLECTION = "0123456789abcdef0123456789abcdef"
OTHER_COLLECTION = "fedcba9876543210fedcba9876543210"
NOTE = "aabbccddeeff00112233445566778899"
MARKER = f"[[note:{COLLECTION}:{NOTE}]]"
FOREIGN = f"[[note:{OTHER_COLLECTION}:{NOTE}]]"


def kinds(document) -> list[ProtectedKind]:
    return [span.kind for span in document.protected_spans]


class MarkerTests(unittest.TestCase):
    def test_complete_marker_in_a_paragraph_is_a_local_occurrence(self) -> None:
        body = f"前言 {MARKER} 后记"
        document = parse_markdown(body, COLLECTION)
        self.assertEqual(len(document.references), 1)
        occurrence = document.references[0]
        self.assertEqual(occurrence.ordinal, 0)
        self.assertIs(occurrence.ownership, Ownership.LOCAL)
        self.assertEqual(occurrence.target_collection_id, COLLECTION)
        self.assertEqual(occurrence.target_note_id, NOTE)
        self.assertEqual(occurrence.length_utf16, 74)
        self.assertEqual(occurrence.start_utf16, to_utf16_offset(body, body.index(MARKER)))
        self.assertEqual(document.slice_span(occurrence.start_utf16, 74), MARKER)
        self.assertEqual(document.body, body)

    def test_foreign_ownership_is_detected_without_rewriting_the_marker(self) -> None:
        document = parse_markdown(f"复制 {FOREIGN}", COLLECTION)
        self.assertEqual(len(document.references), 1)
        self.assertIs(document.references[0].ownership, Ownership.FOREIGN)
        self.assertEqual(document.references[0].target_note_id, NOTE)

    def test_incomplete_old_or_uppercase_markers_stay_text(self) -> None:
        body = " | ".join([
            "[[note:]]",
            "[[note:K1]]",
            f"[[note:{COLLECTION}]]",
            f"[[note:{COLLECTION.upper()}:{NOTE.upper()}]]",
            "[[note:" + COLLECTION + ":" + NOTE,
        ])
        self.assertEqual(parse_markdown(body, COLLECTION).references, ())

    def test_ordinals_follow_source_order(self) -> None:
        body = f"{MARKER}\n\n{MARKER}\n\n{FOREIGN}"
        references = parse_markdown(body, COLLECTION).references
        self.assertEqual([item.ordinal for item in references], [0, 1, 2])
        self.assertEqual([item.start_utf16 for item in references],
                         sorted(item.start_utf16 for item in references))
        self.assertEqual([item.ownership for item in references],
                         [Ownership.LOCAL, Ownership.LOCAL, Ownership.FOREIGN])


class ProtectionTests(unittest.TestCase):
    def assertHidden(self, body: str, expected_kind: ProtectedKind) -> None:
        document = parse_markdown(body, COLLECTION)
        self.assertIn(expected_kind, kinds(document))
        self.assertEqual(document.references, (), f"marker was accepted in {body!r}")

    def test_fenced_code_backticks_and_tildes_hide_markers(self) -> None:
        self.assertHidden(f"```\n{MARKER}\n```", ProtectedKind.FENCED_CODE)
        self.assertHidden(f"~~~markdown\n{MARKER}\n~~~", ProtectedKind.FENCED_CODE)
        # An unclosed fence protects the remainder of the document.
        self.assertHidden(f"```\n{MARKER}\nno close", ProtectedKind.FENCED_CODE)

    def test_inline_code_hides_markers(self) -> None:
        self.assertHidden(f"``{MARKER}`` 之后", ProtectedKind.INLINE_CODE)
        self.assertHidden(f"`a {MARKER} b`", ProtectedKind.INLINE_CODE)

    def test_links_images_autolinks_and_escape_hide_markers(self) -> None:
        self.assertHidden(f"[说明 {MARKER}](https://example.com)", ProtectedKind.LINK)
        self.assertHidden(f"[x](https://example.com/{MARKER})", ProtectedKind.LINK)
        self.assertHidden(f"![图]({MARKER}.png)", ProtectedKind.IMAGE)
        self.assertHidden(f"<https://example.com/?q={MARKER}>", ProtectedKind.AUTOLINK)
        self.assertHidden(f"\\{MARKER} 转义", ProtectedKind.ESCAPED)

    def test_reference_definition_line_is_protected(self) -> None:
        body = f"[doc]: https://example.com/{MARKER}\n\n{MARKER}"
        document = parse_markdown(body, COLLECTION)
        self.assertIn(ProtectedKind.REFERENCE_DEFINITION, kinds(document))
        self.assertEqual(len(document.references), 1)

    def test_escaped_backslash_then_marker_is_still_a_reference(self) -> None:
        body = f"\\\\{MARKER}"
        document = parse_markdown(body, COLLECTION)
        self.assertEqual(len(document.references), 1)
        self.assertEqual(document.references[0].start_utf16, 2)

    def test_code_inside_paragraph_only_protects_its_own_span(self) -> None:
        body = f"`{MARKER}` 与 {MARKER}"
        document = parse_markdown(body, COLLECTION)
        self.assertEqual(len(document.references), 1)
        self.assertEqual(document.references[0].start_utf16, to_utf16_offset(
            body, body.rindex(MARKER)))


class SpanAndBlockTests(unittest.TestCase):
    def test_utf16_offsets_shift_after_astral_characters(self) -> None:
        body = "😀😀" + MARKER
        document = parse_markdown(body, COLLECTION)
        # Two code points but four UTF-16 units.
        self.assertEqual(document.references[0].start_utf16, 4)

    def test_crlf_lines_are_counted_as_two_units(self) -> None:
        body = f"第一行\r\n第二行 {MARKER}\r\n"
        document = parse_markdown(body, COLLECTION)
        self.assertEqual(document.references[0].start_utf16,
                         to_utf16_offset(body, body.index(MARKER)))

    def test_six_preview_categories_are_outlined(self) -> None:
        body = (
            "# 标题\n\n"
            "段落文字\n第二行\n\n"
            "- 列表项\n"
            "> 引用\n"
            "```python\nprint('代码')\n```\n"
            "[链接](https://example.com)\n"
        )
        document = parse_markdown(body, COLLECTION)
        kinds_found = [block.kind for block in document.blocks]
        self.assertEqual(kinds_found[:4], [
            BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.BLOCK_QUOTE,
        ])
        self.assertIn(BlockKind.CODE_FENCE, kinds_found)
        heading = document.blocks[0]
        self.assertEqual(heading.level, 1)
        self.assertEqual(document.slice_span(heading.start_utf16, heading.length_utf16), "# 标题")
        list_item = next(block for block in document.blocks if block.kind is BlockKind.LIST_ITEM)
        self.assertEqual(list_item.marker, "-")
        code_block = next(block for block in document.blocks if block.kind is BlockKind.CODE_FENCE)
        self.assertIn("print('代码')",
                      document.slice_span(code_block.start_utf16, code_block.length_utf16))

    def test_headings_need_a_space_and_lists_accept_ordered_markers(self) -> None:
        document = parse_markdown("#hashtag\n\n1. first\n2) second\n", COLLECTION)
        self.assertEqual([b.kind for b in document.blocks],
                         [BlockKind.PARAGRAPH, BlockKind.LIST_ITEM, BlockKind.LIST_ITEM])
        self.assertEqual([b.marker for b in document.blocks if b.kind is BlockKind.LIST_ITEM],
                         ["1.", "2)"])

    def test_protected_spans_are_sorted_and_inside_the_body(self) -> None:
        body = f"`{MARKER}`\n\n```\n{MARKER}\n```\n\n[M]({MARKER})"
        document = parse_markdown(body, COLLECTION)
        self.assertEqual(document.protected_spans,
                         tuple(sorted(document.protected_spans, key=lambda span: span.start_utf16)))
        for span in document.protected_spans:
            self.assertGreaterEqual(span.start_utf16, 0)
            self.assertLessEqual(span.start_utf16 + span.length_utf16,
                                 to_utf16_offset(body, len(body)))
            self.assertTrue(document.is_protected(span.start_utf16, span.length_utf16))

    def test_invalid_input_is_rejected(self) -> None:
        with self.assertRaises(RuleViolation):
            parse_markdown("bad\ud800body", COLLECTION)
        with self.assertRaises(RuleViolation):
            parse_markdown(b"bytes", COLLECTION)
        with self.assertRaises(RuleViolation):
            parse_markdown("text", "not-an-identity")

    def test_empty_body_is_empty_not_an_error(self) -> None:
        document = parse_markdown("", COLLECTION)
        self.assertEqual(document.references, ())
        self.assertEqual(document.blocks, ())
        self.assertEqual(document.protected_spans, ())


if __name__ == "__main__":
    unittest.main()
