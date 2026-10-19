"""Synthetic archive packaging; no SQLite consistency or product acceptance claim."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import hashlib
import io
import os
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import zipfile

from pmkb.contracts.common import ResultStatus
from pmkb.infrastructure.file_io import publication, zip_writer
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy, inspect_zip


class ZipWriterTests(unittest.TestCase):
    def setUp(self):
        self.area = tempfile.TemporaryDirectory()
        self.addCleanup(self.area.cleanup)
        self.root = Path(self.area.name)
        self.target = self.root / "synthetic.zip"
        self.data = b"synthetic snapshot, NOT a SQLite database\x00\r\n" * 10

    def policy(self, **overrides):
        values = dict(allowed_entries=frozenset({"manifest.json", "snapshot.sqlite"}),
                      required_entries=frozenset({"manifest.json", "snapshot.sqlite"}),
                      max_archive_bytes=100_000, max_entries=2,
                      max_entry_bytes=10_000, max_total_uncompressed_bytes=15_000,
                      max_capture_bytes=1000, chunk_size=7)
        values.update(overrides)
        return ArchivePolicy(**values)

    def entries(self, snapshot=None):
        return [zip_writer.ArchiveSource("manifest.json", b'{"synthetic":true}'),
                zip_writer.ArchiveSource("snapshot.sqlite", self.data if snapshot is None else snapshot)]

    def assert_no_temporary(self):
        self.assertEqual(list(self.root.glob(".pmkb-*.tmp")), [])

    def assert_rejected(self, entries=None, policy=None, **kwargs):
        reply = zip_writer.publish_zip(self.target, self.entries() if entries is None else entries,
                                       policy or self.policy(), **kwargs)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()
        return reply

    def test_bytes_roundtrip_crc_sizes_and_hashes_for_both_compressions(self):
        for compression in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            with self.subTest(compression=compression):
                target = self.root / f"synthetic-{compression}.zip"
                reply = zip_writer.publish_zip(
                    target, self.entries(), self.policy(), compression=compression,
                    expected_sha256={"snapshot.sqlite": hashlib.sha256(self.data).hexdigest()},
                    expected_sizes={"snapshot.sqlite": len(self.data)},
                )
                self.assertEqual(reply.status, ResultStatus.OK)
                self.assertEqual(reply.byte_count, target.stat().st_size)
                report = inspect_zip(target, self.policy(), capture_entries={"snapshot.sqlite"})
                self.assertEqual(report.entries[1].captured_bytes, self.data)
                with zipfile.ZipFile(target) as archive:
                    self.assertIsNone(archive.testzip())
                    self.assertTrue(all(info.compress_type == compression for info in archive.infolist()))
                self.assert_no_temporary()

    def test_source_descriptor_is_immutable(self):
        source = self.entries()[0]
        with self.assertRaises(FrozenInstanceError):
            source.source = b"changed"

    def test_frozen_file_source_streams_without_read_bytes_and_remains_unchanged(self):
        snapshot = self.root / "frozen.snapshot"
        snapshot.write_bytes(self.data)
        with patch.object(Path, "read_bytes", side_effect=AssertionError("whole-file buffering forbidden")):
            reply = zip_writer.publish_zip(self.target, self.entries(snapshot), self.policy())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(snapshot.read_bytes(), self.data)
        self.assert_no_temporary()

    def test_existing_target_is_never_overwritten(self):
        self.target.write_bytes(b"existing")
        reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"existing")
        self.assert_no_temporary()

    def test_names_required_entries_and_budgets_rejected_before_temp_creation(self):
        cases = [([zip_writer.ArchiveSource("../manifest.json", b"x")], self.policy()),
                 ([zip_writer.ArchiveSource("manifest.json", b"x")], self.policy()),
                 ([*self.entries(), zip_writer.ArchiveSource("manifest.json", b"x")], self.policy()),
                 ([zip_writer.ArchiveSource("MANIFEST.JSON", b"x"), *self.entries()], self.policy()),
                 ([zip_writer.ArchiveSource("extra.txt", b"x"), *self.entries()], self.policy()),
                 (self.entries(), self.policy(max_entries=1)),
                 (self.entries(), self.policy(max_entry_bytes=10)),
                 (self.entries(), self.policy(max_total_uncompressed_bytes=30))]
        for entries, policy in cases:
            with self.subTest(entries=entries, policy=policy), patch.object(
                    zip_writer.tempfile, "mkstemp", side_effect=AssertionError("invalid input created a temp")):
                self.assert_rejected(entries, policy)

    def test_wrong_expected_hash_or_size_never_publishes(self):
        for kwargs in ({"expected_sha256": {"snapshot.sqlite": "0" * 64}},
                       {"expected_sizes": {"snapshot.sqlite": len(self.data) + 1}}):
            with self.subTest(kwargs=kwargs):
                self.assert_rejected(**kwargs)

    def test_file_source_or_alias_as_destination_is_rejected_without_changes(self):
        self.target.write_bytes(self.data)
        reply = zip_writer.publish_zip(self.target, self.entries(self.target), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.target.read_bytes(), self.data)
        alias = self.root / "alias.zip"
        os.link(self.target, alias)
        reply = zip_writer.publish_zip(alias, self.entries(self.target), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(alias.read_bytes(), self.data)
        self.assert_no_temporary()

    def test_nonregular_or_missing_source_has_no_output(self):
        for source in (self.root, self.root / "missing.snapshot"):
            with self.subTest(source=source):
                self.assert_rejected(self.entries(source))

    def test_cancel_before_start_has_no_side_effect(self):
        cancel = Event()
        cancel.set()
        reply = zip_writer.publish_zip(self.target, self.entries(), self.policy(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_cancel_during_member_write_removes_only_owned_temp(self):
        cancel = Event()
        unrelated = self.root / ".pmkb-unrelated.tmp"
        unrelated.write_bytes(b"not ours")
        actual = zipfile._ZipWriteFile.write

        def cancel_after_write(member, data):
            result = actual(member, data)
            cancel.set()
            return result

        with patch.object(zipfile._ZipWriteFile, "write", new=cancel_after_write):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.iterdir()), [unrelated])

    def test_partial_write_or_fsync_failure_cannot_report_success(self):
        for owner, attribute in ((zipfile._ZipWriteFile, "write"), (zip_writer.os, "fsync")):
            with self.subTest(attribute=attribute), patch.object(owner, attribute, side_effect=OSError("injected")):
                self.assert_rejected()

    def test_archive_resource_budget_is_enforced_during_construction(self):
        self.assert_rejected(policy=self.policy(max_archive_bytes=50))

    def test_racing_writers_have_one_winner(self):
        with ThreadPoolExecutor(max_workers=4) as executor:
            replies = list(executor.map(lambda _: zip_writer.publish_zip(
                self.target, self.entries(), self.policy()), range(8)))
        self.assertEqual(sum(reply.status == ResultStatus.OK for reply in replies), 1)
        self.assertTrue(all(reply.status in {ResultStatus.OK, ResultStatus.ERROR} for reply in replies))
        inspect_zip(self.target, self.policy())
        self.assert_no_temporary()

    def test_unknown_publication_retains_publication_candidate_and_never_retries(self):
        with patch.object(publication, "_publish_no_replace", side_effect=OSError("unknown")) as publish:
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(publish.call_count, 1)
        self.assertIsNotNone(reply.temporary_path)
        inspect_zip(reply.temporary_path, self.policy())
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.root.glob(".pmkb-zip-*.tmp")), [])

    def test_late_cancel_after_publication_is_not_rollback(self):
        cancel = Event()
        actual = publication._publish_no_replace

        def publish_then_cancel(source, destination):
            consumed = actual(source, destination)
            cancel.set()
            return consumed

        with patch.object(publication, "_publish_no_replace", side_effect=publish_then_cancel):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy(), cancel)
        self.assertEqual(reply.status, ResultStatus.OK)
        inspect_zip(self.target, self.policy())
        self.assert_no_temporary()

    def test_source_changed_during_streaming_is_not_published_or_deleted(self):
        source = self.root / "frozen.snapshot"
        source.write_bytes(self.data)
        actual = zipfile._ZipWriteFile.write
        changed = False

        def change_during_member_write(member, data):
            nonlocal changed
            count = actual(member, data)
            if member._zinfo.filename == "snapshot.sqlite" and not changed:
                changed = True
                source.write_bytes(b"now a different source")
            return count

        with patch.object(zipfile._ZipWriteFile, "write", new=change_during_member_write):
            reply = self.assert_rejected(self.entries(source))
        self.assertTrue(changed)
        self.assertEqual(reply.error.code, "ARCHIVE_SOURCE_CHANGED")
        self.assertEqual(source.read_bytes(), b"now a different source")

    def test_path_replaced_before_open_is_rejected_instead_of_reading_new_file(self):
        source = self.root / "frozen.snapshot"
        source.write_bytes(self.data)
        moved = self.root / "moved.snapshot"
        actual = zip_writer._write_source

        def replace_before_open(archive, item, policy, compression, cancel):
            if item.entry.name == "snapshot.sqlite":
                source.rename(moved)
                source.write_bytes(self.data)
            return actual(archive, item, policy, compression, cancel)

        with patch.object(zip_writer, "_write_source", side_effect=replace_before_open):
            reply = self.assert_rejected(self.entries(source))
        self.assertEqual(reply.error.code, "ARCHIVE_SOURCE_CHANGED")
        self.assertEqual(source.read_bytes(), self.data)
        self.assertEqual(moved.read_bytes(), self.data)

    def test_source_reads_are_bounded_by_the_explicit_chunk_size(self):
        source = self.root / "frozen.snapshot"
        source.write_bytes(self.data)
        actual = Path.open
        reads = []

        class ObservedRead:
            def __init__(self, stream):
                self.stream = stream

            def __getattr__(self, attribute):
                return getattr(self.stream, attribute)

            def read(self, size=-1):
                reads.append(size)
                return self.stream.read(size)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

        def observed_open(path, *args, **kwargs):
            stream = actual(path, *args, **kwargs)
            return ObservedRead(stream) if path == source else stream

        with patch.object(Path, "open", new=observed_open):
            reply = zip_writer.publish_zip(self.target, self.entries(source), self.policy())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertGreater(len(reads), 2)
        self.assertEqual(set(reads), {self.policy().chunk_size})
        self.assert_no_temporary()

    def test_mutated_source_since_manifest_capture_rejected_by_hash_binding(self):
        source = self.root / "frozen.snapshot"
        source.write_bytes(b"different but same-length manifest snapshot expectation")
        self.assert_rejected(self.entries(source),
                             expected_sha256={"snapshot.sqlite": hashlib.sha256(self.data).hexdigest()})

    def test_corrupted_archive_fails_readback_crc_before_publication(self):
        actual = zip_writer.inspect_zip

        def corrupt_then_inspect(path, *args, **kwargs):
            raw = path.read_bytes()
            path.write_bytes(raw.replace(b'{"synthetic":true}', b'{"synthetic":fals}'))
            return actual(path, *args, **kwargs)

        with patch.object(zip_writer, "inspect_zip", side_effect=corrupt_then_inspect), \
                patch.object(zip_writer, "publish_chunks", side_effect=AssertionError("must not publish")):
            reply = self.assert_rejected(compression=zipfile.ZIP_STORED)
        self.assertEqual(reply.error.code, "ARCHIVE_INVALID_ZIP")

    def test_archive_close_failure_never_reaches_publication(self):
        actual = zipfile.ZipFile.close

        def close_then_fail(archive):
            was_open = archive.fp is not None
            actual(archive)
            if was_open and archive.mode == "w":
                raise OSError("injected close acknowledgement failure")

        with patch.object(zipfile.ZipFile, "close", new=close_then_fail), \
                patch.object(zip_writer, "publish_chunks", side_effect=AssertionError("must not publish")):
            self.assert_rejected()

    def test_cancellation_during_readonly_verification_never_publishes(self):
        cancel = Event()
        actual = zip_writer.inspect_zip

        def inspect_then_cancel(*args, **kwargs):
            report = actual(*args, **kwargs)
            cancel.set()
            return report

        with patch.object(zip_writer, "inspect_zip", side_effect=inspect_then_cancel), \
                patch.object(zip_writer, "publish_chunks", side_effect=AssertionError("must not publish")):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_competing_target_created_after_verification_is_retained(self):
        actual = zip_writer.publish_chunks

        def compete_then_publish(destination, chunks, cancel):
            self.target.write_bytes(b"competing owner")
            return actual(destination, chunks, cancel)

        with patch.object(zip_writer, "publish_chunks", side_effect=compete_then_publish):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"competing owner")
        self.assert_no_temporary()

    def test_publication_success_then_acknowledgement_failure_is_unknown_not_deleted(self):
        actual = publication._publish_no_replace

        def publish_then_fail(source, target):
            actual(source, target)
            raise OSError("publication outcome unknown")

        with patch.object(publication, "_publish_no_replace", side_effect=publish_then_fail):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        inspect_zip(self.target, self.policy())
        self.assertEqual(list(self.root.glob(".pmkb-zip-*.tmp")), [])

    def test_cleanup_failure_reports_retained_build_without_deleting_output(self):
        with patch.object(zip_writer, "_cleanup_staging", return_value=False):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.warnings[-1].code, "ARCHIVE_TEMPORARY_RETAINED")
        self.assertTrue(reply.temporary_path.exists())
        inspect_zip(self.target, self.policy())
        inspect_zip(reply.temporary_path, self.policy())

    def test_replaced_build_path_is_neither_published_nor_removed(self):
        actual = zip_writer.inspect_zip
        replaced = []
        moved = self.root / "moved-build"

        def replace_after_inspection(path, *args, **kwargs):
            report = actual(path, *args, **kwargs)
            path.rename(moved)
            path.write_bytes(b"foreign owner")
            replaced.append(path)
            return report

        with patch.object(zip_writer, "inspect_zip", side_effect=replace_after_inspection):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assertEqual(replaced[0].read_bytes(), b"foreign owner")
        inspect_zip(moved, self.policy())
        self.assertEqual(reply.warnings[-1].code, "ARCHIVE_TEMPORARY_RETAINED")

    def test_expected_values_are_frozen_before_source_enumeration(self):
        hashes = {"snapshot.sqlite": hashlib.sha256(self.data).hexdigest()}
        sizes = {"snapshot.sqlite": len(self.data)}

        def sources():
            hashes["snapshot.sqlite"] = "0" * 64
            sizes["snapshot.sqlite"] = 0
            yield from self.entries()

        reply = zip_writer.publish_zip(self.target, sources(), self.policy(),
                                       expected_sha256=hashes, expected_sizes=sizes)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assert_no_temporary()

    def test_invalid_expected_values_and_configuration_are_rejected_without_io(self):
        bad = [dict(expected_sizes={"snapshot.sqlite": True}),
               dict(expected_sizes={"snapshot.sqlite": -1}),
               dict(expected_sizes={"snapshot.sqlite": 1.0}),
               dict(expected_sizes={"other": 1}), dict(expected_sizes=[]),
               dict(expected_sha256={"snapshot.sqlite": "x" * 64}),
               dict(expected_sha256={"other": "0" * 64}),
               dict(expected_sha256=[]), dict(compression=True),
               dict(compression=zipfile.ZIP_BZIP2), dict(cancel=object())]
        for kwargs in bad:
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, TypeError)):
                zip_writer.publish_zip(self.target, self.entries(), self.policy(), **kwargs)
            self.assertEqual(list(self.root.iterdir()), [])

    def test_immutable_sources_reject_mutable_bytes_and_text_paths(self):
        for value in (bytearray(b"x"), memoryview(b"x"), str(self.root / "source"), None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                zip_writer.ArchiveSource("snapshot.sqlite", value)

    def test_missing_parent_is_not_created_and_directory_syntax_is_not_repaired(self):
        missing = self.root / "missing" / "out.zip"
        reply = zip_writer.publish_zip(missing, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(missing.parent.exists())
        for suffix in (os.sep, os.sep + "."):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                zip_writer.publish_zip(str(self.target) + suffix, self.entries(), self.policy())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_short_staging_write_is_rejected(self):
        class ShortStream(io.BytesIO):
            def write(self, data):
                super().write(data[:-1])
                return max(0, len(data) - 1)

        bounded = zip_writer._BudgetedOutput(ShortStream(), 100)
        with self.assertRaises(OSError):
            bounded.write(b"synthetic")

    def test_unicode_equivalent_source_names_are_rejected_before_writing(self):
        policy = self.policy(allowed_entries={"caf\u00e9.txt"}, required_entries=set())
        sources = [zip_writer.ArchiveSource("caf\u00e9.txt", b"first"),
                   zip_writer.ArchiveSource("cafe\u0301.txt", b"second")]
        reply = self.assert_rejected(sources, policy)
        self.assertEqual(reply.error.code, "ARCHIVE_DUPLICATE_ENTRY")

    def test_zero_length_members_and_empty_allowlist_are_supported(self):
        reply = zip_writer.publish_zip(self.target, self.entries(b""), self.policy(),
                                       expected_sizes={"snapshot.sqlite": 0})
        self.assertEqual(reply.status, ResultStatus.OK)
        empty = self.root / "empty.zip"
        policy = self.policy(allowed_entries=set(), required_entries=set())
        reply = zip_writer.publish_zip(empty, [], policy)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(inspect_zip(empty, policy).entries, ())
        self.assert_no_temporary()

    def test_unexpected_publication_boundary_exception_is_unknown_not_false_failure(self):
        actual = zip_writer.publish_chunks

        def publish_without_receipt(*args, **kwargs):
            actual(*args, **kwargs)
            raise OSError("lost entire publisher reply")

        with patch.object(zip_writer, "publish_chunks", side_effect=publish_without_receipt):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        inspect_zip(self.target, self.policy())
        self.assertIsNotNone(reply.temporary_path)
        inspect_zip(reply.temporary_path, self.policy())

    def test_cancel_during_publisher_copy_does_not_publish(self):
        cancel = Event()
        actual = zip_writer.os.fsync
        count = 0

        def cancel_second_fsync(descriptor):
            nonlocal count
            actual(descriptor)
            count += 1
            if count == 2:
                cancel.set()

        with patch.object(zip_writer.os, "fsync", side_effect=cancel_second_fsync):
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy(), cancel)
        self.assertEqual(count, 2)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertFalse(self.target.exists())
        self.assert_no_temporary()

    def test_unreadable_identity_retains_only_build_path_and_closes_descriptor(self):
        actual_close = zip_writer.os.close
        with patch.object(zip_writer.os, "fstat", side_effect=OSError("identity unavailable")), \
                patch.object(zip_writer.os, "close", wraps=actual_close) as close:
            reply = zip_writer.publish_zip(self.target, self.entries(), self.policy())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertGreaterEqual(close.call_count, 1)
        self.assertFalse(self.target.exists())
        self.assertIsNotNone(reply.temporary_path)
        self.assertTrue(reply.temporary_path.exists())
        self.assertEqual(reply.warnings[-1].code, "ARCHIVE_TEMPORARY_RETAINED")


if __name__ == "__main__":
    unittest.main()
