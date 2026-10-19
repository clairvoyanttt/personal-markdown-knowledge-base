"""Real temporary files plus fault injection; not collection/backup acceptance."""
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from pmkb.contracts.common import ResultStatus
from pmkb.infrastructure.file_io import publication


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.area = TemporaryDirectory()
        self.addCleanup(self.area.cleanup)
        self.directory = Path(self.area.name)
        self.target = self.directory / "输出.md"

    def assert_no_staging_files(self):
        self.assertEqual(list(self.directory.glob(".pmkb-*.tmp")), [])

    def test_bytes_are_published_without_newline_or_unicode_changes(self):
        data = "中文😀\r\n第二行\n".encode("utf-8")
        reply = publication.publish_bytes(self.target, data)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(reply.byte_count, len(data))
        self.assertEqual(self.target.read_bytes(), data)
        self.assert_no_staging_files()

    def test_existing_file_is_unchanged(self):
        self.target.write_bytes(b"original")
        reply = publication.publish_bytes(self.target, b"new")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"original")
        self.assert_no_staging_files()

    def test_existing_directory_is_not_removed(self):
        self.target.mkdir()
        (self.target / "keep.txt").write_bytes(b"keep")
        reply = publication.publish_bytes(self.target, b"new")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual((self.target / "keep.txt").read_bytes(), b"keep")
        self.assert_no_staging_files()

    def test_target_created_after_staging_is_not_overwritten(self):
        def chunks():
            yield b"candidate"
            self.target.write_bytes(b"competitor")

        reply = publication.publish_chunks(self.target, chunks())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "TARGET_EXISTS")
        self.assertEqual(self.target.read_bytes(), b"competitor")
        self.assert_no_staging_files()

    def test_concurrent_publishers_have_one_winner(self):
        payloads = [f"writer-{index}".encode() for index in range(12)]
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(lambda data: publication.publish_bytes(self.target, data), payloads))
        self.assertEqual(sum(reply.status == ResultStatus.OK for reply in replies), 1)
        self.assertTrue(all(reply.status in (ResultStatus.OK, ResultStatus.ERROR) for reply in replies))
        self.assertIn(self.target.read_bytes(), payloads)
        self.assert_no_staging_files()

    def test_failure_during_iteration_cleans_only_own_temporary(self):
        unrelated = self.directory / ".pmkb-unrelated.tmp"
        unrelated.write_bytes(b"not ours")

        def chunks():
            yield b"partial"
            raise OSError("injected disk/read failure")

        reply = publication.publish_chunks(self.target, chunks())
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assertEqual(unrelated.read_bytes(), b"not ours")
        self.assertEqual(list(self.directory.glob(".pmkb-*.tmp")), [unrelated])

    def test_fsync_failure_does_not_publish(self):
        with patch.object(publication.os, "fsync", side_effect=OSError("injected")):
            reply = publication.publish_bytes(self.target, b"data")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assert_no_staging_files()

    def test_identity_read_failure_closes_descriptor_and_reports_retained_path(self):
        actual_close = os.close
        with patch.object(publication.os, "fstat", side_effect=OSError("cannot read identity")), \
                patch.object(publication.os, "close", wraps=actual_close) as close:
            reply = publication.publish_bytes(self.target, b"data")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertGreaterEqual(close.call_count, 1)
        self.assertIsNotNone(reply.temporary_path)
        self.assertEqual(reply.warnings[0].code, "TEMPORARY_RETAINED")
        self.assertTrue(reply.temporary_path.is_file())
        self.assertFalse(self.target.exists())

    def test_cancel_before_start_has_no_file_effect(self):
        cancel = Event()
        cancel.set()
        reply = publication.publish_bytes(self.target, b"data", cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_during_staging_has_no_publication(self):
        cancel = Event()

        def chunks():
            yield b"first"
            cancel.set()
            yield b"later"

        reply = publication.publish_chunks(self.target, chunks(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertFalse(self.target.exists())
        self.assert_no_staging_files()

    def test_cancel_after_publication_does_not_undo_success(self):
        cancel = Event()
        actual = publication._publish_no_replace

        def publish_then_cancel(source, target):
            consumed = actual(source, target)
            cancel.set()
            return consumed

        with patch.object(publication, "_publish_no_replace", side_effect=publish_then_cancel):
            reply = publication.publish_bytes(self.target, b"saved", cancel)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(self.target.read_bytes(), b"saved")
        self.assert_no_staging_files()

    def test_publish_then_exception_is_unknown_and_formal_file_is_retained(self):
        actual = publication._publish_no_replace

        def publish_then_fail(source, target):
            actual(source, target)
            raise OSError("lost publication acknowledgement")

        with patch.object(publication, "_publish_no_replace", side_effect=publish_then_fail):
            reply = publication.publish_bytes(self.target, b"saved")
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(self.target.read_bytes(), b"saved")
        self.assertEqual(reply.error.code, "PUBLICATION_UNCONFIRMED")

    def test_unknown_publication_keeps_staging_for_inspection(self):
        with patch.object(publication, "_publish_no_replace", side_effect=OSError("unknown")):
            reply = publication.publish_bytes(self.target, b"candidate")
        self.assertEqual(reply.status, ResultStatus.UNCONFIRMED)
        self.assertIsNotNone(reply.temporary_path)
        self.assertEqual(reply.temporary_path.read_bytes(), b"candidate")
        self.assertFalse(self.target.exists())

    def test_temporary_replacement_is_neither_published_nor_deleted(self):
        moved_original = self.directory / "original-temp"
        replacement = []
        actual_check = publication._is_owned_file

        def replace_closed_staging(path, identity):
            if not replacement:
                os.rename(path, moved_original)
                path.write_bytes(b"someone else")
                replacement.append(path)
            return actual_check(path, identity)

        with patch.object(publication, "_is_owned_file", side_effect=replace_closed_staging):
            reply = publication.publish_bytes(self.target, b"ours")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "STATE_BLOCKED")
        self.assertFalse(self.target.exists())
        self.assertEqual(replacement[0].read_bytes(), b"someone else")
        self.assertEqual(moved_original.read_bytes(), b"ours")

    def test_missing_parent_is_not_created(self):
        target = self.directory / "missing" / "file.md"
        reply = publication.publish_bytes(target, b"data")
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(target.parent.exists())

    def test_invalid_chunk_does_not_publish(self):
        reply = publication.publish_chunks(self.target, [b"first", "not bytes"])
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.target.exists())
        self.assert_no_staging_files()

    def test_invalid_windows_basename_rejected_before_io(self):
        with self.assertRaises(ValueError):
            publication.publish_bytes(self.directory / "NUL.md", b"data")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_empty_payload_is_a_valid_complete_file(self):
        reply = publication.publish_bytes(self.target, b"")
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(self.target.read_bytes(), b"")

    def test_directory_syntax_is_not_normalized_into_a_new_file(self):
        for suffix in (os.sep, os.sep + "."):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                publication.publish_bytes(str(self.target) + suffix, b"data")
        self.assertFalse(self.target.exists())

    def test_cleanup_failure_retains_temporary_and_warns(self):
        cancel = Event()

        def chunks():
            yield b"data"
            cancel.set()

        with patch.object(publication, "_cleanup_owned", return_value=False):
            reply = publication.publish_chunks(self.target, chunks(), cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(reply.warnings[0].code, "TEMPORARY_RETAINED")
        self.assertIsNotNone(reply.temporary_path)
        self.assertTrue(reply.temporary_path.is_file())
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
