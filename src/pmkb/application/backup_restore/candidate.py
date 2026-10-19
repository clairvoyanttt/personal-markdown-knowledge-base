"""Private, ownership-bound extraction for D's restore preparation only.

One guarded source handle supplies both the verified bytes and the candidate.
There is no extractall, second source open, SQLite access, collection creation,
receipt initialization, publication or activation. The caller must supply a
trusted existing parent; hostile ancestor replacement is outside this boundary.
The candidate is NOT an initialized or valid restored collection.
"""
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import stat
import tempfile
from threading import Event
import zipfile
import zlib

from pmkb.contracts.common import AppError
from pmkb.infrastructure.file_io.zip_guard import (
    ArchiveEntryInspection, ArchiveInspection, ArchivePolicy, ArchiveValidationError,
    _source_is_regular, _validate_metadata, _verify_member_stream,
)
from .manifest import BackupManifest, ManifestPolicy, ManifestValidationError, decode_manifest


_ENTRIES = frozenset({"manifest.json", "snapshot.sqlite"})


def _error(code: str, reason: str) -> AppError:
    return AppError(code, reason, "Inspect retained paths; do not treat this candidate as a restored collection.")


class CandidateFailure(Exception):
    def __init__(self, error: AppError, warnings: tuple[AppError, ...] = (), *, cancelled: bool = False):
        super().__init__(error.reason)
        self.error = error
        self.warnings = tuple(warnings)
        self.cancelled = cancelled


def _identity(details: os.stat_result) -> tuple[int, int]:
    if details.st_ino == 0:
        raise OSError("Filesystem does not expose a usable ownership identity.")
    return details.st_dev, details.st_ino


def _is_directory(details: os.stat_result) -> bool:
    return stat.S_ISDIR(details.st_mode) and not getattr(details, "st_file_attributes", 0) & 0x400


def _matches(path: Path, identity: tuple[int, int], *, directory: bool = False) -> bool:
    try:
        details = path.lstat()
        return ((_is_directory(details) if directory else _source_is_regular(details))
                and details.st_ino != 0 and (details.st_dev, details.st_ino) == identity)
    except OSError:
        return False


def _unchanged(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (os.path.samestat(expected, actual) and expected.st_size == actual.st_size
            and expected.st_mtime_ns == actual.st_mtime_ns)


def _cancel(event: Event | None) -> None:
    if event is not None and event.is_set():
        raise CandidateFailure(_error("RESTORE_CANDIDATE_CANCELLED", "Candidate preparation was cancelled."), cancelled=True)


@dataclass(slots=True)
class _OwnedArea:
    directory: Path
    directory_identity: tuple[int, int] | None
    snapshot: Path
    snapshot_identity: tuple[int, int] | None = None
    snapshot_removed: bool = False
    directory_removed: bool = False

    def cleanup(self) -> tuple[AppError, ...]:
        warnings = []
        if self.directory_removed:
            return ()
        if self.directory_identity is None:
            return (_error("RESTORE_CANDIDATE_RETAINED", f"Allocated candidate directory ownership could not be confirmed; retained: {self.directory}"),)
        if not _matches(self.directory, self.directory_identity, directory=True):
            if not os.path.lexists(self.directory):
                self.directory_removed = True
                return ()
            return (_error("RESTORE_CANDIDATE_RETAINED", "Candidate directory identity changed; nothing was removed."),)
        if not self.snapshot_removed:
            if not os.path.lexists(self.snapshot):
                self.snapshot_removed = True
            elif self.snapshot_identity is not None and _matches(self.snapshot, self.snapshot_identity):
                try:
                    self.snapshot.unlink()
                    self.snapshot_removed = True
                except OSError:
                    warnings.append(_error("RESTORE_CANDIDATE_RETAINED", "Owned candidate file could not be removed."))
            else:
                warnings.append(_error("RESTORE_CANDIDATE_RETAINED", "Candidate file identity changed; it was not removed."))
        # rmdir is deliberately non-recursive and refuses any unfamiliar child.
        if _matches(self.directory, self.directory_identity, directory=True):
            try:
                self.directory.rmdir()
                self.directory_removed = True
            except OSError:
                warnings.append(_error("RESTORE_CANDIDATE_RETAINED", "Candidate directory is nonempty or could not be removed."))
        else:
            warnings.append(_error("RESTORE_CANDIDATE_RETAINED", "Candidate directory identity changed before cleanup."))
        return tuple(warnings)


class RestoreCandidate:
    """Controlled unmodified input; cleanup never deletes moved or foreign files.

    verify_unchanged rechecks identity, content hash and size before a controlled
    consumer uses the snapshot. It does not validate SQLite or reserve the path
    against a hostile writer after the check. C must own its own checked handle
    and initialization boundary; this helper cannot authorize unchecked reopens.
    """
    __slots__ = ("_area", "_manifest", "_archive", "_snapshot_stat", "_chunk_size")

    def __init__(self, area: _OwnedArea, manifest: BackupManifest,
                 archive: ArchiveInspection, snapshot_stat: os.stat_result, chunk_size: int):
        self._area, self._manifest, self._archive = area, manifest, archive
        self._snapshot_stat, self._chunk_size = snapshot_stat, chunk_size

    @property
    def snapshot_path(self) -> Path:
        return self._area.snapshot

    @property
    def manifest(self) -> BackupManifest:
        return self._manifest

    @property
    def archive(self) -> ArchiveInspection:
        return self._archive

    def cleanup(self) -> tuple[AppError, ...]:
        return self._area.cleanup()

    def verify_unchanged(self) -> None:
        area = self._area
        try:
            if (area.directory_removed or not _matches(area.directory, area.directory_identity, directory=True)
                    or area.snapshot_identity is None or not _matches(area.snapshot, area.snapshot_identity)):
                raise OSError("Candidate ownership is no longer confirmed.")
            before = area.snapshot.lstat()
            if not _unchanged(self._snapshot_stat, before):
                raise OSError("Candidate metadata changed.")
            with area.snapshot.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                if not _source_is_regular(opened) or not _unchanged(before, opened):
                    raise OSError("Candidate changed while opening.")
                digest, size = hashlib.sha256(), 0
                while chunk := stream.read(self._chunk_size):
                    digest.update(chunk)
                    size += len(chunk)
                    if size > self.manifest.snapshot_size_bytes:
                        raise OSError("Candidate grew.")
                after, final = os.fstat(stream.fileno()), area.snapshot.lstat()
                if (not _unchanged(opened, after) or not _unchanged(opened, final)
                        or not _source_is_regular(final) or size != self.manifest.snapshot_size_bytes
                        or not hmac.compare_digest(digest.hexdigest(), self.manifest.snapshot_sha256)
                        or not _matches(area.directory, area.directory_identity, directory=True)):
                    raise OSError("Candidate content or identity changed.")
        except Exception as error:
            raise CandidateFailure(_error("RESTORE_CANDIDATE_CHANGED", "Prepared snapshot no longer matches the verified candidate.")) from error


def _source_check(path: Path, expected: os.stat_result, opened: os.stat_result) -> None:
    current = path.lstat()
    if (not _source_is_regular(current) or not _source_is_regular(opened)
            or not _unchanged(expected, opened) or not _unchanged(opened, current)):
        raise ArchiveValidationError("source_changed", "Source identity, size or modification time changed.")


def prepare_restore_candidate(
    source: Path, work_parent: Path, manifest_policy: ManifestPolicy,
    archive_policy: ArchivePolicy, cancel: Event | None = None,
) -> RestoreCandidate:
    """Validate one read, stream snapshot privately, and retain an ownership handle.

    The exact two-entry D profile is explicit and still requires A/C agreement.
    zip_guard's private metadata and raw-stream validators are reused inside
    this repository; they are not a shared public parser or a second publication
    implementation. Raw verification precedes candidate writes, because
    ZipExtFile alone can stop at a forged uncompressed-length prefix.
    """
    if not isinstance(source, Path) or not isinstance(work_parent, Path):
        raise ValueError("source and work_parent must be Path objects.")
    if not isinstance(manifest_policy, ManifestPolicy) or not isinstance(archive_policy, ArchivePolicy):
        raise ValueError("Explicit manifest and archive policies are required.")
    if (archive_policy.allowed_entries != _ENTRIES or archive_policy.required_entries != _ENTRIES
            or archive_policy.max_entries < 2):
        raise ValueError("Candidate extraction requires exactly manifest.json and snapshot.sqlite.")
    if cancel is not None and not isinstance(cancel, Event):
        raise ValueError("cancel must be threading.Event or None.")
    area = None
    try:
        _cancel(cancel)
        parent = Path(os.path.abspath(work_parent))
        if not _is_directory(parent.lstat()):
            raise OSError("Candidate parent must be an existing ordinary directory.")
        source = Path(os.path.abspath(source))
        before = source.lstat()
        if not _source_is_regular(before):
            raise ArchiveValidationError("source_type", "Source must be regular, not a reparse point.")
        with source.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            _identity(opened)
            _source_check(source, before, opened)
            if opened.st_size > archive_policy.max_archive_bytes:
                raise ArchiveValidationError("resource_budget", "Archive exceeds the caller byte budget.")
            _cancel(cancel)
            with zipfile.ZipFile(stream, "r") as archive:
                infos = archive.infolist()
                _validate_metadata(infos, archive_policy)
                manifest_info = next(info for info in infos if info.orig_filename == "manifest.json")
                capture_limit = min(manifest_policy.max_json_bytes, archive_policy.max_capture_bytes)
                if manifest_info.file_size > capture_limit:
                    raise ArchiveValidationError("capture_budget", "Manifest exceeds the capture/JSON budget.")
                directory = Path(tempfile.mkdtemp(prefix=".pmkb-restore-", dir=parent))
                area = _OwnedArea(directory, None, directory / "snapshot.sqlite")
                area.directory_identity = _identity(directory.lstat())
                descriptor = os.open(area.snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    area.snapshot_identity = _identity(os.fstat(descriptor))
                    output = os.fdopen(descriptor, "wb")
                    descriptor = None
                finally:
                    if descriptor is not None:
                        os.close(descriptor)
                entries, total, captured = [], 0, 0
                with output:
                    for info in infos:
                        _cancel(cancel)
                        content = bytearray() if info.orig_filename == "manifest.json" else None
                        digest, size = hashlib.sha256(), 0
                        with archive.open(info, "r") as member:
                            _verify_member_stream(
                                archive, info, archive_policy,
                                remaining_total=archive_policy.max_total_uncompressed_bytes - total,
                                remaining_capture=capture_limit - captured if content is not None else None,
                                check_cancel=lambda: _cancel(cancel),
                            )
                            while True:
                                _cancel(cancel)
                                chunk = member.read(archive_policy.chunk_size)
                                if not chunk:
                                    break
                                size += len(chunk)
                                total += len(chunk)
                                if size > archive_policy.max_entry_bytes or total > archive_policy.max_total_uncompressed_bytes:
                                    raise ArchiveValidationError("resource_budget", "Actual streamed bytes exceed the caller budget.")
                                digest.update(chunk)
                                if content is not None:
                                    captured += len(chunk)
                                    if captured > capture_limit:
                                        raise ArchiveValidationError("capture_budget", "Actual manifest bytes exceed the capture/JSON budget.")
                                    content.extend(chunk)
                                elif output.write(chunk) != len(chunk):
                                    raise OSError("Short candidate write.")
                        if size != info.file_size:
                            raise ArchiveValidationError("size_mismatch", "Actual entry size differs from metadata.")
                        entries.append(ArchiveEntryInspection(info.orig_filename, size, digest.hexdigest(),
                                                             bytes(content) if content is not None else None))
                    _cancel(cancel)
                    output.flush()
                    os.fsync(output.fileno())
                # Every member is closed and CRC checked before the output is accepted.
            _source_check(source, opened, os.fstat(stream.fileno()))
        _cancel(cancel)
        values = {entry.name: entry for entry in entries}
        manifest = decode_manifest(values["manifest.json"].captured_bytes, manifest_policy)
        snapshot = values["snapshot.sqlite"]
        if (snapshot.size_bytes != manifest.snapshot_size_bytes
                or not hmac.compare_digest(snapshot.sha256, manifest.snapshot_sha256)):
            raise ArchiveValidationError("snapshot_mismatch", "Manifest differs from actual extracted snapshot bytes.")
        if not _matches(area.directory, area.directory_identity, directory=True) or not _matches(area.snapshot, area.snapshot_identity):
            raise OSError("Candidate ownership changed during preparation.")
        observed = area.snapshot.lstat()
        if observed.st_size != snapshot.size_bytes:
            raise OSError("Candidate size changed during preparation.")
        _cancel(cancel)
        candidate = RestoreCandidate(area, manifest, ArchiveInspection(tuple(entries), opened.st_size, total),
                                     observed, archive_policy.chunk_size)
        # A successful write count is not itself evidence about the closed file.
        # Recheck the owned candidate, never reopen the original ZIP input.
        candidate.verify_unchanged()
        _cancel(cancel)
        return candidate
    except Exception as error:
        warnings = area.cleanup() if area is not None else ()
        if isinstance(error, CandidateFailure):
            raise CandidateFailure(error.error, error.warnings + warnings, cancelled=error.cancelled) from error
        if isinstance(error, ArchiveValidationError):
            mapped = _error("ARCHIVE_" + error.code.upper(), str(error))
        elif isinstance(error, ManifestValidationError):
            mapped = _error("MANIFEST_" + error.code.upper(), "The captured backup manifest is invalid.")
        elif isinstance(error, (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError,
                                NotImplementedError, zlib.error, UnicodeError)):
            mapped = _error("ARCHIVE_INVALID_ZIP", "ZIP structure, entry header or CRC verification failed.")
        else:
            mapped = _error("RESTORE_CANDIDATE_IO", "Could not safely prepare the private restore candidate.")
        raise CandidateFailure(mapped, warnings) from error
