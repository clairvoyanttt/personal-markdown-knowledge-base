"""API01 orchestration. IO and editor state are supplied through narrow ports."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
import re
import secrets
from threading import Event

from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.collection.ports import (
    BufferState, CollectionBackend, CollectionFailure, CollectionSession,
    IdentityRegistry, PreparedCollection, PublishOutcome,
)
from pmkb.contracts.collections import (
    ActivationMode, ActivationReply, ActivationTicketView, CancelActivationRequest,
    CompleteActivationRequest, DraftLeaveDecision, LeaveChoice, PrepareActivationRequest, TargetStage,
)
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.contracts.notes import SaveState


@dataclass
class _Ticket:
    ticket_id: str
    source: CallContext
    target: PreparedCollection
    stage: TargetStage
    cancel: Event = field(default_factory=Event)
    saved: list[str] = field(default_factory=list)
    configuration_updated: bool | None = False
    created: bool = False
    activated: CallContext | None = None
    uncertain: bool = False
    finished: bool = False
    pending_save_id: str | None = None
    pending_command_id: str | None = None
    warnings: list[AppError] = field(default_factory=list)


class CollectionCoordinatorService:
    """One instance on one application event loop; Complete/Cancel serialize.

    Cancellation of the caller never abandons an in-flight side effect. It
    records intent, waits for its actual outcome, then reports known effects.
    """
    def __init__(self, backend: CollectionBackend, registry: IdentityRegistry, session: CollectionSession):
        self.backend, self.registry, self.session = backend, registry, session
        self._lock = asyncio.Lock()
        self._tickets: dict[str, _Ticket] = {}
        self._closed = False

    async def close(self):
        """Runtime finalizer, after all accepted operations have settled.

        Clean only owned candidates. Published and uncertain artifacts survive;
        this is resource disposal, not approval to discard editor drafts.
        """
        signal = Event()
        async with self._serialized(signal):
            self._closed = True
            warnings = []
            for ticket in self._tickets.values():
                before = len(ticket.warnings)
                await self._cleanup(ticket, signal)
                ticket.finished = True
                warnings.extend(ticket.warnings[before:])
                if ticket.stage == TargetStage.UNKNOWN:
                    warnings.append(AppError('IO_FAILURE', 'Unconfirmed publication was retained.', 'Inspect the target; do not delete it as a temporary file.'))
            if warnings:
                raise CollectionFailure('IO_FAILURE', 'Some candidate resources require manual inspection.', 'Inspect the reported operation stages and target locations.')

    @staticmethod
    async def _settle(awaitable, signal: Event):
        work = asyncio.ensure_future(awaitable)
        while True:
            try:
                return await asyncio.shield(work)
            except asyncio.CancelledError:
                signal.set()
                if work.cancelled():
                    raise CollectionFailure("IO_FAILURE", "An underlying operation did not supply a confirmed outcome.", unknown=True)

    @asynccontextmanager
    async def _serialized(self, signal):
        await self._settle(self._lock.acquire(), signal)
        try:
            yield
        finally:
            self._lock.release()

    async def _io(self, method, signal, *args):
        return await self._settle(asyncio.to_thread(method, *args), signal)

    def _context(self, context):
        if self._closed:
            raise CollectionFailure('STATE_BLOCKED', 'The collection service has closed.')
        if not ActiveContextGate._valid_context(context):
            raise CollectionFailure("INVALID_ARGUMENT", "Invalid call context.")
        if self.session.context != context:
            raise CollectionFailure("STALE_CONTEXT", "The active collection changed; start a new operation.")

    def _buffers(self):
        states = tuple(self.session.buffers())
        if (any(not isinstance(b, BufferState) or not isinstance(b.buffer_id, str)
                or type(b.edit_version) is not int or b.edit_version < 0 or type(b.is_dirty) is not bool for b in states)
                or len({b.buffer_id for b in states}) != len(states)):
            raise CollectionFailure("STATE_BLOCKED", "Editor supplied an invalid buffer-state snapshot.")
        return tuple(sorted(states, key=lambda b: b.buffer_id))

    @staticmethod
    def _reply(t):
        formal = t.target.directory if t.created or t.target.mode == ActivationMode.OPEN else None
        return ActivationReply(t.ticket_id, t.stage, t.created, formal,
                               t.target.collection_id if formal else None,
                               t.activated.context_generation if t.activated else None,
                               t.configuration_updated, tuple(t.saved), t.pending_save_id, t.pending_command_id)

    def _result(self, context, status, value=None, failure=None, warnings=()):
        delivery = self.session.context if value and getattr(value, "stage", None) == TargetStage.ACTIVATED else context
        error = AppError(failure.code, failure.reason, failure.action) if failure else None
        return OperationResult(status, context, delivery, value, error, tuple(warnings))

    def _outcome(self, t, failure=None, cancelled=False):
        if t.uncertain:
            status = ResultStatus.UNCONFIRMED
        elif t.activated and failure is None and not t.warnings:
            status = ResultStatus.OK
        elif t.created or t.saved or t.configuration_updated is True or t.activated:
            status = ResultStatus.PARTIAL
        else:
            status = ResultStatus.CANCELLED if cancelled else ResultStatus.ERROR
        if failure is None and status == ResultStatus.PARTIAL:
            failure = CollectionFailure("STATE_BLOCKED", "The operation has confirmed effects and unfinished work or cleanup.")
        if failure is None and status == ResultStatus.UNCONFIRMED:
            failure = CollectionFailure("COMMIT_UNCONFIRMED" if t.pending_save_id else "IO_FAILURE", "The operation still has unconfirmed effects.", unknown=True)
        return self._result(t.source, status, self._reply(t), failure, t.warnings)

    def _ticket(self, request):
        self._context(request.context)
        if not isinstance(request.ticket_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request.ticket_id):
            raise CollectionFailure("INVALID_ARGUMENT", "Invalid activation ticket identifier.")
        t = self._tickets.get(request.ticket_id)
        if t is None or t.source != request.context:
            raise CollectionFailure("INVALID_ARGUMENT", "Unknown activation ticket.")
        return t

    async def _cleanup(self, t, signal):
        if t.stage == TargetStage.UNKNOWN:
            return  # Publication might be formal; do not infer absence.
        try:
            for warning in await self._io(self.backend.cleanup, signal, t.target):
                t.warnings.append(AppError("IO_FAILURE", warning, "Inspect the stated temporary location."))
        except (CollectionFailure, OSError):
            t.warnings.append(AppError("IO_FAILURE", "Candidate cleanup did not complete.", "Inspect the temporary candidate; do not delete the collection directory."))

    async def prepare_activation(self, request: PrepareActivationRequest, cancel: Event | None = None):
        signal = cancel if cancel is not None else Event()
        context = getattr(request, "context", CallContext(None, 0))
        target = None
        started = False
        async with self._serialized(signal):
            try:
                if not isinstance(request, PrepareActivationRequest):
                    raise CollectionFailure("INVALID_ARGUMENT", "Invalid prepare request.")
                self._context(context)
                if self.session.has_unconfirmed_save:
                    raise CollectionFailure("STATE_BLOCKED", "A save result must be resolved before switching.")
                if not isinstance(request.mode, ActivationMode) or not isinstance(request.target_directory, str) or not Path(request.target_directory).is_absolute():
                    raise CollectionFailure("INVALID_ARGUMENT", "Choose an absolute collection directory and valid mode.")
                if signal.is_set():
                    return self._result(context, ResultStatus.CANCELLED)
                target = await self._io(self.backend.prepare, signal, request.mode, request.target_directory)
                self._context(context)
                await self._io(self.registry.check, signal, target.collection_id, target.directory)
                stage = TargetStage.TEMP_VALIDATED if request.mode == ActivationMode.CREATE else TargetStage.EXISTING_READY
                ticket = _Ticket(secrets.token_hex(16), context, target, stage)
                if signal.is_set():
                    await self._cleanup(ticket, signal)
                    return self._result(context, ResultStatus.CANCELLED, warnings=ticket.warnings)
                self._tickets[ticket.ticket_id] = ticket
                view = ActivationTicketView(ticket.ticket_id, request.mode, target.directory, target.collection_id, stage)
                return self._result(context, ResultStatus.OK, view)
            except CollectionFailure as error:
                if target is not None:
                    scratch = _Ticket("", context, target, TargetStage.TEMP_VALIDATED)
                    await self._cleanup(scratch, signal)
                    return self._result(context, ResultStatus.ERROR, failure=error, warnings=scratch.warnings)
                return self._result(context, ResultStatus.ERROR, failure=error)
            except (OSError, ValueError) as error:
                return self._result(context, ResultStatus.ERROR, failure=CollectionFailure("IO_FAILURE", "Collection preparation failed."))

    async def complete_activation(self, request: CompleteActivationRequest, cancel: Event | None = None):
        signal = cancel if cancel is not None else Event()
        context = getattr(request, "context", CallContext(None, 0))
        t = None
        started = False
        async with self._serialized(signal):
            try:
                if not isinstance(request, CompleteActivationRequest):
                    raise CollectionFailure("INVALID_ARGUMENT", "Invalid completion request.")
                t = self._ticket(request)
                if t.finished or self.session.has_unconfirmed_save:
                    raise CollectionFailure("STATE_BLOCKED", "Resolve any pending result and prepare a new ticket.")
                original = self._buffers()
                dirty = {b.buffer_id: b for b in original if b.is_dirty}
                if not isinstance(request.decisions, tuple):
                    raise CollectionFailure("INVALID_ARGUMENT", "Decisions must be a frozen tuple.")
                choices = {}
                for choice in request.decisions:
                    if (not isinstance(choice, DraftLeaveDecision) or not isinstance(choice.buffer_id, str) or choice.buffer_id in choices
                            or choice.buffer_id not in dirty or type(choice.observed_edit_version) is not int
                            or choice.observed_edit_version != dirty[choice.buffer_id].edit_version
                            or not isinstance(choice.choice, LeaveChoice)):
                        raise CollectionFailure("INVALID_ARGUMENT", "Draft choices are duplicate, stale or invalid.")
                    choices[choice.buffer_id] = choice
                if choices.keys() != dirty.keys():
                    raise CollectionFailure("INVALID_ARGUMENT", "Every unsaved buffer requires a current choice.")
                if signal.is_set() or t.cancel.is_set() or any(c.choice == LeaveChoice.CANCEL for c in choices.values()):
                    t.finished = True
                    await self._cleanup(t, signal)
                    return self._outcome(t, cancelled=True)
                started = True
                await self._io(self.backend.validate, signal, t.target, False)
                expected = {b.buffer_id: b for b in original}
                for identity, choice in choices.items():
                    if signal.is_set() or t.cancel.is_set():
                        t.finished = True
                        await self._cleanup(t, signal)
                        return self._outcome(t, cancelled=True)
                    if choice.choice == LeaveChoice.SAVE:
                        result = await self._settle(self.session.save_buffer(identity, choice.observed_edit_version, t.cancel), signal)
                        value = result.value
                        if result.status == ResultStatus.UNCONFIRMED:
                            t.uncertain = True
                            t.pending_save_id = value.pending_save_id if value else None
                            t.pending_command_id = value.command_id if value else None
                            raise CollectionFailure("COMMIT_UNCONFIRMED", "Source save must be resolved before switching.", unknown=True)
                        if result.status == ResultStatus.CANCELLED:
                            t.finished = True
                            await self._cleanup(t, signal)
                            return self._outcome(t, cancelled=True)
                        if result.status != ResultStatus.OK:
                            raise CollectionFailure(result.error.code if result.error else "IO_FAILURE", result.error.reason if result.error else "Source save did not complete.")
                        if (value is None or value.state != SaveState.COMMITTED
                                or value.captured_edit_version != choice.observed_edit_version
                                or value.saved_revision is None or value.collection_revision is None):
                            t.uncertain = True
                            raise CollectionFailure("COMMIT_UNCONFIRMED", "Source save did not supply usable commit evidence.", unknown=True)
                        t.saved.append(identity)
                        expected[identity] = replace(expected[identity], is_dirty=False)
                expected_states = tuple(sorted(expected.values(), key=lambda b: b.buffer_id))
                if self._buffers() != expected_states:
                    raise CollectionFailure("REVISION_CONFLICT", "Drafts changed; obtain new leave decisions.")
                self._context(context)
                if signal.is_set() or t.cancel.is_set():
                    t.finished = True
                    await self._cleanup(t, signal)
                    return self._outcome(t, cancelled=True)
                await self._io(self.backend.validate, signal, t.target, False)
                if t.target.mode == ActivationMode.CREATE:
                    try:
                        publication = await self._io(self.backend.publish, signal, t.target)
                    except CollectionFailure as error:
                        if error.unknown:
                            t.stage, t.uncertain = TargetStage.UNKNOWN, True
                        raise
                    if not isinstance(publication, PublishOutcome) or publication.stage != TargetStage.PUBLISHED_NEW:
                        t.stage, t.uncertain = TargetStage.UNKNOWN, True
                        raise CollectionFailure("IO_FAILURE", "Target publication is unconfirmed.", unknown=True)
                    t.stage, t.created = TargetStage.PUBLISHED_NEW, True
                    t.warnings.extend(AppError("IO_FAILURE", w, "Inspect temporary leftovers.") for w in publication.warnings)
                if signal.is_set() or t.cancel.is_set():
                    t.finished = True
                    return self._outcome(t, cancelled=True)
                try:
                    t.configuration_updated = await self._io(self.registry.register, signal, t.target.collection_id, t.target.directory)
                    if type(t.configuration_updated) is not bool:
                        t.configuration_updated, t.uncertain = None, True
                        raise CollectionFailure("IO_FAILURE", "Identity registration did not supply a confirmed outcome.", unknown=True)
                except CollectionFailure as error:
                    if error.unknown:
                        t.configuration_updated, t.uncertain = None, True
                    raise
                await self._io(self.backend.validate, signal, t.target, t.created)
                if signal.is_set() or t.cancel.is_set():
                    t.finished = True
                    return self._outcome(t, cancelled=True)
                if self._buffers() != expected_states:
                    raise CollectionFailure("REVISION_CONFLICT", "Drafts changed before activation; original edits were retained.")
                discard = tuple(identity for identity, c in choices.items() if c.choice == LeaveChoice.DISCARD)
                try:
                    activated = await self._settle(self.session.activate(context, t.target, expected_states, discard, (t.cancel, signal)), signal)
                except (CollectionFailure, OSError, ValueError) as error:
                    actual = self.session.context
                    if actual == CallContext(t.target.collection_id, context.context_generation + 1):
                        t.activated, t.stage, t.finished = actual, TargetStage.ACTIVATED, True
                        failure = error if isinstance(error, CollectionFailure) else CollectionFailure("IO_FAILURE", "Collection activated but resource cleanup failed.")
                        return self._outcome(t, failure)
                    raise
                if activated is None:
                    if signal.is_set() or t.cancel.is_set():
                        t.finished = True
                        return self._outcome(t, cancelled=True)
                    raise CollectionFailure("REVISION_CONFLICT", "Activation was rejected; original edits were retained.")
                if activated != CallContext(t.target.collection_id, context.context_generation + 1):
                    t.uncertain = True
                    raise CollectionFailure("IO_FAILURE", "Activation context could not be confirmed.", unknown=True)
                t.activated, t.stage, t.finished = activated, TargetStage.ACTIVATED, True
                return self._outcome(t)
            except CollectionFailure as error:
                if t is None:
                    return self._result(context, ResultStatus.ERROR, failure=error)
                # Argument errors allow re-prompting on the same ticket. Once IO
                # begins, a failed attempt is terminal; Cancel can clean its temp.
                if started:
                    t.finished = True
                t.uncertain = t.uncertain or error.unknown
                return self._outcome(t, error)
            except (OSError, ValueError):
                failure = CollectionFailure("IO_FAILURE", "Collection transition failed; inspect its reported stage.")
                if t:
                    t.finished = True
                    return self._outcome(t, failure)
                return self._result(context, ResultStatus.ERROR, failure=failure)

    async def cancel_activation(self, request: CancelActivationRequest, cancel: Event | None = None):
        signal = cancel if cancel is not None else Event()
        context = getattr(request, "context", CallContext(None, 0))
        try:
            if not isinstance(request, CancelActivationRequest):
                raise CollectionFailure("INVALID_ARGUMENT", "Invalid cancellation request.")
            t = self._ticket(request)
            t.cancel.set()  # Record intent BEFORE waiting for Complete's lock.
            async with self._serialized(signal):
                self._context(context)
                await self._cleanup(t, signal)
                t.finished = True
                return self._outcome(t, cancelled=True)
        except CollectionFailure as error:
            return self._result(context, ResultStatus.ERROR, failure=error)
