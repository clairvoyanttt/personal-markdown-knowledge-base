"""D's API08 saved-snapshot orchestration, with narrow B/C ports.

The existing public BackupRequest/BackupReply remain unchanged. The ports below
are D-internal integration drafts, NOT an implemented SQLite capture or Qt
session. C captures and validates a single frozen saved view and its provenance;
D checks those facts and the actual packaged bytes before publication. No SAVE,
API11 verification, source-path access, collection activation or automatic retry
is performed here. Explicit policies are technical budgets, not note limits.
"""
from dataclasses import dataclass, replace
import ntpath
from pathlib import Path
from threading import Event
from typing import Protocol

from pmkb.application.output_support import (
    OutputFailure, OutputSession, absolute_path, file_error, recheck_state, result,
    run_io, settle, validate_choice, validate_context, validate_state,
)
from pmkb.contracts.backup import (
    BackupReply, BackupRequest, FilePublicationState, UnsavedBackupChoice,
)
from pmkb.contracts.common import AppError, CallContext, OperationResult, ResultStatus
from pmkb.infrastructure.file_io.publication import PublicationResult
from pmkb.infrastructure.file_io.windows_names import validate_windows_basename
from pmkb.infrastructure.file_io.zip_guard import ArchivePolicy
from .manifest import (
    BackupManifest, ManifestPolicy, ManifestValidationError, SnapshotFacts,
    check_manifest_against_facts, validate_manifest,
)
from .package_components import publish_package


@dataclass(frozen=True, slots=True)
class SnapshotLease:
    """C-owned immutable saved view, with independently checked snapshot facts.

    Capture context, manifest provenance and facts must describe the SAME actual
    capture. The timestamp is from that capture, not a later packaging clock.
    A Path is a C-owned frozen file, never a user database to remove. release()
    owns its controlled cleanup. C must clean failed captures for which no lease
    is returned; D cannot guess a resource path from an exception.
    """
    context: CallContext
    frozen_snapshot: bytes | Path
    manifest: BackupManifest
    facts: SnapshotFacts


class SnapshotSourcePort(Protocol):
    async def capture_saved(self, context: CallContext, cancel: Event) -> SnapshotLease:
        """Capture saved data on C's owning worker; never include live drafts."""
        ...

    async def release(self, snapshot: SnapshotLease) -> tuple[AppError, ...]:
        """Release only C's own freeze resources; never remove the source DB."""
        ...


def _error(code: str, reason: str, action: str = "Review the operation before choosing another target.") -> AppError:
    return AppError(code, reason, action)


def _unknown_error() -> AppError:
    return _error("IO_FAILURE", "The backup file publication outcome is unknown.",
                  "Inspect the reported target; do not replay, overwrite, delete it or use SAVE verification.")


def _canonical_target(target: Path) -> Path:
    """Match the file layer's parent spelling without following the final leaf.

    Windows TEMP may use an 8.3 parent alias. Resolve that existing directory
    before publication so the receipt can still be checked by exact path.
    A failure here proves publication has not been attempted.
    """
    parent = target.parent.resolve(strict=True)
    if not parent.is_dir():
        raise NotADirectoryError("Output parent must be an existing directory.")
    return parent / target.name


class BackupServiceImpl:
    """API08 implementation; public DTOs are stable, these injected ports are not.

    Session acknowledgements must be bound by B to this operation's decision,
    collection and exact dirty versions. Capture and publication are settled
    before cancellation returns. A late result retains its original delivery
    context, so B must not display it as output of a newly active collection.
    """

    def __init__(self, session: OutputSession, source: SnapshotSourcePort,
                 manifest_policy: ManifestPolicy, archive_policy: ArchivePolicy):
        if not isinstance(manifest_policy, ManifestPolicy) or not isinstance(archive_policy, ArchivePolicy):
            raise ValueError("Explicit manifest and archive policies are required.")
        entries = frozenset({"manifest.json", "snapshot.sqlite"})
        if (archive_policy.allowed_entries != entries or archive_policy.required_entries != entries
                or archive_policy.max_entries < 2):
            raise ValueError("The D backup profile requires exactly manifest.json and snapshot.sqlite.")
        if not callable(getattr(session, "inspect", None)):
            raise ValueError("A captured output-session port is required.")
        if not all(callable(getattr(source, method, None)) for method in ("capture_saved", "release")):
            raise ValueError("A controlled snapshot source and release port are required.")
        self._session, self._source = session, source
        self._manifest_policy, self._archive_policy = manifest_policy, archive_policy

    def _validate_snapshot(self, snapshot: SnapshotLease, context: CallContext) -> None:
        if not isinstance(snapshot, SnapshotLease):
            raise OutputFailure("STATE_BLOCKED", "The source did not return a controlled saved snapshot.")
        validate_context(snapshot.context)
        if snapshot.context != context:
            raise OutputFailure("STALE_CONTEXT", "The captured snapshot belongs to another context.")
        if type(snapshot.frozen_snapshot) is not bytes and not isinstance(snapshot.frozen_snapshot, Path):
            raise OutputFailure("STATE_BLOCKED", "The source did not provide immutable bytes or a frozen file.")
        try:
            validate_manifest(snapshot.manifest, self._manifest_policy)
            check_manifest_against_facts(snapshot.manifest, snapshot.facts, self._manifest_policy)
        except (ManifestValidationError, TypeError, ValueError) as error:
            raise OutputFailure("STATE_BLOCKED", "Captured metadata differs from the checked saved snapshot.",
                                "Ask the source adapter to recheck the same saved capture; nothing was published.") from error
        if snapshot.manifest.source_collection_id != context.collection_id:
            raise OutputFailure("STALE_CONTEXT", "The snapshot source identity does not match this operation.")

    @staticmethod
    def _reply(snapshot: SnapshotLease, target: Path, state: FilePublicationState) -> BackupReply:
        manifest = snapshot.manifest
        return BackupReply(str(target), manifest.source_collection_id, manifest.source_location,
                           manifest.source_collection_revision, manifest.saved_note_count,
                           manifest.backup_captured_at_utc, state)

    async def _release(self, snapshot: SnapshotLease, signal: Event) -> tuple[AppError, ...]:
        try:
            warnings = await settle(self._source.release(snapshot), signal)
            if type(warnings) is not tuple or any(not isinstance(item, AppError) for item in warnings):
                raise ValueError("The source supplied an invalid cleanup result.")
            return warnings
        except Exception:
            return (_error("SNAPSHOT_RETAINED", "Snapshot resource release could not be confirmed.",
                           "Ask the source adapter to inspect only its own frozen resources; keep the backup and source database."),)

    async def backup_collection(self, request: BackupRequest,
                                cancel: Event | None = None) -> OperationResult[BackupReply]:
        context = (request.context if isinstance(request, BackupRequest)
                   and isinstance(request.context, CallContext) else CallContext(None, 0))
        if cancel is not None and not isinstance(cancel, Event):
            return result(context, ResultStatus.ERROR,
                          error=_error("INVALID_ARGUMENT", "Cancellation must be threading.Event or None."))
        signal = cancel if cancel is not None else Event()
        snapshot = None
        outcome = None
        try:
            if not isinstance(request, BackupRequest):
                raise OutputFailure("INVALID_ARGUMENT", "A typed backup request is required.")
            validate_context(request.context)
            if not isinstance(request.unsaved_choice, UnsavedBackupChoice):
                raise OutputFailure("INVALID_ARGUMENT", "Choose how to handle unsaved edits.")
            target = absolute_path(request.destination_file)
            try:
                validate_windows_basename(ntpath.basename(request.destination_file))
                validate_windows_basename(target.name)
            except ValueError as error:
                raise OutputFailure("INVALID_NAME", "Choose a valid complete backup filename.") from error
            if signal.is_set() or request.unsaved_choice is UnsavedBackupChoice.RETURN_TO_SAVE:
                return result(context, ResultStatus.CANCELLED)

            before = await settle(self._session.inspect(), signal)
            validate_state(before, context)
            validate_choice(before, request.unsaved_choice)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            target = await run_io(lambda: _canonical_target(target), signal)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            # A second capture catches new drafts after an initial NO_UNSAVED or
            # saved-only decision, before C starts freezing saved data.
            latest = await settle(self._session.inspect(), signal)
            recheck_state(before, latest, request.unsaved_choice)
            if signal.is_set():
                return result(context, ResultStatus.CANCELLED)
            captured = await settle(self._source.capture_saved(context, signal), signal)
            if isinstance(captured, SnapshotLease):
                snapshot = captured  # Release even a structurally invalid lease.
            self._validate_snapshot(captured, context)
            if signal.is_set():
                outcome = result(context, ResultStatus.CANCELLED)
            else:
                after = await settle(self._session.inspect(), signal)
                recheck_state(before, after, request.unsaved_choice)
                if signal.is_set():
                    outcome = result(context, ResultStatus.CANCELLED)
                else:
                    # From dispatch onward a lost acknowledgement is UNKNOWN,
                    # never a reason to replay or delete the selected target.
                    try:
                        published = await run_io(lambda: publish_package(
                            target, snapshot.frozen_snapshot, snapshot.manifest,
                            self._manifest_policy, self._archive_policy, signal), signal)
                        if (not isinstance(published, PublicationResult)
                                or not isinstance(published.status, ResultStatus)
                                or not isinstance(published.destination, Path)
                                or not published.destination.is_absolute()
                                or published.destination != target
                                or type(published.byte_count) is not int or published.byte_count < 0
                                or (published.status is ResultStatus.OK
                                    and not 0 < published.byte_count <= self._archive_policy.max_archive_bytes)
                                or (published.temporary_path is not None
                                    and not isinstance(published.temporary_path, Path))
                                or (published.status in {ResultStatus.OK, ResultStatus.CANCELLED}
                                    and published.error is not None)
                                or (published.status in {ResultStatus.ERROR, ResultStatus.UNCONFIRMED}
                                    and not isinstance(published.error, AppError))
                                or type(published.warnings) is not tuple
                                or any(not isinstance(item, AppError) for item in published.warnings)):
                            raise ValueError("Invalid publication acknowledgement.")
                    except Exception:
                        outcome = result(context, ResultStatus.UNCONFIRMED,
                            self._reply(snapshot, target, FilePublicationState.UNKNOWN), _unknown_error())
                    else:
                        warnings = published.warnings
                        if published.status is ResultStatus.OK and published.error is None:
                            outcome = result(context, ResultStatus.OK,
                                self._reply(snapshot, published.destination, FilePublicationState.PUBLISHED),
                                warnings=warnings)
                        elif published.status is ResultStatus.CANCELLED:
                            outcome = result(context, ResultStatus.CANCELLED, warnings=warnings)
                        elif published.status is ResultStatus.ERROR:
                            outcome = result(context, ResultStatus.ERROR,
                                error=file_error(published.error), warnings=warnings)
                        else:
                            outcome = result(context, ResultStatus.UNCONFIRMED,
                                self._reply(snapshot, published.destination, FilePublicationState.UNKNOWN),
                                _unknown_error(), warnings)
        except OutputFailure as error:
            mapped = error.error
            if error.unknown:
                mapped = _error("STATE_BLOCKED", "A source/session operation lost its outcome; backup publication was not attempted.",
                                "Ask that adapter to settle or inspect its own operation; no backup reply facts were fabricated.")
            outcome = result(context, ResultStatus.ERROR, error=mapped)
        except Exception:
            outcome = result(context, ResultStatus.ERROR,
                error=_error("IO_FAILURE", "The saved snapshot could not be prepared; backup publication was not attempted."))

        # No finally-return can erase a known file outcome. Cleanup delegates
        # only C-owned resources and adds warnings, never deletes arbitrary Paths.
        if snapshot is not None:
            warnings = await self._release(snapshot, signal)
            if warnings:
                outcome = replace(outcome, warnings=outcome.warnings + warnings)
        return outcome
