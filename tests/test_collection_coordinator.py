import asyncio
from dataclasses import replace
from pathlib import Path
from threading import Event
import tempfile
import unittest

from pmkb.application.collection.coordinator import CollectionCoordinatorService
from pmkb.application.collection.headless_session import HeadlessCollectionSession
from pmkb.application.collection.ports import BufferState, CollectionFailure
from pmkb.contracts.collections import *
from pmkb.contracts.common import AppError, OperationResult, ResultStatus
from pmkb.contracts.notes import SaveReply, SaveState
from pmkb.infrastructure.storage.collection_catalog import LocalCollectionBackend
from pmkb.infrastructure.file_io.identity_registry import LocalIdentityRegistry

BUF = "b" * 32

class EditingSession(HeadlessCollectionSession):
    def __init__(self):
        super().__init__()
        self.state = ()
        self.save_outcome = "ok"
        self.pending = False
        self.reject_activation = False
        self.edit_during_save = False
        self.discarded = ()

    def buffers(self): return self.state
    @property
    def has_unconfirmed_save(self): return self.pending

    async def save_buffer(self, buffer_id, edit_version, cancel):
        if self.save_outcome == "cancelled":
            return OperationResult(ResultStatus.CANCELLED, self.context, self.context, None, None)
        if self.save_outcome == "failed":
            return OperationResult(ResultStatus.ERROR, self.context, self.context, None, AppError("IO_FAILURE", "failed", "retry"))
        if self.save_outcome == "pending":
            self.pending = True
            reply = SaveReply("c"*32, "d"*32, edit_version, SaveState.PENDING, None, None, "e"*32)
            return OperationResult(ResultStatus.UNCONFIRMED, self.context, self.context, reply, AppError("COMMIT_UNCONFIRMED", "unknown", "check"))
        self.state = tuple(replace(b, is_dirty=False) if b.buffer_id == buffer_id else b for b in self.state)
        if self.edit_during_save:
            self.state = tuple(replace(b, edit_version=b.edit_version+1, is_dirty=True) for b in self.state)
        reply = SaveReply("c"*32, "d"*32, edit_version, SaveState.COMMITTED, 2, 2, None)
        return OperationResult(ResultStatus.OK, self.context, self.context, reply, None)

    async def activate(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
        if any(signal.is_set() for signal in cancel_signals) or self.reject_activation or tuple(sorted(self.state, key=lambda b:b.buffer_id)) != expected_buffers:
            return None
        result = self.gate.try_activate(expected, target.collection_id)
        if not result[0]: return None
        self.discarded = discard_ids
        self.state = ()
        return result[1]

class FailingRegistry:
    def __init__(self, unknown=False): self.unknown = unknown
    def check(self, identity, directory): pass
    def register(self, identity, directory):
        raise CollectionFailure("IO_FAILURE", "registry unavailable", unknown=self.unknown)

class CollectionCoordinatorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = EditingSession()
        self.backend = LocalCollectionBackend()
        self.registry = LocalIdentityRegistry(self.root / "config/config.json")
        self.service = CollectionCoordinatorService(self.backend, self.registry, self.session)

    async def prepare(self, name="target", mode=ActivationMode.CREATE):
        folder = self.root / name
        if mode == ActivationMode.CREATE: folder.mkdir()
        result = await self.service.prepare_activation(PrepareActivationRequest(self.session.context, mode, str(folder)))
        self.assertEqual(result.status, ResultStatus.OK)
        return result.value

    async def complete(self, ticket, decisions=()):
        return await self.service.complete_activation(CompleteActivationRequest(self.session.context, ticket.ticket_id, decisions))

    def dirty(self):
        self.session.state = (BufferState(BUF, 4, True),)
        return DraftLeaveDecision(BUF, 4, LeaveChoice.DISCARD)

    async def test_real_create_then_open_advances_context(self):
        ticket = await self.prepare()
        self.assertIsNone(self.session.context.collection_id)
        result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.OK)
        self.assertEqual(result.value.stage, TargetStage.ACTIVATED)
        self.assertTrue((self.root / "target/knowledge.sqlite").is_file())
        opened = await self.prepare("target", ActivationMode.OPEN)
        result = await self.complete(opened)
        self.assertEqual(result.status, ResultStatus.OK)
        self.assertEqual(self.session.context.context_generation, 2)
        self.assertFalse(result.value.created_by_this_operation)

    async def test_cancel_before_publication_preserves_unsaved_state(self):
        decision = self.dirty(); ticket = await self.prepare()
        result = await self.service.cancel_activation(CancelActivationRequest(self.session.context, ticket.ticket_id))
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertEqual(self.session.state, (BufferState(BUF, 4, True),))
        self.assertEqual(list((self.root/"target").iterdir()), [])

    async def test_discard_occurs_only_after_real_activation(self):
        decision = self.dirty(); ticket = await self.prepare()
        self.assertEqual(self.session.discarded, ())
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.OK)
        self.assertEqual(self.session.discarded, (BUF,))

    async def test_user_cancel_decision_preserves_drafts_without_publishing(self):
        decision = replace(self.dirty(), choice=LeaveChoice.CANCEL); ticket = await self.prepare()
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertTrue(self.session.state[0].is_dirty)
        self.assertFalse((self.root/"target/knowledge.sqlite").exists())

    async def test_missing_duplicate_or_stale_draft_decisions_are_rejected(self):
        decision = self.dirty(); ticket = await self.prepare()
        for decisions in [(), (decision, decision), (replace(decision, observed_edit_version=3),)]:
            with self.subTest(decisions=decisions):
                result = await self.complete(ticket, decisions)
                self.assertEqual(result.status, ResultStatus.ERROR)
                self.assertTrue(self.session.state[0].is_dirty)

    async def test_ordinary_save_failure_stops_switch_without_discard(self):
        decision = replace(self.dirty(), choice=LeaveChoice.SAVE); ticket = await self.prepare()
        self.session.save_outcome = "failed"
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertIsNone(self.session.context.collection_id)
        self.assertTrue(self.session.state[0].is_dirty)
        self.assertFalse((self.root/"target/knowledge.sqlite").exists())

    async def test_pending_save_reports_identifiers_and_blocks_new_prepare(self):
        decision = replace(self.dirty(), choice=LeaveChoice.SAVE); ticket = await self.prepare()
        self.session.save_outcome = "pending"
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(result.value.pending_save_id, "e"*32)
        self.assertEqual(result.value.pending_command_id, "c"*32)
        self.assertEqual(result.value.stage, TargetStage.TEMP_VALIDATED)
        blocked = await self.service.prepare_activation(PrepareActivationRequest(self.session.context, ActivationMode.OPEN, str(self.root/"target")))
        self.assertEqual(blocked.error.code, "STATE_BLOCKED")

    async def test_new_edit_after_save_preserves_edit_and_reports_partial(self):
        decision = replace(self.dirty(), choice=LeaveChoice.SAVE); ticket = await self.prepare()
        self.session.edit_during_save = True
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.value.saved_buffer_ids, (BUF,))
        self.assertIsNone(self.session.context.collection_id)
        self.assertEqual(self.session.state[0], BufferState(BUF, 5, True))

    async def test_registration_failure_after_publication_retains_valid_new_collection(self):
        decision = self.dirty(); ticket = await self.prepare()
        self.service.registry = FailingRegistry()
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.value.stage, TargetStage.PUBLISHED_NEW)
        self.assertIsNone(self.session.context.collection_id)
        self.assertTrue(self.session.state[0].is_dirty)
        self.backend.prepare(ActivationMode.OPEN, str(self.root/"target"))
        cancelled = await self.service.cancel_activation(CancelActivationRequest(self.session.context, ticket.ticket_id))
        self.assertEqual(cancelled.status, ResultStatus.PARTIAL)
        self.assertTrue((self.root/"target/knowledge.sqlite").exists())

    async def test_registration_unknown_uses_none_without_pending_save_identifiers(self):
        ticket = await self.prepare(); self.service.registry = FailingRegistry(unknown=True)
        result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        self.assertIsNone(result.value.configuration_updated)
        self.assertIsNone(result.value.pending_save_id)
        self.assertEqual(result.value.stage, TargetStage.PUBLISHED_NEW)

    async def test_activation_failure_after_publication_and_registration_keeps_drafts(self):
        decision = self.dirty(); ticket = await self.prepare(); self.session.reject_activation = True
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertTrue(result.value.configuration_updated)
        self.assertTrue(self.session.state[0].is_dirty)
        self.assertIsNone(result.value.activated_generation)

    async def test_external_file_after_prepare_is_never_overwritten(self):
        ticket = await self.prepare()
        formal = self.root/"target/knowledge.sqlite"; formal.write_bytes(b"external")
        result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.ERROR)
        self.assertEqual(formal.read_bytes(), b"external")

    async def test_stale_ticket_is_rejected_after_activation(self):
        ticket = await self.prepare(); old = self.session.context
        await self.complete(ticket)
        result = await self.service.cancel_activation(CancelActivationRequest(old, ticket.ticket_id))
        self.assertEqual(result.error.code, "STALE_CONTEXT")

    async def test_pre_cancelled_request_makes_no_files(self):
        folder = self.root/"cancelled"; folder.mkdir(); cancel = Event(); cancel.set()
        result = await self.service.prepare_activation(PrepareActivationRequest(self.session.context, ActivationMode.CREATE, str(folder)), cancel)
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertEqual(list(folder.iterdir()), [])

    async def test_invalid_request_returns_argument_error(self):
        result = await self.service.prepare_activation(PrepareActivationRequest(self.session.context, "CREATE", "relative"))
        self.assertEqual(result.error.code, "INVALID_ARGUMENT")

    async def test_cancel_after_publication_has_partial_details_and_error(self):
        ticket = await self.prepare()
        self.session.reject_activation = True
        await self.complete(ticket)
        result = await self.service.cancel_activation(CancelActivationRequest(self.session.context, ticket.ticket_id))
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertIsNotNone(result.error)

    async def test_cancel_waits_for_inflight_publication_and_preserves_formal_file(self):
        import threading
        class SlowBackend(LocalCollectionBackend):
            def __init__(self):
                super().__init__(); self.entered = threading.Event(); self.release = threading.Event()
            def publish(self, target):
                self.entered.set()
                if not self.release.wait(10): raise RuntimeError("test release timed out")
                return super().publish(target)
        backend = SlowBackend(); self.service.backend = backend
        ticket = await self.prepare()
        task = asyncio.create_task(self.complete(ticket))
        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 10))
        cancellation = asyncio.create_task(self.service.cancel_activation(CancelActivationRequest(self.session.context, ticket.ticket_id)))
        await asyncio.sleep(0)
        self.assertFalse(cancellation.done())
        backend.release.set()
        result, cancelled = await asyncio.gather(task, cancellation)
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(cancelled.status, ResultStatus.PARTIAL)
        self.assertTrue((self.root/"target/knowledge.sqlite").exists())
        self.assertIsNone(self.session.context.collection_id)

    async def test_cancelling_await_does_not_abandon_a_publishing_worker(self):
        import threading
        class SlowBackend(LocalCollectionBackend):
            def __init__(self):
                super().__init__(); self.entered = threading.Event(); self.release = threading.Event()
            def publish(self, target):
                self.entered.set()
                if not self.release.wait(10): raise RuntimeError("test release timed out")
                return super().publish(target)
        backend = SlowBackend(); self.service.backend = backend
        ticket = await self.prepare()
        task = asyncio.create_task(self.complete(ticket))
        self.assertTrue(await asyncio.to_thread(backend.entered.wait, 10))
        task.cancel(); backend.release.set()
        result = await task
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertTrue((self.root/"target/knowledge.sqlite").exists())
        self.assertIsNone(self.session.context.collection_id)

    async def test_cancel_before_queued_ui_activation_prevents_discard(self):
        class WaitingSession(EditingSession):
            def __init__(self):
                super().__init__(); self.entered = asyncio.Event(); self.release = asyncio.Event()
            async def activate(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
                self.entered.set(); await self.release.wait()
                if any(signal.is_set() for signal in cancel_signals): return None
                return await super().activate(expected, target, expected_buffers, discard_ids)
        session = WaitingSession(); self.session = session; self.service.session = session
        decision = self.dirty(); ticket = await self.prepare()
        operation = asyncio.create_task(self.complete(ticket, (decision,)))
        await asyncio.wait_for(session.entered.wait(), 10)
        cancellation = asyncio.create_task(self.service.cancel_activation(CancelActivationRequest(session.context, ticket.ticket_id)))
        await asyncio.sleep(0); session.release.set()
        result, _ = await asyncio.gather(operation, cancellation)
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertIsNone(session.context.collection_id)
        self.assertEqual(session.discarded, ())
        self.assertTrue(session.state[0].is_dirty)

    async def test_bad_ticket_type_returns_error_instead_of_raising(self):
        result = await self.service.complete_activation(CompleteActivationRequest(self.session.context, [], ()))
        self.assertEqual(result.error.code, "INVALID_ARGUMENT")

    async def test_publication_unknown_is_not_treated_as_source_save_unknown(self):
        class UncertainBackend(LocalCollectionBackend):
            def publish(self, target):
                super().publish(target)
                raise CollectionFailure("IO_FAILURE", "unknown publication", unknown=True)
        self.service.backend = UncertainBackend()
        ticket = await self.prepare()
        result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        self.assertEqual(result.value.stage, TargetStage.UNKNOWN)
        self.assertIsNone(result.value.pending_save_id)
        cancelled = await self.service.cancel_activation(CancelActivationRequest(self.session.context, ticket.ticket_id))
        self.assertEqual(cancelled.status, ResultStatus.UNCONFIRMED)
        self.assertIsNotNone(cancelled.error)
        self.assertTrue((self.root/"target/knowledge.sqlite").exists())

    async def test_missing_registry_outcome_cannot_be_treated_as_success(self):
        class MissingOutcomeRegistry:
            def check(self, *args): pass
            def register(self, *args): return None
        ticket = await self.prepare(); self.service.registry = MissingOutcomeRegistry()
        result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.UNCONFIRMED)
        self.assertIsNone(result.value.configuration_updated)
        self.assertIsNone(self.session.context.collection_id)

    async def test_source_save_cancel_with_no_effect_is_cancelled_not_error(self):
        decision = replace(self.dirty(), choice=LeaveChoice.SAVE); ticket = await self.prepare()
        self.session.save_outcome = "cancelled"
        result = await self.complete(ticket, (decision,))
        self.assertEqual(result.status, ResultStatus.CANCELLED)
        self.assertTrue(self.session.state[0].is_dirty)
        self.assertFalse((self.root/"target/knowledge.sqlite").exists())

    async def test_cleanup_failure_after_install_reports_activated_context(self):
        class CleanupFailSession(EditingSession):
            async def activate(self, *args):
                result = await super().activate(*args)
                if result is not None: raise CollectionFailure("IO_FAILURE", "old resource cleanup failed")
                return result
        self.session = CleanupFailSession(); self.service.session = self.session
        ticket = await self.prepare(); result = await self.complete(ticket)
        self.assertEqual(result.status, ResultStatus.PARTIAL)
        self.assertEqual(result.value.stage, TargetStage.ACTIVATED)
        self.assertEqual(result.delivery_context, self.session.context)
        self.assertEqual(result.value.activated_generation, 1)
