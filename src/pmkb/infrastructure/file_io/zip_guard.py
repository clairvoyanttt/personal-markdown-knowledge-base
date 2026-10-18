"""Read-only ZIP-container inspection with caller-selected resource budgets.

This module does not define a backup format, interpret manifest fields, validate
SQLite, extract members, or initialize a restored collection. A valid report is
only evidence about this read of the container. Callers must use a trusted parent
directory: final-component checks do not defend against hostile parent-directory
replacement, and later reopening the same pathname is not a validated restore.
"""

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import stat
import unicodedata
import zipfile
import zlib

from pmkb.infrastructure.file_io.windows_names import validate_windows_basename


class ArchiveValidationError(ValueError):
    """Inspection failed; ``code`` is stable, text is diagnostic, not UI copy."""

    def __init__(self, code: str, message: str, *, entry: str | None = None):
        super().__init__(message)
        self.code = code
        self.entry = entry


def _entry_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _check_name(name: str) -> None:
    if not isinstance(name, str) or not name:
        raise ValueError("Archive entry names must be nonempty strings.")
    if any(unicodedata.category(char) in {"Cc", "Cs"} for char in name):
        raise ValueError("Archive entry names must not contain control characters.")
    if any(char in '\\:<>"|?*' for char in name):
        raise ValueError("Archive entry names must use safe relative POSIX paths.")
    for component in name.split("/"):
        validate_windows_basename(component)


@dataclass(frozen=True, slots=True)
class ArchivePolicy:
    """Explicit allowlist and positive read budgets, not product capacity limits.

    Entry byte limits refer to uncompressed data; max_archive_bytes bounds the
    source file before ZipFile can allocate its central-directory structures.
    Capture is opt-in and bounded across all captured entries together.
    """

    allowed_entries: frozenset[str]
    required_entries: frozenset[str]
    max_archive_bytes: int
    max_entries: int
    max_entry_bytes: int
    max_total_uncompressed_bytes: int
    max_capture_bytes: int
    chunk_size: int = 64 * 1024

    def __post_init__(self):
        for field in ("max_archive_bytes", "max_entries", "max_entry_bytes",
                      "max_total_uncompressed_bytes", "max_capture_bytes", "chunk_size"):
            value = getattr(self, field)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{field} must be a positive integer, not bool.")
        for field in ("allowed_entries", "required_entries"):
            values = getattr(self, field)
            if isinstance(values, (str, bytes)):
                raise ValueError(f"{field} must be a collection of entry names.")
            try:
                names = frozenset(values)
            except TypeError as error:
                raise ValueError(f"{field} must contain entry names.") from error
            for name in names:
                _check_name(name)
            if len({_entry_key(name) for name in names}) != len(names):
                raise ValueError(f"{field} contains case/Unicode-ambiguous entry names.")
            object.__setattr__(self, field, names)
        if not self.required_entries <= self.allowed_entries:
            raise ValueError("required_entries must be a subset of allowed_entries.")


@dataclass(frozen=True, slots=True)
class ArchiveEntryInspection:
    name: str
    size_bytes: int
    sha256: str
    captured_bytes: bytes | None


@dataclass(frozen=True, slots=True)
class ArchiveInspection:
    entries: tuple[ArchiveEntryInspection, ...]
    archive_size_bytes: int
    total_uncompressed_bytes: int


def _source_is_regular(details: os.stat_result) -> bool:
    # On Windows a final reparse point can be more than a POSIX-style symlink.
    return (stat.S_ISREG(details.st_mode)
            and not getattr(details, "st_file_attributes", 0) & 0x400)


def _validate_metadata(infos: list[zipfile.ZipInfo], policy: ArchivePolicy) -> None:
    if len(infos) > policy.max_entries:
        raise ArchiveValidationError("resource_budget", "Archive contains too many entries.")
    seen: set[str] = set()
    total = 0
    for info in infos:
        name = info.orig_filename
        mode = stat.S_IFMT(info.external_attr >> 16)
        if info.is_dir() or mode not in {0, stat.S_IFREG} or info.external_attr & 0x10:
            raise ArchiveValidationError("unsupported_entry_type", "Only regular file entries are accepted.", entry=name)
        try:
            _check_name(name)
        except ValueError as error:
            raise ArchiveValidationError("unsafe_entry_name", str(error), entry=name) from error
        if name != info.filename:
            raise ArchiveValidationError("unsafe_entry_name", "Original entry name was truncated or changed.", entry=name)
        key = _entry_key(name)
        if key in seen:
            raise ArchiveValidationError("duplicate_entry", "Duplicate or case/Unicode-ambiguous entry.", entry=name)
        seen.add(key)
        if name not in policy.allowed_entries:
            raise ArchiveValidationError("unexpected_entry", "Entry is not in the caller allowlist.", entry=name)
        if info.flag_bits & 1:
            raise ArchiveValidationError("encrypted_entry", "Encrypted entries are not supported.", entry=name)
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ArchiveValidationError("unsupported_compression", "Only stored/deflated entries are supported.", entry=name)
        total += info.file_size
        if (info.file_size < 0 or info.compress_size < 0
                or info.file_size > policy.max_entry_bytes
                or total > policy.max_total_uncompressed_bytes):
            raise ArchiveValidationError("resource_budget", "Declared entry sizes exceed the caller budget.", entry=name)
    if not policy.required_entries <= {info.orig_filename for info in infos}:
        raise ArchiveValidationError("missing_entry", "A required archive entry is missing.")


def inspect_zip(
    source: str | os.PathLike[str],
    policy: ArchivePolicy,
    *,
    capture_entries: frozenset[str] = frozenset(),
    expected_sha256: Mapping[str, str] | None = None,
) -> ArchiveInspection:
    """Hash/CRC-check every allowlisted entry without extracting or modifying it.

    ``capture_entries`` can retain selected small entries (e.g. a caller-owned
    manifest) in the immutable result. SHA-256 comparisons are optional; hashes
    supplied by an untrusted manifest alone do not establish authenticity.
    Rejecting a container never writes or removes its source or any output.
    """
    if not isinstance(policy, ArchivePolicy):
        raise TypeError("policy must be ArchivePolicy.")
    if isinstance(capture_entries, (str, bytes)):
        raise ValueError("capture_entries must be a collection of names.")
    try:
        capture = frozenset(capture_entries)
    except TypeError as error:
        raise ValueError("capture_entries must be a collection of entry names.") from error
    if expected_sha256 is None:
        expected = {}
    elif isinstance(expected_sha256, Mapping):
        expected = dict(expected_sha256)
    else:
        raise ValueError("expected_sha256 must be a mapping of names to SHA-256 values, or None.")
    if not (capture | expected.keys()) <= policy.allowed_entries:
        raise ValueError("Captured/expected entries must belong to allowed_entries.")
    for digest in expected.values():
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)):
            raise ValueError("Expected SHA-256 values must have 64 hexadecimal characters.")

    path = Path(source)
    try:
        before = path.lstat()
        if not _source_is_regular(before):
            raise ArchiveValidationError("source_type", "Source must be a regular file, not a link/reparse point.")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            current_path = path.lstat()
            if (not _source_is_regular(opened) or not _source_is_regular(current_path)
                    or not os.path.samestat(before, opened)
                    or not os.path.samestat(current_path, opened)):
                raise ArchiveValidationError("source_changed", "Source identity changed while opening.")
            # This check intentionally precedes ZipFile construction, not only infolist.
            if opened.st_size > policy.max_archive_bytes:
                raise ArchiveValidationError("resource_budget", "Archive file exceeds the caller read budget.")
            with zipfile.ZipFile(stream, "r") as archive:
                infos = archive.infolist()
                _validate_metadata(infos, policy)
                names = {info.orig_filename for info in infos}
                if not (capture | expected.keys()) <= names:
                    raise ArchiveValidationError("missing_entry", "A requested capture/hash entry is missing.")
                if sum(info.file_size for info in infos if info.orig_filename in capture) > policy.max_capture_bytes:
                    raise ArchiveValidationError("capture_budget", "Requested capture exceeds the caller budget.")
                result = []
                total = 0
                captured = 0
                for info in infos:
                    size = 0
                    digest = hashlib.sha256()
                    content = bytearray() if info.orig_filename in capture else None
                    with archive.open(info, "r") as member:
                        while chunk := member.read(policy.chunk_size):
                            size += len(chunk)
                            total += len(chunk)
                            if size > policy.max_entry_bytes or total > policy.max_total_uncompressed_bytes:
                                raise ArchiveValidationError("resource_budget", "Actual stream bytes exceed the caller budget.", entry=info.orig_filename)
                            digest.update(chunk)
                            if content is not None:
                                captured += len(chunk)
                                if captured > policy.max_capture_bytes:
                                    raise ArchiveValidationError("capture_budget", "Actual capture bytes exceed the caller budget.", entry=info.orig_filename)
                                content.extend(chunk)
                    if size != info.file_size:
                        raise ArchiveValidationError("size_mismatch", "Actual entry size differs from its metadata.", entry=info.orig_filename)
                    actual_hash = digest.hexdigest()
                    if info.orig_filename in expected and not hmac.compare_digest(actual_hash, expected[info.orig_filename].lower()):
                        raise ArchiveValidationError("hash_mismatch", "Entry SHA-256 differs from the expected digest.", entry=info.orig_filename)
                    result.append(ArchiveEntryInspection(info.orig_filename, size, actual_hash,
                                                         bytes(content) if content is not None else None))
            after = os.fstat(stream.fileno())
            final_path = path.lstat()
            if (not _source_is_regular(final_path) or not os.path.samestat(opened, final_path)
                    or opened.st_size != after.st_size or opened.st_mtime_ns != after.st_mtime_ns):
                raise ArchiveValidationError("source_changed", "Source changed during inspection.")
        return ArchiveInspection(tuple(result), opened.st_size, total)
    except ArchiveValidationError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError, RuntimeError,
            NotImplementedError, zlib.error, UnicodeError) as error:
        raise ArchiveValidationError("invalid_zip", "ZIP structure, entry header, or CRC verification failed.") from error
    except OSError as error:
        raise ArchiveValidationError("source_io", "Source could not be read safely.") from error
