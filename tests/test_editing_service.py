"""B behavior tests. Storage below is a test double, never a product backend."""
import asyncio
from dataclasses import replace
from threading import Event
import unittest

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.editing.service import EditingService
from pmkb.application.editing.ports import SavedNote, SaveEvidence, SaveEvidenceState
from pmkb.contracts.common import CallContext, ResultStatus
from pmkb.contracts.notes import (CreateDraftRequest, ApplyDraftEditRequest,
                                 LoadNoteRequest, SaveNoteRequest, SaveState)
from pmkb.contracts.commit_resolution import (CheckCommitRequest, CloseUnconfirmedRequest,
                                             CommitCheckState)

A, N = 'a' * 32, 'b' * 32


class MemoryStorage:
    def __init__(self):
        self.calls = []
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.release.set()
        self.state = SaveEvidenceState.COMMITTED
        self.check_state = SaveEvidenceState.UNRESOLVED
        self.failure = None

    async def load_note(self, context, note_id, cancel):
        return SavedNote(note_id, 1, 'saved name', 'saved body', ('AI',))

    async def save(self, attempt, cancel):
        self.calls.append(attempt)
        self.started.set()
        await self.release.wait()
        if self.failure:
            raise self.failure
        return self.evidence(attempt, self.state)

    async def check_save(self, attempt, cancel):
        return self.evidence(attempt, self.check_state)

    @staticmethod
    def evidence(attempt, state):
        revision = (attempt.snapshot.base_revision or 0) + 1
        return SaveEvidence(attempt.context, attempt.command_id, attempt.note_id,
                            attempt.snapshot, state,
                            revision if state == SaveEvidenceState.COMMITTED else None,
                            2 if state == SaveEvidenceState.COMMITTED else None)


class EditingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gate = ActiveContextGate()
        self.gate.try_activate(self.gate.current, A)
        self.ctx = self.gate.current
        self.storage = MemoryStorage()
        self.service = EditingService(self.gate, self.storage)
        self.command = 0

    async def create(self, name='note'):
        r = await self.service.create_draft(CreateDraftRequest(self.ctx, name))
        self.assertEqual(r.status, ResultStatus.OK)
        return r.value

    async def edit(self, view, **changes):
        s = view.snapshot
        r = await self.service.apply_draft_edit(ApplyDraftEditRequest(
            self.ctx, s.buffer_id, s.edit_version,
            changes.get('name', s.name), changes.get('body', s.body),
            changes.get('tags', s.tags)))
        self.assertEqual(r.status, ResultStatus.OK)
        return r.value

    def request(self, view):
        self.command += 1
        return SaveNoteRequest(self.ctx, f'{self.command:032x}', view.snapshot)

    async def pending(self):
        view = await self.create()
        self.storage.state = SaveEvidenceState.UNRESOLVED
        result = await self.service.save_note(self.request(view))
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        return view, result.value

    async def test_new_draft_is_dirty_even_empty_and_has_no_saved_identity(self):
        v = await self.create('')
        self.assertTrue(v.is_dirty)
        self.assertIsNone(v.snapshot.note_id)
        self.assertIsNone(v.snapshot.base_revision)
        self.assertEqual(v.snapshot.edit_version, 0)

    async def test_all_edits_increment_and_stale_edit_preserves_latest_input(self):
        original = await self.create()
        v = await self.edit(original, name='', body='中文😀\r\n[[ref]]', tags=('AI', 'ai'))
        self.assertEqual(v.snapshot.edit_version, 1)
        result = await self.service.apply_draft_edit(ApplyDraftEditRequest(
            self.ctx, v.snapshot.buffer_id, 0, 'lost', '', ()))
        self.assertEqual(result.error.code, 'REVISION_CONFLICT')
        self.assertEqual(self.service.view(v.snapshot.buffer_id), v)

    async def test_load_existing_buffer_never_overwrites_unsaved_edits(self):
        r = await self.service.load_note(LoadNoteRequest(self.ctx, N))
        self.assertFalse(r.value.is_dirty)
        edited = await self.edit(r.value, body='keep me')
        loaded = await self.service.load_note(LoadNoteRequest(self.ctx, N))
        self.assertEqual(loaded.value, edited)

    async def test_save_confirms_only_captured_version_and_advances_base(self):
        view = await self.create()
        self.storage.release.clear()
        task = asyncio.create_task(self.service.save_note(self.request(view)))
        await self.storage.started.wait()
        later = await self.edit(view, body='typed while saving')
        self.storage.release.set()
        result = await task
        current = self.service.view(view.snapshot.buffer_id)
        self.assertEqual(result.status, ResultStatus.OK)
        self.assertEqual(result.value.captured_edit_version, 0)
        self.assertTrue(current.is_dirty)
        self.assertEqual(current.snapshot.body, later.snapshot.body)
        self.assertEqual(current.snapshot.base_revision, 1)
        second = await self.service.save_note(self.request(current))
        self.assertEqual(second.value.saved_revision, 2)
        self.assertFalse(self.service.view(current.snapshot.buffer_id).is_dirty)

    async def test_confirmed_failure_keeps_input_and_previous_saved_fact(self):
        loaded = (await self.service.load_note(LoadNoteRequest(self.ctx, N))).value
        changed = await self.edit(loaded, body='new')
        self.storage.state = SaveEvidenceState.NOT_COMMITTED
        result = await self.service.save_note(self.request(changed))
        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertIsNone(result.value)
        self.assertEqual(self.service.view(changed.snapshot.buffer_id), changed)
        self.assertFalse(self.service.has_unconfirmed_save)

    async def test_exception_after_dispatch_is_unknown_not_ordinary_failure(self):
        self.storage.failure = OSError('private path must not be exposed')
        v = await self.create()
        r = await self.service.save_note(self.request(v))
        self.assertEqual(r.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(r.value.state, SaveState.PENDING)
        self.assertNotIn('private path', r.error.reason)
        self.assertTrue(self.service.view(v.snapshot.buffer_id).is_dirty)

    async def test_unknown_blocks_new_writes_and_reads_but_preserves_editing(self):
        view, pending = await self.pending()
        changed = await self.edit(view, body='still in memory')
        result = await self.service.save_note(self.request(changed))
        self.assertEqual(result.error.code, 'STATE_BLOCKED')
        result = await self.service.load_note(LoadNoteRequest(self.ctx, N))
        self.assertEqual(result.error.code, 'STATE_BLOCKED')
        self.assertEqual(len(self.storage.calls), 1)

    async def test_check_ok_not_committed_does_not_mean_save_succeeded(self):
        view, p = await self.pending()
        self.storage.check_state = SaveEvidenceState.NOT_COMMITTED
        r = await self.service.check_commit(CheckCommitRequest(self.ctx, p.pending_save_id, p.command_id))
        self.assertEqual(r.status, ResultStatus.OK)
        self.assertEqual(r.value.state, CommitCheckState.NOT_COMMITTED)
        self.assertTrue(self.service.view(view.snapshot.buffer_id).is_dirty)
        self.assertFalse(self.service.has_unconfirmed_save)

    async def test_check_committed_only_confirms_original_frozen_version(self):
        view, p = await self.pending()
        await self.edit(view, body='later')
        self.storage.check_state = SaveEvidenceState.COMMITTED
        r = await self.service.check_commit(CheckCommitRequest(self.ctx, p.pending_save_id, p.command_id))
        self.assertEqual(r.value.state, CommitCheckState.COMMITTED)
        self.assertTrue(self.service.view(view.snapshot.buffer_id).is_dirty)
        self.assertEqual(self.service.view(view.snapshot.buffer_id).snapshot.note_id, p.note_id)

    async def test_forged_evidence_cannot_confirm_save(self):
        async def wrong(attempt, cancel):
            return replace(self.storage.evidence(attempt, SaveEvidenceState.COMMITTED),
                           context=CallContext('c' * 32, 1))
        self.storage.save = wrong
        r = await self.service.save_note(self.request(await self.create()))
        self.assertEqual(r.status, ResultStatus.UNCONFIRMED)

    async def test_unknown_exit_requires_all_latest_buffers_and_both_acknowledgements(self):
        view, p = await self.pending()
        other = await self.create('second')
        versions = {v.snapshot.buffer_id: v.snapshot.edit_version for v in (view, other)}
        request = CloseUnconfirmedRequest(self.ctx, p.pending_save_id, p.command_id,
                                         True, True, versions)
        r = await self.service.close_unconfirmed_session(request)
        self.assertTrue(r.value.ready_to_close)
        self.assertTrue(r.value.commit_still_unknown)
        self.assertEqual(len(r.value.buffers_to_discard_on_exit), 2)
        # Permission is not an eager discard; a later edit invalidates the permission.
        await self.edit(other, body='changed after confirmation')
        self.assertFalse(self.service.exit_permit_current(request))
        r = await self.service.close_unconfirmed_session(request)
        self.assertEqual(r.status, ResultStatus.ERROR)
        self.assertEqual(len(self.service.views()), 2)

    async def test_pre_dispatch_cancel_is_safe_but_late_cancel_keeps_success(self):
        v = await self.create()
        cancel = Event()
        cancel.set()
        r = await self.service.save_note(self.request(v), cancel)
        self.assertEqual(r.status, ResultStatus.CANCELLED)
        self.assertFalse(self.storage.calls)
        cancel.clear()
        self.storage.release.clear()
        task = asyncio.create_task(self.service.save_note(self.request(v), cancel))
        await self.storage.started.wait()
        cancel.set()
        self.storage.release.set()
        self.assertEqual((await task).status, ResultStatus.OK)

    async def test_outer_task_cancel_waits_for_real_storage_outcome(self):
        self.storage.release.clear()
        v = await self.create()
        task = asyncio.create_task(self.service.save_note(self.request(v)))
        await self.storage.started.wait()
        task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.storage.release.set()
        self.assertEqual((await task).status, ResultStatus.OK)

    async def test_missing_storage_never_reports_saved(self):
        service = EditingService(self.gate)
        v = (await service.create_draft(CreateDraftRequest(self.ctx, 'note'))).value
        r = await service.save_note(self.request(v))
        self.assertEqual(r.status, ResultStatus.ERROR)
        self.assertEqual(r.error.code, 'STATE_BLOCKED')
        self.assertTrue(service.view(v.snapshot.buffer_id).is_dirty)

    async def test_stale_context_and_boolean_version_are_rejected(self):
        v = await self.create()
        r = await self.service.apply_draft_edit(ApplyDraftEditRequest(
            self.ctx, v.snapshot.buffer_id, False, '', '', ()))
        self.assertEqual(r.error.code, 'INVALID_ARGUMENT')
        r = await self.service.create_draft(CreateDraftRequest(CallContext(A, 0), 'bad'))
        self.assertEqual(r.error.code, 'STALE_CONTEXT')

    async def test_snapshot_tampering_and_command_reuse_do_not_write(self):
        v = await self.create()
        request = self.request(v)
        r = await self.service.save_note(replace(request, snapshot=replace(v.snapshot, body='forged')))
        self.assertEqual(r.status, ResultStatus.ERROR)
        self.assertFalse(self.storage.calls)
        await self.service.save_note(request)
        latest = self.service.view(v.snapshot.buffer_id)
        r = await self.service.save_note(replace(request, snapshot=latest.snapshot))
        self.assertEqual(r.error.code, 'REVISION_CONFLICT')
        self.assertEqual(len(self.storage.calls), 1)

    async def test_tag_shape_limits_and_duplicates_reject_without_mutation(self):
        v = await self.create()
        for tags in [('AI', 'AI'), ('bad\t',), ('x' * 31,), tuple(str(i) for i in range(21))]:
            r = await self.service.apply_draft_edit(ApplyDraftEditRequest(
                self.ctx, v.snapshot.buffer_id, 0, 'note', '', tags))
            self.assertEqual(r.status, ResultStatus.ERROR)
            self.assertEqual(self.service.view(v.snapshot.buffer_id), v)

    async def test_save_rejects_boolean_snapshot_version_without_dispatch(self):
        v = await self.create()
        r = await self.service.save_note(replace(self.request(v),
            snapshot=replace(v.snapshot, edit_version=False)))
        self.assertEqual(r.error.code, 'INVALID_ARGUMENT')
        self.assertFalse(self.storage.calls)

    async def test_invalid_name_and_body_preserve_inputs_without_write(self):
        for name, body in [('x' * 101, ''), ('\tnote', ''), ('note', '\ud800')]:
            v = await self.create(name)
            v = await self.edit(v, body=body)
            result = await self.service.save_note(self.request(v))
            self.assertEqual(result.status, ResultStatus.ERROR)
            self.assertEqual(self.service.view(v.snapshot.buffer_id), v)
        self.assertFalse(self.storage.calls)

    async def test_tag_normalization_uses_explicit_design_whitespace_set(self):
        v = await self.create('😀' * 100)
        v = await self.edit(v, tags=('\u3000AI\u00a0', 'ai', '😀' * 30))
        self.assertEqual(v.snapshot.tags, ('AI', 'ai', '😀' * 30))
        self.assertEqual((await self.service.save_note(self.request(v))).status, ResultStatus.OK)

    async def test_false_revision_evidence_and_foreign_body_are_unknown(self):
        original_save = self.storage.save
        async def wrong(attempt, cancel):
            return replace(self.storage.evidence(attempt, SaveEvidenceState.COMMITTED),
                           saved_revision=True)
        self.storage.save = wrong
        result = await self.service.save_note(self.request(await self.create()))
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        p = result.value
        async def wrong_check(attempt, cancel):
            return replace(self.storage.evidence(attempt, SaveEvidenceState.COMMITTED),
                           snapshot=replace(attempt.snapshot, body='other content'))
        self.storage.check_save = wrong_check
        checked = await self.service.check_commit(CheckCommitRequest(self.ctx, p.pending_save_id, p.command_id))
        self.assertEqual(checked.status, ResultStatus.UNCONFIRMED)

    async def test_exit_authorization_does_not_follow_mutated_callers_mapping(self):
        v, p = await self.pending()
        versions = {v.snapshot.buffer_id: v.snapshot.edit_version}
        request = CloseUnconfirmedRequest(self.ctx, p.pending_save_id, p.command_id, True, True, versions)
        self.assertEqual((await self.service.close_unconfirmed_session(request)).status, ResultStatus.OK)
        newer = await self.edit(v, body='newer')
        versions[v.snapshot.buffer_id] = newer.snapshot.edit_version
        self.assertFalse(self.service.exit_permit_current(request))


if __name__ == '__main__':
    unittest.main()
