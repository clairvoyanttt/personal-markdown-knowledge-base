"""B's proposed internal C adapter seam, not an approved API10 replacement.

C owns transactions, validation, parsing and matching SAVE evidence. A return of
NOT_COMMITTED requires positive non-commit/rollback evidence, never just an absent
receipt. COMMITTED certifies the exact attempt (including canonical input rules)
and its matching SAVE receipt/content/lastCommitId. Restore/delete receipts do
not qualify. Provider/caller integration review is still required.
"""
from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Protocol

from pmkb.contracts.common import AppError, CallContext
from pmkb.contracts.notes import DraftSnapshot


@dataclass(frozen=True, slots=True)
class SavedNote:
    note_id: str
    revision: int
    name: str
    body: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SaveAttempt:
    context: CallContext
    command_id: str
    pending_save_id: str
    note_id: str
    snapshot: DraftSnapshot


class SaveEvidenceState(StrEnum):
    COMMITTED = 'COMMITTED'
    NOT_COMMITTED = 'NOT_COMMITTED'
    UNRESOLVED = 'UNRESOLVED'


@dataclass(frozen=True, slots=True)
class SaveEvidence:
    context: CallContext
    command_id: str
    note_id: str
    snapshot: DraftSnapshot
    state: SaveEvidenceState
    saved_revision: int | None = None
    collection_revision: int | None = None
    error: AppError | None = None


class EditingStorage(Protocol):
    async def load_note(self, context: CallContext, note_id: str,
                        cancel: Event) -> SavedNote | None: ...
    async def save(self, attempt: SaveAttempt, cancel: Event) -> SaveEvidence: ...
    async def check_save(self, attempt: SaveAttempt, cancel: Event) -> SaveEvidence: ...
