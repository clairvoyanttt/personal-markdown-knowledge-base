"""B's ``EditingStorage`` port implemented on C's saved-version storage.

B owns the draft; this adapter only loads saved notes and runs the one write C
is allowed to make. All storage calls are submitted to a single-worker executor,
so the SQLite connection is always created, used and closed on the same thread
and two saves can never run concurrently. The adapter never re-raises a storage
error as success: an unconfirmed commit becomes ``UNRESOLVED``, which keeps B's
pending-save protection in place.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from pmkb.application.editing.ports import (
    SavedNote,
    SaveAttempt,
    SaveEvidence,
    SaveEvidenceState,
)
from pmkb.contracts.common import AppError
from pmkb.infrastructure.storage.store import CollectionStore
from pmkb.infrastructure.storage.writing import CommitResult, CommitState


class SqliteEditingStorage:
    """Serialized saved-version adapter for B's editor service."""

    def __init__(self, store: CollectionStore) -> None:
        self._store = store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pmkb-storage")
        self._closed = False

    @property
    def store(self) -> CollectionStore:
        return self._store

    def close(self) -> None:
        """Stop the storage thread after in-flight work; safe to call twice."""
        if not self._closed:
            self._closed = True
            self._executor.shutdown(wait=True)

    def __enter__(self) -> "SqliteEditingStorage":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    async def _submit(self, call):
        if self._closed:
            raise RuntimeError("The storage adapter is closed.")
        future = self._executor.submit(call)
        return await asyncio.wrap_future(future)

    async def load_note(self, context, note_id: str, cancel: Event) -> SavedNote | None:
        if context.collection_id != self._store.collection_id:
            raise _stale_context()
        data = await self._submit(lambda: self._store.load_note(note_id))
        if data is None:
            return None
        return SavedNote(data.note_id, data.revision, data.name, data.body, data.tags)

    async def save(self, attempt: SaveAttempt, cancel: Event) -> SaveEvidence:
        if not isinstance(attempt, SaveAttempt):
            raise ValueError("Expected a save attempt.")
        if attempt.context.collection_id != self._store.collection_id:
            return self._evidence(attempt, CommitResult(
                CommitState.NOT_COMMITTED, None, None, _error(
                    "STALE_CONTEXT",
                    "The save context no longer refers to the active collection.",
                    "Reopen the collection before saving.",
                )))
        if cancel.is_set():
            return self._evidence(attempt, CommitResult(
                CommitState.NOT_COMMITTED, None, None, _error(
                    "CANCELLED",
                    "The save was cancelled before it reached storage; nothing was written.",
                    "Save again when ready.",
                )))
        snapshot = attempt.snapshot
        result = await self._submit(lambda: self._store.save(
            command_id=attempt.command_id,
            operation_id=attempt.pending_save_id,
            note_id=attempt.note_id,
            name=snapshot.name,
            body=snapshot.body,
            tags=tuple(snapshot.tags),
            base_revision=snapshot.base_revision,
        ))
        return self._evidence(attempt, result)

    async def check_save(self, attempt: SaveAttempt, cancel: Event) -> SaveEvidence:
        if not isinstance(attempt, SaveAttempt):
            raise ValueError("Expected a save attempt.")
        if attempt.context.collection_id != self._store.collection_id:
            return self._evidence(attempt, CommitResult(
                CommitState.NOT_COMMITTED, None, None, _error(
                    "STALE_CONTEXT",
                    "The save context no longer refers to the active collection.",
                    "Reopen the collection before verifying.",
                )))
        snapshot = attempt.snapshot
        result = await self._submit(lambda: self._store.check_save(
            command_id=attempt.command_id,
            note_id=attempt.note_id,
            name=snapshot.name,
            body=snapshot.body,
            tags=tuple(snapshot.tags),
            base_revision=snapshot.base_revision,
        ))
        return self._evidence(attempt, result)

    def _evidence(self, attempt: SaveAttempt, result: CommitResult) -> SaveEvidence:
        state = SaveEvidenceState(result.state.value)
        return SaveEvidence(
            attempt.context,
            attempt.command_id,
            attempt.note_id,
            attempt.snapshot,
            state,
            result.saved_revision if state is SaveEvidenceState.COMMITTED else None,
            result.collection_revision if state is SaveEvidenceState.COMMITTED else None,
            result.error if state is not SaveEvidenceState.COMMITTED else None,
        )


def _error(code: str, reason: str, action: str) -> AppError:
    return AppError(code, reason, action)


def _stale_context() -> RuntimeError:
    return RuntimeError("The storage adapter belongs to a different collection.")
