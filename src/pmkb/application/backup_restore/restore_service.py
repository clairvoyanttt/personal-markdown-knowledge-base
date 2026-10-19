"""Executable D-side API09 draft; C/A adapters are required, never simulated.

The source candidate is verified bytes, NOT a valid SQLite collection. C must
open a checked handle, validate business facts, preserve note identities and
invalid references, remove old receipts and create RESTORE_INIT plus each
RESTORE_NOTE_INIT. A owns identity reservation and final no-overwrite publication.
Only the injected ports can supply those real effects. This module never opens
SQLite, changes the active collection or calls a SAVE/commit resolver.
"""
import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import re
import stat
from threading import Event
from typing import Protocol

from pmkb.application.output_support import (
    OutputFailure, OutputSession, absolute_path, result, run_io, settle,
    validate_context, validate_state,
)
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy
from .candidate import CandidateFailure, RestoreCandidate, prepare_restore_candidate
from .manifest import ManifestPolicy, ManifestValidationError, SnapshotFacts, check_manifest_against_facts


@dataclass(frozen=True, slots=True)
class RestoreRequest:
    context: CallContext
    source_file: str
    destination_directory: str


class RestoreStage(StrEnum):
    PREPARATION_UNKNOWN = "PREPARATION_UNKNOWN"
    INITIALIZATION_UNKNOWN = "INITIALIZATION_UNKNOWN"
    PUBLICATION_UNKNOWN = "PUBLICATION_UNKNOWN"
    PUBLISHED = "PUBLISHED"


@dataclass(frozen=True, slots=True)
class RestoreReply:
    destination_directory: str
    collection_id: str | None
    saved_note_count: int
    source_collection_id: str
    source_location: str
    stage: RestoreStage
    candidate_path: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreTarget:
    """A-owned token, not permission for D to delete its directory."""
    token: str
    directory: str
    collection_id: str


@dataclass(frozen=True, slots=True)
class InitializedRestore:
    """C's checked receipt, not a replacement for checking actual DB contents.

    C guarantees source preservation, READY output, revision 1, old receipts
    removed and lastCommitId bound to each note's own new initialization.
    D checks the report's binding/counts; tests of this DTO do not prove SQLite.
    """
    token: str
    target_token: str
    collection_id: str
    saved_note_count: int
    initialized_note_count: int
    source_snapshot_sha256: str
    collection_revision: int


@dataclass(frozen=True, slots=True)
class RestorePublication:
    status: ResultStatus
    destination_directory: str
    collection_id: str
    error: AppError | None = None
    warnings: tuple[AppError, ...] = ()


class RestoreStoragePort(Protocol):
    async def validate_snapshot(self, candidate: RestoreCandidate, cancel: Event) -> SnapshotFacts:
        """C checks its own source handle and actual rows; no sourceLocation I/O."""
        ...

    async def initialize_restore(self, candidate: RestoreCandidate, target: RestoreTarget,
                                 cancel: Event) -> InitializedRestore:
        """Build and validate C-owned output; do not mutate the source candidate.

        Preserve IDs and invalid states, convert only shared-parser-authorized
        ownership spans, reserve missing/deleted identities, create fresh init
        receipts and refuse conflicting business mirrors. If a reply is lost,
        retain C's output for inspection. A known failure cleans C-owned work.
        """
        ...

    async def release(self, initialized: InitializedRestore) -> tuple[AppError, ...]:
        """Release only known C-owned extras, never a possibly formal file."""
        ...


class RestoreTargetPort(Protocol):
    async def prepare(self, destination: Path, source_id: str, cancel: Event) -> RestoreTarget:
        """A reserves a fresh nonconflicting ID and an empty target ownership token."""
        ...

    async def publish(self, target: RestoreTarget, initialized: InitializedRestore,
                      cancel: Event) -> RestorePublication:
        """Recheck empty-target/ownership/READY output then publish once.

        Reuse A's file/collection boundary. Do not auto-open or activate. Return
        UNCONFIRMED if formal effects cannot be determined; never replay writes.
        """
        ...

    async def release(self, target: RestoreTarget, published: bool) -> tuple[AppError, ...]:
        """Only owned unpublished extras; a published valid collection survives."""
        ...


def _token(value) -> bool:
    return (isinstance(value, str) and bool(value)
            and not any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value))


def _count(value) -> bool:
    return type(value) is int and 0 <= value < 2 ** 63


def _preflight(destination: Path, work_parent: Path) -> None:
    # A will repeat the empty check under its actual prepare/publish boundary.
    target, parent = destination.resolve(strict=False), work_parent.resolve(strict=True)
    if parent == target or parent.is_relative_to(target):
        raise OutputFailure("INVALID_ARGUMENT", "Candidate work must be outside the restore target.")
    try:
        info = destination.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OutputFailure("TARGET_NOT_EMPTY", "Restore requires an ordinary empty directory.")
    if next(destination.iterdir(), None) is not None:
        raise OutputFailure("TARGET_NOT_EMPTY", "The restore target is not empty.")


class RestoreService:
    """D workflow on one service loop; ports are drafts pending A/B/C agreement."""
    def __init__(self, session: OutputSession, storage: RestoreStoragePort, targets: RestoreTargetPort,
                 work_parent: Path, manifest_policy: ManifestPolicy, archive_policy: ArchivePolicy):
        if not isinstance(work_parent, Path) or not work_parent.is_absolute():
            raise ValueError("Use an explicit trusted absolute candidate parent.")
        if not isinstance(manifest_policy, ManifestPolicy) or not isinstance(archive_policy, ArchivePolicy):
            raise ValueError("Explicit resource/version policies are required.")
        entries = frozenset({"manifest.json", "snapshot.sqlite"})
        if (archive_policy.allowed_entries != entries or archive_policy.required_entries != entries
                or archive_policy.max_entries < 2):
            raise ValueError("Restore uses the explicit two-entry D profile.")
        if not callable(getattr(session, "inspect", None)):
            raise ValueError("A real captured-session port is required.")
        if not all(callable(getattr(storage, method, None))
                   for method in ("validate_snapshot", "initialize_restore", "release")):
            raise ValueError("Controlled C validation, initialization and release ports are required.")
        if not all(callable(getattr(targets, method, None)) for method in ("prepare", "publish", "release")):
            raise ValueError("Controlled A target preparation, publication and release ports are required.")
        self.session, self.storage, self.targets = session, storage, targets
        self.work_parent, self.manifest_policy, self.archive_policy = work_parent, manifest_policy, archive_policy
        self._lock = asyncio.Lock()

    async def restore_backup(self, request: RestoreRequest, cancel: Event | None = None) -> OperationResult[RestoreReply]:
        if not isinstance(request, RestoreRequest):
            raise TypeError("Expected the D-draft RestoreRequest.")
        if cancel is not None and not isinstance(cancel, Event):
            raise ValueError("cancel must be threading.Event or None.")
        signal = cancel if cancel is not None else Event()
        await settle(self._lock.acquire(), signal)
        try:
            return await self._restore(request, signal)
        finally:
            self._lock.release()

    async def _restore(self, request: RestoreRequest, signal: Event) -> OperationResult[RestoreReply]:
        context = request.context
        candidate = target = initialized = None
        phase, uncertain, published = "input", False, False
        warnings = []
        status, value, error = ResultStatus.ERROR, None, None

        async def check_session():
            state = await settle(self.session.inspect(), signal)
            validate_state(state, context, require_active=False)

        def cancelled():
            if signal.is_set():
                raise OutputFailure("OUTPUT_CANCELLED", "Restore was cancelled before publication.")

        def reply(stage):
            return RestoreReply(str(destination), target.collection_id if isinstance(target, RestoreTarget) else None,
                candidate.manifest.saved_note_count, candidate.manifest.source_collection_id,
                candidate.manifest.source_location, stage,
                str(candidate.snapshot_path) if stage is not RestoreStage.PUBLISHED else None)

        try:
            validate_context(context, require_active=False)
            source = absolute_path(request.source_file)
            destination = absolute_path(request.destination_directory)
            cancelled()
            await check_session()
            await run_io(lambda: _preflight(destination, self.work_parent), signal)
            cancelled()
            phase = "candidate"
            candidate = await run_io(lambda: prepare_restore_candidate(source, self.work_parent,
                self.manifest_policy, self.archive_policy, signal), signal)
            await run_io(candidate.verify_unchanged, signal)
            facts = await settle(self.storage.validate_snapshot(candidate, signal), signal)
            check_manifest_against_facts(candidate.manifest, facts, self.manifest_policy)
            await run_io(candidate.verify_unchanged, signal)
            await check_session()
            cancelled()
            phase = "prepare"
            target = await settle(self.targets.prepare(destination, candidate.manifest.source_collection_id, signal), signal)
            if (not isinstance(target, RestoreTarget) or not _token(target.token)
                    or not isinstance(target.collection_id, str)
                    or re.fullmatch(r"[0-9a-f]{32}", target.collection_id) is None
                    or target.collection_id == candidate.manifest.source_collection_id
                    or absolute_path(target.directory) != destination):
                raise OutputFailure("STATE_BLOCKED", "Target preparation supplied an invalid identity or location.")
            phase = "prepared"
            await check_session()
            cancelled()
            phase = "initialize"
            initialized = await settle(self.storage.initialize_restore(candidate, target, signal), signal)
            phase = "initialized"
            if (not isinstance(initialized, InitializedRestore) or not _token(initialized.token)
                    or initialized.target_token != target.token or initialized.collection_id != target.collection_id
                    or not _count(initialized.saved_note_count) or not _count(initialized.initialized_note_count)
                    or type(initialized.collection_revision) is not int or initialized.collection_revision != 1
                    or initialized.saved_note_count != candidate.manifest.saved_note_count
                    or initialized.initialized_note_count != initialized.saved_note_count
                    or initialized.source_snapshot_sha256 != candidate.manifest.snapshot_sha256):
                raise OutputFailure("INVALID_BACKUP", "Restore initialization report is inconsistent with the checked input.")
            await run_io(candidate.verify_unchanged, signal)
            await check_session()
            cancelled()
            phase = "publish"
            publication = await settle(self.targets.publish(target, initialized, signal), signal)
            if (not isinstance(publication, RestorePublication)
                    or not isinstance(publication.status, ResultStatus)
                    or publication.destination_directory != target.directory
                    or publication.collection_id != target.collection_id
                    or publication.status not in {ResultStatus.OK, ResultStatus.ERROR,
                                                  ResultStatus.CANCELLED, ResultStatus.UNCONFIRMED}
                    or type(publication.warnings) is not tuple
                    or any(not isinstance(item, AppError) for item in publication.warnings)
                    or (publication.status is ResultStatus.OK and publication.error is not None)
                    or (publication.status is ResultStatus.CANCELLED and publication.error is not None)
                    or (publication.status in {ResultStatus.ERROR, ResultStatus.UNCONFIRMED}
                        and not isinstance(publication.error, AppError))):
                raise OutputFailure("IO_FAILURE", "Publication returned an invalid receipt; inspect the target.", unknown=True)
            warnings.extend(publication.warnings)
            if publication.status is ResultStatus.UNCONFIRMED:
                raise OutputFailure("IO_FAILURE", "Restore publication could not be confirmed.", unknown=True)
            if publication.status is ResultStatus.OK:
                published, status, value, phase = True, ResultStatus.OK, reply(RestoreStage.PUBLISHED), "published"
            elif publication.status is ResultStatus.CANCELLED:
                status, phase = ResultStatus.CANCELLED, "not_published"
            else:
                error = publication.error
                phase = "not_published"
        except OutputFailure as failure:
            if failure.unknown and candidate is not None and phase in {"prepare", "initialize", "publish"}:
                uncertain = True
                stages = {"prepare": RestoreStage.PREPARATION_UNKNOWN,
                          "initialize": RestoreStage.INITIALIZATION_UNKNOWN,
                          "publish": RestoreStage.PUBLICATION_UNKNOWN}
                status, value, error = ResultStatus.UNCONFIRMED, reply(stages[phase]), failure.error
            elif failure.error.code == "OUTPUT_CANCELLED":
                status, error = ResultStatus.CANCELLED, None
            else:
                error = failure.error
        except CandidateFailure as failure:
            warnings.extend(failure.warnings)
            if failure.cancelled:
                status = ResultStatus.CANCELLED
            else:
                error = AppError("IO_FAILURE" if failure.error.code == "RESTORE_CANDIDATE_IO" else "INVALID_BACKUP",
                                 "The backup or candidate could not be read and verified safely.",
                                 "Inspect the backup and choose a valid source and writable work location.")
        except ManifestValidationError:
            error = AppError("INVALID_BACKUP", "The backup manifest disagrees with independently checked snapshot facts.",
                             "Use a consistent supported backup; do not repair identity by name.")
        except Exception:
            if candidate is not None and phase in {"prepare", "initialize", "publish"}:
                uncertain = True
                stages = {"prepare": RestoreStage.PREPARATION_UNKNOWN,
                          "initialize": RestoreStage.INITIALIZATION_UNKNOWN,
                          "publish": RestoreStage.PUBLICATION_UNKNOWN}
                status, value = ResultStatus.UNCONFIRMED, reply(stages[phase])
                error = AppError("IO_FAILURE", "A restore effect lost its receipt; relevant candidates were retained.",
                                 "Inspect the reported stage and target; do not automatically replay or delete them.")
            else:
                error = AppError("IO_FAILURE", "Restore preparation could not be completed.", "Review the source and target; existing data was not intentionally replaced.")
        finally:
            if not uncertain:
                cleanup_calls = []
                if initialized is not None:
                    cleanup_calls.append(lambda: self.storage.release(initialized))
                if target is not None:
                    cleanup_calls.append(lambda: self.targets.release(target, published))
                for release in cleanup_calls:
                    try:
                        released = await settle(release(), signal)
                        if type(released) is not tuple or any(not isinstance(item, AppError) for item in released):
                            raise ValueError("Invalid release receipt.")
                        warnings.extend(released)
                    except Exception:
                        warnings.append(AppError("IO_FAILURE", "Owned restore extras could not be confirmed as cleaned.",
                                                 "Inspect retained resources; do not delete a formal collection."))
                if candidate is not None:
                    try:
                        warnings.extend(await run_io(candidate.cleanup, signal))
                    except Exception:
                        warnings.append(AppError("IO_FAILURE", "The input candidate could not be safely cleaned.", "Inspect only the operation's retained candidate paths."))
        return result(context, status, value, error, tuple(warnings))
