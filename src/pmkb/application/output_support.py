"""D-internal output orchestration rules, pending A/B/C port agreement.

The UI adapter must capture state on its owning thread and bind acknowledged
dirty-buffer versions to the user's current output decision and context.
These types do not inspect widgets, freeze SQLite, approve draft loss or save.
OperationResult delivery remains at the originating context: late output is
real, but must not be displayed as work performed for a newly active collection.
"""
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
import re
from threading import Event
from typing import Protocol, TypeVar

from pmkb.contracts.backup import UnsavedBackupChoice
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus


_MAX = 2 ** 63 - 1
_ID = re.compile(r"[0-9a-f]{32}")
T = TypeVar("T")


class OutputFailure(Exception):
    """Typed D port failure; unknown is not proof of rollback or nonpublication."""

    def __init__(self, code: str, reason: str,
                 action: str = "Review the operation and choose whether to try again.",
                 *, unknown: bool = False):
        super().__init__(reason)
        self.error = AppError(code, reason, action)
        self.unknown = unknown


@dataclass(frozen=True, slots=True)
class OutputBufferState:
    buffer_id: str
    edit_version: int
    is_dirty: bool
    note_id: str | None = None


@dataclass(frozen=True, slots=True)
class OutputSessionState:
    context: CallContext
    buffers: tuple[OutputBufferState, ...]
    has_unconfirmed_save: bool = False
    acknowledged_dirty_versions: tuple[tuple[str, int], ...] = ()


class OutputSession(Protocol):
    async def inspect(self) -> OutputSessionState:
        """Capture on B's owning thread, including context-bound decision evidence.

        Acknowledgements are for this operation's user decision, not a global
        sticky 'ignore unsaved' preference. Include new unsaved notes as well.
        """
        ...


def _version(value: object) -> bool:
    return type(value) is int and 0 <= value <= _MAX


def _key(value: object) -> bool:
    return (isinstance(value, str) and bool(value)
            and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in value))


def validate_context(context: CallContext, require_active: bool = True) -> None:
    if (not isinstance(context, CallContext) or not _version(context.context_generation)
            or (context.collection_id is not None
                and (not isinstance(context.collection_id, str)
                     or _ID.fullmatch(context.collection_id) is None))):
        raise OutputFailure("INVALID_ARGUMENT", "Invalid output call context.")
    if require_active and context.collection_id is None:
        raise OutputFailure("STATE_BLOCKED", "Create or open a collection before output.")


def validate_state(state: OutputSessionState, context: CallContext,
                   require_active: bool = True) -> None:
    validate_context(context, require_active)
    if not isinstance(state, OutputSessionState):
        raise OutputFailure("STATE_BLOCKED", "The session did not supply a typed output state.")
    validate_context(state.context, require_active)
    if state.context != context:
        raise OutputFailure("STALE_CONTEXT", "The active collection changed; start a new operation.")
    if type(state.has_unconfirmed_save) is not bool:
        raise OutputFailure("STATE_BLOCKED", "The session supplied an invalid save state.")
    if state.has_unconfirmed_save:
        raise OutputFailure("STATE_BLOCKED", "Resolve the pending save before output.")
    if type(state.buffers) is not tuple:
        raise OutputFailure("STATE_BLOCKED", "Buffer state must be an immutable captured tuple.")
    seen_buffers, seen_notes = set(), set()
    for buffer in state.buffers:
        if (not isinstance(buffer, OutputBufferState) or not _key(buffer.buffer_id)
                or not _version(buffer.edit_version) or type(buffer.is_dirty) is not bool
                or (buffer.note_id is not None
                    and (not isinstance(buffer.note_id, str) or _ID.fullmatch(buffer.note_id) is None))
                or buffer.buffer_id in seen_buffers
                or (buffer.note_id is not None and buffer.note_id in seen_notes)):
            raise OutputFailure("STATE_BLOCKED", "Invalid or duplicate captured buffer state.")
        seen_buffers.add(buffer.buffer_id)
        if buffer.note_id is not None:
            seen_notes.add(buffer.note_id)
    if state.context.collection_id is None and state.buffers:
        raise OutputFailure("STATE_BLOCKED", "Buffers cannot belong to an unopened collection.")
    acknowledgements = state.acknowledged_dirty_versions
    if type(acknowledgements) is not tuple:
        raise OutputFailure("STATE_BLOCKED", "Dirty acknowledgements must be an immutable tuple.")
    seen_ack = set()
    for pair in acknowledgements:
        if (type(pair) is not tuple or len(pair) != 2 or not _key(pair[0])
                or not _version(pair[1]) or pair[0] in seen_ack):
            raise OutputFailure("STATE_BLOCKED", "Invalid or duplicate dirty-version acknowledgement.")
        seen_ack.add(pair[0])


def dirty_versions(state: OutputSessionState, note_id: str | None = None) -> tuple[tuple[str, int], ...]:
    """None means all buffers for backup; export passes the selected saved ID."""
    return tuple(sorted((buffer.buffer_id, buffer.edit_version) for buffer in state.buffers
                        if buffer.is_dirty and (note_id is None or buffer.note_id == note_id)))


def validate_choice(state: OutputSessionState, choice: UnsavedBackupChoice,
                    note_id: str | None = None) -> None:
    if not isinstance(choice, UnsavedBackupChoice):
        raise OutputFailure("INVALID_ARGUMENT", "Choose how to handle unsaved edits.")
    dirty = dirty_versions(state, note_id)
    if choice is UnsavedBackupChoice.NO_UNSAVED and dirty:
        raise OutputFailure("STATE_BLOCKED", "Unsaved edits exist; a current output decision is required.")
    if choice is UnsavedBackupChoice.CONTINUE_SAVED_ONLY:
        acknowledged = dict(state.acknowledged_dirty_versions)
        if any(acknowledged.get(key) != version for key, version in dirty):
            raise OutputFailure("STATE_BLOCKED", "New or changed drafts need a fresh saved-only decision.")


def recheck_state(before: OutputSessionState, after: OutputSessionState,
                  choice: UnsavedBackupChoice, note_id: str | None = None) -> None:
    validate_state(after, before.context)
    validate_choice(after, choice, note_id)
    if dirty_versions(before, note_id) != dirty_versions(after, note_id):
        raise OutputFailure("STATE_BLOCKED", "Drafts changed during capture; review output again.")


def absolute_path(value: object) -> Path:
    """Lexical only. Actual existence/permissions are checked by the file adapter."""
    if (not isinstance(value, str) or not value
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
            or any(part in {".", ".."} for part in value.replace("\\", "/").split("/"))):
        raise OutputFailure("INVALID_ARGUMENT", "Choose a complete absolute local output path.")
    path = Path(value)
    if not path.is_absolute():
        raise OutputFailure("INVALID_ARGUMENT", "Output paths must be absolute.")
    return path


async def settle(awaitable: Awaitable[T], signal: Event) -> T:
    """Record outer cancellation as intent, but await the actual child outcome."""
    work = asyncio.ensure_future(awaitable)
    while True:
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            signal.set()
            if work.cancelled():
                raise OutputFailure("IO_FAILURE", "The underlying operation lost its outcome.",
                                    "Inspect the operation; do not infer rollback or retry writes.", unknown=True)


async def run_io(call: Callable[[], T], signal: Event) -> T:
    return await settle(asyncio.to_thread(call), signal)


def file_error(error: AppError | None) -> AppError:
    """Map internal file errors without exposing diagnostics as stable API codes."""
    if isinstance(error, AppError) and error.code == "TARGET_EXISTS":
        return AppError("TARGET_EXISTS", "The selected output already exists.", "Choose a new target; existing files are never overwritten.")
    if isinstance(error, AppError) and error.code in {"INVALID_NAME", "INVALID_ARGUMENT"}:
        return AppError(error.code, "The selected output path is invalid.", "Choose a valid absolute output location.")
    return AppError("IO_FAILURE", "The output operation could not be confirmed as complete.",
                    "Inspect the reported location; do not automatically replay an unknown publication.")


def result(context: CallContext, status: ResultStatus, value=None,
           error: AppError | None = None, warnings: tuple[AppError, ...] = ()) -> OperationResult:
    if not isinstance(status, ResultStatus):
        raise ValueError("Expected a result status.")
    if status is ResultStatus.OK and (value is None or error is not None):
        raise ValueError("OK requires a value and no error.")
    if status in {ResultStatus.ERROR, ResultStatus.CANCELLED} and value is not None:
        raise ValueError("ERROR/CANCELLED cannot carry successful output fields.")
    if status is ResultStatus.ERROR and not isinstance(error, AppError):
        raise ValueError("ERROR requires an error.")
    if status is ResultStatus.UNCONFIRMED and (value is None or not isinstance(error, AppError)):
        raise ValueError("UNCONFIRMED requires a value describing uncertainty and an error.")
    if type(warnings) is not tuple or any(not isinstance(item, AppError) for item in warnings):
        raise ValueError("Warnings must be captured AppErrors.")
    return OperationResult(status, context, context, value, error, warnings)
