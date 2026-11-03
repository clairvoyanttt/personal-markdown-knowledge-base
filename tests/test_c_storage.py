"""C saved-version storage: real SQLite transactions, receipts and evidence.

These use real temporary collection databases created through A's approved
bootstrap. No fake connection or in-memory substitute is involved; failures are
produced with real constraint/evidence mismatches, not by mocking sqlite3.
"""
from __future__ import annotations

from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pmkb.application.collection.ports import CollectionFailure
from pmkb.contracts.collections import ActivationMode
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.connection import connect_reader, connect_writer
from pmkb.infrastructure.storage.store import CollectionStore
from pmkb.infrastructure.storage.writing import CommitState, check_save, delete_note, save_note


def new_collection(root: Path) -> tuple[Path, str]:
    directory = root / "collection"
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class StorageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def save(self, *, note_id: str | None = None, name: str = "Note", body: str = "Body",
             tags: tuple[str, ...] = (), base_revision: int | None = None):
        return self.store.save(
            command_id=new_identifier(),
            operation_id=new_identifier(),
            note_id=note_id or new_identifier(),
            name=name,
            body=body,
            tags=tags,
            base_revision=base_revision,
        )

    def receipt_rows(self) -> list[tuple]:
        with closing(connect_reader(self.db)) as reader:
            return reader.execute(
                "SELECT commandId, operationKind, noteId, beforeRevision, afterRevision,"
                " collectionRevision FROM CommitReceipt ORDER BY rowid"
            ).fetchall()


class SaveTransactionTests(StorageTestCase):
    def test_new_save_commits_receipt_and_advances_collection_revision(self) -> None:
        note_id = new_identifier()
        result = self.save(note_id=note_id, name="  数据结构 ", body="正文😀", tags=("AI", "ai", "AI"))
        self.assertIs(result.state, CommitState.COMMITTED)
        self.assertEqual(result.saved_revision, 1)
        self.assertIsNone(result.error)
        self.assertEqual(result.collection_revision, 2)
        self.assertIsNotNone(result.committed_note)

        saved = self.store.load_note(note_id)
        self.assertEqual(saved.name, "数据结构")
        self.assertEqual(saved.body, "正文😀")
        self.assertEqual(saved.tags, ("AI", "ai"))
        self.assertEqual(saved.revision, 1)
        self.assertEqual(self.store.metadata().collection_revision, 2)

        receipts = self.receipt_rows()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0][1], "SAVE")
        self.assertEqual(receipts[0][2], note_id)
        self.assertIsNone(receipts[0][3])
        self.assertEqual(receipts[0][4], 1)
        self.assertEqual(receipts[0][5], 2)

    def test_update_requires_current_base_revision(self) -> None:
        note_id = new_identifier()
        self.assertIs(self.save(note_id=note_id, name="A", base_revision=None).state,
                      CommitState.COMMITTED)
        updated = self.save(note_id=note_id, name="B", body="v2", base_revision=1)
        self.assertIs(updated.state, CommitState.COMMITTED)
        self.assertEqual(updated.saved_revision, 2)
        self.assertEqual(self.store.load_note(note_id).name, "B")

        stale = self.save(note_id=note_id, name="C", body="v3", base_revision=1)
        self.assertIs(stale.state, CommitState.NOT_COMMITTED)
        self.assertEqual(stale.error.code, "REVISION_CONFLICT")
        self.assertEqual(self.store.load_note(note_id).name, "B")
        self.assertEqual(self.store.metadata().collection_revision, 3)

    def test_duplicate_name_is_rejected_but_self_is_excluded(self) -> None:
        self.save(name="Markdown 笔记")
        clash = self.save(name="  markdown 笔记 ")
        self.assertIs(clash.state, CommitState.NOT_COMMITTED)
        self.assertEqual(clash.error.code, "DUPLICATE_NAME")

        note_id = new_identifier()
        self.save(note_id=note_id, name="Alpha")
        own = self.save(note_id=note_id, name="ALPHA", base_revision=1)
        self.assertIs(own.state, CommitState.COMMITTED)
        self.assertEqual(self.store.load_note(note_id).name, "ALPHA")

    def test_duplicate_command_is_not_written_twice(self) -> None:
        command = new_identifier()
        first = self.store.save(command_id=command, operation_id=new_identifier(),
                                note_id=new_identifier(), name="Once", body="1",
                                tags=(), base_revision=None)
        self.assertIs(first.state, CommitState.COMMITTED)
        replay = self.store.save(command_id=command, operation_id=new_identifier(),
                                 note_id=new_identifier(), name="Twice", body="2",
                                 tags=(), base_revision=None)
        self.assertIs(replay.state, CommitState.NOT_COMMITTED)
        self.assertEqual(replay.error.code, "INVALID_ARGUMENT")
        self.assertEqual(self.store.metadata().collection_revision, 2)

    def test_foreign_keys_are_enabled_on_every_connection(self) -> None:
        with closing(connect_writer(self.db)) as writer:
            self.assertEqual(writer.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                writer.execute(
                    "INSERT INTO Notes(noteId,keyState,name,nameKey,body,revision,lastCommitId)"
                    " VALUES (?,?,?,?,?,1,?)",
                    (new_identifier(), "LIVE", "orphan", "orphankey", "body", new_identifier()),
                )

    def test_triggers_forbid_identity_reuse_and_rewrite(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="Keep")
        with closing(connect_writer(self.db)) as writer:
            with self.assertRaises(sqlite3.IntegrityError):
                writer.execute("UPDATE NoteKeys SET noteId=? WHERE noteId=?",
                               (new_identifier(), note_id))

    def test_invalid_name_and_tag_fail_before_any_write(self) -> None:
        bad_name = self.save(name="   ")
        self.assertIs(bad_name.state, CommitState.NOT_COMMITTED)
        self.assertEqual(bad_name.error.code, "INVALID_NAME")

        bad_tag = self.save(name="ok", tags=("x" * 31,))
        self.assertIs(bad_tag.state, CommitState.NOT_COMMITTED)
        self.assertEqual(bad_tag.error.code, "INVALID_TAG")

        self.assertEqual(self.store.metadata().collection_revision, 1)
        self.assertEqual(self.receipt_rows(), [])
        self.assertEqual(self.store.summaries(), ())

    def test_unpaired_surrogate_body_is_rejected(self) -> None:
        result = self.save(body="bad\ud800body")
        self.assertIs(result.state, CommitState.NOT_COMMITTED)
        self.assertEqual(result.error.code, "INVALID_ARGUMENT")

    def test_references_are_replaced_with_the_saved_revision(self) -> None:
        target = new_identifier()
        self.save(note_id=target, name="Target")
        marker = format_reference_marker(self.collection_id, target)
        source = new_identifier()
        first = self.save(note_id=source, name="Source", body=f"看 {marker}")
        self.assertIs(first.state, CommitState.COMMITTED)

        with closing(connect_reader(self.db)) as reader:
            rows = reader.execute(
                "SELECT sourceRevision, ordinal, targetNoteId, lengthUtf16 FROM RefOccurrence"
                " WHERE sourceNoteId=?", (source,)
            ).fetchall()
        self.assertEqual(rows, [(1, 0, target, 74)])

        second = self.save(note_id=source, name="Source", body="没有引用了", base_revision=1)
        self.assertIs(second.state, CommitState.COMMITTED)
        with closing(connect_reader(self.db)) as reader:
            self.assertEqual(
                reader.execute("SELECT COUNT(*) FROM RefOccurrence WHERE sourceNoteId=?",
                               (source,)).fetchone()[0],
                0,
            )

    def test_failed_write_rolls_back_completely(self) -> None:
        # Fault injection: a trigger aborts the receipt insert after identity, body,
        # tags and occurrences were written inside the same transaction.
        with closing(connect_writer(self.db)) as writer:
            writer.execute(
                "CREATE TRIGGER test_fail BEFORE INSERT ON CommitReceipt"
                " BEGIN SELECT RAISE(ABORT,'injected failure'); END"
            )
        result = self.save(name="Rollback", body="body", tags=("t",))
        self.assertIs(result.state, CommitState.NOT_COMMITTED)
        self.assertEqual(result.error.code, "IO_FAILURE")
        self.assertEqual(self.store.summaries(), ())
        self.assertEqual(self.receipt_rows(), [])
        self.assertEqual(self.store.metadata().collection_revision, 1)

        with closing(connect_writer(self.db)) as writer:
            writer.execute("DROP TRIGGER test_fail")
        self.assertIs(self.save(name="Rollback").state, CommitState.COMMITTED)


class DeleteTransactionTests(StorageTestCase):
    def test_delete_keeps_identity_and_historical_name(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="Old Name")
        result = self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                                   note_id=note_id, expected_revision=1)
        self.assertIs(result.state, CommitState.COMMITTED)
        self.assertIsNone(result.saved_revision)
        self.assertEqual(result.collection_revision, 3)
        self.assertIsNone(self.store.load_note(note_id))

        with closing(connect_reader(self.db)) as reader:
            ledger = reader.execute(
                "SELECT state, liveNoteId, lastSavedName FROM NoteKeys WHERE noteId=?", (note_id,)
            ).fetchone()
        self.assertEqual(ledger, ("DELETED", None, "Old Name"))

    def test_delete_rejects_stale_revision_and_unknown_note(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="N")
        self.save(note_id=note_id, name="N2", base_revision=1)
        stale = self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                                  note_id=note_id, expected_revision=1)
        self.assertIs(stale.state, CommitState.NOT_COMMITTED)
        self.assertEqual(stale.error.code, "REVISION_CONFLICT")

        missing = self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                                    note_id=new_identifier(), expected_revision=1)
        self.assertIs(missing.state, CommitState.NOT_COMMITTED)
        self.assertEqual(missing.error.code, "NOTE_NOT_FOUND")
        self.assertEqual(self.store.metadata().collection_revision, 3)

    def test_deleted_identity_cannot_be_recreated(self) -> None:
        note_id = new_identifier()
        self.save(note_id=note_id, name="Gone")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=note_id, expected_revision=1)
        reuse = self.save(note_id=note_id, name="Gone Again")
        self.assertIs(reuse.state, CommitState.NOT_COMMITTED)
        self.assertEqual(reuse.error.code, "INVALID_ARGUMENT")

    def test_delete_cascades_outbound_but_keeps_inbound(self) -> None:
        target = new_identifier()
        source = new_identifier()
        self.save(note_id=target, name="Target")
        marker = format_reference_marker(self.collection_id, target)
        self.save(note_id=source, name="Source", body=f"link {marker}")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=source, expected_revision=1)
        with closing(connect_reader(self.db)) as reader:
            self.assertEqual(
                reader.execute("SELECT COUNT(*) FROM RefOccurrence WHERE sourceNoteId=?",
                               (source,)).fetchone()[0], 0)
            self.assertEqual(
                reader.execute("SELECT COUNT(*) FROM RefOccurrence WHERE targetNoteId=?",
                               (target,)).fetchone()[0], 0)

        # Deleting the target keeps the source occurrence as an invalid relation.
        survivor = new_identifier()
        self.save(note_id=survivor, name="Survivor", body=f"link {marker}")
        self.store.delete(command_id=new_identifier(), operation_id=new_identifier(),
                          note_id=target, expected_revision=1)
        with closing(connect_reader(self.db)) as reader:
            kept = reader.execute(
                "SELECT COUNT(*) FROM RefOccurrence WHERE targetNoteId=?", (target,)
            ).fetchone()[0]
        self.assertEqual(kept, 1)


class EvidenceTests(StorageTestCase):
    def test_check_save_matches_exact_command_and_content(self) -> None:
        note_id = new_identifier()
        command = new_identifier()
        body = "证据正文"
        self.store.save(command_id=command, operation_id=new_identifier(), note_id=note_id,
                        name="Evidence", body=body, tags=("t",), base_revision=None)

        evidence = check_save(self.db, collection_id=self.collection_id, command_id=command,
                              note_id=note_id, name="Evidence", body=body, tags=("t",),
                              base_revision=None)
        self.assertIs(evidence.state, CommitState.COMMITTED)
        self.assertEqual(evidence.saved_revision, 1)

    def test_check_save_does_not_claim_success_for_a_deleted_or_changed_note(self) -> None:
        note_id = new_identifier()
        command = new_identifier()
        self.store.save(command_id=command, operation_id=new_identifier(), note_id=note_id,
                        name="N", body="b", tags=(), base_revision=None)
        with closing(connect_writer(self.db)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE Notes SET body='tampered' WHERE noteId=?", (note_id,))
            writer.execute("COMMIT")

        evidence = check_save(self.db, collection_id=self.collection_id, command_id=command,
                              note_id=note_id, name="N", body="b", tags=(), base_revision=None)
        self.assertIs(evidence.state, CommitState.UNRESOLVED)
        self.assertIsNone(evidence.saved_revision)
        self.assertEqual(evidence.error.code, "COMMIT_UNCONFIRMED")

    def test_missing_receipt_is_unresolved_not_a_rollback_claim(self) -> None:
        evidence = check_save(self.db, collection_id=self.collection_id,
                              command_id=new_identifier(), note_id=new_identifier(),
                              name="N", body="b", tags=(), base_revision=None)
        self.assertIs(evidence.state, CommitState.UNRESOLVED)

    def test_wrong_collection_identity_is_rejected(self) -> None:
        note_id = new_identifier()
        result = save_note(self.db, collection_id=new_identifier(), command_id=new_identifier(),
                           operation_id=new_identifier(), note_id=note_id, name="N",
                           body="b", tags=(), base_revision=None)
        self.assertIs(result.state, CommitState.NOT_COMMITTED)
        self.assertEqual(result.error.code, "STALE_CONTEXT")

    def test_invalid_identifier_fails_closed(self) -> None:
        result = save_note(self.db, collection_id=self.collection_id, command_id="not-an-id",
                           operation_id=new_identifier(), note_id=new_identifier(), name="N",
                           body="b", tags=(), base_revision=None)
        self.assertIs(result.state, CommitState.NOT_COMMITTED)
        self.assertEqual(result.error.code, "INVALID_ARGUMENT")


if __name__ == "__main__":
    unittest.main()
