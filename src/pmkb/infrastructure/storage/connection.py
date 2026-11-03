"""SQLite connection policy for one collection.

Rules enforced here rather than in every caller:

* ``foreign_keys=ON`` is enabled and *verified* per connection; a connection that
  cannot prove it stays unusable (deferred constraints are meaningless without it).
* ``autocommit=True`` so Python never opens an implicit transaction behind the
  caller's back; every write uses an explicit ``BEGIN``/``COMMIT``/``ROLLBACK``
  whose result is checked (``Connection.commit()`` is not used as a rollback
  proof, and a cancelled future never proves a rollback either).
* Readers use a real read-only URI connection, so a query can never mutate data.
* Connections are never handed to the UI and never shared across threads: each
  function opens and closes its own connection in the calling storage thread.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from pmkb.domain.identity import require_identifier
from pmkb.domain.rules import RuleViolation, require_version


class StorageFailure(Exception):
    """A storage operation that did not succeed, with an explicit unknown flag.

    ``unknown=True`` means the result of the attempted write could not be
    confirmed; callers must not report success or rollback in that case.
    """

    def __init__(
        self,
        code: str,
        reason: str,
        action: str = "Check the collection database and retry.",
        *,
        unknown: bool = False,
    ) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.action = action
        self.unknown = unknown


@dataclass(frozen=True, slots=True)
class CollectionMetadata:
    collection_id: str
    schema_version: int
    collection_revision: int
    state: str


SUPPORTED_SCHEMA_VERSION = 1


def _database_path(database: str | Path) -> Path:
    path = Path(database)
    if not path.is_file():
        raise StorageFailure("INVALID_ARGUMENT", "No collection database exists at this location.")
    return path.resolve(strict=True)


def connect_writer(database: str | Path) -> sqlite3.Connection:
    """Open the exclusive write connection for the calling storage thread."""
    path = _database_path(database)
    try:
        connection = sqlite3.connect(path, timeout=10.0, autocommit=True)
    except sqlite3.Error as error:
        raise StorageFailure("IO_FAILURE", "Unable to open the collection database.") from error
    _enable_foreign_keys(connection)
    return connection


def connect_reader(database: str | Path) -> sqlite3.Connection:
    """Open an independent read-only connection (used for verification and queries)."""
    path = _database_path(database)
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=10.0, autocommit=True
        )
        connection.execute("PRAGMA query_only=ON")
    except sqlite3.Error as error:
        raise StorageFailure("IO_FAILURE", "Unable to open the collection database for reading.") from error
    _enable_foreign_keys(connection)
    return connection


def _enable_foreign_keys(connection: sqlite3.Connection) -> None:
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        enabled = connection.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.Error as error:
        connection.close()
        raise StorageFailure("IO_FAILURE", "Foreign-key checks are unavailable.") from error
    if not enabled or enabled[0] != 1:
        connection.close()
        raise StorageFailure("IO_FAILURE", "Foreign-key checks could not be enabled.")


def begin(connection: sqlite3.Connection, *, immediate: bool = True) -> None:
    """Start an explicit transaction and confirm that it actually started."""
    statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
    try:
        connection.execute(statement)
    except sqlite3.Error as error:
        raise StorageFailure("IO_FAILURE", "The database transaction could not start.") from error
    if not connection.in_transaction:
        raise StorageFailure("IO_FAILURE", "The database transaction did not start.")


def commit(connection: sqlite3.Connection) -> None:
    """COMMIT explicitly; a raised error means the outcome is unknown."""
    try:
        connection.execute("COMMIT")
    except sqlite3.Error as error:
        raise StorageFailure(
            "COMMIT_UNCONFIRMED",
            "The database could not confirm the commit.",
            "Verify the saved command instead of repeating the write.",
            unknown=True,
        ) from error
    if connection.in_transaction:
        raise StorageFailure(
            "COMMIT_UNCONFIRMED",
            "The database still reports an open transaction after commit.",
            "Verify the saved command instead of repeating the write.",
            unknown=True,
        )


def rollback(connection: sqlite3.Connection) -> bool:
    """Best-effort ROLLBACK; True means the transaction is confirmed closed."""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        return False
    return not connection.in_transaction


def read_metadata(connection: sqlite3.Connection) -> CollectionMetadata:
    """Read and validate the singleton collection metadata row."""
    try:
        rows = connection.execute(
            "SELECT singletonId, collectionId, schemaVersion, collectionRevision, state"
            " FROM CollectionMeta"
        ).fetchall()
    except sqlite3.Error as error:
        raise StorageFailure("INVALID_ARGUMENT", "The collection metadata is unreadable.") from error
    if len(rows) != 1:
        raise StorageFailure("INVALID_ARGUMENT", "The collection must have exactly one metadata row.")
    singleton, collection_id, schema_version, collection_revision, state = rows[0]
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise StorageFailure(
            "SCHEMA_UNSUPPORTED",
            "The collection format version is not supported.",
            "Open a supported collection; no migration was performed.",
        )
    if singleton != 1 or state not in {"PREPARED", "READY"}:
        raise StorageFailure("INVALID_ARGUMENT", "The collection metadata is not valid.")
    try:
        require_identifier(collection_id, "Collection identity")
        require_version(collection_revision, "Collection revision", minimum=1)
    except RuleViolation as error:
        raise StorageFailure("INVALID_ARGUMENT", error.reason) from error
    return CollectionMetadata(collection_id, schema_version, collection_revision, state)


def require_ready(connection: sqlite3.Connection, expected_collection_id: str) -> CollectionMetadata:
    """Verify the identity and READY state before any saved-version operation."""
    metadata = read_metadata(connection)
    if metadata.state != "READY":
        raise StorageFailure("STATE_BLOCKED", "The collection is not ready for saved data.")
    if metadata.collection_id != expected_collection_id:
        raise StorageFailure(
            "STALE_CONTEXT",
            "The database belongs to a different collection than the request context.",
        )
    return metadata


def integrity_check(connection: sqlite3.Connection) -> None:
    try:
        quick = connection.execute("PRAGMA quick_check").fetchall()
        foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise StorageFailure("INVALID_ARGUMENT", "The collection integrity check could not run.") from error
    if quick != [("ok",)] or foreign:
        raise StorageFailure("INVALID_ARGUMENT", "The collection integrity check failed.")
