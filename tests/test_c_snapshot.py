"""C snapshot capture and restore initialization (the storage half of API09)."""
from __future__ import annotations

from contextlib import closing
from datetime import timezone
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pmkb.application.parsing import ReferenceState, parse_markdown
from pmkb.contracts.collections import ActivationMode
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.domain.rules import RuleViolation
from pmkb.infrastructure.storage import writing
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.connection import StorageFailure, connect_reader
from pmkb.infrastructure.storage.reader import capture_snapshot, metadata
from pmkb.infrastructure.storage.store import CollectionStore
from pmkb.infrastructure.storage.writing import (
    RetainedIdentity,
    RestoredNote,
    initialize_restored_collection,
)


def new_collection(root: Path, name: str = "collection") -> tuple[Path, str]:
    directory = root / name
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class SnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id: str | None = None, name: str = "Note", body: str = "Body",
             tags: tuple[str, ...] = (), base_revision: int | None = None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def test_snapshot_carries_saved_rows_identities_receipts_and_revision(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="Target", tags=("x", "y"))
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="Source", body=marker)

        snapshot = capture_snapshot(self.db)
        self.assertEqual(snapshot.collection_id, self.collection_id)
        self.assertEqual(snapshot.schema_version, 1)
        self.assertEqual(snapshot.collection_revision, 3)
        self.assertEqual(snapshot.captured_utc.tzinfo, timezone.utc)
        self.assertEqual(len(snapshot.notes), 2)
        note = next(item for item in snapshot.notes if item.note_id == target)
        self.assertEqual(note.tags, ("x", "y"))
        self.assertEqual(note.revision, 1)
        source_note = next(item for item in snapshot.notes if item.note_id == source)
        self.assertEqual(len(source_note.references), 1)
        self.assertEqual(source_note.references[0].target_note_id, target)
        self.assertEqual(source_note.references[0].length_utf16, 74)
        self.assertEqual({item.operation_kind for item in snapshot.receipts}, {"SAVE"})
        self.assertEqual(len(snapshot.identities), 2)
        self.assertTrue(all(item.state == "LIVE" for item in snapshot.identities))

    def test_snapshot_keeps_deleted_identity_in_the_ledger(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="Gone")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=note_id, expected_revision=1)
        snapshot = capture_snapshot(self.db)
        self.assertEqual(snapshot.notes, ())
        identity = next(item for item in snapshot.identities if item.note_id == note_id)
        self.assertEqual((identity.state, identity.last_saved_name), ("DELETED", "Gone"))


class RestoreInitTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root, "source")
        self.store = CollectionStore(self.db, self.collection_id)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id=None, name="Note", body="Body", tags=(), base_revision=None):
        return self.store.save(command_id=new_identifier(), operation_id=new_identifier(),
                               note_id=note_id or new_identifier(), name=name, body=body,
                               tags=tags, base_revision=base_revision)

    def test_restore_creates_independent_ready_collection_with_init_receipts(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="目标", tags=("t",))
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=f"看 {marker}")
        snapshot = capture_snapshot(self.db)

        new_id = new_identifier()
        converted = tuple(
            RestoredNote(item.note_id, item.name,
                         item.body.replace(f"[[note:{self.collection_id}:", f"[[note:{new_id}:"),
                         item.tags)
            for item in snapshot.notes
        )
        target_path = self.root / "restored.sqlite"
        result = initialize_restored_collection(
            target_path, new_collection_id=new_id, notes=converted,
        )
        self.assertEqual(result.collection_id, new_id)
        self.assertEqual(result.collection_revision, 1)
        self.assertEqual(result.initialized_notes, 2)
        self.assertNotEqual(new_id, self.collection_id)

        restored_meta = metadata(target_path)
        self.assertEqual((restored_meta.collection_id, restored_meta.state,
                          restored_meta.collection_revision), (new_id, "READY", 1))

        with closing(connect_reader(target_path)) as reader:
            receipts = reader.execute(
                "SELECT operationKind, COUNT(*) FROM CommitReceipt GROUP BY operationKind"
            ).fetchall()
            notes = reader.execute(
                "SELECT noteId, revision, lastCommitId FROM Notes ORDER BY noteId"
            ).fetchall()
            references = reader.execute(
                "SELECT sourceNoteId, sourceRevision, targetCollectionId, targetNoteId, lengthUtf16"
                " FROM RefOccurrence"
            ).fetchall()
        self.assertEqual(dict(receipts), {"RESTORE_INIT": 1, "RESTORE_NOTE_INIT": 2})
        self.assertEqual({row[1] for row in notes}, {1})
        self.assertEqual(len({row[2] for row in notes}), 2)
        self.assertEqual(len(references), 1)
        self.assertEqual(references[0][1], 1)
        self.assertEqual(references[0][2], new_id)
        self.assertEqual(references[0][3], target)
        self.assertEqual(references[0][4], 74)

        restored_store = CollectionStore(target_path, new_id)
        document = parse_markdown(restored_store.load_note(source).body, new_id)
        resolved = restored_store.classify(document)
        self.assertIs(resolved[0].state, ReferenceState.VALID)
        self.assertEqual(resolved[0].target_name, "目标")
        self.assertEqual([item.note_id for item in restored_store.backlinks(target)], [source])

    def test_retained_deleted_identity_stays_invalid_and_unreusable(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="历史名称")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="来源", body=marker)
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)

        new_id = new_identifier()
        converted = marker.replace(
            f"[[note:{self.collection_id}:", f"[[note:{new_id}:")
        target_path = self.root / "restored.sqlite"
        initialize_restored_collection(
            target_path,
            new_collection_id=new_id,
            notes=(RestoredNote(source, "来源", converted, ()),),
            retained=(RetainedIdentity(target, "DELETED", "历史名称"),),
        )
        restored_store = CollectionStore(target_path, new_id)
        resolved = restored_store.classify(parse_markdown(converted, new_id))
        self.assertIs(resolved[0].state, ReferenceState.DELETED)
        self.assertEqual(resolved[0].target_name, "历史名称")

        reuse = restored_store.save(command_id=new_identifier(), operation_id=new_identifier(),
                                    note_id=target, name="重建", body="b", tags=(),
                                    base_revision=None)
        self.assertIsNotNone(reuse.error)

    def leftover_candidates(self) -> list[str]:
        return sorted(path.name for path in self.root.iterdir() if ".pmkb-" in path.name)

    def test_existing_target_is_refused_and_invalid_input_leaves_no_file(self) -> None:
        existing = self.root / "existing.sqlite"
        existing.write_bytes(b"occupied")
        with self.assertRaises(StorageFailure) as error:
            initialize_restored_collection(existing, new_collection_id=new_identifier(), notes=())
        self.assertEqual(error.exception.code, "TARGET_EXISTS")
        self.assertEqual(existing.read_bytes(), b"occupied")
        self.assertEqual(self.leftover_candidates(), [])

        invalid_path = self.root / "invalid.sqlite"
        with self.assertRaises(RuleViolation):
            initialize_restored_collection(
                invalid_path, new_collection_id=new_identifier(),
                notes=(RestoredNote(new_identifier(), " ", "body", ()),),
            )
        self.assertFalse(invalid_path.exists())
        self.assertEqual(self.leftover_candidates(), [])

    def test_competing_creator_winning_the_target_is_not_deleted(self) -> None:
        # C-REV-01: the racer creates the target after our candidate exists and
        # before publication. Its bytes must survive and our candidate is cleaned.
        target_path = self.root / "restored.sqlite"
        real_publish = writing._publish_candidate

        def racing_publish(candidate, identity, target, collection_id, notes, retained):
            target.write_bytes(b"other creator bytes")
            return real_publish(candidate, identity, target, collection_id, notes, retained)

        with mock.patch.object(writing, "_publish_candidate", racing_publish):
            with self.assertRaises(StorageFailure) as error:
                initialize_restored_collection(
                    target_path, new_collection_id=new_identifier(),
                    notes=(RestoredNote(new_identifier(), "N", "body", ()),),
                )
        self.assertEqual(error.exception.code, "TARGET_EXISTS")
        self.assertEqual(target_path.read_bytes(), b"other creator bytes")
        self.assertEqual(self.leftover_candidates(), [])

    def test_replaced_candidate_is_not_deleted_on_failure(self) -> None:
        # C-REV-01: cleanup must prove ownership through the file identity, not a
        # path that somebody else may now own.
        target_path = self.root / "restored.sqlite"
        captured: dict[str, Path] = {}

        def replace_then_fail(candidate, identity, target, collection_id, notes, retained):
            captured["path"] = candidate
            candidate.unlink()
            candidate.write_bytes(b"intruder bytes")
            raise StorageFailure("IO_FAILURE", "simulated publish failure")

        with mock.patch.object(writing, "_publish_candidate", replace_then_fail):
            with self.assertRaises(StorageFailure):
                initialize_restored_collection(
                    target_path, new_collection_id=new_identifier(),
                    notes=(RestoredNote(new_identifier(), "N", "body", ()),),
                )
        self.assertFalse(target_path.exists())
        self.assertEqual(captured["path"].read_bytes(), b"intruder bytes")

    def test_replacement_before_initialization_is_never_written(self) -> None:
        # C-REV-01 follow-up probe 1: replace the candidate after exclusive
        # creation and before the real initializer opens it. The other creator's
        # bytes must stay unchanged and no target may appear.
        target_path = self.root / "restored.sqlite"
        captured: dict[str, Path] = {}
        real_write = writing._write_restored_candidate

        def replace_then_write(candidate, identity, collection_id, notes, retained):
            captured["path"] = candidate
            candidate.unlink()
            candidate.write_bytes(b"other creator empty file")
            return real_write(candidate, identity, collection_id, notes, retained)

        with mock.patch.object(writing, "_write_restored_candidate", replace_then_write):
            with self.assertRaises(StorageFailure) as error:
                initialize_restored_collection(
                    target_path, new_collection_id=new_identifier(),
                    notes=(RestoredNote(new_identifier(), "N", "body", ()),),
                )
        self.assertTrue(error.exception.unknown)
        self.assertFalse(target_path.exists())
        self.assertEqual(captured["path"].read_bytes(), b"other creator empty file")

    def test_overwritten_candidate_is_not_published_as_success(self) -> None:
        # C-REV-01 follow-up probe 2: overwrite the initialized candidate in place
        # (same inode). Content validity, not just identity, must stop publication.
        target_path = self.root / "restored.sqlite"
        real_publish = writing._publish_candidate

        def overwrite_then_publish(candidate, identity, target, collection_id, notes, retained):
            candidate.write_bytes(b"plain synthetic text, not a database")
            return real_publish(candidate, identity, target, collection_id, notes, retained)

        with mock.patch.object(writing, "_publish_candidate", overwrite_then_publish):
            with self.assertRaises(StorageFailure):
                initialize_restored_collection(
                    target_path, new_collection_id=new_identifier(),
                    notes=(RestoredNote(new_identifier(), "N", "body", ()),),
                )
        self.assertFalse(target_path.exists())
        self.assertEqual(self.leftover_candidates(), [])

    def test_replacement_between_verification_and_link_is_not_published(self) -> None:
        # C-REV-01 follow-up: if the candidate is swapped after the pre-link
        # verification, the created name must not be reported as a valid target.
        target_path = self.root / "restored.sqlite"
        real_link = os.link

        def swap_then_link(source, destination, **kwargs):
            path = Path(source)
            path.unlink()
            path.write_bytes(b"replacement between check and link")
            return real_link(source, destination, **kwargs)

        with mock.patch.object(writing.os, "link", swap_then_link):
            with self.assertRaises(StorageFailure) as error:
                initialize_restored_collection(
                    target_path, new_collection_id=new_identifier(),
                    notes=(RestoredNote(new_identifier(), "N", "body", ()),),
                )
        self.assertTrue(error.exception.unknown)
        self.assertFalse(target_path.exists())

    def test_duplicate_note_identities_in_payload_are_rejected(self) -> None:
        note_id = new_identifier()
        path = self.root / "duplicate.sqlite"
        with self.assertRaises(StorageFailure):
            initialize_restored_collection(
                path, new_collection_id=new_identifier(),
                notes=(RestoredNote(note_id, "A", "body", ()),
                       RestoredNote(note_id, "B", "body", ())),
            )
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
