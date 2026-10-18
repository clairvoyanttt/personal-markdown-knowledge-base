"""Minimal real collection bootstrap/catalog, not note CRUD or a storage queue."""
from contextlib import closing
from importlib.resources import files
import os
from pathlib import Path
import re
import secrets
import sqlite3

from pmkb.application.collection.ports import CollectionFailure, PreparedCollection, PublishOutcome
from pmkb.contracts.collections import ActivationMode, TargetStage

_COLUMNS = {
    "CollectionMeta": {"singletonId", "collectionId", "schemaVersion", "collectionRevision", "state"},
    "Notes": {"noteId", "keyState", "name", "nameKey", "body", "revision", "lastCommitId"},
    "NoteKeys": {"noteId", "state", "liveNoteId", "lastSavedName"},
    "NoteTags": {"noteId", "tag"},
    "RefOccurrence": {"sourceNoteId", "sourceRevision", "ordinal", "targetCollectionId", "targetNoteId", "startUtf16", "lengthUtf16", "displayHint"},
    "CommitReceipt": {"commandId", "operationId", "collectionId", "operationKind", "noteId", "beforeRevision", "afterRevision", "collectionRevision"},
}

_FOREIGN_KEYS = {
    "NoteKeys": {("Notes", "liveNoteId", "noteId", "NO ACTION")},
    "Notes": {("NoteKeys", "noteId", "noteId", "NO ACTION"), ("NoteKeys", "keyState", "state", "NO ACTION")},
    "NoteTags": {("Notes", "noteId", "noteId", "CASCADE")},
    "RefOccurrence": {("Notes", "sourceNoteId", "noteId", "CASCADE"), ("Notes", "sourceRevision", "revision", "CASCADE")},
}
_UNIQUE_KEYS = {
    "Notes": {("noteId", "revision"), ("nameKey",)},
    "NoteKeys": {("noteId", "state")},
    "NoteTags": {("noteId", "tag")},
    "RefOccurrence": {("sourceNoteId", "ordinal"), ("sourceNoteId", "startUtf16")},
}


class LocalCollectionBackend:
    def __init__(self) -> None:
        self._owned: dict[str, tuple[int, int]] = {}

    def prepare(self, mode: ActivationMode, directory: str) -> PreparedCollection:
        try:
            path = Path(directory).resolve(strict=True)
            if not path.is_dir():
                raise CollectionFailure("INVALID_ARGUMENT", "Choose an existing collection directory.")
            if mode == ActivationMode.OPEN:
                identity = self._read(path / "knowledge.sqlite", {"READY"})
                return PreparedCollection(str(path), identity, mode)
            if mode != ActivationMode.CREATE:
                raise CollectionFailure("INVALID_ARGUMENT", "Unknown activation mode.")
            if any(path.iterdir()):
                raise CollectionFailure("TARGET_NOT_EMPTY", "New collection directory must be empty.")
            identity = secrets.token_hex(16)
            temporary = path / f".pmkb-{secrets.token_hex(16)}.sqlite"
            # Exclusive creation: do not open/truncate a file supplied by somebody else.
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            stat = temporary.stat()
            self._owned[str(temporary)] = (stat.st_dev, stat.st_ino)
            target = PreparedCollection(str(path), identity, mode, str(temporary))
            try:
                with closing(sqlite3.connect(temporary, autocommit=True)) as db:
                    db.executescript(files(__package__).joinpath("schema.sql").read_text(encoding="utf-8"))
                    db.execute("BEGIN")
                    db.execute("INSERT INTO CollectionMeta VALUES (1, ?, 1, 1, 'PREPARED')", (identity,))
                    db.execute("COMMIT")
                self.validate(target, published=False)
                return target
            except Exception:
                self.cleanup(target)
                raise
        except CollectionFailure:
            raise
        except (OSError, sqlite3.Error) as error:
            raise CollectionFailure("IO_FAILURE", "Unable to prepare or read the collection.") from error

    def _read(self, database: Path, states: set[str]) -> str:
        if not database.is_file():
            raise CollectionFailure("INVALID_ARGUMENT", "No readable knowledge.sqlite at this location.")
        try:
            with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, autocommit=True)) as db:
                db.execute("PRAGMA foreign_keys=ON")
                db.execute("PRAGMA query_only=ON")
                if db.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                    raise CollectionFailure("IO_FAILURE", "Foreign-key checks are unavailable.")
                rows = db.execute("SELECT singletonId,collectionId,schemaVersion,collectionRevision,state FROM CollectionMeta").fetchall()
                if len(rows) != 1:
                    raise CollectionFailure("INVALID_ARGUMENT", "Invalid collection metadata.")
                singleton, identity, version, revision, state = rows[0]
                if version != 1:
                    raise CollectionFailure("SCHEMA_UNSUPPORTED", "Collection format version is not supported.", "Choose a supported collection; no migration was performed.")
                if (singleton != 1 or not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity)
                        or type(revision) is not int or revision < 1 or state not in states):
                    raise CollectionFailure("INVALID_ARGUMENT", "Collection metadata is not valid or ready.")
                for table, columns in _COLUMNS.items():
                    actual = {row[1] for row in db.execute(f'PRAGMA table_info("{table}")')}
                    if not columns <= actual:
                        raise CollectionFailure("INVALID_ARGUMENT", "Collection schema is incomplete.")
                for table, expected in _FOREIGN_KEYS.items():
                    actual = {(row[2], row[3], row[4], row[6]) for row in db.execute(f'PRAGMA foreign_key_list("{table}")')}
                    if not expected <= actual:
                        raise CollectionFailure("INVALID_ARGUMENT", "Collection relationship constraints are incomplete.")
                for table, expected in _UNIQUE_KEYS.items():
                    actual = {
                        tuple(item[0] for item in db.execute("SELECT name FROM pragma_index_info(?) ORDER BY seqno", (index[1],)))
                        for index in db.execute(f'PRAGMA index_list("{table}")') if index[2] == 1 and index[4] == 0
                    }
                    if not expected <= actual:
                        raise CollectionFailure("INVALID_ARGUMENT", "Collection uniqueness constraints are incomplete.")
                triggers = {row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='trigger'")}
                if not {"TK_NoDelete", "TK_NoIdChange", "TN_NoIdChange", "TK_State"} <= triggers:
                    raise CollectionFailure("INVALID_ARGUMENT", "Collection identity protections are incomplete.")
                if db.execute("PRAGMA quick_check").fetchall() != [("ok",)] or db.execute("PRAGMA foreign_key_check").fetchall():
                    raise CollectionFailure("INVALID_ARGUMENT", "Collection integrity checks failed.")
                return identity
        except sqlite3.Error as error:
            raise CollectionFailure("INVALID_ARGUMENT", "Unreadable or invalid collection database.") from error

    def validate(self, target: PreparedCollection, published: bool) -> None:
        database = Path(target.directory) / "knowledge.sqlite" if published or target.mode == ActivationMode.OPEN else Path(target.temporary_file or "")
        identity = self._read(database, {"READY"} if published or target.mode == ActivationMode.OPEN else {"PREPARED", "READY"})
        if identity != target.collection_id:
            raise CollectionFailure("INVALID_ARGUMENT", "Collection identity changed after preparation.")

    def _is_owned(self, temporary: Path) -> bool:
        try:
            stat = temporary.lstat()
            return not temporary.is_symlink() and self._owned.get(str(temporary)) == (stat.st_dev, stat.st_ino)
        except OSError:
            return False

    def publish(self, target: PreparedCollection) -> PublishOutcome:
        if target.mode != ActivationMode.CREATE or not target.temporary_file:
            raise CollectionFailure("INVALID_ARGUMENT", "Only a prepared new collection can be published.")
        temporary, directory = Path(target.temporary_file), Path(target.directory)
        if temporary.parent != directory or not self._is_owned(temporary):
            raise CollectionFailure("INVALID_ARGUMENT", "Candidate is no longer owned by this operation.")
        self.validate(target, published=False)
        if any(child != temporary for child in directory.iterdir()):
            raise CollectionFailure("TARGET_NOT_EMPTY", "Destination changed after preparation.")
        with closing(sqlite3.connect(temporary, autocommit=True)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN")
            db.execute("UPDATE CollectionMeta SET state='READY' WHERE singletonId=1")
            db.execute("COMMIT")
        formal = directory / "knowledge.sqlite"
        try:
            # Same-directory hard-link publication is atomic and never overwrites.
            # A filesystem without hard links fails safely; no replacing fallback.
            os.link(temporary, formal)
        except FileExistsError as error:
            raise CollectionFailure("TARGET_EXISTS", "A collection already exists at the destination.") from error
        except OSError as error:
            if not formal.exists() or not os.path.samefile(temporary, formal):
                raise CollectionFailure("IO_FAILURE", "Collection publication result could not be confirmed.", unknown=True) from error
        warnings = self.cleanup(target)
        return PublishOutcome(TargetStage.PUBLISHED_NEW, warnings)

    def cleanup(self, target: PreparedCollection) -> tuple[str, ...]:
        if not target.temporary_file:
            return ()
        temporary = Path(target.temporary_file)
        if temporary.parent != Path(target.directory):
            return ("Candidate path is not owned; it was not removed.",)
        if not temporary.exists():
            self._owned.pop(str(temporary), None)
            return ()
        if not self._is_owned(temporary):
            return ("Candidate was replaced; it was not removed.",)
        try:
            temporary.unlink()
            self._owned.pop(str(temporary), None)
            return ()
        except OSError:
            return (f"Temporary candidate could not be removed: {temporary}",)
