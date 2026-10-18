"""D-internal pure ownership conversion over C-authorized UTF-16 spans.

These frozen adapter types are NOT an agreed API05/API09 contract. C must supply
the classified saved-version occurrences from the shared parser and verified
snapshot. This module neither parses Markdown nor verifies target existence or
an identity ledger. No database, filesystem, receipts, or collection activation
are involved. A/C remain responsible for a genuinely new collection identity,
complete snapshot validation, and preservation of invalid target identities.
"""

from dataclasses import dataclass, replace
import re
from typing import Iterable

from pmkb.application.export.rendering import ReferenceKind
from pmkb.domain.text_spans import slice_utf16


@dataclass(frozen=True, slots=True)
class RestoreReference:
    """One classified saved occurrence authorized for restoration by C.

    display_hint is optional trusted historical text. It is retained verbatim;
    it is never used to find a target or repair a deleted/missing identity.
    """

    start_utf16: int
    length_utf16: int
    marker_text: str
    kind: ReferenceKind
    display_hint: str | None = None


@dataclass(frozen=True, slots=True)
class RestoreTextResult:
    """Converted text and occurrences ordered by their original source spans."""

    body: str
    references: tuple[RestoreReference, ...]
    changed_occurrences: int


_ID = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(r"\[\[note:([0-9a-f]{32}):([0-9a-f]{32})\]\]")
_MARKER_UTF16_LENGTH = 74


def _check_identity(value: str, label: str) -> None:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain exactly 32 lowercase hex characters")


def _check_hint(hint: str | None) -> None:
    if hint is None:
        return
    if not isinstance(hint, str) or not hint:
        raise ValueError("A historical display hint must be non-empty Unicode text or None")
    slice_utf16(hint, 0, 0)
    if any(ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029" for char in hint):
        raise ValueError("A historical display hint must be single-line text without controls")


def restore_reference_ownership(
    saved_body: str,
    source_collection_id: str,
    new_collection_id: str,
    references: Iterable[RestoreReference],
) -> RestoreTextResult:
    """Change only the collection component of authorized source-owned markers.

    VALID/DELETED/MISSING occurrences must belong to the source collection.
    Their note IDs, classification, hints and spans are retained. Foreign
    occurrences must not belong to the source, stay literal, and must not
    collide with the new identity (otherwise they could become valid by error).

    Every marker is ASCII and exactly 74 UTF-16 units long, so replacing its
    32-unit ownership component cannot shift subsequent occurrence positions.
    Input is fully checked before a converted result is returned. Omitting an
    occurrence leaves its text untouched; this is deliberately not a parser.
    """
    if not isinstance(saved_body, str):
        raise ValueError("Expected saved Markdown text")
    # Reject ill-formed Unicode even outside an authorized occurrence.
    slice_utf16(saved_body, 0, 0)
    _check_identity(source_collection_id, "Source collection identity")
    _check_identity(new_collection_id, "New collection identity")
    if source_collection_id == new_collection_id:
        raise ValueError("Restoration requires an independent new collection identity")
    try:
        occurrences = tuple(references)
    except TypeError as error:
        raise ValueError("Expected authorized restore reference occurrences") from error

    checked: list[RestoreReference] = []
    for item in occurrences:
        if not isinstance(item, RestoreReference):
            raise ValueError("Expected a RestoreReference adapter input")
        if (type(item.start_utf16) is not int or type(item.length_utf16) is not int
                or item.start_utf16 < 0 or item.length_utf16 != _MARKER_UTF16_LENGTH):
            raise ValueError("Reference span must use integer UTF-16 units and a fixed 74-unit marker")
        if not isinstance(item.marker_text, str):
            raise ValueError("Authorized span must contain a complete literal reference marker")
        match = _MARKER.fullmatch(item.marker_text)
        if match is None:
            raise ValueError("Authorized span must contain a complete literal reference marker")
        if slice_utf16(saved_body, item.start_utf16, item.length_utf16) != item.marker_text:
            raise ValueError("Authorized marker does not match the original saved source")
        if not isinstance(item.kind, ReferenceKind):
            raise ValueError("Unknown reference classification")
        _check_hint(item.display_hint)
        target_collection_id = match.group(1)
        if item.kind is ReferenceKind.FOREIGN_COLLECTION:
            if target_collection_id == source_collection_id:
                raise ValueError("A source-owned marker cannot be classified as foreign")
            if target_collection_id == new_collection_id:
                raise ValueError("New collection identity collides with a known foreign occurrence")
        elif target_collection_id != source_collection_id:
            raise ValueError("A foreign marker cannot be classified as source-owned")
        checked.append(item)

    checked.sort(key=lambda item: item.start_utf16)
    encoded = saved_body.encode("utf-16-le", errors="strict")
    chunks: list[str] = []
    restored: list[RestoreReference] = []
    cursor = 0
    changed = 0
    for item in checked:
        if item.start_utf16 < cursor:
            raise ValueError("Authorized reference spans must not overlap")
        chunks.append(encoded[cursor * 2:item.start_utf16 * 2].decode("utf-16-le"))
        text = item.marker_text
        if item.kind is not ReferenceKind.FOREIGN_COLLECTION:
            text = f"[[note:{new_collection_id}:{text[40:72]}]]"
            changed += 1
        chunks.append(text)
        restored.append(replace(item, marker_text=text))
        cursor = item.start_utf16 + item.length_utf16
    chunks.append(encoded[cursor * 2:].decode("utf-16-le"))
    return RestoreTextResult("".join(chunks), tuple(restored), changed)
