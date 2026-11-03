"""C-side restore payload: convert one saved snapshot into a new collection.

The source snapshot is never modified. Ownership is converted only for markers
that the shared parser actually authorized and that belong to the source
collection; foreign markers stay literal, and deleted/missing targets keep their
identity and historical name instead of being rebound to a same-named note.
"""
from __future__ import annotations

from dataclasses import dataclass

from pmkb.application.backup_restore.manifest import ReferenceCounts
from pmkb.application.backup_restore.references import (
    RestoreReference,
    restore_reference_ownership,
)
from pmkb.application.export.rendering import ReferenceKind
from pmkb.application.parsing import parse_markdown
from pmkb.domain.identity import require_identifier
from pmkb.domain.naming import normalize_name
from pmkb.domain.rules import RuleViolation
from pmkb.infrastructure.storage.reader import CollectionSnapshot
from pmkb.infrastructure.storage.writing import RestoredNote, RetainedIdentity


@dataclass(frozen=True, slots=True)
class RestorePayload:
    """Converted notes, retained identities and the observed classification."""

    notes: tuple[RestoredNote, ...]
    retained: tuple[RetainedIdentity, ...]
    reference_counts: ReferenceCounts


def build_restore_payload(snapshot: CollectionSnapshot,
                          new_collection_id: str) -> RestorePayload:
    """Convert a saved snapshot into ``initialize_restored_collection`` inputs."""
    require_identifier(new_collection_id, "New collection identity")
    if new_collection_id == snapshot.collection_id:
        raise RuleViolation(
            "INVALID_ARGUMENT", "Restoration requires an independent new collection identity.",
        )
    identities = {item.note_id: item for item in snapshot.identities}
    live_names = {item.note_id: item.name for item in snapshot.notes}
    counts = {"VALID": 0, "DELETED": 0, "MISSING": 0, "FOREIGN_COLLECTION": 0}

    notes: list[RestoredNote] = []
    for note in snapshot.notes:
        document = parse_markdown(note.body, snapshot.collection_id)
        references: list[RestoreReference] = []
        for occurrence in document.references:
            kind, hint = _classify(identities, live_names, snapshot.collection_id, occurrence)
            counts[kind.value] += 1
            references.append(RestoreReference(
                start_utf16=occurrence.start_utf16,
                length_utf16=occurrence.length_utf16,
                marker_text=occurrence.marker_text,
                kind=kind,
                display_hint=hint,
            ))
        converted = restore_reference_ownership(
            note.body, snapshot.collection_id, new_collection_id, references,
        )
        normalized = normalize_name(note.name)
        notes.append(RestoredNote(
            note_id=note.note_id,
            name=normalized.name,
            body=converted.body,
            tags=note.tags,
        ))

    retained = tuple(
        RetainedIdentity(item.note_id, item.state, item.last_saved_name)
        for item in snapshot.identities
        if item.state != "LIVE"
    )
    return RestorePayload(
        notes=tuple(notes),
        retained=retained,
        reference_counts=ReferenceCounts(
            valid=counts["VALID"],
            deleted=counts["DELETED"],
            missing=counts["MISSING"],
            foreign=counts["FOREIGN_COLLECTION"],
        ),
    )


def count_snapshot_references(snapshot: CollectionSnapshot) -> ReferenceCounts:
    """Classify every saved occurrence exactly like :func:`build_restore_payload`."""
    identities = {item.note_id: item for item in snapshot.identities}
    live_names = {item.note_id: item.name for item in snapshot.notes}
    counts = {"VALID": 0, "DELETED": 0, "MISSING": 0, "FOREIGN_COLLECTION": 0}
    for note in snapshot.notes:
        document = parse_markdown(note.body, snapshot.collection_id)
        for occurrence in document.references:
            kind, _hint = _classify(identities, live_names, snapshot.collection_id, occurrence)
            counts[kind.value] += 1
    return ReferenceCounts(
        valid=counts["VALID"],
        deleted=counts["DELETED"],
        missing=counts["MISSING"],
        foreign=counts["FOREIGN_COLLECTION"],
    )


def _classify(identities: dict, live_names: dict, collection_id: str,
              occurrence) -> tuple[ReferenceKind, str | None]:
    if occurrence.target_collection_id != collection_id:
        return ReferenceKind.FOREIGN_COLLECTION, None
    identity = identities.get(occurrence.target_note_id)
    if identity is None:
        return ReferenceKind.MISSING, None
    if identity.state == "LIVE":
        return ReferenceKind.VALID, live_names.get(occurrence.target_note_id)
    if identity.state == "DELETED":
        return ReferenceKind.DELETED, identity.last_saved_name
    return ReferenceKind.MISSING, identity.last_saved_name
