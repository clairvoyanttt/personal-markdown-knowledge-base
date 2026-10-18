"""Narrow integration ports: A orchestrates, B owns drafts/UI, C/D own IO."""
from dataclasses import dataclass
from threading import Event
from typing import Protocol
from pmkb.contracts.collections import ActivationMode, TargetStage
from pmkb.contracts.common import CallContext, OperationResult
from pmkb.contracts.notes import SaveReply

class CollectionFailure(Exception):
    def __init__(self, code: str, reason: str, action: str = "Check the location and retry.", *, unknown: bool = False):
        super().__init__(reason)
        self.code, self.reason, self.action, self.unknown = code, reason, action, unknown

@dataclass(frozen=True, slots=True)
class PreparedCollection:
    directory: str
    collection_id: str
    mode: ActivationMode
    temporary_file: str | None = None

@dataclass(frozen=True, slots=True)
class PublishOutcome:
    stage: TargetStage
    warnings: tuple[str, ...] = ()

@dataclass(frozen=True, slots=True)
class BufferState:
    buffer_id: str
    edit_version: int
    is_dirty: bool

class CollectionBackend(Protocol):
    # Synchronous IO methods run via the coordinator's worker boundary.
    def prepare(self, mode: ActivationMode, directory: str) -> PreparedCollection: ...
    def validate(self, target: PreparedCollection, published: bool) -> None: ...
    def publish(self, target: PreparedCollection) -> PublishOutcome: ...
    def cleanup(self, target: PreparedCollection) -> tuple[str, ...]: ...

class IdentityRegistry(Protocol):
    def check(self, collection_id: str, directory: str) -> None: ...
    def register(self, collection_id: str, directory: str) -> bool: ...

class CollectionSession(Protocol):
    @property
    def context(self) -> CallContext: ...
    @property
    def has_unconfirmed_save(self) -> bool: ...
    def buffers(self) -> tuple[BufferState, ...]: ...
    async def save_buffer(self, buffer_id: str, edit_version: int,
                          cancel: Event | None) -> OperationResult[SaveReply]: ...
    async def activate(self, expected: CallContext, target: PreparedCollection,
                       expected_buffers: tuple[BufferState, ...],
                       discard_ids: tuple[str, ...], cancel_signals: tuple[Event, ...] = ()) -> CallContext | None: ...
    # B must check cancel signals/context/ALL buffer versions, install and discard atomically
    # on the Qt thread. None means no activation and no discarded drafts.
