"""API04 saved-version search / tag filter and API05 saved reference views.

These are C's precise Python objects for the two query interfaces. They read only
the saved version: a draft never reaches this service, and a read result carries
the collection revision it was computed against so a late callback can be
dropped by the caller instead of overwriting newer state.

A whitespace-only query returns ERROR (never an empty success), so the caller
clears the previous result list rather than showing stale hits as "no match".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from threading import Event
from typing import Callable, TypeVar

from pmkb.application.parsing import ReferenceState
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.domain.identity import require_identifier
from pmkb.domain.rules import RuleViolation
from pmkb.infrastructure.storage.connection import StorageFailure
from pmkb.infrastructure.storage.store import CollectionStore

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SearchRequest:
    """API04 input: one query scoped to the active collection."""

    context: CallContext
    query_id: str
    keyword: str
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchHit:
    note_id: str
    name: str
    revision: int


@dataclass(frozen=True, slots=True)
class SearchReply:
    """API04 output; ``total`` is the number of saved matches in this collection."""

    query_id: str
    keyword: str
    tags: tuple[str, ...]
    hits: tuple[SearchHit, ...]
    total: int
    collection_revision: int


@dataclass(frozen=True, slots=True)
class SavedViewRequest:
    """API05 input: a saved note whose references and backlinks are requested."""

    context: CallContext
    request_id: str
    note_id: str


@dataclass(frozen=True, slots=True)
class ReferenceView:
    """One saved occurrence with its classification and display name."""

    target_collection_id: str
    target_note_id: str
    start_utf16: int
    length_utf16: int
    marker_text: str
    state: ReferenceState
    target_name: str | None
    reason: str
    navigable: bool


@dataclass(frozen=True, slots=True)
class BacklinkView:
    note_id: str
    name: str
    revision: int


@dataclass(frozen=True, slots=True)
class SavedNoteView:
    """API05 output: saved note plus references and saved-only backlinks."""

    note_id: str
    revision: int
    name: str
    body: str
    tags: tuple[str, ...]
    references: tuple[ReferenceView, ...]
    backlinks: tuple[BacklinkView, ...]
    collection_revision: int


class SavedQueryService:
    """Query service bound to one active collection store."""

    def __init__(self, store: CollectionStore) -> None:
        self._store = store

    @property
    def store(self) -> CollectionStore:
        return self._store

    def _check_context(self, context: object) -> CallContext:
        if not isinstance(context, CallContext):
            raise RuleViolation("INVALID_ARGUMENT", "A request context is required.")
        if context.collection_id != self._store.collection_id:
            raise StorageFailure(
                "STALE_CONTEXT",
                "The query context no longer refers to the active collection.",
                "Run the query again for the current collection.",
            )
        if type(context.context_generation) is not int or context.context_generation < 0:
            raise RuleViolation("INVALID_ARGUMENT", "The context generation is not a valid version.")
        return context

    def search_saved(self, request: SearchRequest) -> SearchReply:
        """Synchronous core used by the async wrapper and by tests."""
        if not isinstance(request, SearchRequest):
            raise RuleViolation("INVALID_ARGUMENT", "Expected a search request.")
        self._check_context(request.context)
        require_identifier(request.query_id, "Query identity")
        page = self._store.search(keyword=request.keyword, tags=tuple(request.tags))
        return SearchReply(
            request.query_id,
            page.keyword,
            page.tags,
            tuple(SearchHit(item.note_id, item.name, item.revision) for item in page.items),
            len(page.items),
            page.collection_revision,
        )

    async def search(self, request: SearchRequest, cancel: Event | None = None) -> OperationResult[SearchReply]:
        context = getattr(request, "context", None)
        signal = cancel or Event()
        if not isinstance(context, CallContext):
            return _error(CallContext(None, 0), "INVALID_ARGUMENT", "A request context is required.")
        if signal.is_set():
            return _result(context, ResultStatus.CANCELLED)
        try:
            reply = await _run_io(lambda: self.search_saved(request), signal)
        except RuleViolation as error:
            return _error(context, error.code, error.reason, error.action)
        except StorageFailure as error:
            return _error(context, error.code, error.reason, error.action)
        except asyncio.CancelledError:
            return _result(context, ResultStatus.CANCELLED)
        if signal.is_set():
            return _result(context, ResultStatus.CANCELLED)
        return _result(context, ResultStatus.OK, reply)

    def saved_view(self, request: SavedViewRequest) -> SavedNoteView:
        """Synchronous core: one consistent saved snapshot for the whole view.

        Note body, tags, reference classification, backlinks and the collection
        revision come from a single read transaction (C-REV-02), so the response
        can never combine an older body with newer relations or a newer
        collection revision.
        """
        if not isinstance(request, SavedViewRequest):
            raise RuleViolation("INVALID_ARGUMENT", "Expected a saved view request.")
        self._check_context(request.context)
        require_identifier(request.request_id, "Request identity")
        require_identifier(request.note_id, "Note identity")
        data = self._store.saved_view(request.note_id)
        if data is None:
            raise RuleViolation("NOTE_NOT_FOUND", "No saved note has this identity.")
        return SavedNoteView(
            data.note_id,
            data.revision,
            data.name,
            data.body,
            data.tags,
            tuple(
                ReferenceView(
                    item.occurrence.target_collection_id,
                    item.occurrence.target_note_id,
                    item.occurrence.start_utf16,
                    item.occurrence.length_utf16,
                    item.occurrence.marker_text,
                    item.state,
                    item.target_name,
                    item.reason,
                    item.navigable,
                )
                for item in data.references
            ),
            tuple(BacklinkView(item.note_id, item.name, item.revision)
                  for item in data.backlinks),
            data.collection_revision,
        )

    async def load_saved_view(self, request: SavedViewRequest,
                              cancel: Event | None = None) -> OperationResult[SavedNoteView]:
        context = getattr(request, "context", None)
        signal = cancel or Event()
        if not isinstance(context, CallContext):
            return _error(CallContext(None, 0), "INVALID_ARGUMENT", "A request context is required.")
        if signal.is_set():
            return _result(context, ResultStatus.CANCELLED)
        try:
            view = await _run_io(lambda: self.saved_view(request), signal)
        except RuleViolation as error:
            return _error(context, error.code, error.reason, error.action)
        except StorageFailure as error:
            return _error(context, error.code, error.reason, error.action)
        except asyncio.CancelledError:
            return _result(context, ResultStatus.CANCELLED)
        if signal.is_set():
            return _result(context, ResultStatus.CANCELLED)
        return _result(context, ResultStatus.OK, view)


# ---------------------------------------------------------------------------
# small local result helpers (kept private so C does not depend on another
# owner's module; a shared helper can be extracted once A owns it)


async def _run_io(call: Callable[[], T], signal: Event) -> T:
    """Run blocking storage work off the service loop; record cancellation intent."""
    work = asyncio.ensure_future(asyncio.to_thread(call))
    while True:
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            signal.set()
            if work.cancelled():
                raise


def _result(context: CallContext, status: ResultStatus, value: T | None = None,
            error: AppError | None = None) -> OperationResult[T]:
    return OperationResult(status, context, context, value, error)


def _error(context: CallContext, code: str, reason: str,
           action: str = "Correct the input and retry.") -> OperationResult:
    return OperationResult(ResultStatus.ERROR, context, context, None, AppError(code, reason, action))
