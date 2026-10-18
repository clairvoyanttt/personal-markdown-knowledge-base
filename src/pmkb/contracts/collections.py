from dataclasses import dataclass
from enum import StrEnum
from threading import Event
from typing import Protocol
from .common import CallContext, OperationResult

class ActivationMode(StrEnum):
    CREATE = "CREATE"
    OPEN = "OPEN"

class LeaveChoice(StrEnum):
    SAVE = "SAVE"
    DISCARD = "DISCARD"
    CANCEL = "CANCEL"

class TargetStage(StrEnum):
    TEMP_VALIDATED = "TEMP_VALIDATED"
    EXISTING_READY = "EXISTING_READY"
    PUBLISHED_NEW = "PUBLISHED_NEW"
    ACTIVATED = "ACTIVATED"
    UNKNOWN = "UNKNOWN"

@dataclass(frozen=True, slots=True)
class PrepareActivationRequest:
    context: CallContext
    mode: ActivationMode
    target_directory: str

@dataclass(frozen=True, slots=True)
class DraftLeaveDecision:
    buffer_id: str
    observed_edit_version: int
    choice: LeaveChoice

@dataclass(frozen=True, slots=True)
class CompleteActivationRequest:
    context: CallContext
    ticket_id: str
    decisions: tuple[DraftLeaveDecision, ...]

@dataclass(frozen=True, slots=True)
class CancelActivationRequest:
    context: CallContext
    ticket_id: str

@dataclass(frozen=True, slots=True)
class ActivationTicketView:
    ticket_id: str
    mode: ActivationMode
    target_directory: str
    target_collection_id: str
    stage: TargetStage

@dataclass(frozen=True, slots=True)
class ActivationReply:
    ticket_id: str
    stage: TargetStage
    created_by_this_operation: bool
    formal_target_directory: str | None
    target_collection_id: str | None
    activated_generation: int | None
    configuration_updated: bool | None
    saved_buffer_ids: tuple[str, ...]
    pending_save_id: str | None
    pending_command_id: str | None

class CollectionCoordinator(Protocol):
    async def prepare_activation(self, request: PrepareActivationRequest,
                                 cancel: Event | None = None) -> OperationResult[ActivationTicketView]: ...
    async def complete_activation(self, request: CompleteActivationRequest,
                                  cancel: Event | None = None) -> OperationResult[ActivationReply]: ...
    async def cancel_activation(self, request: CancelActivationRequest,
                                cancel: Event | None = None) -> OperationResult[ActivationReply]: ...
