"""C's restore storage port: validated facts, converted initialization, release."""
from __future__ import annotations

import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace

from pmkb.application.backup_restore.restore_service import RestoreTarget
from pmkb.application.output_support import OutputFailure
from pmkb.application.parsing import ReferenceState, parse_markdown
from pmkb.contracts.collections import ActivationMode
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage import reader
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.restore_plan import count_snapshot_references
from pmkb.infrastructure.storage.restore_storage import CRestoreStorage
from pmkb.infrastructure.storage.store import CollectionStore


def new_collection(root: Path, name: str = "source") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class RestoreStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)
        self.adapter = CRestoreStorage()

        self.target = new_identifier()
        self.deleted = new_identifier()
        self.source = new_identifier()
        self.save(note_id=self.target, name="目标", tags=("t",))
        self.save(note_id=self.deleted, name="历史名称")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=self.deleted, expected_revision=1)
        self.valid_marker = format_reference_marker(self.collection_id, self.target)
        self.deleted_marker = format_reference_marker(self.collection_id, self.deleted)
        self.missing_marker = format_reference_marker(self.collection_id, new_identifier())
        self.foreign_marker = format_reference_marker("f" * 32, new_identifier())
        self.body = "\n".join((self.valid_marker, self.deleted_marker,
                               self.missing_marker, self.foreign_marker))
        self.save(note_id=self.source, name="来源", body=self.body)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id=None, name="Note", body="Body", tags=(), base_revision=None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def candidate(self):
        snapshot = reader.capture_snapshot(self.db)
        metadata = reader.metadata(self.db)
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        manifest = SimpleNamespace(
            schema_version=metadata.schema_version,
            source_collection_id=metadata.collection_id,
            source_collection_revision=metadata.collection_revision,
            saved_note_count=len(snapshot.notes),
            reference_counts=count_snapshot_references(snapshot),
            snapshot_sha256=digest,
            snapshot_size_bytes=self.db.stat().st_size,
        )
        return SimpleNamespace(snapshot_path=self.db, manifest=manifest,
                               verify_unchanged=lambda: None)

    def test_validate_snapshot_reports_real_facts(self) -> None:
        facts = asyncio.run(self.adapter.validate_snapshot(self.candidate(), Event()))
        self.assertEqual(facts.source_collection_id, self.collection_id)
        self.assertEqual(facts.saved_note_count, 2)
        self.assertEqual(facts.schema_version, 1)
        counts = facts.reference_counts
        self.assertEqual((counts.valid, counts.deleted, counts.missing, counts.foreign),
                         (1, 1, 1, 1))

    def test_facts_pass_d_manifest_cross_check(self) -> None:
        from datetime import datetime, timezone

        from pmkb.application.backup_restore.manifest import (
            BackupManifest,
            ManifestPolicy,
            check_manifest_against_facts,
        )

        candidate = self.candidate()
        facts = asyncio.run(self.adapter.validate_snapshot(candidate, Event()))
        declared = candidate.manifest
        manifest = BackupManifest(
            format_version=1,
            schema_version=declared.schema_version,
            snapshot_entry="snapshot.sqlite",
            source_collection_id=declared.source_collection_id,
            source_location="C:/synthetic/source",
            backup_captured_at_utc=datetime.now(timezone.utc),
            source_collection_revision=declared.source_collection_revision,
            saved_note_count=declared.saved_note_count,
            reference_counts=declared.reference_counts,
            snapshot_sha256=declared.snapshot_sha256,
            snapshot_size_bytes=declared.snapshot_size_bytes,
        )
        policy = ManifestPolicy(frozenset({1}), frozenset({1}), 1024 * 1024)
        check_manifest_against_facts(manifest, facts, policy)

    def test_initialize_converts_ownership_and_keeps_invalid_targets(self) -> None:
        new_id = new_identifier()
        directory = self.root / "restore"
        directory.mkdir()
        target = RestoreTarget("target-token", str(directory), new_id)
        initialized = asyncio.run(
            self.adapter.initialize_restore(self.candidate(), target, Event()))

        self.assertEqual(initialized.target_token, "target-token")
        self.assertEqual(initialized.collection_id, new_id)
        self.assertEqual(initialized.saved_note_count, 2)
        self.assertEqual(initialized.initialized_note_count, 2)
        self.assertEqual(initialized.collection_revision, 1)
        restored_path = Path(initialized.token)
        self.assertTrue(restored_path.is_file())
        self.assertEqual(restored_path.parent, directory)

        metadata = reader.metadata(restored_path)
        self.assertEqual((metadata.collection_id, metadata.state, metadata.collection_revision),
                         (new_id, "READY", 1))
        restored = CollectionStore(restored_path, new_id)
        self.assertEqual(len(restored.summaries()), 2)
        self.assertIsNone(restored.load_note(self.deleted))

        body = restored.load_note(self.source).body
        self.assertIn(format_reference_marker(new_id, self.target), body)
        self.assertNotIn(format_reference_marker(self.collection_id, self.target), body)
        self.assertIn(self.foreign_marker, body)
        resolved = restored.classify(parse_markdown(body, new_id))
        self.assertEqual([item.state for item in resolved],
                         [ReferenceState.VALID, ReferenceState.DELETED,
                          ReferenceState.MISSING, ReferenceState.FOREIGN_COLLECTION])
        self.assertEqual(resolved[0].target_name, "目标")
        self.assertEqual(resolved[1].target_name, "历史名称")

        warnings = asyncio.run(self.adapter.release(initialized))
        self.assertEqual(warnings, ())
        self.assertFalse(restored_path.exists())
        self.assertEqual(asyncio.run(self.adapter.release(initialized)), ())

    def test_non_empty_target_is_refused_without_artifact(self) -> None:
        directory = self.root / "restore"
        directory.mkdir()
        (directory / "occupied.txt").write_text("keep", encoding="utf-8")
        target = RestoreTarget("target-token", str(directory), new_identifier())
        with self.assertRaises(OutputFailure) as error:
            asyncio.run(self.adapter.initialize_restore(self.candidate(), target, Event()))
        self.assertEqual(error.exception.error.code, "TARGET_NOT_EMPTY")
        self.assertEqual([item.name for item in directory.iterdir()], ["occupied.txt"])

    def test_release_keeps_a_replaced_candidate(self) -> None:
        directory = self.root / "restore"
        directory.mkdir()
        target = RestoreTarget("target-token", str(directory), new_identifier())
        initialized = asyncio.run(
            self.adapter.initialize_restore(self.candidate(), target, Event()))
        path = Path(initialized.token)
        path.unlink()
        path.write_bytes(b"intruder bytes")
        warnings = asyncio.run(self.adapter.release(initialized))
        self.assertEqual(len(warnings), 1)
        self.assertEqual(path.read_bytes(), b"intruder bytes")


if __name__ == "__main__":
    unittest.main()
