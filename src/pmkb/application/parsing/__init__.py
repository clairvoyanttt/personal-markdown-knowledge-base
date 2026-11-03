"""M04: the single shared Markdown adapter.

Every consumer (preview, references, export, restore) must use
:func:`parse_markdown` instead of scanning markers itself. Nothing here writes
to the database, the filesystem or the UI.
"""
from pmkb.application.parsing.document import (
    Block,
    BlockKind,
    Ownership,
    ParsedDocument,
    ProtectedKind,
    ProtectedSpan,
    ReferenceOccurrence,
    ReferenceState,
    ResolvedReference,
)
from pmkb.application.parsing.markdown import parse_markdown

__all__ = [
    "Block",
    "BlockKind",
    "Ownership",
    "ParsedDocument",
    "ProtectedKind",
    "ProtectedSpan",
    "ReferenceOccurrence",
    "ReferenceState",
    "ResolvedReference",
    "parse_markdown",
]
