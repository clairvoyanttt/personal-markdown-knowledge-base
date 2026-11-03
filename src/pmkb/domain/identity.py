"""Internal reference identity: 32-hex collection/note pairs and the marker text.

The user-visible marker is ``[[note:<collectionId>:<noteId>]]`` with both parts
exactly 32 lowercase hexadecimal characters, so a complete marker is 74 UTF-16
units long (all ASCII). Nothing here touches a database: deciding whether the
target exists, was deleted, or belongs to another collection is the storage /
query layer's job.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from pmkb.domain.rules import RuleViolation, require_text

IDENTITY_HEX_LENGTH = 32
MARKER_PREFIX = "[[note:"
MARKER_SUFFIX = "]]"
MARKER_UTF16_LENGTH = 74

_IDENTIFIER = re.compile(r"[0-9a-f]{32}")
_MARKER = re.compile(r"\[\[note:([0-9a-f]{32}):([0-9a-f]{32})\]\]")


@dataclass(frozen=True, slots=True)
class MarkerIdentity:
    """The two identity components carried by one complete marker."""

    collection_id: str
    note_id: str

    @property
    def marker(self) -> str:
        return format_reference_marker(self.collection_id, self.note_id)


def is_identifier(value: object) -> bool:
    """True only for exactly 32 lowercase hexadecimal characters."""
    return isinstance(value, str) and _IDENTIFIER.fullmatch(value) is not None


def require_identifier(value: object, label: str) -> str:
    if not is_identifier(value):
        raise RuleViolation(
            "INVALID_ARGUMENT",
            f"{label} must be 32 lowercase hexadecimal characters.",
        )
    return value


def new_identifier() -> str:
    """A fresh 128-bit identity; no counter, name, position or index is involved."""
    import secrets

    return secrets.token_hex(16)


def format_reference_marker(collection_id: object, note_id: object) -> str:
    require_identifier(collection_id, "Collection identity")
    require_identifier(note_id, "Note identity")
    return f"{MARKER_PREFIX}{collection_id}:{note_id}{MARKER_SUFFIX}"


def parse_reference_marker(text: object) -> MarkerIdentity | None:
    """Return the identities only for a *complete* marker, otherwise ``None``.

    ``[[note:]]`` and the old short form ``[[note:K1]]`` stay ordinary text and
    never create a relationship.
    """
    if not isinstance(text, str):
        return None
    match = _MARKER.fullmatch(text)
    return None if match is None else MarkerIdentity(match.group(1), match.group(2))


def parse_reference_marker_at(text: str, index: int) -> MarkerIdentity | None:
    """Match a complete marker starting exactly at ``index`` (used by the scanner)."""
    if not isinstance(text, str) or type(index) is not int or index < 0:
        return None
    if text.startswith(MARKER_PREFIX, index):
        end = index + MARKER_UTF16_LENGTH
        if end <= len(text):
            return parse_reference_marker(text[index:end])
    return None


def display_hint(value: object) -> str | None:
    """Validate an optional trusted historical name used only for display.

    A hint is never used to find a target; it is preserved verbatim so a deleted
    or missing reference can still show the last saved name it had.
    """
    if value is None:
        return None
    hint = require_text(value, "Display hint")
    if not hint:
        raise RuleViolation("INVALID_ARGUMENT", "A display hint must be non-empty text or absent.")
    for character in hint:
        if ord(character) < 0x20 or ord(character) == 0x7F or character in "\u2028\u2029":
            raise RuleViolation("INVALID_ARGUMENT", "A display hint must be single-line text.")
    return hint
