from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Event
from typing import Protocol
from .common import CallContext, OperationResult

class UnsavedBackupChoice(StrEnum):
    NO_UNSAVED = "NO_UNSAVED"
    RETURN_TO_SAVE = "RETURN_TO_SAVE"
    CONTINUE_SAVED_ONLY = "CONTINUE_SAVED_ONLY"

class FilePublicationState(StrEnum):
    PUBLISHED = "PUBLISHED"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class BackupRequest:
    context: CallContext
    destination_file: str
    unsaved_choice: UnsavedBackupChoice

@dataclass(frozen=True, slots=True)
class BackupReply:
    destination_file: str
    source_collection_id: str
    source_location: str
    source_collection_revision: int
    saved_note_count: int
    backup_captured_at_utc: datetime
    publication_state: FilePublicationState

class BackupService(Protocol):
    async def backup_collection(self, request: BackupRequest,
                                cancel: Event | None = None) -> OperationResult[BackupReply]: ...
