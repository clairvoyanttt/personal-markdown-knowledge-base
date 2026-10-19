"""Build, verify, and exclusively publish a ZIP from caller-frozen sources.

This is a D-internal archive adapter, NOT API08 or a SQLite snapshot service.
The caller supplies immutable bytes or an already frozen regular snapshot file,
the complete allowlist/resource policy, and any expected hashes/sizes. It must
not pass a live database and assume that ordinary file reads freeze SQLite.

Sources are streamed through opened handles and checked for observable changes.
Trusted parent directories are required; this does not defend against hostile
ancestor replacement or a process modifying bytes while forging old metadata.
Use expected_sha256 to bind a previously captured manifest to the packaged data.
"""

from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass, replace
import ntpath
import os
from pathlib import Path
import stat
import tempfile
from threading import Event
import unicodedata
import zipfile

from pmkb.contracts.common import AppError, ResultStatus
from .publication import PublicationResult, publish_chunks
from .windows_names import validate_windows_basename
from .zip_guard import ArchivePolicy, ArchiveValidationError, inspect_zip


@dataclass(frozen=True, slots=True)
class ArchiveSource:
    """An entry name and immutable data or caller-owned frozen source pathname.

    Path sources remain caller-owned and are never removed or modified. Naming
    and resource validation runs against the explicit ArchivePolicy before any
    staging file is allocated. A frozen dataclass does not freeze a file itself.
    """

    name: str
    source: bytes | Path

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Entry name must be nonempty text.")
        if type(self.source) is not bytes and not isinstance(self.source, Path):
            raise ValueError("Source must be immutable bytes or pathlib.Path.")


@dataclass(frozen=True, slots=True)
class _PreparedSource:
    entry: ArchiveSource
    size: int
    details: os.stat_result | None


class _Cancelled(Exception):
    pass


def _cancelled(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Cancelled()


def _regular(details: os.stat_result) -> bool:
    return (stat.S_ISREG(details.st_mode)
            and not getattr(details, "st_file_attributes", 0) & 0x400)


def _same_file_version(before: os.stat_result, after: os.stat_result) -> bool:
    return (_regular(before) and _regular(after) and os.path.samestat(before, after)
            and before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns)


def _error(code: str, message: str) -> AppError:
    return AppError(code, message, "Inspect the source and result; do not automatically replay publication.")


def _cleanup_staging(path: Path, identity: tuple[int, int] | None) -> bool:
    """Remove only this adapter's positively identified unpublished build file."""
    if identity is None or identity[1] == 0:
        return False
    try:
        details = path.lstat()
        if not _regular(details) or (details.st_dev, details.st_ino) != identity:
            return False
        path.unlink()
        return True
    except OSError:
        return False


def _freeze_expectations(values, policy: ArchivePolicy, *, sizes: bool) -> dict:
    if values is None:
        return {}
    if not isinstance(values, Mapping):
        raise ValueError("Expected values must be a mapping or None.")
    frozen = dict(values)
    if not frozen.keys() <= policy.allowed_entries:
        raise ValueError("Expected entry names must belong to the allowlist.")
    for value in frozen.values():
        if sizes:
            if type(value) is not int or value < 0:
                raise ValueError("Expected sizes must be nonnegative integers, not bool.")
        elif (not isinstance(value, str) or len(value) != 64
              or any(char not in "0123456789abcdefABCDEF" for char in value)):
            raise ValueError("Expected SHA-256 values must contain 64 hexadecimal characters.")
    return frozen


def _prepare(entries: Iterable[ArchiveSource], policy: ArchivePolicy,
             target: Path, cancel: Event | None) -> tuple[_PreparedSource, ...]:
    names: list[str] = []
    sources: list[ArchiveSource] = []
    keys: set[str] = set()
    for entry in entries:
        _cancelled(cancel)
        if not isinstance(entry, ArchiveSource):
            raise ValueError("Entries must be ArchiveSource values.")
        if len(sources) >= policy.max_entries:
            raise ArchiveValidationError("resource_budget", "Too many archive sources.")
        key = unicodedata.normalize("NFC", entry.name).casefold()
        if key in keys:
            raise ArchiveValidationError("duplicate_entry", "Duplicate/case/Unicode-ambiguous source.", entry=entry.name)
        keys.add(key)
        names.append(entry.name)
        sources.append(entry)
    # Reuse the public policy's own safe-name validation, not zip_guard private
    # helpers. Explicit duplicate checks above also detect identical spellings.
    try:
        replace(policy, allowed_entries=frozenset(names), required_entries=frozenset())
        if any(len(name.encode("utf-8")) > 65535 for name in names):
            raise ValueError("Entry name exceeds the ZIP UTF-8 name field.")
    except ValueError as error:
        raise ArchiveValidationError("unsafe_entry_name", "An archive source name is unsafe.") from error
    if not set(names) <= policy.allowed_entries:
        raise ArchiveValidationError("unexpected_entry", "Source is not in the caller allowlist.")
    if not policy.required_entries <= set(names):
        raise ArchiveValidationError("missing_entry", "A required source entry is missing.")

    prepared: list[_PreparedSource] = []
    total = 0
    for entry in sources:
        _cancelled(cancel)
        details = None
        if isinstance(entry.source, Path):
            source_path = entry.source
            if os.path.normcase(os.path.abspath(source_path)) == os.path.normcase(str(target)):
                raise ArchiveValidationError("source_destination_conflict", "Source and output must be distinct.")
            details = source_path.lstat()
            if not _regular(details):
                raise ArchiveValidationError("source_type", "Only regular, non-reparse source files are accepted.", entry=entry.name)
            if os.path.lexists(target) and os.path.samestat(details, target.lstat()):
                raise ArchiveValidationError("source_destination_conflict", "Output aliases a source file.")
            size = details.st_size
        else:
            size = len(entry.source)
        total += size
        if size < 0 or size > policy.max_entry_bytes or total > policy.max_total_uncompressed_bytes:
            raise ArchiveValidationError("resource_budget", "Sources exceed the caller uncompressed byte budget.", entry=entry.name)
        prepared.append(_PreparedSource(entry, size, details))
    return tuple(prepared)


class _BudgetedOutput:
    """Bound writes including central-directory and ZIP header rewrites."""

    def __init__(self, stream, limit: int):
        self.stream = stream
        self.limit = limit

    def write(self, data):
        if self.stream.tell() + len(data) > self.limit:
            raise ArchiveValidationError("resource_budget", "Constructed ZIP exceeds the caller archive byte budget.")
        count = self.stream.write(data)
        if count != len(data):
            raise OSError("Incomplete archive staging write.")
        return count

    def __getattr__(self, name):
        return getattr(self.stream, name)


def _write_source(archive: zipfile.ZipFile, item: _PreparedSource,
                  policy: ArchivePolicy, compression: int, cancel: Event | None) -> None:
    info = zipfile.ZipInfo(item.entry.name)
    info.compress_type = compression
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o600) << 16
    info.file_size = item.size
    with archive.open(info, "w", force_zip64=True) as member:
        if type(item.entry.source) is bytes:
            for offset in range(0, item.size, policy.chunk_size):
                _cancelled(cancel)
                chunk = item.entry.source[offset:offset + policy.chunk_size]
                if member.write(chunk) != len(chunk):
                    raise OSError("Incomplete ZIP member write.")
        else:
            path = item.entry.source
            assert item.details is not None
            with path.open("rb") as stream:
                opened = os.fstat(stream.fileno())
                current_path = path.lstat()
                if (not _same_file_version(item.details, opened)
                        or not _same_file_version(opened, current_path)):
                    raise ArchiveValidationError("source_changed", "Source changed while opening.", entry=item.entry.name)
                count = 0
                while True:
                    _cancelled(cancel)
                    chunk = stream.read(policy.chunk_size)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > item.size:
                        raise ArchiveValidationError("source_changed", "Source grew during packaging.", entry=item.entry.name)
                    if member.write(chunk) != len(chunk):
                        raise OSError("Incomplete ZIP member write.")
                if (count != item.size or not _same_file_version(opened, os.fstat(stream.fileno()))
                        or not _same_file_version(opened, path.lstat())):
                    raise ArchiveValidationError("source_changed", "Source changed during packaging.", entry=item.entry.name)
    _cancelled(cancel)


def _staging_chunks(path: Path, expected: os.stat_result, chunk_size: int):
    """Read our verified build through one handle; do not trust a later pathname."""
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (not _same_file_version(expected, opened)
                or not _same_file_version(opened, path.lstat())):
            raise ArchiveValidationError("source_changed", "Verified build changed before publication.")
        count = 0
        while chunk := stream.read(chunk_size):
            count += len(chunk)
            if count > expected.st_size:
                raise ArchiveValidationError("source_changed", "Verified build grew before publication.")
            yield chunk
        if (count != expected.st_size or not _same_file_version(opened, os.fstat(stream.fileno()))
                or not _same_file_version(opened, path.lstat())):
            raise ArchiveValidationError("source_changed", "Verified build changed while reading.")


def publish_zip(
    destination: str | os.PathLike[str],
    entries: Iterable[ArchiveSource],
    policy: ArchivePolicy,
    cancel: Event | None = None,
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    expected_sha256: Mapping[str, str] | None = None,
    expected_sizes: Mapping[str, int] | None = None,
) -> PublicationResult:
    """Fully stage/close/CRC-hash-check before non-overwriting publication.

    Invalid configuration raises ValueError/TypeError without file writes.
    Unsafe sources, changed files, container verification and ordinary I/O
    failures return ERROR. Cancellation before publication returns CANCELLED.
    After the publication syscall, the existing publisher's OK/UNCONFIRMED
    semantics apply; late cancellation never deletes a published artifact.

    The build staging file and publisher candidate are distinct. We clean only
    our positively owned build. In UNCONFIRMED the publisher candidate remains
    untouched, and neither candidate nor destination is automatically retried.
    OK proves a published validated ZIP, not SQLite or business correctness.
    """
    if not isinstance(policy, ArchivePolicy):
        raise TypeError("policy must be ArchivePolicy.")
    if cancel is not None and not isinstance(cancel, Event):
        raise ValueError("cancel must be threading.Event or None.")
    if type(compression) is not int or compression not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise ValueError("Only stored/deflated ZIP compression is supported.")
    try:
        raw = os.fspath(destination)
        if not isinstance(raw, str) or not raw or "\x00" in raw:
            raise ValueError("Expected a nonempty text destination.")
        validate_windows_basename(ntpath.basename(raw))
        target = Path(os.path.abspath(raw))
        validate_windows_basename(target.name)
        iterator = iter(entries)
    except (TypeError, ValueError) as error:
        raise ValueError("Invalid output destination or source iterable.") from error
    hashes = _freeze_expectations(expected_sha256, policy, sizes=False)
    sizes = _freeze_expectations(expected_sizes, policy, sizes=True)
    staging: Path | None = None
    identity: tuple[int, int] | None = None

    def finish(reply: PublicationResult) -> PublicationResult:
        if staging is not None and not _cleanup_staging(staging, identity):
            warning = _error("ARCHIVE_TEMPORARY_RETAINED", f"Build staging could not be safely cleaned: {staging}")
            return replace(reply, temporary_path=reply.temporary_path or staging,
                           warnings=reply.warnings + (warning,))
        return reply

    try:
        _cancelled(cancel)
        parent = target.parent.resolve(strict=True)
        if not parent.is_dir():
            raise NotADirectoryError("Output parent must exist.")
        target = parent / target.name
        prepared = _prepare(iterator, policy, target, cancel)
        if not (hashes.keys() | sizes.keys()) <= {item.entry.name for item in prepared}:
            raise ArchiveValidationError("missing_entry", "An expected hash/size source is missing.")
        if os.path.lexists(target):
            return PublicationResult(ResultStatus.ERROR, target,
                                     error=_error("TARGET_EXISTS", "The selected output already exists."))
        _cancelled(cancel)
        descriptor, name = tempfile.mkstemp(prefix=".pmkb-zip-", suffix=".tmp", dir=parent)
        staging = Path(name)
        try:
            details = os.fstat(descriptor)
            identity = (details.st_dev, details.st_ino)
            stream = os.fdopen(descriptor, "w+b")
            descriptor = None
        finally:
            if descriptor is not None:
                os.close(descriptor)
        with stream:
            with zipfile.ZipFile(_BudgetedOutput(stream, policy.max_archive_bytes), "w",
                                 compression=compression, allowZip64=True) as archive:
                for item in prepared:
                    _cancelled(cancel)
                    _write_source(archive, item, policy, compression, cancel)
            stream.flush()
            os.fsync(stream.fileno())
        _cancelled(cancel)
        before_inspection = staging.lstat()
        if (not _regular(before_inspection)
                or (before_inspection.st_dev, before_inspection.st_ino) != identity):
            raise ArchiveValidationError("source_changed", "Build staging identity changed.")
        report = inspect_zip(staging, policy, expected_sha256=hashes)
        _cancelled(cancel)
        for item in report.entries:
            if item.name in sizes and item.size_bytes != sizes[item.name]:
                raise ArchiveValidationError("size_mismatch", "Entry size differs from the caller expectation.", entry=item.name)
        verified = staging.lstat()
        if not _same_file_version(before_inspection, verified):
            raise ArchiveValidationError("source_changed", "Build staging changed during verification.")
        _cancelled(cancel)
        try:
            with closing(_staging_chunks(staging, verified, policy.chunk_size)) as chunks:
                reply = publish_chunks(target, chunks, cancel)
        except Exception:
            # The normal publisher reports outcomes in its receipt. If that
            # whole boundary unexpectedly loses its reply, a formal file may
            # already exist. Keep our known build for inspection; do not call
            # finish(), remove the formal destination, or replay the write.
            return PublicationResult(
                ResultStatus.UNCONFIRMED, target, temporary_path=staging,
                error=_error("ARCHIVE_PUBLICATION_UNCONFIRMED",
                             "Publisher reply was lost; retain output and build paths for inspection."),
            )
        return finish(reply)
    except _Cancelled:
        return finish(PublicationResult(ResultStatus.CANCELLED, target))
    except ArchiveValidationError as error:
        return finish(PublicationResult(ResultStatus.ERROR, target,
                                       error=_error("ARCHIVE_" + error.code.upper(), str(error))))
    except Exception:
        return finish(PublicationResult(ResultStatus.ERROR, target,
                                       error=_error("ARCHIVE_IO_FAILURE", "Could not complete archive construction or verification; publication was not confirmed.")))
