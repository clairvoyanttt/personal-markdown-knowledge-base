"""Synthetic archive extraction tests, not SQLite restoration acceptance."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
import zipfile

from pmkb.application.backup_restore.candidate import (
    CandidateFailure, prepare_restore_candidate,
)
from pmkb.application.backup_restore.manifest import (
    BackupManifest, ManifestPolicy, ReferenceCounts, encode_manifest,
)
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy


NAMES = frozenset({"manifest.json", "snapshot.sqlite"})


class RestoreCandidateTests(unittest.TestCase):
    def setUp(self):
        self.area = TemporaryDirectory()
        self.addCleanup(self.area.cleanup)
        self.root = Path(self.area.name)
        self.parent = self.root / "work"
        self.parent.mkdir()
        self.source = self.root / "backup.zip"
        self.payload = "Synthetic, not SQLite. 😀\r\n".encode("utf-8")
        self.mp = ManifestPolicy(frozenset({1}), frozenset({1}), 8192)
        self.ap = ArchivePolicy(NAMES, NAMES, 1024 * 1024, 2,
                                512 * 1024, 768 * 1024, 8192, 113)
        self.manifest = BackupManifest(
            1, 1, "snapshot.sqlite", "a" * 32,
            r"Z:\source-never-required-to-exist", datetime(2026, 10, 18, tzinfo=timezone.utc),
            2, 1, ReferenceCounts(1, 0, 0, 1),
            hashlib.sha256(self.payload).hexdigest(), len(self.payload),
        )
        self.archive()

    def archive(self, manifest=None, payload=None, entries=None):
        with zipfile.ZipFile(self.source, "w", compression=zipfile.ZIP_STORED) as archive:
            for name, content in (entries if entries is not None else (
                    ("manifest.json", encode_manifest(manifest or self.manifest, self.mp)),
                    ("snapshot.sqlite", self.payload if payload is None else payload))):
                archive.writestr(name, content)

    def prepare(self, cancel=None):
        return prepare_restore_candidate(self.source, self.parent, self.mp, self.ap, cancel)

    def test_one_stream_extracts_private_candidate_and_preserves_source(self):
        original = self.source.read_bytes()
        candidate = self.prepare()
        self.assertEqual(candidate.snapshot_path.read_bytes(), self.payload)
        self.assertEqual(candidate.manifest, self.manifest)
        self.assertEqual({i.name for i in candidate.archive.entries}, NAMES)
        self.assertEqual(list(candidate.snapshot_path.parent.iterdir()), [candidate.snapshot_path])
        candidate.verify_unchanged()
        self.assertEqual(candidate.cleanup(), ())
        self.assertEqual(candidate.cleanup(), ())
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual(self.source.read_bytes(), original)

    def test_initial_cancellation_allocates_nothing(self):
        event = Event()
        event.set()
        with self.assertRaises(CandidateFailure) as caught:
            self.prepare(event)
        self.assertTrue(caught.exception.cancelled)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_digest_or_size_mismatch_rejects_and_cleans_candidate(self):
        for field, value in (("snapshot_sha256", "0" * 64), ("snapshot_size_bytes", 999)):
            with self.subTest(field=field):
                self.archive(replace(self.manifest, **{field: value}))
                with self.assertRaises(CandidateFailure):
                    self.prepare()
                self.assertEqual(list(self.parent.iterdir()), [])

    def test_only_two_required_entries_are_accepted(self):
        for entries in ((('snapshot.sqlite', self.payload),),
                        (('manifest.json', b'{}'), ('../escape', b'bad')),
                        (('manifest.json', b'{}'), ('snapshot.sqlite', self.payload), ('extra', b'bad'))):
            with self.subTest(entries=entries):
                self.archive(entries=entries)
                with self.assertRaises(CandidateFailure):
                    self.prepare()
                self.assertFalse((self.root / "escape").exists())
                self.assertEqual(list(self.parent.iterdir()), [])

    def test_changed_candidate_is_not_accepted_as_verified(self):
        candidate = self.prepare()
        candidate.snapshot_path.write_bytes(b"modified")
        with self.assertRaises(CandidateFailure):
            candidate.verify_unchanged()
        self.assertEqual(candidate.cleanup(), ())

    def test_cleanup_never_removes_replacement_or_unrelated_file(self):
        candidate = self.prepare()
        snapshot = candidate.snapshot_path
        moved = snapshot.with_name("owned-but-moved")
        snapshot.rename(moved)
        snapshot.write_bytes(b"not owned")
        self.assertTrue(candidate.cleanup())
        self.assertEqual(snapshot.read_bytes(), b"not owned")
        self.assertEqual(moved.read_bytes(), self.payload)

    def test_cleanup_keeps_unrelated_file_in_owned_directory(self):
        candidate = self.prepare()
        unrelated = candidate.snapshot_path.with_name("user.txt")
        unrelated.write_bytes(b"keep")
        self.assertTrue(candidate.cleanup())
        self.assertFalse(candidate.snapshot_path.exists())
        self.assertEqual(unrelated.read_bytes(), b"keep")

    def test_missing_parent_is_typed_failure_not_created(self):
        with self.assertRaises(CandidateFailure):
            prepare_restore_candidate(self.source, self.root / "absent", self.mp, self.ap)
        self.assertFalse((self.root / "absent").exists())

    def test_incompatible_policy_is_configuration_error_before_io(self):
        bad = replace(self.ap, allowed_entries=NAMES | {"extra"})
        with self.assertRaises(ValueError):
            prepare_restore_candidate(self.source, self.parent, self.mp, bad)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_source_is_opened_once_and_members_are_read_in_bounded_chunks(self):
        self.payload *= 1000
        self.manifest = replace(self.manifest, snapshot_sha256=hashlib.sha256(self.payload).hexdigest(),
                                snapshot_size_bytes=len(self.payload))
        self.archive(entries=(("snapshot.sqlite", self.payload),
                              ("manifest.json", encode_manifest(self.manifest, self.mp))))
        original_open, original_read = Path.open, zipfile.ZipExtFile.read
        source_opens, reads = [], []

        def opened(path, *args, **kwargs):
            if path == self.source:
                source_opens.append(args)
            return original_open(path, *args, **kwargs)

        def read(member, amount=-1):
            reads.append(amount)
            return original_read(member, amount)

        with patch.object(Path, "open", opened), patch.object(zipfile.ZipExtFile, "read", read):
            candidate = self.prepare()
        self.assertEqual(len(source_opens), 1)
        self.assertTrue(reads)
        self.assertEqual(set(reads), {self.ap.chunk_size})
        self.assertEqual(candidate.snapshot_path.read_bytes(), self.payload)
        candidate.cleanup()

    def test_corrupt_snapshot_crc_rejects_without_touching_backup(self):
        content = bytearray(self.source.read_bytes())
        offset = content.index(self.payload)
        content[offset] ^= 1
        self.source.write_bytes(content)
        original = self.source.read_bytes()
        with self.assertRaises(CandidateFailure) as caught:
            self.prepare()
        self.assertEqual(caught.exception.error.code, "ARCHIVE_INVALID_ZIP")
        self.assertEqual(self.source.read_bytes(), original)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_manifest_parse_error_cleans_extracted_bytes(self):
        self.archive(entries=(("snapshot.sqlite", self.payload), ("manifest.json", b"{}")))
        with self.assertRaises(CandidateFailure) as caught:
            self.prepare()
        self.assertTrue(caught.exception.error.code.startswith("MANIFEST_"))
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_resource_limits_reject_before_constructing_zip_or_allocating_workspace(self):
        policy = replace(self.ap, max_archive_bytes=1)
        with patch("pmkb.application.backup_restore.candidate.zipfile.ZipFile") as reader:
            with self.assertRaises(CandidateFailure):
                prepare_restore_candidate(self.source, self.parent, self.mp, policy)
        reader.assert_not_called()
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_manifest_capture_limit_is_enforced_before_workspace(self):
        with self.assertRaises(CandidateFailure) as caught:
            prepare_restore_candidate(self.source, self.parent, self.mp, replace(self.ap, max_capture_bytes=1))
        self.assertEqual(caught.exception.error.code, "ARCHIVE_CAPTURE_BUDGET")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_unusual_entry_types_duplicates_and_compression_are_rejected(self):
        cases = []
        link = zipfile.ZipInfo("snapshot.sqlite")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        cases.append([(link, self.payload)])
        cases.append([("snapshot.sqlite", self.payload), ("snapshot.sqlite", self.payload)])
        cases.append([("SNAPSHOT.SQLITE", self.payload), ("snapshot.sqlite", self.payload)])
        unsupported = zipfile.ZipInfo("snapshot.sqlite")
        unsupported.compress_type = zipfile.ZIP_BZIP2
        cases.append([(unsupported, self.payload)])
        import warnings
        for extra in cases:
            with self.subTest(entries=extra), warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                self.archive(entries=[("manifest.json", encode_manifest(self.manifest, self.mp))] + extra)
                with self.assertRaises(CandidateFailure):
                    self.prepare()
                self.assertEqual(list(self.parent.iterdir()), [])

    def test_source_change_after_read_is_rejected_and_candidate_removed(self):
        original_read = zipfile.ZipExtFile.read
        changed = False

        def read(member, amount=-1):
            nonlocal changed
            data = original_read(member, amount)
            if member.name == "snapshot.sqlite" and not data and not changed:
                details = self.source.stat()
                os.utime(self.source, ns=(details.st_atime_ns, details.st_mtime_ns + 1_000_000_000))
                changed = True
            return data

        with patch.object(zipfile.ZipExtFile, "read", read):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertEqual(caught.exception.error.code, "ARCHIVE_SOURCE_CHANGED")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_source_replaced_before_open_is_rejected(self):
        original_open = Path.open
        replaced = False

        def opened(path, *args, **kwargs):
            nonlocal replaced
            if path == self.source and not replaced:
                replaced = True
                path.rename(self.root / "old.zip")
                path.write_bytes(b"foreign replacement")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", opened):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertEqual(caught.exception.error.code, "ARCHIVE_SOURCE_CHANGED")
        self.assertEqual(self.source.read_bytes(), b"foreign replacement")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_midstream_cancel_cleans_own_candidate(self):
        event = Event()
        original_read = zipfile.ZipExtFile.read

        def read(member, amount=-1):
            result = original_read(member, amount)
            if member.name == "snapshot.sqlite" and result:
                event.set()
            return result

        with patch.object(zipfile.ZipExtFile, "read", read):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare(event)
        self.assertTrue(caught.exception.cancelled)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_write_flush_fsync_and_close_failures_are_typed_and_cleaned(self):
        original_fdopen = os.fdopen

        class BrokenOutput:
            def __init__(self, stream, failure):
                self.stream, self.failure = stream, failure

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.stream.close()
                if self.failure == "close":
                    raise OSError("injected close")

            def fileno(self):
                return self.stream.fileno()

            def write(self, data):
                return 0 if self.failure == "short" else self.stream.write(data)

            def flush(self):
                if self.failure == "flush":
                    raise OSError("injected flush")
                self.stream.flush()

        for failure in ("short", "flush", "close", "fsync"):
            with self.subTest(failure=failure):
                def opened(descriptor, mode):
                    return BrokenOutput(original_fdopen(descriptor, mode), failure)
                with patch("pmkb.application.backup_restore.candidate.os.fdopen", opened):
                    if failure == "fsync":
                        with patch("pmkb.application.backup_restore.candidate.os.fsync", side_effect=OSError("injected fsync")):
                            with self.assertRaises(CandidateFailure):
                                self.prepare()
                    else:
                        with self.assertRaises(CandidateFailure):
                            self.prepare()
                self.assertEqual(list(self.parent.iterdir()), [])

    def test_cleanup_warns_and_keeps_replaced_directory(self):
        candidate = self.prepare()
        original = candidate.snapshot_path.parent
        relocated = original.with_name("relocated")
        original.rename(relocated)
        original.mkdir()
        foreign = original / "snapshot.sqlite"
        foreign.write_bytes(b"not owned")
        self.assertTrue(candidate.cleanup())
        self.assertEqual(foreign.read_bytes(), b"not owned")
        self.assertEqual((relocated / "snapshot.sqlite").read_bytes(), self.payload)
        with self.assertRaises(CandidateFailure):
            candidate.verify_unchanged()

    def test_unlink_failure_is_reported_and_retry_only_cleans_confirmed_owned_file(self):
        candidate = self.prepare()
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == candidate.snapshot_path:
                raise PermissionError("injected permission")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            self.assertTrue(candidate.cleanup())
        self.assertTrue(candidate.snapshot_path.exists())
        self.assertEqual(candidate.cleanup(), ())

    def test_failure_retains_cleanup_warning_without_deleting_source(self):
        self.archive(replace(self.manifest, snapshot_sha256="0" * 64))
        original_unlink = Path.unlink

        def unlink(path, *args, **kwargs):
            if path.name == "snapshot.sqlite":
                raise PermissionError("injected cleanup")
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertTrue(caught.exception.warnings)
        self.assertTrue(self.source.exists())
        self.assertEqual(len(list(self.parent.iterdir())), 1)

    def test_unconfirmed_new_directory_identity_is_retained_and_warned(self):
        from pmkb.application.backup_restore import candidate as module
        original_identity = module._identity
        calls = 0

        def identity(details):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected ownership lookup")
            return original_identity(details)

        with patch.object(module, "_identity", identity):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertTrue(caught.exception.warnings)
        retained = list(self.parent.iterdir())
        self.assertEqual(len(retained), 1)
        self.assertTrue(retained[0].is_dir())
        self.assertIn(str(retained[0]), caught.exception.warnings[0].reason)

    def test_foreign_file_winning_exclusive_candidate_name_is_not_truncated(self):
        original_open = os.open

        def opened(path, flags, *args, **kwargs):
            if Path(path).name == "snapshot.sqlite":
                with Path(path).open("wb") as stream:
                    stream.write(b"competing writer")
            return original_open(path, flags, *args, **kwargs)

        with patch("pmkb.application.backup_restore.candidate.os.open", opened):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertTrue(caught.exception.warnings)
        foreign = next(self.parent.iterdir()) / "snapshot.sqlite"
        self.assertEqual(foreign.read_bytes(), b"competing writer")

    def test_actual_oversized_member_bytes_are_rejected_not_just_metadata(self):
        original_read = zipfile.ZipExtFile.read

        def read(member, amount=-1):
            result = original_read(member, amount)
            if member.name == "snapshot.sqlite" and result:
                return result * self.ap.max_entry_bytes
            return result

        with patch.object(zipfile.ZipExtFile, "read", read):
            with self.assertRaises(CandidateFailure) as caught:
                self.prepare()
        self.assertEqual(caught.exception.error.code, "ARCHIVE_RESOURCE_BUDGET")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_candidate_digest_check_catches_same_size_timestamp_preserving_mutation(self):
        candidate = self.prepare()
        before = candidate.snapshot_path.stat()
        mutated = bytes([self.payload[0] ^ 1]) + self.payload[1:]
        candidate.snapshot_path.write_bytes(mutated)
        os.utime(candidate.snapshot_path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with self.assertRaises(CandidateFailure):
            candidate.verify_unchanged()
        self.assertEqual(candidate.cleanup(), ())

    def test_two_preparations_share_parent_without_sharing_ownership(self):
        first, second = self.prepare(), self.prepare()
        self.assertNotEqual(first.snapshot_path.parent, second.snapshot_path.parent)
        self.assertEqual(first.cleanup(), ())
        second.verify_unchanged()
        self.assertEqual(second.snapshot_path.read_bytes(), self.payload)
        self.assertEqual(second.cleanup(), ())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_actual_source_link_is_rejected_without_following_or_deleting(self):
        linked = self.root / "linked.zip"
        try:
            linked.symlink_to(self.source)
        except OSError as error:
            self.skipTest(f"Filesystem/Windows symlink permission unavailable: {getattr(error, 'winerror', error.errno)}")
        with self.assertRaises(CandidateFailure) as caught:
            prepare_restore_candidate(linked, self.parent, self.mp, self.ap)
        self.assertEqual(caught.exception.error.code, "ARCHIVE_SOURCE_TYPE")
        self.assertTrue(linked.is_symlink())
        self.assertTrue(self.source.exists())


if __name__ == "__main__":
    unittest.main()
