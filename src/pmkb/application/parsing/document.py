"""Shared Markdown adapter types: protected spans, reference occurrences, blocks.

M04 is the single parser for preview (API06), references (API05), export (API07)
and restore conversion (API09). Consumers must not re-implement marker scanning.
Spans are UTF-16 half-open intervals because that is the retained storage
contract; use :meth:`ParsedDocument.slice_span` and never slice Python strings
with a UTF-16 offset directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from pmkb.domain.text_spans import slice_utf16, to_utf16_offset


class ProtectedKind(StrEnum):
    """Region kinds where a ``[[note:...]]`` sequence is ordinary literal text."""

    FENCED_CODE = "FENCED_CODE"
    INLINE_CODE = "INLINE_CODE"
    LINK = "LINK"
    IMAGE = "IMAGE"
    AUTOLINK = "AUTOLINK"
    ESCAPED = "ESCAPED"
    REFERENCE_DEFINITION = "REFERENCE_DEFINITION"


@dataclass(frozen=True, slots=True)
class ProtectedSpan:
    kind: ProtectedKind
    start_utf16: int
    length_utf16: int

    @property
    def end_utf16(self) -> int:
        return self.start_utf16 + self.length_utf16


class BlockKind(StrEnum):
    """Structural outline used by the current-draft preview (FR03)."""

    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST_ITEM = "LIST_ITEM"
    BLOCK_QUOTE = "BLOCK_QUOTE"
    CODE_FENCE = "CODE_FENCE"


@dataclass(frozen=True, slots=True)
class Block:
    kind: BlockKind
    start_utf16: int
    length_utf16: int
    level: int | None = None
    marker: str | None = None


class Ownership(StrEnum):
    """Whether a complete marker belongs to the collection being parsed."""

    LOCAL = "LOCAL"
    FOREIGN = "FOREIGN"


class ReferenceState(StrEnum):
    """Saved-version classification, resolved against the identity ledger only.

    Values match the shared vocabulary used by export/restore; the classification
    never falls back to a name, a partial identifier or the other collection.
    """

    VALID = "VALID"
    DELETED = "DELETED"
    MISSING = "MISSING"
    FOREIGN_COLLECTION = "FOREIGN_COLLECTION"


@dataclass(frozen=True, slots=True)
class ReferenceOccurrence:
    """One complete marker found outside every protected span."""

    ordinal: int
    start_utf16: int
    length_utf16: int
    marker_text: str
    target_collection_id: str
    target_note_id: str
    ownership: Ownership


@dataclass(frozen=True, slots=True)
class ResolvedReference:
    """An occurrence plus its saved-version classification and display name."""

    occurrence: ReferenceOccurrence
    state: ReferenceState
    target_name: str | None
    reason: str

    @property
    def navigable(self) -> bool:
        return self.state is ReferenceState.VALID


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    """Immutable parse result for one draft or saved body.

    ``body`` is retained so callers can slice an authorized span without trusting
    the parser's own text reconstruction.
    """

    collection_id: str
    body: str
    protected_spans: tuple[ProtectedSpan, ...]
    references: tuple[ReferenceOccurrence, ...]
    blocks: tuple[Block, ...]

    def slice_span(self, start_utf16: int, length_utf16: int) -> str:
        return slice_utf16(self.body, start_utf16, length_utf16)

    def marker_text(self, occurrence: ReferenceOccurrence) -> str:
        return slice_utf16(self.body, occurrence.start_utf16, occurrence.length_utf16)

    def utf16_offset(self, codepoint_offset: int) -> int:
        return to_utf16_offset(self.body, codepoint_offset)

    def occurrence_at(self, start_utf16: int, length_utf16: int) -> ReferenceOccurrence | None:
        for occurrence in self.references:
            if occurrence.start_utf16 == start_utf16 and occurrence.length_utf16 == length_utf16:
                return occurrence
        return None

    def is_protected(self, start_utf16: int, length_utf16: int) -> bool:
        end = start_utf16 + length_utf16
        return any(
            span.start_utf16 < end and start_utf16 < span.end_utf16
            for span in self.protected_spans
        )
