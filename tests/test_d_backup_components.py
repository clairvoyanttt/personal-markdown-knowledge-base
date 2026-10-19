"""D component round trips on synthetic bytes, NOT database restore acceptance."""
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
import zipfile

from pmkb.application.backup_restore.manifest import (
    BackupManifest, ManifestPolicy, ManifestValidationError, ReferenceCounts, SnapshotFacts,
    encode_manifest,
)
from pmkb.application.backup_restore.package_components import (
    PackageValidationError, inspect_package, publish_package,
)
from pmkb.application.backup_restore.references import RestoreReference, restore_reference_ownership
from pmkb.application.export.rendering import ReferenceKind
from pmkb.contracts.common import ResultStatus
from pmkb.domain.text_spans import to_utf16_offset
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy


SOURCE = "a" * 32
RESTORED = "b" * 32
FOREIGN = "c" * 32
NAMES = frozenset({"manifest.json", "snapshot.sqlite"})


class BackupComponentWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.area = TemporaryDirectory()
        self.addCleanup(self.area.cleanup)
        self.directory = Path(self.area.name)
        self.destination = self.directory / "synthetic.zip"
        self.manifest_policy = ManifestPolicy(frozenset({1}), frozenset({1}), 8192)
        self.archive_policy = ArchivePolicy(NAMES, NAMES, 4 * 1024 * 1024, 2,
                                            2 * 1024 * 1024, 3 * 1024 * 1024, 8192)
        self.payload = "合成内容，不是SQLite库。\r\n😀\n".encode("utf-8")

    def manifest(self, payload=None):
        payload = self.payload if payload is None else payload
        return BackupManifest(
            format_version=1, schema_version=1, snapshot_entry="snapshot.sqlite",
            source_collection_id=SOURCE, source_location=r"Z:\not-required-to-exist\source",
            backup_captured_at_utc=datetime(2026, 10, 18, 5, 30, 1, 123456, tzinfo=timezone.utc),
            source_collection_revision=7, saved_note_count=2,
            reference_counts=ReferenceCounts(1, 1, 1, 1),
            snapshot_sha256=hashlib.sha256(payload).hexdigest(), snapshot_size_bytes=len(payload),
        )

    def facts(self, manifest):
        return SnapshotFacts(
            schema_version=manifest.schema_version, source_collection_id=manifest.source_collection_id,
            source_collection_revision=manifest.source_collection_revision,
            saved_note_count=manifest.saved_note_count, reference_counts=manifest.reference_counts,
            snapshot_sha256=manifest.snapshot_sha256, snapshot_size_bytes=manifest.snapshot_size_bytes,
        )

    def test_bytes_roundtrip_checks_container_and_manifest_not_database(self):
        manifest = self.manifest()
        reply = publish_package(self.destination, self.payload, manifest,
                                self.manifest_policy, self.archive_policy)
        self.assertEqual(reply.status, ResultStatus.OK)
        before = self.destination.read_bytes()
        report = inspect_package(self.destination, self.manifest_policy, self.archive_policy)
        self.assertEqual(report.manifest, manifest)
        self.assertFalse(report.supplied_facts_checked)
        self.assertEqual(self.destination.read_bytes(), before)
        self.assertEqual(list(self.directory.iterdir()), [self.destination])

    def test_explicit_external_facts_are_compared_but_not_obtained_from_database(self):
        manifest = self.manifest()
        publish_package(self.destination, self.payload, manifest, self.manifest_policy, self.archive_policy)
        report = inspect_package(self.destination, self.manifest_policy, self.archive_policy,
                                 facts=self.facts(manifest))
        self.assertTrue(report.supplied_facts_checked)
        with self.assertRaises(ManifestValidationError):
            inspect_package(self.destination, self.manifest_policy, self.archive_policy,
                            facts=replace(self.facts(manifest), saved_note_count=3))

    def test_path_source_streams_and_remains_unchanged(self):
        payload = self.payload * 16384
        source_path = self.directory / "frozen-synthetic-input.bin"
        source_path.write_bytes(payload)
        manifest = self.manifest(payload)
        reply = publish_package(self.destination, source_path, manifest,
                                self.manifest_policy, self.archive_policy)
        self.assertEqual(reply.status, ResultStatus.OK)
        self.assertEqual(source_path.read_bytes(), payload)
        self.assertEqual(inspect_package(self.destination, self.manifest_policy,
                                         self.archive_policy).manifest, manifest)

    def test_manifest_hash_mismatch_is_rejected_before_publication(self):
        manifest = replace(self.manifest(), snapshot_sha256="0" * 64)
        reply = publish_package(self.destination, self.payload, manifest,
                                self.manifest_policy, self.archive_policy)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_manifest_size_mismatch_is_rejected_before_publication(self):
        manifest = replace(self.manifest(), snapshot_size_bytes=len(self.payload) + 1)
        reply = publish_package(self.destination, self.payload, manifest,
                                self.manifest_policy, self.archive_policy)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertFalse(self.destination.exists())

    def test_existing_target_is_never_overwritten(self):
        self.destination.write_bytes(b"existing user file")
        reply = publish_package(self.destination, self.payload, self.manifest(),
                                self.manifest_policy, self.archive_policy)
        self.assertEqual(reply.status, ResultStatus.ERROR)
        self.assertEqual(self.destination.read_bytes(), b"existing user file")

    def test_cancel_before_start_does_not_create_archive(self):
        cancel = Event()
        cancel.set()
        reply = publish_package(self.destination, self.payload, self.manifest(),
                                self.manifest_policy, self.archive_policy, cancel)
        self.assertEqual(reply.status, ResultStatus.CANCELLED)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_malformed_manifest_is_rejected_before_file_creation(self):
        with self.assertRaises(ManifestValidationError):
            publish_package(self.destination, self.payload,
                            replace(self.manifest(), source_collection_id="bad"),
                            self.manifest_policy, self.archive_policy)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_tampered_manifest_is_rejected_on_read_without_mutating_source(self):
        wrong = replace(self.manifest(), snapshot_sha256="0" * 64)
        with zipfile.ZipFile(self.destination, "w") as archive:
            archive.writestr("manifest.json", encode_manifest(wrong, self.manifest_policy))
            archive.writestr("snapshot.sqlite", self.payload)
        before = self.destination.read_bytes()
        with self.assertRaises(PackageValidationError):
            inspect_package(self.destination, self.manifest_policy, self.archive_policy)
        self.assertEqual(self.destination.read_bytes(), before)

    def test_package_reinspection_does_not_turn_old_invalid_references_valid(self):
        owned = [f"[[note:{SOURCE}:{digit * 32}]]" for digit in "123"]
        foreign = f"[[note:{FOREIGN}:{'1' * 32}]]"
        body = "😀\r\n" + "\r\n".join(owned + [foreign]) + f"\r\n`{owned[0]}`\r\n"
        payload = body.encode("utf-8")  # Deliberately a synthetic text payload, not SQLite.
        manifest = self.manifest(payload)
        publish_package(self.destination, payload, manifest, self.manifest_policy, self.archive_policy)
        report = inspect_package(self.destination, self.manifest_policy, self.archive_policy)
        kinds = [ReferenceKind.VALID, ReferenceKind.DELETED, ReferenceKind.MISSING,
                 ReferenceKind.FOREIGN_COLLECTION]
        items = tuple(RestoreReference(to_utf16_offset(body, body.index(marker)), len(marker), marker, kind)
                      for marker, kind in zip(owned + [foreign], kinds))
        transformed = restore_reference_ownership(body, report.manifest.source_collection_id,
                                                  RESTORED, items)
        self.assertEqual(transformed.changed_occurrences, 3)
        self.assertEqual([item.kind for item in transformed.references], kinds)
        self.assertIn(foreign, transformed.body)
        self.assertIn(f"`{owned[0]}`", transformed.body)
        self.assertEqual(body.encode("utf-8"), payload)
        self.assertEqual(transformed.body.count(f"[[note:{RESTORED}:"), 3)


if __name__ == "__main__":
    unittest.main()
