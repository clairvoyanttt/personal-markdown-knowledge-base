from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Protocol
from .common import CallContext, OperationResult

@dataclass(frozen=True, slots=True)
class CreateDraftRequest:
    context: CallContext
    initial_name: str

@dataclass(frozen=True, slots=True)
class LoadNoteRequest:
    context: CallContext
    note_id: str

@dataclass(frozen=True, slots=True)
class ApplyDraftEditRequest:
    context: CallContext
    buffer_id: str
    expected_edit_version: int
    name: str
    body: str
    tags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class DraftSnapshot:
    buffer_id: str
    note_id: str | None
    base_revision: int | None
    edit_version: int
    name: str
    body: str
    tags: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class SaveNoteRequest:
    context: CallContext
    command_id: str
    snapshot: DraftSnapshot

@dataclass(frozen=True, slots=True)
class DraftView:
    snapshot: DraftSnapshot
    is_dirty: bool

class SaveState(StrEnum):
    COMMITTED = "COMMITTED"
    PENDING = "PENDING"

@dataclass(frozen=True, slots=True)
class SaveReply:
    command_id: str
    note_id: str
    captured_edit_version: int
    state: SaveState
    saved_revision: int | None
    collection_revision: int | None
    pending_save_id: str | None

class NoteService(Protocol):
    async def create_draft(self, request: CreateDraftRequest,
                           cancel: Event | None = None) -> OperationResult[DraftView]: ...
    async def load_note(self, request: LoadNoteRequest,
                        cancel: Event | None = None) -> OperationResult[DraftView]: ...
    async def apply_draft_edit(self, request: ApplyDraftEditRequest,
                               cancel: Event | None = None) -> OperationResult[DraftView]: ...
    async def save_note(self, request: SaveNoteRequest,
                        cancel: Event | None = None) -> OperationResult[SaveReply]: ...
