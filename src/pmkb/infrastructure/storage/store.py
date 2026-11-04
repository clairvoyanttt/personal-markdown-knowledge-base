"""One saved collection as a service-facing object (C's API10 surface).

This is the only entry point application services use. It never returns a
``sqlite3.Connection``, keeps writes serialized with one lock, and opens each
connection inside the calling storage thread. B's editor adapter and C's query
service both go through this object, so there is one connection/transaction
policy rather than two drifting copies.
"""
from __future__ import annotations

from pathlib import Path
import threading

from pmkb.application.parsing import ParsedDocument, ResolvedReference
from pmkb.infrastructure.storage import reader, writing
from pmkb.infrastructure.storage.reader import (
    BacklinkSource,
    CollectionSnapshot,
    NoteSummary,
    SavedNoteData,
    SavedViewData,
    SearchPage,
)
from pmkb.infrastructure.storage.writing import CommitResult

__all__ = ["CollectionStore"]


class CollectionStore:
    """Serialized writer plus consistent readers for exactly one collection."""

    def __init__(self, database: str | Path, collection_id: str) -> None:
        self._database = str(Path(database))
        self._collection_id = collection_id
        self._write_lock = threading.Lock()

    @property
    def database(self) -> str:
        return self._database

    @property
    def collection_id(self) -> str:
        return self._collection_id

    # ------------------------------------------------------------------ writes

    def save(
        self,
        *,
        command_id: str,
        operation_id: str,
        note_id: str,
        name: str,
        body: str,
        tags: tuple[str, ...],
        base_revision: int | None,
    ) -> CommitResult:
        with self._write_lock:
            return writing.save_note(
                self._database,
                collection_id=self._collection_id,
                command_id=command_id,
                operation_id=operation_id,
                note_id=note_id,
                name=name,
                body=body,
                tags=tags,
                base_revision=base_revision,
            )

    def delete(
        self,
        *,
        command_id: str,
        operation_id: str,
        note_id: str,
        expected_revision: int,
    ) -> CommitResult:
        with self._write_lock:
            return writing.delete_note(
                self._database,
                collection_id=self._collection_id,
                command_id=command_id,
                operation_id=operation_id,
                note_id=note_id,
                expected_revision=expected_revision,
            )

    def check_save(
        self,
        *,
        command_id: str,
        note_id: str,
        name: str,
        body: str,
        tags: tuple[str, ...],
        base_revision: int | None,
    ) -> CommitResult:
        return writing.check_save(
            self._database,
            collection_id=self._collection_id,
            command_id=command_id,
            note_id=note_id,
            name=name,
            body=body,
            tags=tags,
            base_revision=base_revision,
        )

    # ------------------------------------------------------------------ reads

    def load_note(self, note_id: str) -> SavedNoteData | None:
        return reader.load_note(self._database, self._collection_id, note_id)

    def summaries(self, *, tags: tuple[str, ...] = ()) -> tuple[NoteSummary, ...]:
        """Saved notes (and their saved tags), optionally filtered by tags (AND)."""
        return reader.list_summaries(self._database, self._collection_id, tags=tags)

    def search(self, *, keyword: str, tags: tuple[str, ...] = ()) -> SearchPage:
        return reader.search_notes(self._database, self._collection_id, keyword=keyword, tags=tags)

    def backlinks(self, note_id: str) -> tuple[BacklinkSource, ...]:
        return reader.list_backlinks(self._database, self._collection_id, note_id)

    def saved_view(self, note_id: str) -> SavedViewData | None:
        """Saved note + references + backlinks from one consistent snapshot."""
        return reader.saved_view(self._database, self._collection_id, note_id)

    def classify(self, document: ParsedDocument) -> tuple[ResolvedReference, ...]:
        return reader.classify_references(self._database, self._collection_id, document)

    def snapshot(self) -> CollectionSnapshot:
        return reader.capture_snapshot(self._database)

    def metadata(self):
        """Current metadata (identity, schema version, collection revision, state)."""
        return reader.metadata(self._database)
