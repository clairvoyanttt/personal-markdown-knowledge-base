"""Controlled saved-version writes: SAVE, DELETE and restore initialization.

Each public function opens its own connection in the calling storage thread and
runs exactly one explicit transaction. The order is fixed by the design:

    verify identity/version -> re-check input rules -> write identity, body,
    tags and outbound occurrences -> write the receipt -> bump the collection
    revision -> COMMIT -> independently re-read the committed rows.

A normal failure is reported only after ROLLBACK is confirmed. If the commit or
its verification cannot be confirmed the result is ``UNRESOLVED`` (unknown), never
a silent success and never a claimed rollback. An absent receipt alone never
proves that nothing was written.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
import secrets
import sqlite3

from pmkb.application.parsing import ParsedDocument, parse_markdown
from pmkb.contracts.common import AppError
from pmkb.domain.identity import require_identifier
from pmkb.domain.naming import normalize_name, normalize_tags
from pmkb.domain.rules import MAX_VERSION, RuleViolation, is_version
from pmkb.infrastructure.storage.connection import (
    CollectionMetadata,
    StorageFailure,
    begin,
    commit,
    connect_reader,
    connect_writer,
    integrity_check,
    read_metadata,
    require_ready,
    rollback,
)


class CommitState(StrEnum):
    """Saved-version commit evidence, matching API10/API11 vocabulary."""

    COMMITTED = "COMMITTED"
    NOT_COMMITTED = "NOT_COMMITTED"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class CommitResult:
    state: CommitState
    saved_revision: int | None
    collection_revision: int | None
    error: AppError | None = None
    committed_note: "CommittedNote | None" = None


@dataclass(frozen=True, slots=True)
class CommittedNote:
    """The exact saved projection verified after commit."""

    note_id: str
    revision: int
    name: str
    name_key: str
    body: str
    tags: tuple[str, ...]
    last_commit_id: str


def app_error(code: str, reason: str, action: str = "Correct the input and retry.") -> AppError:
    return AppError(code, reason, action)


def _unknown(error: Exception | None = None) -> CommitResult:
    reason = "The collection could not confirm the saved-version result."
    return CommitResult(CommitState.UNRESOLVED, None, None,
                        app_error("COMMIT_UNCONFIRMED", reason,
                                  "Verify the original command instead of writing again."))


def _failure(error: StorageFailure) -> CommitResult:
    if error.unknown:
        return CommitResult(CommitState.UNRESOLVED, None, None,
                            app_error(error.code, error.reason, error.action))
    return CommitResult(CommitState.NOT_COMMITTED, None, None,
                        app_error(error.code, error.reason, error.action))


def _reject(error: RuleViolation) -> CommitResult:
    return CommitResult(CommitState.NOT_COMMITTED, None, None,
                        app_error(error.code, error.reason, error.action))


def save_note(
    database: str | Path,
    *,
    collection_id: str,
    command_id: str,
    operation_id: str,
    note_id: str,
    name: object,
    body: object,
    tags: object,
    base_revision: int | None,
) -> CommitResult:
    """Atomically save one note and return verifiable receipt evidence.

    ``base_revision`` is the draft's captured saved revision (``None`` for a new
    note). A stale base is rejected as ``REVISION_CONFLICT``; the name/tag rules
    are re-applied here so a caller's own validation cannot be the only check.
    """
    try:
        require_identifier(collection_id, "Collection identity")
        require_identifier(command_id, "Command identity")
        require_identifier(operation_id, "Operation identity")
        require_identifier(note_id, "Note identity")
        if base_revision is not None and not is_version(base_revision, minimum=1):
            raise RuleViolation("REVISION_CONFLICT", "The saved base revision is not a valid version.")
        normalized = normalize_name(name)
        accepted_tags = normalize_tags(tags)
        parsed = parse_markdown(body, collection_id)
        text = parsed.body
    except RuleViolation as error:
        return _reject(error)

    connection = None
    try:
        connection = connect_writer(database)
        begin(connection)
        metadata = require_ready(connection, collection_id)
        saved = _write_save(connection, metadata, command_id=command_id, operation_id=operation_id,
                            note_id=note_id, name=normalized.name, name_key=normalized.key,
                            body=text, tags=accepted_tags, parsed=parsed,
                            base_revision=base_revision)
        commit(connection)
    except sqlite3.IntegrityError as error:
        return _integrity_failure(connection, error)
    except sqlite3.Error as error:
        return _sqlite_failure(connection, error)
    except StorageFailure as error:
        _close(connection)
        return _failure(error)
    except RuleViolation as error:
        _close(connection)
        return _reject(error)
    except Exception:
        if connection is not None and not rollback(connection):
            return _unknown()
        _close(connection)
        return _unknown()
    _close(connection)
    return _verify_save(database, collection_id, command_id, note_id, saved)


def delete_note(
    database: str | Path,
    *,
    collection_id: str,
    command_id: str,
    operation_id: str,
    note_id: str,
    expected_revision: int,
) -> CommitResult:
    """Atomically move one note from LIVE to DELETED and keep its identity ledger."""
    try:
        require_identifier(collection_id, "Collection identity")
        require_identifier(command_id, "Command identity")
        require_identifier(operation_id, "Operation identity")
        require_identifier(note_id, "Note identity")
        if not is_version(expected_revision, minimum=1):
            raise RuleViolation("REVISION_CONFLICT", "The expected saved revision is not a valid version.")
    except RuleViolation as error:
        return _reject(error)

    connection = None
    try:
        connection = connect_writer(database)
        begin(connection)
        metadata = require_ready(connection, collection_id)
        name = _write_delete(connection, metadata, command_id=command_id, operation_id=operation_id,
                             note_id=note_id, expected_revision=expected_revision)
        commit(connection)
    except sqlite3.IntegrityError as error:
        return _integrity_failure(connection, error)
    except sqlite3.Error as error:
        return _sqlite_failure(connection, error)
    except StorageFailure as error:
        _close(connection)
        return _failure(error)
    except RuleViolation as error:
        _close(connection)
        return _reject(error)
    except Exception:
        if connection is not None and not rollback(connection):
            return _unknown()
        _close(connection)
        return _unknown()
    _close(connection)
    return _verify_delete(database, collection_id, command_id, note_id, expected_revision, name)


# ---------------------------------------------------------------------------
# transaction bodies


def _write_save(
    connection: sqlite3.Connection,
    metadata: CollectionMetadata,
    *,
    command_id: str,
    operation_id: str,
    note_id: str,
    name: str,
    name_key: str,
    body: str,
    tags: tuple[str, ...],
    parsed: ParsedDocument,
    base_revision: int | None,
) -> CommittedNote:
    if connection.execute("SELECT 1 FROM CommitReceipt WHERE commandId=?", (command_id,)).fetchone():
        raise RuleViolation("INVALID_ARGUMENT", "This save command was already recorded.")

    duplicate = connection.execute(
        "SELECT noteId FROM Notes WHERE nameKey=? AND noteId<>?", (name_key, note_id)
    ).fetchone()
    if duplicate is not None:
        raise RuleViolation(
            "DUPLICATE_NAME",
            "Another note in this collection already uses that name.",
            "Choose a different name.",
        )

    ledger = connection.execute(
        "SELECT state, liveNoteId FROM NoteKeys WHERE noteId=?", (note_id,)
    ).fetchone()
    existing = connection.execute(
        "SELECT revision FROM Notes WHERE noteId=?", (note_id,)
    ).fetchone()

    if base_revision is None:
        if existing is not None:
            raise RuleViolation(
                "REVISION_CONFLICT",
                "A saved version already exists for this note identity.",
                "Reload the saved note before saving.",
            )
        if ledger is not None:
            raise RuleViolation(
                "INVALID_ARGUMENT",
                "A deleted or reserved note identity cannot be reused for a new note.",
            )
        revision = 1
        connection.execute(
            "INSERT INTO NoteKeys(noteId,state,liveNoteId,lastSavedName) VALUES (?,?,?,?)",
            (note_id, "LIVE", note_id, name),
        )
        connection.execute(
            "INSERT INTO Notes(noteId,keyState,name,nameKey,body,revision,lastCommitId)"
            " VALUES (?,?,?,?,?,?,?)",
            (note_id, "LIVE", name, name_key, body, revision, command_id),
        )
    else:
        if ledger is None or ledger[0] != "LIVE" or existing is None:
            raise RuleViolation(
                "REVISION_CONFLICT",
                "The note no longer has a live saved version to update.",
                "Reload the saved note before saving.",
            )
        current = existing[0]
        if current != base_revision:
            raise RuleViolation(
                "REVISION_CONFLICT",
                "The saved note changed after this draft was captured.",
                "Reload the saved note; your draft was kept.",
            )
        revision = base_revision + 1
        if revision > MAX_VERSION:
            raise RuleViolation("STATE_BLOCKED", "The saved revision reached its upper bound.",
                                "Start a new note instead of wrapping the version.")
        connection.execute(
            "UPDATE NoteKeys SET lastSavedName=? WHERE noteId=?", (name, note_id)
        )
        connection.execute(
            "UPDATE Notes SET name=?, nameKey=?, body=?, revision=?, lastCommitId=?"
            " WHERE noteId=? AND revision=?",
            (name, name_key, body, revision, command_id, note_id, base_revision),
        )

    connection.execute("DELETE FROM NoteTags WHERE noteId=?", (note_id,))
    connection.executemany(
        "INSERT INTO NoteTags(noteId,tag) VALUES (?,?)", [(note_id, tag) for tag in tags]
    )

    connection.execute("DELETE FROM RefOccurrence WHERE sourceNoteId=?", (note_id,))
    connection.executemany(
        "INSERT INTO RefOccurrence(sourceNoteId,sourceRevision,ordinal,targetCollectionId,"
        " targetNoteId,startUtf16,lengthUtf16,displayHint) VALUES (?,?,?,?,?,?,?,?)",
        [
            (note_id, revision, item.ordinal, item.target_collection_id, item.target_note_id,
             item.start_utf16, item.length_utf16, None)
            for item in parsed.references
        ],
    )

    collection_revision = metadata.collection_revision + 1
    if collection_revision > MAX_VERSION:
        raise RuleViolation("STATE_BLOCKED", "The collection revision reached its upper bound.")
    connection.execute(
        "INSERT INTO CommitReceipt(commandId,operationId,collectionId,operationKind,noteId,"
        " beforeRevision,afterRevision,collectionRevision) VALUES (?,?,?,?,?,?,?,?)",
        (command_id, operation_id, metadata.collection_id, "SAVE", note_id,
         base_revision, revision, collection_revision),
    )
    connection.execute(
        "UPDATE CollectionMeta SET collectionRevision=? WHERE singletonId=1", (collection_revision,)
    )
    return CommittedNote(note_id, revision, name, name_key, body, tags, command_id)


def _write_delete(
    connection: sqlite3.Connection,
    metadata: CollectionMetadata,
    *,
    command_id: str,
    operation_id: str,
    note_id: str,
    expected_revision: int,
) -> str:
    if connection.execute("SELECT 1 FROM CommitReceipt WHERE commandId=?", (command_id,)).fetchone():
        raise RuleViolation("INVALID_ARGUMENT", "This delete command was already recorded.")
    row = connection.execute("SELECT name, revision FROM Notes WHERE noteId=?", (note_id,)).fetchone()
    ledger = connection.execute("SELECT state FROM NoteKeys WHERE noteId=?", (note_id,)).fetchone()
    if row is None:
        if ledger is not None and ledger[0] == "DELETED":
            raise RuleViolation("NOTE_NOT_FOUND", "This note is already deleted.")
        raise RuleViolation("NOTE_NOT_FOUND", "No saved note has this identity.")
    name, revision = row
    if revision != expected_revision:
        raise RuleViolation(
            "REVISION_CONFLICT",
            "The saved note changed after this delete was confirmed.",
            "Reload the saved note before deleting.",
        )
    if ledger is None or ledger[0] != "LIVE":
        raise RuleViolation("STATE_BLOCKED", "The note identity is not in a live saved state.")

    collection_revision = metadata.collection_revision + 1
    if collection_revision > MAX_VERSION:
        raise RuleViolation("STATE_BLOCKED", "The collection revision reached its upper bound.")
    connection.execute(
        "INSERT INTO CommitReceipt(commandId,operationId,collectionId,operationKind,noteId,"
        " beforeRevision,afterRevision,collectionRevision) VALUES (?,?,?,?,?,?,NULL,?)",
        (command_id, operation_id, metadata.collection_id, "DELETE", note_id,
         revision, collection_revision),
    )
    connection.execute(
        "UPDATE NoteKeys SET state='DELETED', liveNoteId=NULL, lastSavedName=?"
        " WHERE noteId=? AND state='LIVE'",
        (name, note_id),
    )
    if connection.execute("SELECT changes()").fetchone()[0] != 1:
        raise RuleViolation("STATE_BLOCKED", "The note identity could not be marked deleted.")
    connection.execute("DELETE FROM Notes WHERE noteId=?", (note_id,))
    connection.execute(
        "UPDATE CollectionMeta SET collectionRevision=? WHERE singletonId=1", (collection_revision,)
    )
    return name


# ---------------------------------------------------------------------------
# post-commit verification (independent read-only connection)


def _verify_save(
    database: str | Path,
    collection_id: str,
    command_id: str,
    note_id: str,
    saved: CommittedNote,
) -> CommitResult:
    reader = None
    try:
        reader = connect_reader(database)
        begin(reader, immediate=False)
        result = _match_save(reader, collection_id, command_id, note_id,
                             saved.name, saved.name_key, saved.body, saved.tags)
        reader.execute("COMMIT")
    except (StorageFailure, sqlite3.Error):
        if reader is not None:
            rollback(reader)
        _close(reader)
        return _unknown()
    _close(reader)
    if result.state is CommitState.COMMITTED:
        return CommitResult(result.state, result.saved_revision, result.collection_revision, None, saved)
    return result


def check_save(
    database: str | Path,
    *,
    collection_id: str,
    command_id: str,
    note_id: str,
    name: object,
    body: object,
    tags: object,
    base_revision: int | None,
) -> CommitResult:
    """Re-read persisted evidence for one original SAVE command (API11).

    ``COMMITTED`` needs a receipt for *this* collection/command/note with the
    expected before/after revisions, plus a matching body/name/tag projection and
    ``lastCommitId``. A missing receipt is not evidence of rollback, so the
    caller keeps its pending state instead of being told "not saved".
    """
    try:
        require_identifier(collection_id, "Collection identity")
        require_identifier(command_id, "Command identity")
        require_identifier(note_id, "Note identity")
        normalized = normalize_name(name)
        expected_tags = normalize_tags(tags)
        text = parse_markdown(body, collection_id).body
    except RuleViolation:
        return _unknown()
    reader = None
    try:
        reader = connect_reader(database)
        begin(reader, immediate=False)
        result = _match_save(reader, collection_id, command_id, note_id,
                             normalized.name, normalized.key, text, expected_tags,
                             expected_before=base_revision)
        reader.execute("COMMIT")
    except (StorageFailure, sqlite3.Error):
        if reader is not None:
            rollback(reader)
        _close(reader)
        return _unknown()
    _close(reader)
    return result


def _match_save(
    reader: sqlite3.Connection,
    collection_id: str,
    command_id: str,
    note_id: str,
    name: str,
    name_key: str,
    body: str,
    tags: tuple[str, ...],
    expected_before: int | None = None,
) -> CommitResult:
    receipt = reader.execute(
        "SELECT operationKind, collectionId, noteId, beforeRevision, afterRevision,"
        " collectionRevision FROM CommitReceipt WHERE commandId=?", (command_id,)
    ).fetchone()
    if receipt is None:
        return _unknown()
    note = reader.execute(
        "SELECT name, nameKey, body, revision, lastCommitId FROM Notes WHERE noteId=?", (note_id,)
    ).fetchone()
    if note is None:
        return _unknown()
    stored_tags = tuple(
        row[0] for row in reader.execute(
            "SELECT tag FROM NoteTags WHERE noteId=? ORDER BY tag", (note_id,)
        )
    )
    meta = read_metadata(reader)
    before, after = receipt[3], receipt[4]
    if (receipt[0] != "SAVE" or receipt[1] != collection_id or receipt[2] != note_id
            or after is None or note[3] != after or note[4] != command_id
            or note[0] != name or note[1] != name_key or note[2] != body
            or stored_tags != tuple(sorted(tags))
            or meta.collection_id != collection_id
            or meta.collection_revision < (receipt[5] or 0)
            or (expected_before is not None and before != expected_before)):
        return _unknown()
    return CommitResult(CommitState.COMMITTED, after, receipt[5], None)


def _verify_delete(
    database: str | Path,
    collection_id: str,
    command_id: str,
    note_id: str,
    expected_revision: int,
    name: str,
) -> CommitResult:
    reader = None
    try:
        reader = connect_reader(database)
        begin(reader, immediate=False)
        receipt = reader.execute(
            "SELECT operationKind, collectionId, noteId, beforeRevision, afterRevision,"
            " collectionRevision FROM CommitReceipt WHERE commandId=?", (command_id,)
        ).fetchone()
        note = reader.execute("SELECT 1 FROM Notes WHERE noteId=?", (note_id,)).fetchone()
        ledger = reader.execute(
            "SELECT state, lastSavedName FROM NoteKeys WHERE noteId=?", (note_id,)
        ).fetchone()
        reader.execute("COMMIT")
    except (StorageFailure, sqlite3.Error):
        if reader is not None:
            rollback(reader)
        _close(reader)
        return _unknown()
    _close(reader)
    if (receipt is None or receipt[0] != "DELETE" or receipt[1] != collection_id
            or receipt[2] != note_id or receipt[3] != expected_revision
            or note is not None or ledger is None or ledger[0] != "DELETED"
            or ledger[1] != name):
        return _unknown()
    return CommitResult(CommitState.COMMITTED, None, receipt[5], None)


# ---------------------------------------------------------------------------
# restore initialization (C side of API09)


@dataclass(frozen=True, slots=True)
class RestoredNote:
    """One note prepared for a restored collection, already ownership-converted."""

    note_id: str
    name: str
    body: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RetainedIdentity:
    """A deleted/reserved identity that must not be re-created by restoration."""

    note_id: str
    state: str
    last_saved_name: str | None


@dataclass(frozen=True, slots=True)
class RestoreInitResult:
    collection_id: str
    collection_revision: int
    initialized_notes: int
    retained_identities: int


def initialize_restored_collection(
    database: str | Path,
    *,
    new_collection_id: str,
    notes: tuple[RestoredNote, ...],
    retained: tuple[RetainedIdentity, ...] = (),
) -> RestoreInitResult:
    """Create a brand-new collection database from an already converted snapshot.

    The target file must not exist yet; the schema comes from the approved DDL.
    Every note starts at revision 1 with its own ``RESTORE_NOTE_INIT`` receipt,
    one collection-level ``RESTORE_INIT`` receipt exists, and the collection
    revision is 1. Reference ownership is re-derived from the converted body, so
    only markers actually present in the text become occurrences.
    """
    require_identifier(new_collection_id, "New collection identity")
    path = Path(database)
    if path.exists():
        raise StorageFailure("TARGET_EXISTS", "The restore target must not exist yet.")
    connection = None
    try:
        connection = sqlite3.connect(path, timeout=10.0, autocommit=True)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")
        )
        begin(connection)
        connection.execute(
            "INSERT INTO CollectionMeta(singletonId,collectionId,schemaVersion,"
            "collectionRevision,state) VALUES (1,?,1,1,'READY')",
            (new_collection_id,),
        )
        connection.execute(
            "INSERT INTO CommitReceipt(commandId,operationId,collectionId,operationKind,noteId,"
            "beforeRevision,afterRevision,collectionRevision) VALUES (?,?,?,'RESTORE_INIT',"
            "NULL,NULL,NULL,1)",
            (secrets.token_hex(16), secrets.token_hex(16), new_collection_id),
        )
        for entry in retained:
            require_identifier(entry.note_id, "Retained note identity")
            if entry.state not in {"DELETED", "MISSING_RESERVED"}:
                raise RuleViolation("INVALID_ARGUMENT", "A retained identity must be deleted or reserved.")
            connection.execute(
                "INSERT INTO NoteKeys(noteId,state,liveNoteId,lastSavedName) VALUES (?,?,NULL,?)",
                (entry.note_id, entry.state, entry.last_saved_name),
            )
        for item in notes:
            require_identifier(item.note_id, "Note identity")
            normalized = normalize_name(item.name)
            tags = normalize_tags(item.tags)
            parsed = parse_markdown(item.body, new_collection_id)
            receipt_id = secrets.token_hex(16)
            connection.execute(
                "INSERT INTO NoteKeys(noteId,state,liveNoteId,lastSavedName) VALUES (?,?,?,?)",
                (item.note_id, "LIVE", item.note_id, normalized.name),
            )
            connection.execute(
                "INSERT INTO Notes(noteId,keyState,name,nameKey,body,revision,lastCommitId)"
                " VALUES (?,?,?,?,?,1,?)",
                (item.note_id, "LIVE", normalized.name, normalized.key, parsed.body, receipt_id),
            )
            connection.executemany(
                "INSERT INTO NoteTags(noteId,tag) VALUES (?,?)",
                [(item.note_id, tag) for tag in tags],
            )
            connection.executemany(
                "INSERT INTO RefOccurrence(sourceNoteId,sourceRevision,ordinal,targetCollectionId,"
                " targetNoteId,startUtf16,lengthUtf16,displayHint) VALUES (?,1,?,?,?,?,?,NULL)",
                [
                    (item.note_id, occurrence.ordinal, occurrence.target_collection_id,
                     occurrence.target_note_id, occurrence.start_utf16, occurrence.length_utf16)
                    for occurrence in parsed.references
                ],
            )
            connection.execute(
                "INSERT INTO CommitReceipt(commandId,operationId,collectionId,operationKind,"
                "noteId,beforeRevision,afterRevision,collectionRevision)"
                " VALUES (?,?,?,'RESTORE_NOTE_INIT',?,NULL,1,1)",
                (receipt_id, secrets.token_hex(16), new_collection_id, item.note_id),
            )
        integrity_check(connection)
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise StorageFailure("INVALID_ARGUMENT", "Restored relationships are inconsistent.")
        commit(connection)
    except (RuleViolation, StorageFailure):
        if connection is not None:
            rollback(connection)
            connection.close()
            _unlink_quietly(path)
        raise
    except sqlite3.Error as error:
        if connection is not None:
            rollback(connection)
            connection.close()
            _unlink_quietly(path)
        raise StorageFailure("IO_FAILURE", "The restored collection could not be initialized.") from error
    if connection is not None:
        connection.close()
    return RestoreInitResult(new_collection_id, 1, len(notes), len(retained))


# ---------------------------------------------------------------------------
# helpers


def _integrity_failure(connection: sqlite3.Connection | None, error: sqlite3.IntegrityError) -> CommitResult:
    message = str(error)
    if connection is not None and not rollback(connection):
        _close(connection)
        return _unknown()
    _close(connection)
    if "UNIQUE constraint failed: Notes.nameKey" in message:
        return CommitResult(CommitState.NOT_COMMITTED, None, None,
                            app_error("DUPLICATE_NAME", "Another note in this collection already uses that name.",
                                      "Choose a different name."))
    if "FOREIGN KEY" in message.upper() or "CHECK constraint" in message:
        return CommitResult(CommitState.NOT_COMMITTED, None, None,
                            app_error("INVALID_ARGUMENT", "Saved relationships or identity state were rejected.",
                                      "Reload the saved note and retry."))
    return CommitResult(CommitState.NOT_COMMITTED, None, None,
                        app_error("IO_FAILURE", "The database rejected the operation; nothing was saved.",
                                  "Check the collection database and retry."))


def _sqlite_failure(connection: sqlite3.Connection | None, error: sqlite3.Error) -> CommitResult:
    if connection is not None and not rollback(connection):
        _close(connection)
        return _unknown()
    _close(connection)
    return CommitResult(CommitState.NOT_COMMITTED, None, None,
                        app_error("IO_FAILURE", "The database rejected the operation; nothing was saved.",
                                  "Check the collection database and retry."))


def _close(connection: sqlite3.Connection | None) -> None:
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error:
            pass


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
