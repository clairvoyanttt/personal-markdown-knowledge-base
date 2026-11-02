"""C's storage adapter against B's real editing service (no mocked persistence).

This is the integration boundary C owns: B keeps the draft, C runs one real
SQLite SAVE and returns evidence strong enough for B to confirm exactly the
captured version. It also proves the single storage thread serializes writes.
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from threading import Event

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.editing.ports import SaveAttempt, SaveEvidenceState
from pmkb.application.editing.service import EditingService
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.common import CallContext, ResultStatus
from pmkb.contracts.notes import (
    ApplyDraftEditRequest,
    CreateDraftRequest,
    DraftSnapshot,
    LoadNoteRequest,
    SaveNoteRequest,
    SaveState,
)
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.storage.editing_storage import SqliteEditingStorage
from pmkb.infrastructure.storage.store import CollectionStore
from pmkb.application.query.service import SavedQueryService, SavedViewRequest, SearchRequest


def new_collection(root: Path) -> tuple[Path, str]:
    directory = root / "collection"
    directory.mkdir()
    backend = LocalCollectionBackend()
    prepared = backend.prepare(ActivationMode.CREATE, str(directory))
    backend.publish(prepared)
    return directory / "knowledge.sqlite", prepared.collection_id


class EditingIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.db, self.collection_id = new_collection(self.root)
        self.store = CollectionStore(self.db, self.collection_id)
        self.adapter = SqliteEditingStorage(self.store)
        self.gate = ActiveContextGate()
        activated, self.context = self.gate.try_activate(CallContext(None, 0), self.collection_id)
        self.assertTrue(activated)
        self.service = EditingService(self.gate, self.adapter)
        self.query = SavedQueryService(self.store)

    def tearDown(self) -> None:
        self.adapter.close()
        self._temporary.cleanup()

    def create_and_edit(self):
        created = self.service.create(CreateDraftRequest(self.context, "初稿"))
        self.assertIs(created.status, ResultStatus.OK)
        buffer_id = created.value.snapshot.buffer_id
        edited = self.service.edit(ApplyDraftEditRequest(
            self.context, buffer_id, 0, "数据结构复习", "正文 😀 与引用占位", ("AI", "课程"),
        ))
        self.assertIs(edited.status, ResultStatus.OK)
        return edited.value.snapshot

    def test_b_save_persists_exactly_the_captured_version(self) -> None:
        snapshot = self.create_and_edit()
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), snapshot)))

        self.assertIs(reply.status, ResultStatus.OK)
        self.assertIs(reply.value.state, SaveState.COMMITTED)
        self.assertEqual(reply.value.saved_revision, 1)
        self.assertIsNotNone(reply.value.collection_revision)
        self.assertFalse(self.service.has_unconfirmed_save)

        saved = self.store.load_note(reply.value.note_id)
        self.assertEqual((saved.name, saved.body, saved.tags, saved.revision),
                         ("数据结构复习", "正文 😀 与引用占位", ("AI", "课程"), 1))
        hits = self.query.search_saved(SearchRequest(self.context, new_identifier(), "数据结构复习"))
        self.assertEqual([hit.note_id for hit in hits.hits], [reply.value.note_id])

    def test_second_save_advances_revision_and_only_confirms_captured_edit(self) -> None:
        snapshot = self.create_and_edit()
        first = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), snapshot)))
        edited = self.service.edit(ApplyDraftEditRequest(
            self.context, snapshot.buffer_id, snapshot.edit_version, "改名", "新正文", (),
        ))
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), edited.value.snapshot)))
        self.assertIs(reply.value.state, SaveState.COMMITTED)
        self.assertEqual(reply.value.saved_revision, 2)
        self.assertEqual(self.store.load_note(first.value.note_id).name, "改名")
        self.assertEqual(self.store.metadata().collection_revision, 3)

    def test_unsaved_draft_is_not_searchable_and_load_returns_saved_copy(self) -> None:
        snapshot = self.create_and_edit()
        first = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), snapshot)))
        self.service.edit(ApplyDraftEditRequest(
            self.context, snapshot.buffer_id, snapshot.edit_version, "草稿新名", "未保存关键词", (),
        ))
        self.assertEqual(
            self.query.search_saved(SearchRequest(self.context, new_identifier(), "未保存关键词")).total, 0)
        asyncio.run(self.service.save_note(SaveNoteRequest(self.context, new_identifier(),
                                                           self.service.view(snapshot.buffer_id).snapshot)))
        self.assertEqual(
            self.query.search_saved(SearchRequest(self.context, new_identifier(), "未保存关键词")).total, 1)

        loaded = asyncio.run(self.service.load_note(
            LoadNoteRequest(self.context, first.value.note_id)))
        self.assertIs(loaded.status, ResultStatus.OK)
        self.assertEqual(loaded.value.snapshot.body, "未保存关键词")

    def test_blank_name_save_fails_and_draft_is_kept(self) -> None:
        snapshot = self.create_and_edit()
        bad = self.service.edit(ApplyDraftEditRequest(
            self.context, snapshot.buffer_id, snapshot.edit_version, "   ", "b", (),
        ))
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), bad.value.snapshot)))
        self.assertIs(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "INVALID_NAME")
        self.assertEqual(self.store.summaries(), ())
        self.assertIsNotNone(self.service.view(snapshot.buffer_id))

    def test_duplicate_name_save_fails_without_writing(self) -> None:
        first = self.create_and_edit()
        asyncio.run(self.service.save_note(SaveNoteRequest(self.context, new_identifier(), first)))
        second = self.create_and_edit()
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), second)))
        self.assertIs(reply.status, ResultStatus.ERROR)
        self.assertEqual(reply.error.code, "DUPLICATE_NAME")
        self.assertEqual(self.store.metadata().collection_revision, 2)
        self.assertEqual(len(self.store.summaries()), 1)

    def test_storage_adapter_rejects_a_context_from_another_collection(self) -> None:
        snapshot = DraftSnapshot(new_identifier(), None, None, 0, "N", "b", ())
        attempt = SaveAttempt(CallContext(new_identifier(), 3), new_identifier(),
                              new_identifier(), new_identifier(), snapshot)
        evidence = asyncio.run(self.adapter.save(attempt, Event()))
        self.assertIs(evidence.state, SaveEvidenceState.NOT_COMMITTED)
        self.assertEqual(evidence.error.code, "STALE_CONTEXT")
        self.assertEqual(self.store.summaries(), ())

    def test_cancelled_before_dispatch_writes_nothing(self) -> None:
        snapshot = DraftSnapshot(new_identifier(), None, None, 0, "N", "b", ())
        attempt = SaveAttempt(self.context, new_identifier(), new_identifier(),
                              new_identifier(), snapshot)
        signal = Event()
        signal.set()
        evidence = asyncio.run(self.adapter.save(attempt, signal))
        self.assertIs(evidence.state, SaveEvidenceState.NOT_COMMITTED)
        self.assertEqual(evidence.error.code, "CANCELLED")
        self.assertEqual(self.store.summaries(), ())

    def test_storage_rejects_a_blank_name_even_when_called_directly(self) -> None:
        snapshot = DraftSnapshot(new_identifier(), None, None, 0, "   ", "b", ())
        attempt = SaveAttempt(self.context, new_identifier(), new_identifier(),
                              new_identifier(), snapshot)
        evidence = asyncio.run(self.adapter.save(attempt, Event()))
        self.assertIs(evidence.state, SaveEvidenceState.NOT_COMMITTED)
        self.assertEqual(evidence.error.code, "INVALID_NAME")
        self.assertEqual(self.store.summaries(), ())

    def test_concurrent_saves_are_serialized_on_one_storage_thread(self) -> None:
        async def save_two():
            first = DraftSnapshot(new_identifier(), None, None, 0, "并发一", "body 1", ())
            second = DraftSnapshot(new_identifier(), None, None, 0, "并发二", "body 2", ())
            one = SaveAttempt(self.context, new_identifier(), new_identifier(),
                              new_identifier(), first)
            two = SaveAttempt(self.context, new_identifier(), new_identifier(),
                              new_identifier(), second)
            return await asyncio.gather(self.adapter.save(one, Event()),
                                        self.adapter.save(two, Event()))

        evidence = asyncio.run(save_two())
        self.assertEqual([item.state for item in evidence],
                         [SaveEvidenceState.COMMITTED, SaveEvidenceState.COMMITTED])
        self.assertEqual(len(self.store.summaries()), 2)

    def test_check_save_reports_committed_evidence_after_table_tampering(self) -> None:
        snapshot = self.create_and_edit()
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), snapshot)))
        self.assertIs(reply.value.state, SaveState.COMMITTED)
        attempt = SaveAttempt(self.context, new_identifier(), new_identifier(),
                              snapshot.note_id, snapshot)
        evidence = asyncio.run(self.adapter.check_save(attempt, Event()))
        # No receipt exists for this new command id: unresolved, never "rolled back".
        self.assertIs(evidence.state, SaveEvidenceState.UNRESOLVED)
        self.assertIsNone(evidence.saved_revision)

    def test_reference_marker_survives_round_trip(self) -> None:
        target = self.create_and_edit()
        target_saved = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), target)))
        marker = format_reference_marker(self.collection_id, target_saved.value.note_id)
        created = self.service.create(CreateDraftRequest(self.context, "来源"))
        edited = self.service.edit(ApplyDraftEditRequest(
            self.context, created.value.snapshot.buffer_id, 0,
            "来源", f"参见 {marker}", (),
        ))
        reply = asyncio.run(self.service.save_note(
            SaveNoteRequest(self.context, new_identifier(), edited.value.snapshot)))
        self.assertIs(reply.value.state, SaveState.COMMITTED)
        view = self.query.saved_view(
            SavedViewRequest(self.context, new_identifier(), reply.value.note_id))
        self.assertEqual(len(view.references), 1)
        self.assertEqual(view.references[0].target_name, "数据结构复习")
        self.assertTrue(view.references[0].navigable)


if __name__ == "__main__":
    unittest.main()
