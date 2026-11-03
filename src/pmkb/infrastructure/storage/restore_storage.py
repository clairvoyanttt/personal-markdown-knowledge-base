"""C's ``RestoreStoragePort`` implementation (API09) for a verified snapshot.

Artifact layout (proposal pending A's publication port; see the delivery doc):
C creates the initialized collection as a private candidate file inside the
(empty) restore target directory and returns its absolute path as the opaque
``InitializedRestore.token``. A's ``publish`` must verify and atomically expose
that candidate as ``knowledge.sqlite`` without overwriting; C's ``release`` only
removes the candidate while its file identity still matches, never a formal
collection.
"""
from __future__ import annotations

import asyncio
import hashlib
import secrets
import stat
from pathlib import Path
from threading import Event

from pmkb.application.backup_restore.manifest import SnapshotFacts
from pmkb.application.backup_restore.restore_service import (
    InitializedRestore,
    RestoreCandidate,
    RestoreTarget,
)
from pmkb.application.output_support import OutputFailure
from pmkb.contracts.common import AppError
from pmkb.domain.rules import RuleViolation
from pmkb.infrastructure.storage import reader
from pmkb.infrastructure.storage.connection import StorageFailure
from pmkb.infrastructure.storage.restore_plan import (
    build_restore_payload,
    count_snapshot_references,
)
from pmkb.infrastructure.storage.writing import initialize_restored_collection


class CRestoreStorage:
    """Restore storage port backed by C's verified initialization transaction."""

    def __init__(self) -> None:
        self._owned: dict[str, tuple[int, int]] = {}

    # ---------------------------------------------------------------- API09 ports

    async def validate_snapshot(self, candidate: RestoreCandidate,
                                cancel: Event) -> SnapshotFacts:
        return await asyncio.to_thread(self._facts, candidate)

    async def initialize_restore(self, candidate: RestoreCandidate, target: RestoreTarget,
                                 cancel: Event) -> InitializedRestore:
        return await asyncio.to_thread(self._initialize, candidate, target)

    async def release(self, initialized: InitializedRestore) -> tuple[AppError, ...]:
        return await asyncio.to_thread(self._release, initialized)

    # ------------------------------------------------------------------ sync cores

    def _facts(self, candidate: RestoreCandidate) -> SnapshotFacts:
        path = Path(candidate.snapshot_path)
        manifest = candidate.manifest
        candidate.verify_unchanged()
        try:
            metadata = reader.metadata(path)
            snapshot = reader.capture_snapshot(path)
        except StorageFailure as error:
            raise OutputFailure(
                "INVALID_BACKUP", "The verified snapshot is not a readable collection.", error.action,
            ) from error
        if metadata.collection_id != manifest.source_collection_id:
            raise OutputFailure("INVALID_BACKUP", "The snapshot identity disagrees with the manifest.")
        if metadata.collection_revision != manifest.source_collection_revision:
            raise OutputFailure("INVALID_BACKUP", "The snapshot revision disagrees with the manifest.")
        digest, size = _sha256_size(path)
        return SnapshotFacts(
            schema_version=metadata.schema_version,
            source_collection_id=metadata.collection_id,
            source_collection_revision=metadata.collection_revision,
            saved_note_count=len(snapshot.notes),
            reference_counts=count_snapshot_references(snapshot),
            snapshot_sha256=digest,
            snapshot_size_bytes=size,
        )

    def _initialize(self, candidate: RestoreCandidate, target: RestoreTarget) -> InitializedRestore:
        facts = self._facts(candidate)
        directory = Path(target.directory)
        if not directory.is_dir():
            raise OutputFailure("INVALID_ARGUMENT", "The restore target must be an existing directory.")
        if any(directory.iterdir()):
            raise OutputFailure("TARGET_NOT_EMPTY", "The restore target is not empty.")
        snapshot = reader.capture_snapshot(candidate.snapshot_path)
        payload = build_restore_payload(snapshot, target.collection_id)
        if len(payload.notes) != facts.saved_note_count:
            raise OutputFailure("INVALID_BACKUP", "The snapshot note count changed during initialization.")
        candidate_path = directory / f".pmkb-restore-{secrets.token_hex(16)}.sqlite"
        try:
            result = initialize_restored_collection(
                candidate_path,
                new_collection_id=target.collection_id,
                notes=payload.notes,
                retained=payload.retained,
            )
        except StorageFailure as error:
            raise OutputFailure(error.code, error.reason, error.action, unknown=error.unknown) from error
        except RuleViolation as error:
            raise OutputFailure(error.code, error.reason, error.action) from error
        identity = _identity(candidate_path)
        if identity is None:
            raise OutputFailure(
                "IO_FAILURE", "The initialized restore artifact could not be confirmed.",
                "Inspect the target directory; do not treat the restore as complete.", unknown=True,
            )
        self._owned[str(candidate_path)] = identity
        return InitializedRestore(
            token=str(candidate_path),
            target_token=target.token,
            collection_id=target.collection_id,
            saved_note_count=facts.saved_note_count,
            initialized_note_count=result.initialized_notes,
            source_snapshot_sha256=facts.snapshot_sha256,
            collection_revision=result.collection_revision,
        )

    def _release(self, initialized: InitializedRestore) -> tuple[AppError, ...]:
        path = Path(initialized.token)
        identity = self._owned.pop(str(path), None)
        if identity is None:
            return ()
        if _identity(path) != identity:
            return (AppError(
                "STATE_BLOCKED",
                "The restore candidate was replaced and was left for inspection.",
                "Inspect the target directory; do not delete a formal collection.",
            ),)
        try:
            path.unlink()
        except OSError:
            return (AppError(
                "IO_FAILURE", "The restore candidate could not be removed.",
                "Inspect the target directory; a formal collection is never deleted here.",
            ),)
        return ()


def _identity(path: Path) -> tuple[int, int] | None:
    try:
        info = path.lstat()
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino)


def _sha256_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
