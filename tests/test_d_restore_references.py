"""Synthetic D-only ownership conversion; no parser, database or full restore."""

from dataclasses import FrozenInstanceError, replace
import unittest

from pmkb.application.backup_restore.references import (
    RestoreReference,
    RestoreTextResult,
    restore_reference_ownership,
)
from pmkb.application.export.rendering import ReferenceKind
from pmkb.domain.text_spans import to_utf16_offset


SOURCE = "0123456789abcdef0123456789abcdef"
NEW = "fedcba9876543210fedcba9876543210"
OTHER_NEW = "c" * 32
FOREIGN = "a" * 32
NOTE = "b" * 32


def marker(collection=SOURCE, note=NOTE):
    return f"[[note:{collection}:{note}]]"


def occurrence(body, text=None, kind=ReferenceKind.VALID, hint=None, *, start=None):
    text = marker() if text is None else text
    start = body.index(text) if start is None else start
    return RestoreReference(to_utf16_offset(body, start), 74, text, kind, hint)


class RestoreOwnershipTests(unittest.TestCase):
    def test_source_valid_deleted_and_missing_convert_foreign_does_not(self):
        kinds = (ReferenceKind.VALID, ReferenceKind.DELETED, ReferenceKind.MISSING,
                 ReferenceKind.FOREIGN_COLLECTION)
        texts = [marker(SOURCE, str(index) * 32) for index in range(3)]
        texts.append(marker(FOREIGN))
        body = "\r\n".join(texts)
        refs = [occurrence(body, text, kind, "旧[标题]")
                for text, kind in zip(texts, kinds)]
        result = restore_reference_ownership(body, SOURCE, NEW, refs)
        self.assertIsInstance(result, RestoreTextResult)
        self.assertEqual(result.body, body.replace(SOURCE, NEW))
        self.assertEqual(result.changed_occurrences, 3)
        self.assertEqual(tuple(item.kind for item in result.references), kinds)
        self.assertEqual(tuple(item.display_hint for item in result.references),
                         ("旧[标题]",) * 4)
        for original, restored in zip(refs, result.references):
            self.assertEqual(restored.start_utf16, original.start_utf16)
            self.assertEqual(restored.length_utf16, original.length_utf16)
        self.assertEqual(result.references[-1].marker_text, texts[-1])

    def test_same_note_id_in_other_collection_does_not_rebind(self):
        local, foreign = marker(), marker(FOREIGN)
        body = local + foreign
        result = restore_reference_ownership(body, SOURCE, NEW, [
            occurrence(body, local),
            occurrence(body, foreign, ReferenceKind.FOREIGN_COLLECTION),
        ])
        self.assertEqual(result.body, marker(NEW) + foreign)
        self.assertEqual(result.references[1].kind, ReferenceKind.FOREIGN_COLLECTION)

    def test_deleted_and_missing_same_name_remain_invalid_and_keep_note_id(self):
        body = marker()
        for kind in (ReferenceKind.DELETED, ReferenceKind.MISSING):
            with self.subTest(kind=kind):
                original = occurrence(body, kind=kind, hint="重建同名笔记")
                result = restore_reference_ownership(body, SOURCE, NEW, [original])
                self.assertEqual(result.references[0], replace(original, marker_text=marker(NEW)))
                self.assertEqual(result.references[0].kind, kind)

    def test_only_authorized_spans_change_not_code_links_images_or_similar_text(self):
        text = marker()
        body = (f"😀前言\r\n{text}\r\n`{text}`\r\n```md\r\n{text}\r\n```\r\n"
                f"[外链](https://example.test/{SOURCE}) ![图片](../{SOURCE}.png)\r\n"
                f"普通相似文字 [[note:{SOURCE}:not-valid]]  尾部  ")
        result = restore_reference_ownership(body, SOURCE, NEW, [occurrence(body)])
        self.assertEqual(result.body, body.replace(text, marker(NEW), 1))
        self.assertEqual(result.references[0].start_utf16, to_utf16_offset(body, body.index(text)))
        self.assertEqual(len(result.body.encode("utf-16-le")), len(body.encode("utf-16-le")))

    def test_no_occurrences_does_not_discover_markers(self):
        body = "\ufeff😀\r\n" + marker() + "\n  "
        result = restore_reference_ownership(body, SOURCE, NEW, [])
        self.assertEqual(result.body, body)
        self.assertEqual(result.references, ())
        self.assertEqual(result.changed_occurrences, 0)

    def test_foreign_only_has_no_changes(self):
        text = marker(FOREIGN)
        ref = occurrence(text, text, ReferenceKind.FOREIGN_COLLECTION)
        result = restore_reference_ownership(text, SOURCE, NEW, [ref])
        self.assertEqual(result, RestoreTextResult(text, (ref,), 0))

    def test_adjacent_and_unsorted_occurrences_are_returned_in_source_order(self):
        first, second = marker(), marker(SOURCE, "1" * 32)
        body = first + second
        first_ref = occurrence(body, first)
        second_ref = occurrence(body, second, ReferenceKind.MISSING)
        result = restore_reference_ownership(body, SOURCE, NEW, [second_ref, first_ref])
        self.assertEqual(result.body, marker(NEW) + marker(NEW, "1" * 32))
        self.assertEqual([item.start_utf16 for item in result.references], [0, 74])
        self.assertEqual(result.changed_occurrences, 2)

    def test_duplicate_occurrence_is_rejected(self):
        body = marker()
        ref = occurrence(body)
        with self.assertRaises(ValueError):
            restore_reference_ownership(body, SOURCE, NEW, [ref, ref])

    def test_overlapping_span_is_rejected_even_with_matching_identical_markers(self):
        # No two valid literal markers can partially overlap. The second span
        # therefore fails exact source validation before any conversion occurs.
        body = marker() + marker()
        first = occurrence(body)
        overlapping = replace(first, start_utf16=1)
        with self.assertRaises(ValueError):
            restore_reference_ownership(body, SOURCE, NEW, [first, overlapping])

    def test_bad_source_text_or_surrogate_outside_span_is_rejected(self):
        for body in (None, 1, b"text", "\ud800" + marker(), marker() + "\udfff"):
            with self.subTest(body=repr(body)), self.assertRaises(ValueError):
                restore_reference_ownership(body, SOURCE, NEW, [])

    def test_source_and_new_ids_use_exact_lowercase_32_hex(self):
        invalid = (None, True, 42, b"a" * 32, "", "a" * 31, "a" * 33,
                   "A" * 32, "g" * 32, " " + SOURCE, SOURCE + "\n", "\ud800" * 32)
        for value in invalid:
            for source, new in ((value, NEW), (SOURCE, value)):
                with self.subTest(source=repr(source), new=repr(new)), self.assertRaises(ValueError):
                    restore_reference_ownership("", source, new, [])

    def test_same_source_and_new_id_is_rejected(self):
        with self.assertRaises(ValueError):
            restore_reference_ownership("", SOURCE, SOURCE, [])

    def test_new_identity_matching_known_foreign_marker_is_rejected(self):
        text = marker(NEW)
        with self.assertRaises(ValueError):
            restore_reference_ownership(text, SOURCE, NEW,
                                        [occurrence(text, text, ReferenceKind.FOREIGN_COLLECTION)])

    def test_bad_span_types_and_fixed_marker_length_are_rejected(self):
        body = marker()
        ref = occurrence(body)
        for fields in ({"start_utf16": True}, {"start_utf16": 0.0},
                       {"start_utf16": -1}, {"length_utf16": True},
                       {"length_utf16": 74.0}, {"length_utf16": 0},
                       {"length_utf16": 73}, {"length_utf16": 75}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                restore_reference_ownership(body, SOURCE, NEW, [replace(ref, **fields)])

    def test_span_beyond_source_end_is_rejected(self):
        body = marker()
        with self.assertRaises(ValueError):
            restore_reference_ownership(body, SOURCE, NEW,
                                        [replace(occurrence(body), start_utf16=74)])

    def test_span_splitting_emoji_surrogate_pair_is_rejected(self):
        body = "😀" + marker()
        with self.assertRaises(ValueError):
            restore_reference_ownership(body, SOURCE, NEW,
                                        [replace(occurrence(body), start_utf16=1)])

    def test_marker_must_be_exact_literal_and_match_original(self):
        body = marker()
        invalid = (None, True, body.upper(), body + "\n", "[[note:x]]",
                   marker(SOURCE, "d" * 32), "x" * 74)
        for text in invalid:
            with self.subTest(text=repr(text)), self.assertRaises(ValueError):
                restore_reference_ownership(body, SOURCE, NEW,
                                            [replace(occurrence(body), marker_text=text)])

    def test_source_owned_cannot_claim_foreign_status(self):
        body = marker()
        with self.assertRaises(ValueError):
            restore_reference_ownership(body, SOURCE, NEW,
                                        [occurrence(body, kind=ReferenceKind.FOREIGN_COLLECTION)])

    def test_foreign_marker_cannot_claim_any_source_owned_status(self):
        body = marker(FOREIGN)
        for kind in (ReferenceKind.VALID, ReferenceKind.DELETED, ReferenceKind.MISSING):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                restore_reference_ownership(body, SOURCE, NEW, [occurrence(body, body, kind)])

    def test_unknown_kind_is_rejected(self):
        for kind in (None, "VALID", 1):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                restore_reference_ownership(marker(), SOURCE, NEW,
                                            [occurrence(marker(), kind=kind)])

    def test_hints_are_optional_and_not_rewritten_or_normalized(self):
        hint = "  旧😀[标题] " + SOURCE
        ref = occurrence(marker(), kind=ReferenceKind.DELETED, hint=hint)
        result = restore_reference_ownership(marker(), SOURCE, NEW, [ref])
        self.assertEqual(result.references[0].display_hint, hint)
        self.assertIsNone(restore_reference_ownership(marker(), SOURCE, NEW,
                          [occurrence(marker())]).references[0].display_hint)

    def test_invalid_hint_types_unicode_or_single_line_controls_are_rejected(self):
        for hint in (1, True, b"x", "", "\ud800", "x\n", "x\x00", "x\x7f", "x\u2028", "x\u2029"):
            with self.subTest(hint=repr(hint)), self.assertRaises(ValueError):
                restore_reference_ownership(marker(), SOURCE, NEW, [occurrence(marker(), hint=hint)])

    def test_untyped_occurrences_or_non_iterable_are_rejected(self):
        for refs in (None, 1, [None], [{}], [True], "text"):
            with self.subTest(refs=repr(refs)), self.assertRaises(ValueError):
                restore_reference_ownership(marker(), SOURCE, NEW, refs)

    def test_one_shot_authorized_iterable_is_consumed_once(self):
        ref = occurrence(marker())
        result = restore_reference_ownership(marker(), SOURCE, NEW, (item for item in [ref]))
        self.assertEqual(result.body, marker(NEW))

    def test_input_body_occurrences_and_list_stay_unchanged(self):
        body = marker()
        ref = occurrence(body, hint="原名称")
        refs = [ref]
        result = restore_reference_ownership(body, SOURCE, NEW, refs)
        self.assertEqual(body, marker())
        self.assertEqual(refs, [ref])
        self.assertEqual(ref.marker_text, marker())
        self.assertIsInstance(result.references, tuple)
        self.assertIsNot(result.references[0], ref)

    def test_result_and_occurrences_are_immutable_slot_values(self):
        result = restore_reference_ownership(marker(), SOURCE, NEW, [occurrence(marker())])
        for value, attr in ((result, "body"), (result.references[0], "marker_text")):
            with self.subTest(value=type(value).__name__):
                self.assertFalse(hasattr(value, "__dict__"))
                with self.assertRaises(FrozenInstanceError):
                    setattr(value, attr, "changed")

    def test_same_backup_converts_independently_to_two_new_collections(self):
        body = marker() + "\r\n" + marker(FOREIGN)
        refs = [occurrence(body), occurrence(body, marker(FOREIGN), ReferenceKind.FOREIGN_COLLECTION)]
        first = restore_reference_ownership(body, SOURCE, NEW, refs)
        second = restore_reference_ownership(body, SOURCE, OTHER_NEW, refs)
        self.assertEqual(first.body, marker(NEW) + "\r\n" + marker(FOREIGN))
        self.assertEqual(second.body, marker(OTHER_NEW) + "\r\n" + marker(FOREIGN))
        self.assertNotEqual(first.body, second.body)
        self.assertEqual(body, marker() + "\r\n" + marker(FOREIGN))


if __name__ == "__main__":
    unittest.main()
