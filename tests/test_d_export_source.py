"""Export's real SQLite source, cancellation and output preservation."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
from threading import Event
import unittest
from unittest.mock import patch

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.editing.service import EditingService
from pmkb.application.export.service import ExportRequest, ExportService
from pmkb.application.output_support import OutputBufferState, OutputFailure, OutputSessionState
from pmkb.contracts.backup import UnsavedBackupChoice
from pmkb.contracts.common import CallContext, ResultStatus
from pmkb.contracts.notes import ApplyDraftEditRequest, LoadNoteRequest
from pmkb.domain.identity import format_reference_marker, new_identifier
from pmkb.infrastructure.storage.connection import StorageFailure
from pmkb.infrastructure.storage.editing_storage import SqliteEditingStorage
from pmkb.infrastructure.storage.export_source import CExportSource
from pmkb.infrastructure.storage.store import CollectionStore
from tests.test_c_query import new_collection


class ExportSession:
    def __init__(self, context):
        self.state = OutputSessionState(context, ())

    async def inspect(self):
        return self.state


class ExportSourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pmkb-export-source-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database, collection_id = new_collection(self.root)
        self.store = CollectionStore(self.database, collection_id)
        self.context = CallContext(collection_id, 1)
        self.note_id = new_identifier()
        committed = self.store.save(
            command_id=new_identifier(), operation_id=new_identifier(),
            note_id=self.note_id, name="保存名称", body="保存正文\n第二行 😀",
            tags=(), base_revision=None,
        )
        self.assertEqual(committed.state.value, "COMMITTED")
        self.output = self.root / "output"
        self.output.mkdir()
        self.source = CExportSource(self.store)
        self.session = ExportSession(self.context)
        self.service = ExportService(self.source, self.session)
        self.request = ExportRequest(
            self.context, self.note_id, str(self.output), "chosen.md",
            UnsavedBackupChoice.NO_UNSAVED,
        )

    async def test_cancel_during_saved_read_preserves_existing_target(self):
        target = self.output / "chosen.md"
        read_saved = self.store.saved_view
        for existing in (False, True):
            with self.subTest(existing=existing):
                if existing:
                    target.write_bytes(b"original bytes")
                cancel = Event()

                def read_then_cancel(note_id):
                    saved = read_saved(note_id)
                    cancel.set()
                    return saved

                with patch.object(self.store, "saved_view", side_effect=read_then_cancel):
                    result = await self.service.export_note(self.request, cancel)
                self.assertEqual(result.status, ResultStatus.CANCELLED)
                self.assertIsNone(result.error)
                self.assertIsNone(result.value)
                self.assertEqual(list(self.output.iterdir()), [target] if existing else [])
                if existing:
                    self.assertEqual(target.read_bytes(), b"original bytes")

    async def test_storage_failure_code_reason_and_action_reach_service_result(self):
        failure = StorageFailure("COLLECTION_MISMATCH", "Saved identity changed.",
                                 "Reopen the original collection.")
        with patch.object(self.store, "saved_view", side_effect=failure):
            result = await self.service.export_note(self.request)
        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertEqual(result.error.code, failure.code)
        self.assertEqual(result.error.reason, failure.reason)
        self.assertEqual(result.error.suggested_action, failure.action)
        self.assertIsNone(result.value)
        self.assertEqual(list(self.output.iterdir()), [])

    async def test_storage_failure_unknown_flag_survives_adapter_mapping(self):
        failure = StorageFailure("READ_UNCONFIRMED", "Read outcome unavailable.",
                                 "Check the collection.", unknown=True)
        with patch.object(self.store, "saved_view", side_effect=failure):
            with self.assertRaises(OutputFailure) as raised:
                await self.source.read_saved(self.context, self.note_id, Event())
        self.assertEqual(raised.exception.error.code, failure.code)
        self.assertEqual(raised.exception.error.reason, failure.reason)
        self.assertEqual(raised.exception.error.suggested_action, failure.action)
        self.assertTrue(raised.exception.unknown)

    async def test_already_cancelled_source_does_not_read_storage(self):
        cancel = Event()
        cancel.set()
        with patch.object(self.store, "saved_view") as read:
            self.assertIsNone(await self.source.read_saved(self.context, self.note_id, cancel))
        read.assert_not_called()

    async def test_source_rejects_foreign_collection_before_storage_read(self):
        context = CallContext(new_identifier(), 2)
        with patch.object(self.store, "saved_view") as read:
            with self.assertRaises(OutputFailure) as raised:
                await self.source.read_saved(context, self.note_id, Event())
        self.assertEqual(raised.exception.error.code, "STALE_CONTEXT")
        read.assert_not_called()

    async def test_unsaved_note_identity_returns_no_saved_version(self):
        result = await self.service.export_note(replace(self.request, note_id=new_identifier()))
        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertEqual(result.error.code, "NO_SAVED_VERSION")
        self.assertIsNone(result.value)
        self.assertEqual(list(self.output.iterdir()), [])

    async def _cancel_while_reading(self, call):
        started, release, finished = Event(), Event(), Event()
        read_saved = self.store.saved_view
        cancel = Event()

        def blocked_read(note_id):
            started.set()
            try:
                if not release.wait(5):
                    raise AssertionError("The test did not release the read.")
                return read_saved(note_id)
            finally:
                finished.set()

        with patch.object(self.store, "saved_view", side_effect=blocked_read):
            task = asyncio.create_task(call(cancel))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 5))
                task.cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "Cancellation must wait for the in-flight read.")
            finally:
                release.set()
                result = await task
                self.assertTrue(await asyncio.to_thread(finished.wait, 5))
        self.assertTrue(cancel.is_set())
        self.assertEqual(list(self.output.iterdir()), [])
        return result

    async def test_direct_source_task_cancellation_waits_for_inflight_read(self):
        result = await self._cancel_while_reading(
            lambda cancel: self.source.read_saved(self.context, self.note_id, cancel))
        self.assertIsNone(result)

    async def test_export_task_cancellation_waits_and_returns_cancelled(self):
        result = await self._cancel_while_reading(
            lambda cancel: self.service.export_note(self.request, cancel))
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertIsNone(result.error)
        self.assertIsNone(result.value)

    async def test_real_saved_export_preserves_dirty_draft_database_and_existing_file(self):
        target_id = new_identifier()
        target_saved = self.store.save(
            command_id=new_identifier(), operation_id=new_identifier(),
            note_id=target_id, name="目标[*]", body="目标正文", tags=(), base_revision=None,
        )
        self.assertEqual(target_saved.state.value, "COMMITTED")
        marker = format_reference_marker(self.context.collection_id, target_id)
        saved_body = f"😀 保存正文\n{marker}\n`{marker}`\n"
        saved = self.store.save(
            command_id=new_identifier(), operation_id=new_identifier(),
            note_id=self.note_id, name="保存名称", body=saved_body, tags=(), base_revision=1,
        )
        self.assertEqual(saved.state.value, "COMMITTED")
        saved_view = self.store.saved_view(self.note_id)
        gate = ActiveContextGate()
        activated, context = gate.try_activate(CallContext(None, 0), self.context.collection_id)
        self.assertTrue(activated)
        self.assertEqual(context, self.context)
        storage = SqliteEditingStorage(self.store)
        self.addCleanup(storage.close)
        editing = EditingService(gate, storage)
        loaded = await editing.load_note(LoadNoteRequest(context, self.note_id))
        self.assertEqual(loaded.status, ResultStatus.OK)
        snapshot = loaded.value.snapshot
        edited = editing.edit(ApplyDraftEditRequest(
            context, snapshot.buffer_id, snapshot.edit_version,
            "未保存名称", "未保存正文", (),
        ))
        self.assertEqual(edited.status, ResultStatus.OK)
        draft = edited.value.snapshot
        self.session.state = OutputSessionState(
            context, (OutputBufferState(draft.buffer_id, draft.edit_version, True, self.note_id),),
            acknowledged_dirty_versions=((draft.buffer_id, draft.edit_version),),
        )
        request = replace(self.request, file_name=None,
                          unsaved_choice=UnsavedBackupChoice.CONTINUE_SAVED_ONLY)
        result = await self.service.export_note(request)
        self.assertEqual(result.status, ResultStatus.OK)
        output = Path(result.value.destination_file)
        self.assertEqual(output.name, "note-保存名称.md")
        replacement = "内部引用：目标\\[\\*\\]（仅供识别，导出后不支持应用内跳转）"
        expected_bytes = saved_body.replace(marker, replacement, 1).encode("utf-8")
        self.assertEqual(output.read_bytes(), expected_bytes)
        self.assertEqual(result.value.saved_revision, saved_view.revision)
        self.assertEqual(result.value.source_collection_revision, saved_view.collection_revision)

        retry = await self.service.export_note(request)
        self.assertEqual(retry.status, ResultStatus.ERROR)
        self.assertEqual(retry.error.code, "TARGET_EXISTS")
        self.assertIsNone(retry.value)
        self.assertEqual(output.read_bytes(), expected_bytes)
        self.assertEqual(list(self.output.iterdir()), [output])
        self.assertEqual(editing.view(draft.buffer_id).snapshot, draft)
        self.assertTrue(editing.view(draft.buffer_id).is_dirty)
        self.assertEqual(self.store.saved_view(self.note_id), saved_view)
