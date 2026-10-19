"""Synthetic D-internal manifest codec tests, not database or product acceptance."""

from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from pmkb.application.backup_restore.manifest import (
    BackupManifest, ManifestPolicy, ManifestValidationError, ReferenceCounts,
    SnapshotFacts, check_manifest_against_facts, decode_manifest, encode_manifest,
    validate_manifest,
)


SOURCE = "0123456789abcdef0123456789abcdef"
POLICY = ManifestPolicy(frozenset({1}), frozenset({1}), 16_384)


def manifest(**overrides):
    value = BackupManifest(
        format_version=1, schema_version=1, snapshot_entry="snapshot.sqlite",
        source_collection_id=SOURCE, source_location=r"C:\synthetic\课程集合",
        backup_captured_at_utc=datetime(2026, 10, 18, 7, 8, 9, 123456, timezone.utc),
        source_collection_revision=42, saved_note_count=2,
        reference_counts=ReferenceCounts(valid=4, deleted=2, missing=1, foreign=3),
        snapshot_sha256="a" * 64, snapshot_size_bytes=8192,
    )
    return replace(value, **overrides)


def wire(**overrides):
    value = json.loads(encode_manifest(manifest(), POLICY))
    value.update(overrides)
    return json.dumps(value, ensure_ascii=True).encode("utf-8")


def facts(**overrides):
    value = SnapshotFacts(
        schema_version=1, source_collection_id=SOURCE, source_collection_revision=42,
        saved_note_count=2, reference_counts=ReferenceCounts(4, 2, 1, 3),
        snapshot_sha256="a" * 64, snapshot_size_bytes=8192,
    )
    return replace(value, **overrides)


class ManifestCodecTests(unittest.TestCase):
    def test_roundtrip_exact_fields_and_occurrence_counts_not_target_counts(self):
        original = manifest()
        encoded = encode_manifest(original, POLICY)
        self.assertEqual(decode_manifest(encoded, POLICY), original)
        self.assertIn("课程集合".encode(), encoded)
        parsed = json.loads(encoded)
        self.assertEqual(set(parsed), {
            "formatVersion", "schemaVersion", "snapshotEntry", "sourceCollectionId",
            "sourceLocation", "backupCapturedAtUtc", "sourceCollectionRevision",
            "savedNoteCount", "referenceCounts", "snapshotSha256", "snapshotSizeBytes",
        })
        self.assertEqual(parsed["referenceCounts"], {"valid": 4, "deleted": 2, "missing": 1, "foreign": 3})
        self.assertEqual(parsed["backupCapturedAtUtc"], "2026-10-18T07:08:09.123456Z")

    def test_repeated_encoding_is_deterministic_and_does_not_read_clock(self):
        original = manifest()
        self.assertEqual(encode_manifest(original, POLICY), encode_manifest(original, POLICY))

    def test_seconds_and_each_supported_fractional_precision_are_accepted(self):
        for suffix in ("", ".1", ".12", ".123", ".1234", ".12345", ".123456"):
            with self.subTest(suffix=suffix):
                value = decode_manifest(wire(backupCapturedAtUtc=f"2026-10-18T07:08:09{suffix}Z"), POLICY)
                self.assertEqual(value.backup_captured_at_utc.utcoffset(), timedelta(0))
                self.assertEqual(decode_manifest(encode_manifest(value, POLICY), POLICY), value)

    def test_invalid_time_or_unrepresentable_precision_is_not_silently_truncated(self):
        for text in ("2026-10-18T07:08:09.1234567Z", "2026-10-18T07:08:09.1234560Z",
                     "2026-10-18T07:08:09+00:00", "2026-10-18T07:08:09",
                     "2026-02-30T07:08:09Z", "2026-10-18 07:08:09Z",
                     "2026-10-18T07:08:60Z", "2026-10-18T07:08:09z", 12, None):
            with self.subTest(text=text):
                with self.assertRaises(ManifestValidationError):
                    decode_manifest(wire(backupCapturedAtUtc=text), POLICY)

    def test_datetime_input_must_be_aware_utc(self):
        for value in (datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=8))),
                      "2026-01-01T00:00:00Z", None):
            with self.subTest(value=value):
                with self.assertRaises(ManifestValidationError):
                    validate_manifest(manifest(backup_captured_at_utc=value), POLICY)

    def test_source_path_is_only_lexically_checked_never_opened(self):
        for location in (r"Z:\not-present\合成集合", "C:/synthetic/collection", r"\\unreachable-host\share\collection"):
            with self.subTest(location=location), patch("builtins.open", side_effect=AssertionError("No source access")):
                original = manifest(source_location=location)
                self.assertEqual(decode_manifest(encode_manifest(original, POLICY), POLICY), original)

    def test_source_path_requires_complete_valid_windows_directory_shape(self):
        for location in ("", "relative", r"C:relative", r"\rooted", "/unix/root", "  ",
                         "C:\\bad\x00name", r"C:\a\..\b", r"C:\a\b:ads", r"\\?\C:\collection",
                         r"\\.\PhysicalDrive0", "C:\\x\ud800", 123, None):
            with self.subTest(location=repr(location)):
                with self.assertRaises(ManifestValidationError):
                    validate_manifest(manifest(source_location=location), POLICY)

    def test_id_entry_and_digest_are_strict(self):
        for field, values in {
            "source_collection_id": [SOURCE.upper(), "a" * 31, "g" * 32, 1, None],
            "snapshot_entry": ["Snapshot.sqlite", "../snapshot.sqlite", "snapshot.sqlite/", None],
            "snapshot_sha256": ["A" * 64, "a" * 63, "g" * 64, None, 1],
        }.items():
            for value in values:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ManifestValidationError):
                        validate_manifest(manifest(**{field: value}), POLICY)

    def test_nonnegative_counts_and_positive_revisions_reject_bool_float_overflow(self):
        for field in ("format_version", "schema_version", "source_collection_revision", "saved_note_count", "snapshot_size_bytes"):
            invalid = (True, False, 1.0, "1", None, -1, 2 ** 63)
            if field in {"format_version", "schema_version", "source_collection_revision"}:
                invalid += (0,)
            for value in invalid:
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ManifestValidationError):
                        validate_manifest(manifest(**{field: value}), POLICY)
        self.assertIsNotNone(validate_manifest(manifest(saved_note_count=0, snapshot_size_bytes=0), POLICY))

    def test_reference_counts_must_be_exact_typed_nonnegative_integer_fields(self):
        for field in ("valid", "deleted", "missing", "foreign"):
            for bad in (-1, True, 1.0, "1", None, 2 ** 63):
                counts = replace(ReferenceCounts(0, 0, 0, 0), **{field: bad})
                with self.subTest(field=field, bad=bad):
                    with self.assertRaises(ManifestValidationError):
                        validate_manifest(manifest(reference_counts=counts), POLICY)
        with self.assertRaises(ManifestValidationError):
            validate_manifest(manifest(reference_counts={"valid": 1}), POLICY)

    def test_unsupported_versions_have_distinct_error_code(self):
        for key in ("format_version", "schema_version"):
            with self.subTest(key=key), self.assertRaises(ManifestValidationError) as caught:
                validate_manifest(manifest(**{key: 2}), POLICY)
            self.assertEqual(caught.exception.code, "unsupported_version")
            self.assertEqual(caught.exception.field, key)

    def test_wire_numbers_do_not_coerce(self):
        for key in ("formatVersion", "schemaVersion", "sourceCollectionRevision", "savedNoteCount", "snapshotSizeBytes"):
            for invalid in (True, 1.0, "1", None):
                with self.subTest(key=key, invalid=invalid), self.assertRaises(ManifestValidationError):
                    decode_manifest(wire(**{key: invalid}), POLICY)

    def test_missing_unknown_or_wrong_top_shape_fields_fail_closed(self):
        parsed = json.loads(wire())
        for key in parsed:
            changed = dict(parsed)
            del changed[key]
            with self.subTest(missing=key), self.assertRaises(ManifestValidationError):
                decode_manifest(json.dumps(changed).encode(), POLICY)
        for payload in (b"[]", b"null", b'"string"', wire(extraField=1), wire(referenceCounts=[]),
                        wire(referenceCounts={"valid": 1, "deleted": 0, "missing": 0}),
                        wire(referenceCounts={"valid": 1, "deleted": 0, "missing": 0, "foreign": 0, "extra": 1})):
            with self.subTest(payload=payload), self.assertRaises(ManifestValidationError):
                decode_manifest(payload, POLICY)

    def test_duplicates_are_rejected_including_nested_objects(self):
        original = wire()
        duplicates = [original.replace(b'"formatVersion": 1', b'"formatVersion": 1, "formatVersion": 1'),
                      original.replace(b'"valid": 4', b'"valid": 4, "valid": 4'),
                      original.replace(b'"valid": 4', b'"valid": 4, "v\\u0061lid": 4')]
        for payload in duplicates:
            with self.subTest(payload=payload), self.assertRaises(ManifestValidationError) as caught:
                decode_manifest(payload, POLICY)
            self.assertEqual(caught.exception.code, "duplicate_field")

    def test_invalid_utf8_bom_surrogates_nonfinite_and_malformed_json_fail(self):
        for payload in (b"\xff", b"\xef\xbb\xbf" + wire(), b"{", b"{} trailing", wire() + b"\x00",
                        wire().replace(b'"savedNoteCount": 2', b'"savedNoteCount": NaN'),
                        wire().replace(b'"savedNoteCount": 2', b'"savedNoteCount": Infinity'),
                        wire().replace(b'"savedNoteCount": 2', b'"savedNoteCount": -Infinity'),
                        wire(sourceLocation="C:\\x\ud800")):
            with self.subTest(payload=payload), self.assertRaises(ManifestValidationError):
                decode_manifest(payload, POLICY)

    def test_json_byte_budget_is_explicit_and_inclusive_for_read_and_write(self):
        encoded = encode_manifest(manifest(), POLICY)
        exact = replace(POLICY, max_json_bytes=len(encoded))
        self.assertEqual(decode_manifest(encoded, exact), manifest())
        self.assertEqual(encode_manifest(manifest(), exact), encoded)
        short = replace(POLICY, max_json_bytes=len(encoded) - 1)
        for call in (lambda: decode_manifest(encoded, short), lambda: encode_manifest(manifest(), short)):
            with self.assertRaises(ManifestValidationError) as caught:
                call()
            self.assertEqual(caught.exception.code, "resource_budget")

    def test_deep_json_and_large_integer_fail_as_controlled_validation_errors(self):
        for payload in (b"[" * 1200 + b"0" + b"]" * 1200,
                        wire().replace(b'"savedNoteCount": 2', b'"savedNoteCount": ' + b"9" * 5000)):
            with self.subTest(size=len(payload)), self.assertRaises(ManifestValidationError):
                decode_manifest(payload, POLICY)

    def test_signed_integer_boundaries_are_inclusive_without_inventing_note_capacity(self):
        maximum = 2 ** 63 - 1
        original = manifest(source_collection_revision=maximum, saved_note_count=maximum,
                            snapshot_size_bytes=maximum, reference_counts=ReferenceCounts(maximum, maximum, 0, 0))
        self.assertEqual(decode_manifest(encode_manifest(original, POLICY), POLICY), original)

    def test_calendar_boundaries_roundtrip_without_fixed_millisecond_rule(self):
        for timestamp in (datetime(1, 1, 1, tzinfo=timezone.utc),
                          datetime(9999, 12, 31, 23, 59, 59, 999999, timezone.utc)):
            with self.subTest(timestamp=timestamp):
                original = manifest(backup_captured_at_utc=timestamp)
                self.assertEqual(decode_manifest(encode_manifest(original, POLICY), POLICY), original)

    def test_policy_requires_explicit_nonempty_versions_and_positive_budget(self):
        for arguments in ((frozenset(), frozenset({1}), 1), ({True}, {1}, 1), ({1.0}, {1}, 1),
                          ({1}, {0}, 1), ({1}, {1}, True), ({1}, {1}, 0), ("1", {1}, 1)):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                ManifestPolicy(*arguments)

    def test_policy_rejects_bool_or_float_before_deduplicating_equal_versions(self):
        for field in ("supported_format_versions", "supported_schema_versions"):
            for values in ([1, True], [True, 1], [1, 1.0], [1.0, 1]):
                arguments = {"supported_format_versions": [1], "supported_schema_versions": [1],
                             "max_json_bytes": 1024}
                arguments[field] = values
                with self.subTest(field=field, values=values), self.assertRaises(ValueError):
                    ManifestPolicy(**arguments)

    def test_inputs_are_immutable_and_wrong_api_argument_types_are_rejected(self):
        with self.assertRaises(FrozenInstanceError):
            manifest().saved_note_count = 99
        for call in (lambda: encode_manifest({}, POLICY), lambda: validate_manifest(manifest(), {}),
                     lambda: decode_manifest("{}", POLICY), lambda: decode_manifest(bytearray(b"{}"), POLICY)):
            with self.assertRaises((TypeError, ManifestValidationError)):
                call()

    def test_checked_in_manifest_is_synthetic_valid_profile(self):
        content = (Path(__file__).parent / "test-data/d-backup/manifest-example.json").read_bytes()
        self.assertEqual(decode_manifest(content, POLICY), manifest())


class ManifestFactsTests(unittest.TestCase):
    def test_agreement_does_not_read_database_or_source_location(self):
        with patch("builtins.open", side_effect=AssertionError("Pure comparison only")):
            self.assertIsNone(check_manifest_against_facts(manifest(), facts(), POLICY))

    def test_every_snapshot_fact_is_compared_independently(self):
        alternatives = {
            "schema_version": 2, "source_collection_id": "1" * 32,
            "source_collection_revision": 43, "saved_note_count": 3,
            "reference_counts": ReferenceCounts(5, 2, 1, 3),
            "snapshot_sha256": "b" * 64, "snapshot_size_bytes": 8193,
        }
        broad = ManifestPolicy(frozenset({1}), frozenset({1, 2}), 16_384)
        for field, value in alternatives.items():
            with self.subTest(field=field), self.assertRaises(ManifestValidationError) as caught:
                check_manifest_against_facts(manifest(), facts(**{field: value}), broad)
            self.assertEqual(caught.exception.code, "snapshot_mismatch")
            self.assertEqual(caught.exception.field, field)

    def test_all_reference_classifications_are_compared_not_only_total(self):
        with self.assertRaises(ManifestValidationError):
            check_manifest_against_facts(manifest(), facts(reference_counts=ReferenceCounts(3, 3, 1, 3)), POLICY)

    def test_untyped_or_invalid_facts_cannot_validate_a_manifest(self):
        for supplied in (None, {}, facts(saved_note_count=True), facts(snapshot_sha256="x"),
                         facts(source_collection_revision=0), facts(reference_counts=[])):
            with self.subTest(supplied=supplied), self.assertRaises((TypeError, ManifestValidationError)):
                check_manifest_against_facts(manifest(), supplied, POLICY)


if __name__ == "__main__":
    unittest.main()
