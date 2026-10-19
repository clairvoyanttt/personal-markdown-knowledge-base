"""Complete-file, no-overwrite publication for D's local file layer.

This is NOT API07/08/09 or a database snapshot service. Call on a worker thread,
with an already selected target and immutable bytes from a trusted producer.
It does not create parent directories, activate collections, or retry writes.
The supported threat model is ordinary local I/O faults and competing writers,
not a hostile process replacing ancestor directories during a system call.
"""
from collections.abc import Iterable
from dataclasses import dataclass
import os
import ntpath
from pathlib import Path
import stat
import tempfile
from threading import Event

from pmkb.contracts.common import AppError, ResultStatus
from .windows_names import validate_windows_basename


@dataclass(frozen=True, slots=True)
class PublicationResult:
    """D-internal receipt; not a confirmed cross-module API.

    UNCONFIRMED means inspect, do not replay or clean up either candidate path.
    temporary_path may no longer exist or may have changed owner; its presence
    is NEVER permission to delete it. OK proves publication, not power-loss
    durability, database validity, or collection activation.
    """
    status: ResultStatus
    destination: Path
    byte_count: int = 0
    temporary_path: Path | None = None
    error: AppError | None = None
    warnings: tuple[AppError, ...] = ()


def _error(code: str, reason: str) -> AppError:
    return AppError(code, reason, "Inspect the result and choose a new target; do not automatically retry.")


def _is_owned_file(path: Path, identity: tuple[int, int]) -> bool:
    try:
        observed = path.lstat()
    except OSError:
        return False
    reparse = getattr(observed, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return (not reparse and stat.S_ISREG(observed.st_mode)
            and observed.st_ino != 0 and (observed.st_dev, observed.st_ino) == identity)


def _cleanup_owned(path: Path, identity: tuple[int, int]) -> bool:
    """Remove only an owned staging name, NEVER a formal destination.

    Used before publication or to remove the extra POSIX staging hard link
    after a known successful publication. Never used for unknown outcomes.
    """
    if not _is_owned_file(path, identity):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def _publish_no_replace(source: Path, target: Path) -> bool:
    """Return whether the staging name was consumed. Never use os.replace.

    Windows rename refuses an existing destination. POSIX rename does not, so
    the non-product POSIX test path uses an atomic, exclusive hard-link create.
    Unsupported filesystems fail safely; there is no copy/overwrite fallback.
    """
    if os.name == "nt":
        os.rename(source, target)
        return True
    os.link(source, target, follow_symlinks=False)
    return False


def publish_bytes(destination: str | os.PathLike[str], payload: bytes,
                  cancel: Event | None = None) -> PublicationResult:
    if type(payload) is not bytes:
        raise ValueError("Expected immutable bytes")
    return publish_chunks(destination, (payload,), cancel)


def publish_chunks(destination: str | os.PathLike[str], chunks: Iterable[bytes],
                   cancel: Event | None = None) -> PublicationResult:
    """Stage/flush/fsync/close before a single exclusive publication attempt.

    Invalid arguments raise ValueError before I/O. Ordinary staging failures
    return ERROR. Once the publish syscall is entered, exceptions other than
    a definite destination-exists rejection conservatively return UNCONFIRMED.
    Cancellation is observed before publication, never interpreted as rollback.
    A producer must release any source handles itself; this function owns only
    its mkstemp file. Chunks must be immutable bytes; no newline conversion.
    """
    try:
        raw = os.fspath(destination)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("Expected a nonempty text destination")
        # Check the literal final component BEFORE abspath normalizes away a
        # trailing separator or '/.'. Never turn directory syntax into a file.
        validate_windows_basename(ntpath.basename(raw))
        target = Path(os.path.abspath(raw))
        validate_windows_basename(target.name)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid output destination") from error
    if cancel is not None and not isinstance(cancel, Event):
        raise ValueError("Expected threading.Event or None")
    # Validate the iterable before allocating a temporary file.
    try:
        iterator = iter(chunks)
    except TypeError as error:
        raise ValueError("Expected an iterable of immutable bytes") from error
    if cancel is not None and cancel.is_set():
        return PublicationResult(ResultStatus.CANCELLED, target)

    staging: Path | None = None
    identity: tuple[int, int] | None = None
    byte_count = 0

    def finish_unpublished(status: ResultStatus, error: AppError | None = None) -> PublicationResult:
        retained = None
        warnings = ()
        if staging is not None and (identity is None or not _cleanup_owned(staging, identity)):
            retained = staging
            warnings = (_error("TEMPORARY_RETAINED", "Temporary path could not be safely cleaned; inspect its ownership."),)
        return PublicationResult(status, target, byte_count, retained, error, warnings)

    try:
        # Resolve only the existing parent, not the final component. A final
        # symlink/directory/file must be rejected, not followed or overwritten.
        parent = target.parent.resolve(strict=True)
        if not parent.is_dir():
            raise NotADirectoryError("Output parent is not a directory")
        target = parent / target.name
        if os.path.lexists(target):
            return PublicationResult(ResultStatus.ERROR, target,
                                     error=_error("TARGET_EXISTS", "The selected destination already exists."))
        descriptor, temporary_name = tempfile.mkstemp(prefix=".pmkb-", suffix=".tmp", dir=parent)
        staging = Path(temporary_name)
        try:
            observed = os.fstat(descriptor)
            identity = (observed.st_dev, observed.st_ino)
            stream = os.fdopen(descriptor, "wb")
            descriptor = None  # fdopen owns it from here, including close errors.
        finally:
            if descriptor is not None:
                os.close(descriptor)
        with stream:
            for chunk in iterator:
                if cancel is not None and cancel.is_set():
                    break
                if type(chunk) is not bytes:
                    raise ValueError("Producer yielded a non-bytes chunk")
                count = stream.write(chunk)
                byte_count += count
                if count != len(chunk):
                    raise OSError("Incomplete staging write")
            if cancel is None or not cancel.is_set():
                stream.flush()
                os.fsync(stream.fileno())
    except Exception:
        return finish_unpublished(ResultStatus.ERROR, _error("IO_FAILURE", "Could not complete the staged file; nothing was published."))

    if cancel is not None and cancel.is_set():
        return finish_unpublished(ResultStatus.CANCELLED)
    assert staging is not None and identity is not None
    if not _is_owned_file(staging, identity):
        return finish_unpublished(ResultStatus.ERROR, _error("STATE_BLOCKED", "Staging file identity changed; publication was not attempted."))
    try:
        consumed = _publish_no_replace(staging, target)
    except FileExistsError:
        return finish_unpublished(ResultStatus.ERROR, _error("TARGET_EXISTS", "Another writer already owns the selected destination."))
    except Exception:
        # Never unlink target or staging here: publication may have succeeded
        # even when acknowledgement failed. This is not SAVE/API11 evidence.
        return PublicationResult(ResultStatus.UNCONFIRMED, target, byte_count, staging,
                                 _error("PUBLICATION_UNCONFIRMED", "Publication outcome is unknown; retain both paths for inspection."))

    # Late cancellation does not undo a known success. POSIX hard-link cleanup
    # failure is a warning, not an excuse to delete the now-formal destination.
    if not consumed and not _cleanup_owned(staging, identity):
        return PublicationResult(ResultStatus.OK, target, byte_count, staging,
                                 warnings=(_error("TEMPORARY_RETAINED", "File is published but its staging link remains."),))
    return PublicationResult(ResultStatus.OK, target, byte_count)
