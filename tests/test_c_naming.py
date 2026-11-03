"""C pure input rules: names, tags and the identity marker text."""
from __future__ import annotations

import unittest

from pmkb.domain.identity import (
    MARKER_UTF16_LENGTH,
    MarkerIdentity,
    format_reference_marker,
    is_identifier,
    new_identifier,
    parse_reference_marker,
)
from pmkb.domain.naming import (
    MAX_NAME_CODEPOINTS,
    MAX_TAG_CODEPOINTS,
    MAX_TAGS_PER_NOTE,
    name_key,
    normalize_name,
    normalize_tag,
    normalize_tags,
)
from pmkb.domain.rules import RuleViolation, is_version
from pmkb.domain.text_spans import to_utf16_offset

COLLECTION = "0123456789abcdef0123456789abcdef"
NOTE = "fedcba9876543210fedcba9876543210"


class NameTests(unittest.TestCase):
    def test_name_is_trimmed_by_the_explicit_whitespace_set(self) -> None:
        for space in ("\u0020", "\u0085", "\u00a0", "\u1680", "\u2003", "\u200a",
                      "\u202f", "\u205f", "\u3000"):
            with self.subTest(space=ord(space)):
                self.assertEqual(normalize_name(space + "AI" + space).name, "AI")

    def test_name_length_is_counted_in_code_points(self) -> None:
        self.assertEqual(normalize_name("字" * MAX_NAME_CODEPOINTS).name, "字" * 100)
        self.assertEqual(normalize_name("😀" * MAX_NAME_CODEPOINTS).name, "😀" * 100)
        with self.assertRaises(RuleViolation) as error:
            normalize_name("😀" * (MAX_NAME_CODEPOINTS + 1))
        self.assertEqual(error.exception.code, "INVALID_NAME")

    def test_empty_and_whitespace_only_names_are_rejected(self) -> None:
        for value in ("", "   ", "\u3000\u3000"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(RuleViolation) as error:
                    normalize_name(value)
                self.assertEqual(error.exception.code, "INVALID_NAME")

    def test_control_characters_are_rejected_before_trimming(self) -> None:
        # FR04: a leading/trailing tab must be rejected, never trimmed away.
        for value in ("\tAI", "AI\t", "A\nB", "A\x00B", "A\x7fB", "A\u2028B", "A\u2029B"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(RuleViolation) as error:
                    normalize_name(value)
                self.assertEqual(error.exception.code, "INVALID_NAME")

    def test_unpaired_surrogate_is_rejected(self) -> None:
        with self.assertRaises(RuleViolation) as error:
            normalize_name("bad\ud800name")
        self.assertEqual(error.exception.code, "INVALID_ARGUMENT")

    def test_name_key_folds_ascii_only(self) -> None:
        self.assertEqual(name_key("MarkDown"), "markdown")
        self.assertNotEqual(name_key("Ä"), name_key("ä"))
        self.assertNotEqual(name_key("Straße"), name_key("STRASSE"))
        self.assertEqual(normalize_name("  Straße ").key, "straße")

    def test_non_text_names_are_rejected(self) -> None:
        for value in (None, 5, b"name", ("n",)):
            with self.subTest(value=value):
                with self.assertRaises(RuleViolation) as error:
                    normalize_name(value)
                self.assertEqual(error.exception.code, "INVALID_ARGUMENT")


class TagTests(unittest.TestCase):
    def test_tag_is_trimmed_and_keeps_case(self) -> None:
        self.assertEqual(normalize_tag("  AI  "), "AI")
        self.assertNotEqual(normalize_tag("AI"), normalize_tag("ai"))

    def test_tag_length_limits(self) -> None:
        self.assertEqual(len(normalize_tag("码" * MAX_TAG_CODEPOINTS)), MAX_TAG_CODEPOINTS)
        with self.assertRaises(RuleViolation) as error:
            normalize_tag("码" * (MAX_TAG_CODEPOINTS + 1))
        self.assertEqual(error.exception.code, "INVALID_TAG")

    def test_blank_and_control_tags_are_rejected(self) -> None:
        for value in ("", "   ", "\tAI", "AI\n", "A\u2028B"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(RuleViolation) as error:
                    normalize_tag(value)
                self.assertEqual(error.exception.code, "INVALID_TAG")

    def test_duplicates_do_not_add_a_slot_but_case_variants_do(self) -> None:
        accepted = normalize_tags(("AI", "AI", "ai", " AI "))
        self.assertEqual(accepted, ("AI", "ai"))

    def test_twenty_distinct_tags_fit_and_twenty_one_do_not(self) -> None:
        twenty = tuple(f"t{index}" for index in range(MAX_TAGS_PER_NOTE))
        self.assertEqual(len(normalize_tags(twenty)), 20)
        # A repeated tag at the limit is still accepted and does not add a slot.
        self.assertEqual(len(normalize_tags(twenty + ("t0",))), 20)
        with self.assertRaises(RuleViolation) as error:
            normalize_tags(twenty + ("t20",))
        self.assertEqual(error.exception.code, "TAG_LIMIT")

    def test_tag_container_types_are_checked(self) -> None:
        with self.assertRaises(RuleViolation):
            normalize_tags("AI")
        with self.assertRaises(RuleViolation):
            normalize_tags(None)


class IdentityTests(unittest.TestCase):
    def test_identifier_shape(self) -> None:
        self.assertTrue(is_identifier(new_identifier()))
        for value in ("A" * 32, "a" * 31, "a" * 33, "g" * 32, None, 12):
            with self.subTest(value=value):
                self.assertFalse(is_identifier(value))

    def test_marker_is_74_utf16_units_and_round_trips(self) -> None:
        marker = format_reference_marker(COLLECTION, NOTE)
        self.assertEqual(len(marker), MARKER_UTF16_LENGTH)
        self.assertEqual(to_utf16_offset(marker, len(marker)), 74)
        parsed = parse_reference_marker(marker)
        self.assertEqual(parsed, MarkerIdentity(COLLECTION, NOTE))
        self.assertEqual(parsed.marker, marker)

    def test_incomplete_and_old_markers_stay_ordinary_text(self) -> None:
        for value in ("[[note:]]", "[[note:K1]]", "[[note:" + COLLECTION + "]]",
                      "[[note:" + COLLECTION.upper() + ":" + NOTE + "]]", "note:" + COLLECTION,
                      "[[note:" + COLLECTION + ":" + NOTE):
            with self.subTest(value=value):
                self.assertIsNone(parse_reference_marker(value))

    def test_versions_reject_bool_and_overflow(self) -> None:
        self.assertTrue(is_version(1, minimum=1))
        self.assertFalse(is_version(True, minimum=1))
        self.assertFalse(is_version(0, minimum=1))
        self.assertFalse(is_version(2**63))
        self.assertTrue(is_version(2**63 - 1))


if __name__ == "__main__":
    unittest.main()
