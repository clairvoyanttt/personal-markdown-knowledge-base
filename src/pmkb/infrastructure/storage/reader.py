"""Read-only saved-version access: notes, search, backlinks, classification, snapshot.

Every function opens its own read-only connection, runs all reads inside one
explicit read transaction (a consistent view, including the collection revision),
and closes the connection. Draft text never reaches this module: search, filters
and backlinks only ever see rows written by a confirmed save.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator

from pmkb.application.parsing import (
    ParsedDocument,
    ReferenceState,
    ResolvedReference,
    parse_markdown,
)
from pmkb.domain.naming import normalize_tag
from pmkb.domain.rules import RuleViolation, require_text
from pmkb.infrastructure.storage.connection import (
    CollectionMetadata,
    StorageFailure,
    begin,
    connect_reader,
    read_metadata,
    require_ready,
    rollback,
)

REFERENCE_REASONS = {
    ReferenceState.VALID: "目标已保存，可以跳转。",
    ReferenceState.DELETED: "目标已删除，无法跳转。",
    ReferenceState.MISSING: "目标标识不存在或不可用，无法跳转。",
    ReferenceState.FOREIGN_COLLECTION: "引用来自其他集合，无法跳转。",
}


@dataclass(frozen=True, slots=True)
class SavedNoteData:
    """The exact saved projection handed to B's ``SavedNote`` adapter."""

    note_id: str
    revision: int
    name: str
    body: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NoteSummary:
    """One saved-version row for the search/list view.

    ``tags`` are the saved tags (exact case, saved order is not significant) so a
    list or tag filter never has to re-read the note or guess from a draft.
    """

    note_id: str
    name: str
    revision: int
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SearchPage:
    """Saved-version search result for one accepted query."""

    keyword: str
    tags: tuple[str, ...]
    items: tuple[NoteSummary, ...]
    collection_revision: int


@dataclass(frozen=True, slots=True)
class BacklinkSource:
    note_id: str
    name: str
    revision: int


@dataclass(frozen=True, slots=True)
class SavedViewData:
    """One saved note with its relations, all read from the same snapshot.

    Body, tags, reference classification, backlinks and the collection revision
    are produced inside a single read transaction, so the result always
    corresponds to exactly one saved moment (C-REV-02).
    """

    note_id: str
    revision: int
    name: str
    body: str
    tags: tuple[str, ...]
    references: tuple[ResolvedReference, ...]
    backlinks: tuple[BacklinkSource, ...]
    collection_revision: int


@dataclass(frozen=True, slots=True)
class SnapshotReference:
    source_note_id: str
    source_revision: int
    ordinal: int
    target_collection_id: str
    target_note_id: str
    start_utf16: int
    length_utf16: int
    display_hint: str | None


@dataclass(frozen=True, slots=True)
class SnapshotNote:
    note_id: str
    name: str
    name_key: str
    body: str
    revision: int
    last_commit_id: str
    tags: tuple[str, ...]
    references: tuple[SnapshotReference, ...]


@dataclass(frozen=True, slots=True)
class IdentityRecord:
    note_id: str
    state: str
    last_saved_name: str | None


@dataclass(frozen=True, slots=True)
class ReceiptRecord:
    command_id: str
    operation_id: str
    collection_id: str
    operation_kind: str
    note_id: str | None
    before_revision: int | None
    after_revision: int | None
    collection_revision: int


@dataclass(frozen=True, slots=True)
class CollectionSnapshot:
    """A consistent saved-only view of one collection (never contains drafts)."""

    collection_id: str
    schema_version: int
    collection_revision: int
    captured_utc: datetime
    notes: tuple[SnapshotNote, ...]
    identities: tuple[IdentityRecord, ...]
    receipts: tuple[ReceiptRecord, ...]


@contextmanager
def _read_transaction(
    database: str | Path, collection_id: str | None = None
) -> Iterator[tuple[sqlite3.Connection, CollectionMetadata]]:
    """One read transaction; ``collection_id`` also proves READY and the identity."""
    reader = connect_reader(database)
    try:
        begin(reader, immediate=False)
        metadata = read_metadata(reader) if collection_id is None else require_ready(reader, collection_id)
        yield reader, metadata
        reader.execute("COMMIT")
    except (StorageFailure, RuleViolation):
        rollback(reader)
        raise
    except sqlite3.Error as error:
        rollback(reader)
        raise StorageFailure("IO_FAILURE", "Unable to read the collection.") from error
    finally:
        reader.close()


def metadata(database: str | Path) -> CollectionMetadata:
    """Current collection metadata from one consistent read transaction."""
    with _read_transaction(database) as (_reader, metadata_row):
        return metadata_row


def load_note(database: str | Path, collection_id: str, note_id: str) -> SavedNoteData | None:
    """Return the saved note, or ``None`` when no live saved version exists."""
    with _read_transaction(database, collection_id) as (reader, _metadata):
        row = reader.execute("SELECT name, body, revision FROM Notes WHERE noteId=?", (note_id,)).fetchone()
        if row is None:
            return None
        tags = tuple(
            item[0] for item in reader.execute(
                "SELECT tag FROM NoteTags WHERE noteId=? ORDER BY tag", (note_id,)
            )
        )
    return SavedNoteData(note_id, row[2], row[0], row[1], tags)


def list_summaries(database: str | Path, collection_id: str, *,
                   tags: object = ()) -> tuple[NoteSummary, ...]:
    """All saved notes in stable name order, optionally filtered by saved tags.

    ``tags`` uses AND semantics with exact case (``AI`` and ``ai`` differ) and
    never trims or folds the caller's value; the shared input rule validates it.
    A filter that matches nothing returns an empty tuple, not an error.
    """
    accepted_tags = _filter_tags(tags)
    with _read_transaction(database, collection_id) as (reader, _metadata):
        rows = reader.execute(
            "SELECT noteId, name, revision FROM Notes ORDER BY lower(name), noteId"
        ).fetchall()
        saved_tags = _tags_by_note(reader)
    summaries = [
        NoteSummary(note_id, name, revision, saved_tags.get(note_id, ()))
        for note_id, name, revision in rows
    ]
    if accepted_tags:
        wanted = set(accepted_tags)
        summaries = [item for item in summaries if wanted <= set(item.tags)]
    return tuple(summaries)


def _tags_by_note(reader: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for note_id, tag in reader.execute("SELECT noteId, tag FROM NoteTags ORDER BY noteId, tag"):
        grouped.setdefault(note_id, []).append(tag)
    return {note_id: tuple(values) for note_id, values in grouped.items()}


def search_notes(
    database: str | Path,
    collection_id: str,
    *,
    keyword: object,
    tags: object = (),
) -> SearchPage:
    """Continuous literal match over saved name and body; ASCII-insensitive.

    SQLite's built-in ``lower()`` folds ASCII only, which is the same rule used
    for name keys, so the database cannot disagree with the application's
    folding semantics. A whitespace-only keyword is rejected before any query so
    the caller can clear old results instead of showing stale ones.
    """
    text = require_text(keyword, "Search keyword")
    if not text.strip():
        raise RuleViolation("INVALID_ARGUMENT", "Enter a keyword before searching.")
    accepted_tags = _filter_tags(tags)

    with _read_transaction(database, collection_id) as (reader, metadata):
        sql = (
            "SELECT n.noteId, n.name, n.revision FROM Notes n"
            " WHERE (instr(lower(n.name), lower(?)) > 0 OR instr(lower(n.body), lower(?)) > 0)"
        )
        parameters: list[object] = [text, text]
        if accepted_tags:
            placeholders = ",".join("?" for _ in accepted_tags)
            sql += (
                f" AND (SELECT COUNT(DISTINCT t.tag) FROM NoteTags t"
                f" WHERE t.noteId=n.noteId AND t.tag IN ({placeholders})) = ?"
            )
            parameters.extend(accepted_tags)
            parameters.append(len(accepted_tags))
        sql += " ORDER BY lower(n.name), n.noteId"
        rows = reader.execute(sql, parameters).fetchall()
        saved_tags = _tags_by_note(reader)
    return SearchPage(
        text,
        accepted_tags,
        tuple(NoteSummary(note_id, name, revision, saved_tags.get(note_id, ()))
              for note_id, name, revision in rows),
        metadata.collection_revision,
    )


def list_backlinks(database: str | Path, collection_id: str, note_id: str) -> tuple[BacklinkSource, ...]:
    """Saved notes that reference ``note_id`` inside this collection, by saved name."""
    with _read_transaction(database, collection_id) as (reader, _metadata):
        rows = _backlink_rows(reader, collection_id, note_id)
    return tuple(BacklinkSource(*row) for row in rows)


def _backlink_rows(reader: sqlite3.Connection, collection_id: str,
                   note_id: str) -> list[tuple]:
    return reader.execute(
        "SELECT DISTINCT n.noteId, n.name, n.revision FROM RefOccurrence r"
        " JOIN Notes n ON n.noteId=r.sourceNoteId"
        " WHERE r.targetCollectionId=? AND r.targetNoteId=?"
        " ORDER BY lower(n.name), n.noteId",
        (collection_id, note_id),
    ).fetchall()


def classify_references(
    database: str | Path, collection_id: str, document: ParsedDocument
) -> tuple[ResolvedReference, ...]:
    """Classify each parsed occurrence against the saved identity ledger.

    Collection ownership is checked before the note identity. A note id is never
    matched across collections, and a deleted or absent target never resolves to
    another note that happens to share its name.
    """
    if document.collection_id != collection_id:
        raise RuleViolation("INVALID_ARGUMENT", "The parse result belongs to another collection.")
    with _read_transaction(database, collection_id) as (reader, _metadata):
        resolved = _classify_with(reader, collection_id, document)
    return resolved


def _classify_with(reader: sqlite3.Connection, collection_id: str,
                   document: ParsedDocument) -> tuple[ResolvedReference, ...]:
    resolved: list[ResolvedReference] = []
    for occurrence in document.references:
        if occurrence.target_collection_id != collection_id:
            resolved.append(ResolvedReference(
                occurrence, ReferenceState.FOREIGN_COLLECTION, None,
                REFERENCE_REASONS[ReferenceState.FOREIGN_COLLECTION]))
            continue
        row = reader.execute(
            "SELECT k.state, k.lastSavedName, n.name FROM NoteKeys k"
            " LEFT JOIN Notes n ON n.noteId=k.noteId WHERE k.noteId=?",
            (occurrence.target_note_id,),
        ).fetchone()
        if row is None:
            state, name = ReferenceState.MISSING, None
        elif row[0] == "LIVE" and row[2] is not None:
            state, name = ReferenceState.VALID, row[2]
        elif row[0] == "DELETED":
            state, name = ReferenceState.DELETED, row[1]
        else:
            state, name = ReferenceState.MISSING, row[1]
        resolved.append(ResolvedReference(occurrence, state, name, REFERENCE_REASONS[state]))
    return tuple(resolved)


def saved_view(database: str | Path, collection_id: str,
               note_id: str) -> SavedViewData | None:
    """Read one saved note and all of its relations from a single snapshot (C-REV-02).

    Body, tags, reference classification, backlinks and the collection revision
    are read inside one read transaction. A save/rename/delete that commits while
    the view is being built can therefore appear either entirely before or
    entirely after it, never as old body with new relations or a newer collection
    revision attached to an older note revision.
    """
    with _read_transaction(database, collection_id) as (reader, metadata):
        row = reader.execute(
            "SELECT name, body, revision FROM Notes WHERE noteId=?", (note_id,)
        ).fetchone()
        if row is None:
            return None
        tags = tuple(
            item[0] for item in reader.execute(
                "SELECT tag FROM NoteTags WHERE noteId=? ORDER BY tag", (note_id,)
            )
        )
        document = parse_markdown(row[1], collection_id)
        references = _classify_with(reader, collection_id, document)
        backlinks = _backlink_rows(reader, collection_id, note_id)
        collection_revision = metadata.collection_revision
    return SavedViewData(
        note_id,
        row[2],
        row[0],
        row[1],
        tags,
        references,
        tuple(BacklinkSource(*item) for item in backlinks),
        collection_revision,
    )


def capture_snapshot(database: str | Path) -> CollectionSnapshot:
    """Read a consistent saved-only snapshot for backup/restore (API09 input).

    Drafts never reach the database, and the whole read happens inside one read
    transaction, so the snapshot cannot mix two different saved revisions.
    """
    with _read_transaction(database) as (reader, metadata):
        if metadata.state != "READY":
            raise StorageFailure("STATE_BLOCKED", "A non-ready collection cannot be snapshotted.")
        notes: list[SnapshotNote] = []
        for note_id, name, name_key, body, revision, last_commit_id in reader.execute(
            "SELECT noteId, name, nameKey, body, revision, lastCommitId FROM Notes ORDER BY noteId"
        ):
            tags = tuple(
                row[0] for row in reader.execute(
                    "SELECT tag FROM NoteTags WHERE noteId=? ORDER BY tag", (note_id,)
                )
            )
            references = tuple(
                SnapshotReference(*row) for row in reader.execute(
                    "SELECT sourceNoteId, sourceRevision, ordinal, targetCollectionId,"
                    " targetNoteId, startUtf16, lengthUtf16, displayHint"
                    " FROM RefOccurrence WHERE sourceNoteId=? ORDER BY ordinal",
                    (note_id,),
                )
            )
            notes.append(SnapshotNote(note_id, name, name_key, body, revision, last_commit_id,
                                      tags, references))
        identities = tuple(
            IdentityRecord(*row) for row in reader.execute(
                "SELECT noteId, state, lastSavedName FROM NoteKeys ORDER BY noteId"
            )
        )
        receipts = tuple(
            ReceiptRecord(*row) for row in reader.execute(
                "SELECT commandId, operationId, collectionId, operationKind, noteId,"
                " beforeRevision, afterRevision, collectionRevision FROM CommitReceipt"
                " ORDER BY collectionRevision, commandId"
            )
        )
    return CollectionSnapshot(
        metadata.collection_id,
        metadata.schema_version,
        metadata.collection_revision,
        datetime.now(timezone.utc),
        tuple(notes),
        identities,
        receipts,
    )


def _filter_tags(tags: object) -> tuple[str, ...]:
    """Validate filter tags without rewriting them (query side has no trimming).

    The editor normalizes user input before saving, so a filter value must already
    equal its saved form. Accepting ``" AI "`` here would silently perform an
    implicit trim and could hide a mismatch between UI input and stored data.
    """
    if tags is None:
        return ()
    if isinstance(tags, str) or not isinstance(tags, (tuple, list)):
        raise RuleViolation("INVALID_TAG", "Tags for filtering must be a sequence of tag strings.")
    accepted: list[str] = []
    for tag in tags:
        value = require_text(tag, "Filter tag")
        if normalize_tag(value) != value:
            raise RuleViolation(
                "INVALID_TAG",
                "Filter tags must be used exactly as saved (already trimmed, 1-30 code points).",
                "Normalize the tag input before filtering; the query side does not trim or fold.",
            )
        if value not in accepted:
            accepted.append(value)
    return tuple(accepted)
