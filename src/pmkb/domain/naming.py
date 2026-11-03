"""Name and tag rules: the single shared implementation for B/C/D.

Rules implemented here (design §7, FR01/FR04):

* Reject C0 controls, DEL, U+2028/U+2029 and unpaired surrogates *before*
  trimming, so a leading/trailing tab is refused rather than silently removed.
* Trim only the explicit whitespace set below; Python's default ``str.strip``
  is broader and must not change the accepted input.
* Name: 1..100 Unicode code points after trimming.
* Tag: 1..30 Unicode code points; at most 20 distinct tags per note; duplicates
  of the exact same string do not occupy another slot; ``AI`` and ``ai`` differ.
* Name key folds ASCII ``A``-``Z`` only, never ``casefold``/Unicode normalisation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from pmkb.domain.rules import RuleViolation, require_text

MAX_NAME_CODEPOINTS = 100
MAX_TAG_CODEPOINTS = 30
MAX_TAGS_PER_NOTE = 20

_ASCII_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

TRIM_CHARACTERS = frozenset(
    "\u0020\u0085\u00a0\u1680\u202f\u205f\u3000"
    + "".join(chr(codepoint) for codepoint in range(0x2000, 0x200B))
)
"""Allowed leading/trailing whitespace, exactly the design's explicit set."""


def _reject_forbidden_characters(text: str, label: str, code: str) -> None:
    for character in text:
        codepoint = ord(character)
        if codepoint < 0x20 or codepoint == 0x7F or character in "\u2028\u2029":
            raise RuleViolation(code, f"{label} contains a control or line-separator character.")


def trim_edges(text: str) -> str:
    """Trim only :data:`TRIM_CHARACTERS` from both ends (not ``str.strip``)."""
    start, end = 0, len(text)
    while start < end and text[start] in TRIM_CHARACTERS:
        start += 1
    while end > start and text[end - 1] in TRIM_CHARACTERS:
        end -= 1
    return text[start:end]


def name_key(name: str) -> str:
    """Case-insensitive comparison key for names: ASCII A-Z folding only."""
    return require_text(name, "Name").translate(_ASCII_FOLD)


@dataclass(frozen=True, slots=True)
class NormalizedName:
    """The accepted name and the stored comparison key."""

    name: str
    key: str


def normalize_name(raw: object) -> NormalizedName:
    """Validate and trim a note name; raise :class:`RuleViolation` when invalid."""
    text = require_text(raw, "Name")
    _reject_forbidden_characters(text, "Name", "INVALID_NAME")
    name = trim_edges(text)
    if not name:
        raise RuleViolation("INVALID_NAME", "A note name cannot be empty after trimming.")
    if len(name) > MAX_NAME_CODEPOINTS:
        raise RuleViolation(
            "INVALID_NAME",
            f"A note name must be at most {MAX_NAME_CODEPOINTS} code points.",
        )
    return NormalizedName(name, name_key(name))


def normalize_tag(raw: object) -> str:
    """Validate and trim one tag; raise :class:`RuleViolation` when invalid."""
    text = require_text(raw, "Tag")
    _reject_forbidden_characters(text, "Tag", "INVALID_TAG")
    tag = trim_edges(text)
    if not tag:
        raise RuleViolation("INVALID_TAG", "A tag cannot be empty after trimming.")
    if len(tag) > MAX_TAG_CODEPOINTS:
        raise RuleViolation("INVALID_TAG", f"A tag must be at most {MAX_TAG_CODEPOINTS} code points.")
    return tag


def normalize_tags(raw_tags: object) -> tuple[str, ...]:
    """Validate a whole tag list, keeping first-seen order and exact duplicates out."""
    if isinstance(raw_tags, str) or not isinstance(raw_tags, Iterable):
        raise RuleViolation("INVALID_TAG", "Tags must be a sequence of tag strings.")
    accepted: list[str] = []
    seen: set[str] = set()
    for raw in raw_tags:
        tag = normalize_tag(raw)
        if tag in seen:
            continue
        seen.add(tag)
        accepted.append(tag)
    if len(accepted) > MAX_TAGS_PER_NOTE:
        raise RuleViolation(
            "TAG_LIMIT",
            f"A note may keep at most {MAX_TAGS_PER_NOTE} different tags.",
            "Remove a tag before adding another one.",
        )
    return tuple(accepted)
