"""Pure codec and comparisons for a D-internal backup-manifest profile.

The historical design defines the provenance/version/count fields. This profile
adds snapshotSha256 and snapshotSizeBytes as technical consistency checks; those
wire extensions and the caller-selected version/budget policy still require
A/C agreement before becoming the shared API08/API09 format. They are not new
business requirements or a source-authentication mechanism.

SnapshotFacts must come from independently checked snapshot data and the actual
archive-entry bytes. Supplying copied manifest claims as facts proves nothing.
This module reads no database, path, clock, network or filesystem. In particular
sourceLocation is only a historical display/identification field, never a path
to open during restore. V3 fixed-millisecond and restore-time rules do not apply.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import hmac
import json
from pathlib import PureWindowsPath
import re

from pmkb.infrastructure.file_io.windows_names import validate_windows_basename


_MAX_INTEGER = 2 ** 63 - 1
_IDENTIFIER = re.compile(r"[0-9a-f]{32}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UTC_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")
_WIRE_FIELDS = frozenset({
    "formatVersion", "schemaVersion", "snapshotEntry", "sourceCollectionId",
    "sourceLocation", "backupCapturedAtUtc", "sourceCollectionRevision",
    "savedNoteCount", "referenceCounts", "snapshotSha256", "snapshotSizeBytes",
})
_COUNT_FIELDS = frozenset({"valid", "deleted", "missing", "foreign"})


class ManifestValidationError(ValueError):
    """Invalid profile content; code/field are stable, text is not UI wording."""

    def __init__(self, code: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.code = code
        self.field = field


@dataclass(frozen=True, slots=True)
class ReferenceCounts:
    """Per-occurrence classifications, not unique targets or note counts."""

    valid: int
    deleted: int
    missing: int
    foreign: int


@dataclass(frozen=True, slots=True)
class ManifestPolicy:
    """Explicit format/schema allowlists and JSON read/write byte budget.

    No version or budget is chosen globally here. The signed-64-bit integer
    range is a D profile boundary compatible with the SQLite revision fields,
    not an added product note-capacity limit.
    """

    supported_format_versions: frozenset[int]
    supported_schema_versions: frozenset[int]
    max_json_bytes: int

    def __post_init__(self):
        for field in ("supported_format_versions", "supported_schema_versions"):
            supplied = getattr(self, field)
            if isinstance(supplied, (str, bytes)):
                raise ValueError(f"{field} must be a nonempty collection of integer versions.")
            try:
                candidates = tuple(supplied)
            except TypeError as error:
                raise ValueError(f"{field} must contain integer versions.") from error
            # Validate before deduplication: bool/float can compare equal to int
            # and would otherwise disappear based on their input ordering.
            if not candidates or any(type(value) is not int or not 1 <= value <= _MAX_INTEGER for value in candidates):
                raise ValueError(f"{field} must contain positive signed-64-bit integers, not bool.")
            versions = frozenset(candidates)
            object.__setattr__(self, field, versions)
        if type(self.max_json_bytes) is not int or self.max_json_bytes <= 0:
            raise ValueError("max_json_bytes must be an explicit positive integer, not bool.")


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Immutable codec DTO; public functions validate before returning or using it."""

    format_version: int
    schema_version: int
    snapshot_entry: str
    source_collection_id: str
    source_location: str
    backup_captured_at_utc: datetime
    source_collection_revision: int
    saved_note_count: int
    reference_counts: ReferenceCounts
    snapshot_sha256: str
    snapshot_size_bytes: int


@dataclass(frozen=True, slots=True)
class SnapshotFacts:
    """Independent facts supplied by C plus the actual entry hash/byte count.

    The snapshot cannot prove its old source location or system-clock accuracy,
    so neither source_location nor backup_captured_at_utc is inferred here.
    This DTO itself is not evidence that a database was opened or checked.
    """

    schema_version: int
    source_collection_id: str
    source_collection_revision: int
    saved_note_count: int
    reference_counts: ReferenceCounts
    snapshot_sha256: str
    snapshot_size_bytes: int


def _integer(value: object, field: str, *, positive: bool = False) -> None:
    if type(value) is not int or not (1 if positive else 0) <= value <= _MAX_INTEGER:
        raise ManifestValidationError("invalid_field", "Expected a bounded integer, not bool/float.", field=field)


def _string(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError("invalid_field", "Expected a text field.", field=field)
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ManifestValidationError("invalid_field", "Unpaired Unicode surrogate is not valid text.", field=field) from error
    return value


def _counts(value: object) -> None:
    if not isinstance(value, ReferenceCounts):
        raise ManifestValidationError("invalid_field", "Expected typed per-occurrence counts.", field="reference_counts")
    for field in _COUNT_FIELDS:
        _integer(getattr(value, field), f"reference_counts.{field}")


def _snapshot_values(value: BackupManifest | SnapshotFacts, policy: ManifestPolicy) -> None:
    _integer(value.schema_version, "schema_version", positive=True)
    if value.schema_version not in policy.supported_schema_versions:
        raise ManifestValidationError("unsupported_version", "Snapshot schema version is unsupported.", field="schema_version")
    if not _IDENTIFIER.fullmatch(_string(value.source_collection_id, "source_collection_id")):
        raise ManifestValidationError("invalid_field", "Collection ID must be 32 lowercase hexadecimal characters.", field="source_collection_id")
    _integer(value.source_collection_revision, "source_collection_revision", positive=True)
    _integer(value.saved_note_count, "saved_note_count")
    _counts(value.reference_counts)
    if not _SHA256.fullmatch(_string(value.snapshot_sha256, "snapshot_sha256")):
        raise ManifestValidationError("invalid_field", "Snapshot SHA-256 must be 64 lowercase hexadecimal characters.", field="snapshot_sha256")
    _integer(value.snapshot_size_bytes, "snapshot_size_bytes")


def _source_location(value: object) -> None:
    text = _string(value, "source_location")
    normalized = text.replace("/", "\\")
    path = PureWindowsPath(text)
    # This is purely lexical: no resolve(), exists(), stat(), open() or mkdir().
    if (not path.is_absolute() or normalized.startswith(("\\\\?\\", "\\\\.\\"))
            or any(ord(char) < 32 or ord(char) == 127 for char in text)
            or any(component in {".", ".."} for component in normalized.split("\\"))):
        raise ManifestValidationError("invalid_field", "Source location must identify an absolute Windows directory.", field="source_location")
    components = list(path.parts[1:])
    if path.drive.startswith("\\\\"):
        components += path.drive[2:].split("\\")
    try:
        for component in components:
            validate_windows_basename(component)
    except ValueError as error:
        raise ManifestValidationError("invalid_field", "Source directory contains an invalid Windows component.", field="source_location") from error


def _utc_datetime(value: object) -> None:
    if not isinstance(value, datetime):
        raise ManifestValidationError("invalid_field", "Capture time must be an aware UTC datetime.", field="backup_captured_at_utc")
    try:
        valid = value.tzinfo is not None and value.utcoffset() == timedelta(0)
    except (ValueError, OverflowError) as error:
        raise ManifestValidationError("invalid_field", "Capture time has an invalid time zone.", field="backup_captured_at_utc") from error
    if not valid:
        raise ManifestValidationError("invalid_field", "Capture time must be aware and already UTC.", field="backup_captured_at_utc")


def validate_manifest(manifest: BackupManifest, policy: ManifestPolicy) -> BackupManifest:
    """Validate structure and supported versions, not snapshot truth or provenance."""
    if not isinstance(policy, ManifestPolicy):
        raise TypeError("policy must be ManifestPolicy.")
    if not isinstance(manifest, BackupManifest):
        raise TypeError("manifest must be BackupManifest.")
    _integer(manifest.format_version, "format_version", positive=True)
    if manifest.format_version not in policy.supported_format_versions:
        raise ManifestValidationError("unsupported_version", "Backup format version is unsupported.", field="format_version")
    if manifest.snapshot_entry != "snapshot.sqlite":
        raise ManifestValidationError("invalid_field", "Snapshot entry must be exactly snapshot.sqlite.", field="snapshot_entry")
    _snapshot_values(manifest, policy)
    _source_location(manifest.source_location)
    _utc_datetime(manifest.backup_captured_at_utc)
    return manifest


def encode_manifest(manifest: BackupManifest, policy: ManifestPolicy) -> bytes:
    """Encode deterministic strict UTF-8 JSON without reading a clock or file."""
    validate_manifest(manifest, policy)
    counts = manifest.reference_counts
    payload = {
        "formatVersion": manifest.format_version,
        "schemaVersion": manifest.schema_version,
        "snapshotEntry": manifest.snapshot_entry,
        "sourceCollectionId": manifest.source_collection_id,
        "sourceLocation": manifest.source_location,
        "backupCapturedAtUtc": manifest.backup_captured_at_utc.isoformat().replace("+00:00", "Z"),
        "sourceCollectionRevision": manifest.source_collection_revision,
        "savedNoteCount": manifest.saved_note_count,
        "referenceCounts": {field: getattr(counts, field) for field in ("valid", "deleted", "missing", "foreign")},
        "snapshotSha256": manifest.snapshot_sha256,
        "snapshotSizeBytes": manifest.snapshot_size_bytes,
    }
    encoded = (json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n").encode("utf-8", errors="strict")
    if len(encoded) > policy.max_json_bytes:
        raise ManifestValidationError("resource_budget", "Encoded manifest exceeds the caller JSON byte budget.")
    return encoded


def _unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ManifestValidationError("duplicate_field", "Duplicate JSON object field is not allowed.", field=key)
        result[key] = value
    return result


def _nonfinite(_: str) -> None:
    raise ManifestValidationError("invalid_json", "NaN and Infinity are not JSON numbers.")


def _object_fields(value: object, fields: frozenset[str], field: str) -> dict[str, object]:
    if type(value) is not dict or value.keys() != fields:
        raise ManifestValidationError("invalid_fields", "Expected exactly the required profile fields.", field=field)
    return value


def decode_manifest(data: bytes, policy: ManifestPolicy) -> BackupManifest:
    """Read a bounded strict profile; unknown fields are not silently ignored.

    A future agreed profile must version its extensions. Fractional seconds up
    to Python datetime's six-digit precision are supported; finer precision is
    rejected, never silently truncated. Milliseconds are not mandatory.
    """
    if not isinstance(policy, ManifestPolicy):
        raise TypeError("policy must be ManifestPolicy.")
    if type(data) is not bytes:
        raise TypeError("data must be UTF-8 bytes.")
    if len(data) > policy.max_json_bytes:
        raise ManifestValidationError("resource_budget", "Manifest exceeds the caller JSON byte budget.")
    try:
        raw = json.loads(data.decode("utf-8", errors="strict"), object_pairs_hook=_unique_fields, parse_constant=_nonfinite)
    except ManifestValidationError:
        raise
    except (ValueError, UnicodeError, RecursionError) as error:
        raise ManifestValidationError("invalid_json", "Expected well-formed strict UTF-8 JSON without BOM.") from error
    raw = _object_fields(raw, _WIRE_FIELDS, "manifest")
    raw_counts = _object_fields(raw["referenceCounts"], _COUNT_FIELDS, "reference_counts")
    time_text = raw["backupCapturedAtUtc"]
    if not isinstance(time_text, str) or not _UTC_TIME.fullmatch(time_text):
        raise ManifestValidationError("invalid_field", "Capture time must be ISO UTC with Z and supported precision.", field="backup_captured_at_utc")
    try:
        captured = datetime.fromisoformat(time_text[:-1] + "+00:00")
    except ValueError as error:
        raise ManifestValidationError("invalid_field", "Capture time is not a valid calendar time.", field="backup_captured_at_utc") from error
    manifest = BackupManifest(
        format_version=raw["formatVersion"], schema_version=raw["schemaVersion"],
        snapshot_entry=raw["snapshotEntry"], source_collection_id=raw["sourceCollectionId"],
        source_location=raw["sourceLocation"], backup_captured_at_utc=captured,
        source_collection_revision=raw["sourceCollectionRevision"], saved_note_count=raw["savedNoteCount"],
        reference_counts=ReferenceCounts(**raw_counts), snapshot_sha256=raw["snapshotSha256"],
        snapshot_size_bytes=raw["snapshotSizeBytes"],
    )
    return validate_manifest(manifest, policy)


def check_manifest_against_facts(
    manifest: BackupManifest, facts: SnapshotFacts, policy: ManifestPolicy,
) -> None:
    """Compare provided facts; do not interpret a matching manifest as authority."""
    validate_manifest(manifest, policy)
    if not isinstance(facts, SnapshotFacts):
        raise TypeError("facts must be SnapshotFacts from independent snapshot checks.")
    _snapshot_values(facts, policy)
    for field in ("schema_version", "source_collection_id", "source_collection_revision",
                  "saved_note_count", "reference_counts", "snapshot_sha256", "snapshot_size_bytes"):
        declared, observed = getattr(manifest, field), getattr(facts, field)
        equal = hmac.compare_digest(declared, observed) if field == "snapshot_sha256" else declared == observed
        if not equal:
            raise ManifestValidationError("snapshot_mismatch", "Manifest claim differs from supplied independent snapshot facts.", field=field)
