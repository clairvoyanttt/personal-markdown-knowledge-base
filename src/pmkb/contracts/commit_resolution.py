from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Mapping, Protocol
from .common import CallContext, OperationResult

class CommitCheckState(StrEnum):
    COMMITTED = "COMMITTED"
    NOT_COMMITTED = "NOT_COMMITTED"
    UNRESOLVED = "UNRESOLVED"

@dataclass(frozen=True, slots=True)
class CheckCommitRequest:
    context: CallContext
    pending_save_id: str
    command_id: str

@dataclass(frozen=True, slots=True)
class CommitCheckReply:
    command_id: str
    note_id: str
    captured_edit_version: int
    state: CommitCheckState
    saved_revision: int | None
    collection_revision: int | None

@dataclass(frozen=True, slots=True)
class CloseUnconfirmedRequest:
    context: CallContext
    pending_save_id: str
    command_id: str
    acknowledge_commit_unknown: bool
    discard_session_edits: bool
    confirmed_draft_edit_versions: Mapping[str, int]

@dataclass(frozen=True, slots=True)
class CloseUnconfirmedReply:
    ready_to_close: bool
    commit_still_unknown: bool
    buffers_to_discard_on_exit: tuple[str, ...]

class CommitResolver(Protocol):
    async def check_commit(self, request: CheckCommitRequest,
                           cancel: Event | None = None) -> OperationResult[CommitCheckReply]: ...
    async def close_unconfirmed_session(self, request: CloseUnconfirmedRequest,
                                        cancel: Event | None = None) -> OperationResult[CloseUnconfirmedReply]: ...
