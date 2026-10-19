"""Synthetic ZIP-container checks, not SQLite backup/restore acceptance."""

from dataclasses import FrozenInstanceError
import hashlib
import io
from pathlib import Path
import stat
import struct
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

from pmkb.infrastructure.file_io.zip_guard import (
    ArchivePolicy,
    ArchiveValidationError,
    inspect_zip,
)


class ZipGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "synthetic.zip"

    def policy(self, **overrides):
        values = dict(
            allowed_entries=frozenset({"manifest.json", "snapshot.sqlite"}),
            required_entries=frozenset({"manifest.json", "snapshot.sqlite"}),
            max_archive_bytes=100_000,
            max_entries=2,
            max_entry_bytes=10_000,
            max_total_uncompressed_bytes=15_000,
            max_capture_bytes=1000,
            chunk_size=7,
        )
        values.update(overrides)
        return ArchivePolicy(**values)

    def archive(self, entries=None, *, compression=zipfile.ZIP_DEFLATED):
        if entries is None:
            entries = [("manifest.json", b'{"synthetic":true}'),
                       ("snapshot.sqlite", b"not a database: synthetic bytes only")]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", compression=compression) as archive:
                for name, content in entries:
                    archive.writestr(name, content)
        return self.path.read_bytes()

    def rejected(self, code, policy=None, **kwargs):
        with self.assertRaises(ArchiveValidationError) as caught:
            inspect_zip(self.path, policy or self.policy(), **kwargs)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_valid_container_streams_hashes_and_only_captures_requested_entry(self):
        original = self.archive()
        report = inspect_zip(
            self.path, self.policy(), capture_entries={"manifest.json"},
            expected_sha256={"snapshot.sqlite": hashlib.sha256(
                b"not a database: synthetic bytes only").hexdigest()},
        )
        self.assertEqual([entry.name for entry in report.entries],
                         ["manifest.json", "snapshot.sqlite"])
        manifest, snapshot = report.entries
        self.assertEqual(manifest.captured_bytes, b'{"synthetic":true}')
        self.assertIsNone(snapshot.captured_bytes)
        self.assertEqual(snapshot.size_bytes, 36)
        self.assertEqual(report.total_uncompressed_bytes, 54)
        self.assertEqual(report.archive_size_bytes, len(original))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.root.iterdir()), [self.path])
        with self.assertRaises(FrozenInstanceError):
            snapshot.name = "changed"

    def test_stored_compression_is_supported_without_declaring_backup_valid(self):
        self.archive(compression=zipfile.ZIP_STORED)
        self.assertEqual(len(inspect_zip(self.path, self.policy()).entries), 2)

    def test_missing_and_unexpected_entries_rejected(self):
        self.archive([("manifest.json", b"{}")])
        self.rejected("missing_entry")
        self.archive([("manifest.json", b"{}"), ("extra.txt", b"x")])
        self.rejected("unexpected_entry")

    def test_duplicate_names_and_case_aliases_rejected_before_allowlist(self):
        for names in [("manifest.json", "manifest.json"),
                      ("manifest.json", "MANIFEST.JSON")]:
            with self.subTest(names=names):
                self.archive([(name, b"{}") for name in names])
                self.rejected("duplicate_entry")

    def test_unsafe_names_rejected(self):
        for name in ["../manifest.json", "/manifest.json", "C:/manifest.json",
                     "\\\\server\\manifest.json", "folder\\manifest.json",
                     "manifest.json:stream", "a/../manifest.json", "a//b",
                     "./manifest.json", "bad\x01name", "bad\x7fname",
                     "bad\u0085name", "name.", "name ", "a?/b"]:
            with self.subTest(name=repr(name)):
                raw = self.archive([(name, b"x")])
                # Windows ZipInfo normalizes native separators when creating a
                # ZIP; mutate synthetic headers so the unsafe bytes reach the reader.
                if "\\" in name:
                    self.path.write_bytes(raw.replace(name.replace("\\", "/").encode(), name.encode()))
                self.rejected("unsafe_entry_name")

    def test_nul_in_original_filename_is_not_accepted_after_zipfile_truncation(self):
        raw = self.archive([("manifest.jsonXevil", b"{}")])
        self.path.write_bytes(raw.replace(b"manifest.jsonXevil", b"manifest.json\0evil"))
        self.rejected("unsafe_entry_name")

    def test_directories_symlinks_and_special_entries_rejected(self):
        for mode, name in [(stat.S_IFDIR | 0o755, "folder/"),
                           (stat.S_IFLNK | 0o777, "manifest.json"),
                           (stat.S_IFIFO | 0o600, "manifest.json")]:
            with self.subTest(mode=mode):
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                self.archive([(info, b"target")])
                self.rejected("unsupported_entry_type")

    def test_encryption_and_unsupported_compression_rejected(self):
        raw = bytearray(self.archive())
        for signature, flags_offset in [(b"PK\x03\x04", 6), (b"PK\x01\x02", 8)]:
            start = 0
            while (position := raw.find(signature, start)) != -1:
                flags = struct.unpack_from("<H", raw, position + flags_offset)[0]
                struct.pack_into("<H", raw, position + flags_offset, flags | 1)
                start = position + 4
        self.path.write_bytes(raw)
        self.rejected("encrypted_entry")
        self.archive(compression=zipfile.ZIP_BZIP2)
        self.rejected("unsupported_compression")

    def test_crc_failure_and_truncated_container_rejected(self):
        raw = self.archive(compression=zipfile.ZIP_STORED)
        self.path.write_bytes(raw.replace(b'{"synthetic":true}', b'{"synthetic":fals}'))
        self.rejected("invalid_zip")
        self.path.write_bytes(raw[:-30])
        self.rejected("invalid_zip")

    def test_local_header_name_disagreement_rejected(self):
        raw = self.archive()
        self.path.write_bytes(raw.replace(b"manifest.json", b"manifesz.json", 1))
        self.rejected("invalid_zip")

    def test_resource_budgets_check_container_entries_and_declared_sizes(self):
        self.archive()
        cases = [dict(max_archive_bytes=1), dict(max_entries=1),
                 dict(max_entry_bytes=20), dict(max_total_uncompressed_bytes=30)]
        for override in cases:
            with self.subTest(override=override):
                self.rejected("resource_budget", self.policy(**override))

    def test_actual_stream_bytes_are_bounded_even_if_metadata_is_smaller(self):
        self.archive([("manifest.json", b"{}")])
        policy = self.policy(required_entries={"manifest.json"}, max_entry_bytes=10)
        with patch.object(zipfile.ZipFile, "open", return_value=io.BytesIO(b"x" * 11)):
            self.rejected("resource_budget", policy)

    def test_actual_total_read_and_capture_budgets_are_also_checked(self):
        self.archive([("manifest.json", b"x" * 6), ("snapshot.sqlite", b"{}")])
        policy = self.policy(max_total_uncompressed_bytes=10)
        with patch.object(zipfile.ZipFile, "open",
                          side_effect=[io.BytesIO(b"x" * 6), io.BytesIO(b"x" * 6)]):
            self.rejected("resource_budget", policy)
        policy = self.policy(max_capture_bytes=10)
        with patch.object(zipfile.ZipFile, "open",
                          side_effect=[io.BytesIO(b"x" * 6), io.BytesIO(b"x" * 6)]):
            self.rejected("capture_budget", policy,
                          capture_entries={"manifest.json", "snapshot.sqlite"})

    def test_read_size_and_aggregate_capture_are_checked(self):
        self.archive()
        self.rejected("capture_budget", self.policy(max_capture_bytes=30),
                      capture_entries={"manifest.json", "snapshot.sqlite"})
        self.archive([("manifest.json", b"{}")])
        with patch.object(zipfile.ZipFile, "open", return_value=io.BytesIO(b"x")):
            self.rejected("size_mismatch", self.policy(required_entries={"manifest.json"}))

    def test_hash_mismatch_rejected_and_expected_missing_optional_entry_rejected(self):
        self.archive()
        self.rejected("hash_mismatch", expected_sha256={"manifest.json": "0" * 64})
        self.archive([("manifest.json", b"{}")])
        self.rejected("missing_entry", self.policy(required_entries={"manifest.json"}),
                      expected_sha256={"snapshot.sqlite": "0" * 64})

    def test_policy_and_capture_inputs_fail_without_reading_source(self):
        for override in [dict(max_entries=True), dict(chunk_size=0),
                         dict(max_entry_bytes=-1), dict(max_capture_bytes=1.5),
                         dict(required_entries={"unknown"}),
                         dict(allowed_entries={"manifest.json", "MANIFEST.JSON"}),
                         dict(allowed_entries={"../bad"})]:
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.policy(**override)
        with self.assertRaises(ValueError):
            inspect_zip(self.path, self.policy(), capture_entries={"unknown"})
        with self.assertRaises(ValueError):
            inspect_zip(self.path, self.policy(), expected_sha256={"manifest.json": "bad"})

    def test_windows_reserved_devices_cannot_be_explicitly_allowlisted(self):
        for name in ["CON", "NUL.md", "folder/COM¹.md", "LPT².txt", "con .txt",
                     "AUX/more.txt", "CONIN$", "conout$.md"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.policy(allowed_entries={name}, required_entries={name})

    def test_malformed_hash_and_capture_inputs_do_not_touch_source(self):
        for invalid in [False, 0, 1, "", [], [("manifest.json", "0" * 64)]]:
            with self.subTest(expected_sha256=invalid):
                with patch.object(Path, "lstat", side_effect=AssertionError("Invalid input reached source")):
                    with self.assertRaises(ValueError):
                        inspect_zip(self.path, self.policy(), expected_sha256=invalid)
        for invalid in [None, False, 0, 1.5, [[]], "manifest.json", b"manifest.json"]:
            with self.subTest(capture_entries=invalid):
                with patch.object(Path, "lstat", side_effect=AssertionError("Invalid input reached source")):
                    with self.assertRaises(ValueError):
                        inspect_zip(self.path, self.policy(), capture_entries=invalid)

    def test_missing_source_and_directory_rejected_without_writes(self):
        self.rejected("source_io")
        self.path.mkdir()
        self.rejected("source_type")

    def test_final_symlink_rejected_when_host_allows_creating_it(self):
        self.archive()
        linked = self.root / "link.zip"
        try:
            linked.symlink_to(self.path)
        except OSError as error:
            self.skipTest(f"Host does not allow symlink creation: {error}")
        with self.assertRaises(ArchiveValidationError) as caught:
            inspect_zip(linked, self.policy())
        self.assertEqual(caught.exception.code, "source_type")

    def test_container_size_checked_before_zipfile_parses_central_directory(self):
        self.archive()
        with patch("pmkb.infrastructure.file_io.zip_guard.zipfile.ZipFile") as constructor:
            self.rejected("resource_budget", self.policy(max_archive_bytes=1))
        constructor.assert_not_called()

    def test_source_modification_during_read_is_reported_not_accepted(self):
        self.archive()
        original_open = zipfile.ZipFile.open
        changed = False

        def open_and_change(archive, *args, **kwargs):
            nonlocal changed
            member = original_open(archive, *args, **kwargs)
            if not changed:
                with self.path.open("ab") as source:
                    source.write(b"changed")
                changed = True
            return member

        with patch.object(zipfile.ZipFile, "open", new=open_and_change):
            self.rejected("source_changed")


if __name__ == "__main__":
    unittest.main()
