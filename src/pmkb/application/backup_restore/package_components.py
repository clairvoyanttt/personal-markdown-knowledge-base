"""D-internal ZIP/manifest composition, NOT the API08/09 application services.

Input content must already be a frozen saved snapshot supplied by C. This
module does not capture SQLite, choose drafts, read note rows, initialize
receipts, or activate a collection. Its manifest profile still needs A/C
agreement before cross-module adoption. Sources/parents must be trusted.
"""
from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
from threading import Event

from pmkb.infrastructure.file_io.publication import PublicationResult
from pmkb.infrastructure.file_io.zip_guard import ArchiveInspection, ArchivePolicy, inspect_zip
from pmkb.infrastructure.file_io.zip_writer import ArchiveSource, publish_zip
from .manifest import (
    BackupManifest, ManifestPolicy, SnapshotFacts, check_manifest_against_facts,
    decode_manifest, encode_manifest,
)


class PackageValidationError(ValueError):
    """The container's actual entry facts disagree with its manifest."""


@dataclass(frozen=True, slots=True)
class PackageInspection:
    manifest: BackupManifest
    archive: ArchiveInspection
    supplied_facts_checked: bool


def publish_package(
    destination: str | os.PathLike[str],
    frozen_snapshot: bytes | Path,
    manifest: BackupManifest,
    manifest_policy: ManifestPolicy,
    archive_policy: ArchivePolicy,
    cancel: Event | None = None,
) -> PublicationResult:
    """Encode metadata and verify actual packaged hash/size BEFORE publication.

    The capture time is supplied, never guessed at packaging time. Validation
    errors raise before writing. File-layer outcomes retain their original
    ERROR/CANCELLED/UNCONFIRMED meaning; no automatic retry or cleanup of a
    possibly published destination occurs here.
    """
    encoded = encode_manifest(manifest, manifest_policy)
    return publish_zip(
        destination,
        (ArchiveSource("manifest.json", encoded),
         ArchiveSource(manifest.snapshot_entry, frozen_snapshot)),
        archive_policy,
        cancel,
        expected_sha256={"manifest.json": hashlib.sha256(encoded).hexdigest(),
                         manifest.snapshot_entry: manifest.snapshot_sha256},
        expected_sizes={"manifest.json": len(encoded),
                        manifest.snapshot_entry: manifest.snapshot_size_bytes},
    )


def inspect_package(
    source: str | os.PathLike[str],
    manifest_policy: ManifestPolicy,
    archive_policy: ArchivePolicy,
    *,
    facts: SnapshotFacts | None = None,
) -> PackageInspection:
    """Use one guarded container read for manifest bytes and entry measurements.

    Only the small manifest is captured. No extraction or later reopening of
    snapshot.sqlite occurs. Optional facts must come independently from C's
    authoritative validation, not be copied out of the untrusted manifest.
    Even supplied_facts_checked=True does NOT mean this helper queried a database.
    The report does not authorize restoration by reopening an unchecked path.
    """
    archive = inspect_zip(source, archive_policy, capture_entries=frozenset({"manifest.json"}))
    entries = {entry.name: entry for entry in archive.entries}
    manifest_entry = entries.get("manifest.json")
    if manifest_entry is None or manifest_entry.captured_bytes is None:
        raise PackageValidationError("The required manifest was not captured.")
    manifest = decode_manifest(manifest_entry.captured_bytes, manifest_policy)
    snapshot = entries.get(manifest.snapshot_entry)
    if snapshot is None:
        raise PackageValidationError("The snapshot named by the manifest is missing.")
    if snapshot.size_bytes != manifest.snapshot_size_bytes:
        raise PackageValidationError("Actual snapshot size differs from its manifest.")
    if not hmac.compare_digest(snapshot.sha256, manifest.snapshot_sha256.lower()):
        raise PackageValidationError("Actual snapshot SHA-256 differs from its manifest.")
    if facts is not None:
        check_manifest_against_facts(manifest, facts, manifest_policy)
    return PackageInspection(manifest, archive, facts is not None)
