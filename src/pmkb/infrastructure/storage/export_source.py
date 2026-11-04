"""D's ``ExportSourcePort`` implemented on C's single-transaction saved view.

D's export renderer only replaces authorized spans; it must not search markers
itself. This adapter therefore supplies one consistent saved projection: body,
classification, target names, note revision and collection revision all come from
``CollectionStore.saved_view()`` (one read transaction), never from a draft or
from several independent reads.
"""
from __future__ import annotations

import asyncio
from threading import Event

from pmkb.application.export.rendering import ExportReference, ReferenceKind
from pmkb.application.export.service import SavedExportNote
from pmkb.contracts.common import CallContext
from pmkb.infrastructure.storage.connection import StorageFailure
from pmkb.infrastructure.storage.store import CollectionStore


class CExportSource:
    """Saved-version export source for one collection."""

    def __init__(self, store: CollectionStore) -> None:
        self._store = store

    @property
    def store(self) -> CollectionStore:
        return self._store

    async def read_saved(self, context: CallContext, note_id: str,
                         cancel: Event) -> SavedExportNote | None:
        """Read the saved note and its classified references from one snapshot.

        ``None`` means there is no saved version (CB01). A read never promotes a
        draft or writes anything; stale context is rejected instead of exporting
        data from another collection.
        """
        if not isinstance(context, CallContext):
            raise StorageFailure("INVALID_ARGUMENT", "A call context is required.")
        if context.collection_id != self._store.collection_id:
            raise StorageFailure(
                "STALE_CONTEXT", "The export source belongs to another collection.",
                "Export from the collection that is actually open.",
            )
        if not isinstance(note_id, str):
            raise StorageFailure("INVALID_ARGUMENT", "A saved note identity is required.")
        if cancel.is_set():
            # D decides the returned status; a read has no persistent effect.
            raise asyncio.CancelledError()
        view = await asyncio.to_thread(self._store.saved_view, note_id)
        if cancel.is_set():
            raise asyncio.CancelledError()
        if view is None:
            return None
        references = tuple(
            ExportReference(
                start_utf16=item.occurrence.start_utf16,
                length_utf16=item.occurrence.length_utf16,
                marker_text=item.occurrence.marker_text,
                kind=ReferenceKind(item.state.value),
                target_name=item.target_name,
            )
            for item in view.references
        )
        return SavedExportNote(
            context=context,
            note_id=view.note_id,
            saved_revision=view.revision,
            collection_revision=view.collection_revision,
            saved_name=view.name,
            saved_body=view.body,
            references=references,
        )
