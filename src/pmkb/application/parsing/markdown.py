"""One shared, dependency-free Markdown scanner for M04.

Why no third-party parser: the approved design leaves the library choice to C
but requires a fixed version/licence and a *verifiable* UTF-16 span contract.
A small scanner over the six accepted structures keeps the product fully offline
and makes every protected span auditable by the caller; a full CommonMark engine
would add a runtime dependency and a licence review that nobody has approved.

Scope and honest limits: headings, paragraphs, list items, block quotes, fenced
code, inline code, inline/reference links, images, autolinks and reference
definitions. Emphasis, tables, formulas, raw HTML and nested-block reflow are
not interpreted; their characters are left as ordinary text. This is not a
renderer and never rewrites the source.
"""
from __future__ import annotations

import re
from typing import Iterable

from pmkb.application.parsing.document import (
    Block,
    BlockKind,
    Ownership,
    ParsedDocument,
    ProtectedKind,
    ProtectedSpan,
    ReferenceOccurrence,
)
from pmkb.domain.identity import (
    MARKER_UTF16_LENGTH,
    MarkerIdentity,
    parse_reference_marker,
    require_identifier,
)
from pmkb.domain.rules import require_text


_MARKER = re.compile(r"\[\[note:[0-9a-f]{32}:[0-9a-f]{32}\]\]")
_FENCE_OPEN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_QUOTE = re.compile(r"^ {0,3}>")
_LIST_ITEM = re.compile(r"^ {0,3}([-+*]|\d{1,9}[.)])(?:[ \t]+|$)")
_BLANK = re.compile(r"^[ \t]*$")
_REFERENCE_DEFINITION = re.compile(r"^ {0,3}\[[^\[\]]+\]:[ \t]*\S")
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\s]*|[^<>\s@]+@[^<>\s@]+)>"
)


def parse_markdown(body: object, collection_id: object) -> ParsedDocument:
    """Parse one draft or saved body into protected spans, references and blocks.

    Protected spans are computed *before* markers are accepted, so a marker that
    only looks complete inside code, a link, an image, an autolink or after a
    backslash escape never becomes a relationship (FR06 acceptance 10).
    """
    text = require_text(body, "Markdown body")
    identity = require_identifier(collection_id, "Collection identity")

    protected: list[tuple[ProtectedKind, int, int]] = []
    blocks: list[tuple[BlockKind, int, int, int | None, str | None]] = []
    fences: list[tuple[int, int]] = []
    _scan_lines(text, protected, blocks, fences)
    for start, end in _free_segments(text, fences):
        _scan_inline(text, start, end, protected)
    markers = _scan_markers(text, protected, identity)

    prefix = _utf16_prefix(text)
    protected_spans = _to_utf16_spans(text, protected, prefix)
    block_list = _to_utf16_blocks(text, blocks, prefix)
    occurrences = tuple(
        ReferenceOccurrence(
            ordinal=ordinal,
            start_utf16=start,
            length_utf16=MARKER_UTF16_LENGTH,
            marker_text=marker_text,
            target_collection_id=parsed.collection_id,
            target_note_id=parsed.note_id,
            ownership=ownership,
        )
        for ordinal, start, marker_text, parsed, ownership in _utf16_markers(text, markers, prefix)
    )
    return ParsedDocument(identity, text, protected_spans, occurrences, block_list)


# --------------------------------------------------------------------------
# line-level scanning


def _iter_lines(text: str) -> Iterable[tuple[int, int, int]]:
    """Yield ``(line_start, content_end, next_start)`` for every physical line."""
    length = len(text)
    start = 0
    while start < length:
        cursor = start
        while cursor < length and text[cursor] not in "\r\n":
            cursor += 1
        content_end = cursor
        if cursor < length:
            if text[cursor] == "\r" and cursor + 1 < length and text[cursor + 1] == "\n":
                cursor += 2
            else:
                cursor += 1
        yield start, content_end, cursor
        start = cursor


def _scan_lines(
    text: str,
    protected: list[tuple[ProtectedKind, int, int]],
    blocks: list[tuple[BlockKind, int, int, int | None, str | None]],
    fences: list[tuple[int, int]],
) -> None:
    open_fence: tuple[str, int, int] | None = None  # char, length, block start
    paragraph: tuple[int, int] | None = None  # start, content end

    def close_paragraph() -> None:
        nonlocal paragraph
        if paragraph is not None:
            blocks.append((BlockKind.PARAGRAPH, paragraph[0], paragraph[1] - paragraph[0], None, None))
            paragraph = None

    for line_start, content_end, _next_start in _iter_lines(text):
        line = text[line_start:content_end]
        if open_fence is not None:
            fence_char, fence_length, block_start = open_fence
            if _is_closing_fence(line, fence_char, fence_length):
                protected.append((ProtectedKind.FENCED_CODE, block_start, content_end - block_start))
                blocks.append((BlockKind.CODE_FENCE, block_start, content_end - block_start, None, fence_char))
                fences.append((block_start, content_end))
                open_fence = None
            continue
        match = _FENCE_OPEN.match(line)
        if match is not None and not (match.group(1)[0] == "`" and "`" in match.group(2)):
            close_paragraph()
            open_fence = (match.group(1)[0], len(match.group(1)), line_start)
            continue
        if _BLANK.match(line):
            close_paragraph()
            continue
        if _REFERENCE_DEFINITION.match(line):
            close_paragraph()
            protected.append((ProtectedKind.REFERENCE_DEFINITION, line_start, content_end - line_start))
            continue
        heading = _HEADING.match(line)
        if heading is not None and content_end > line_start:
            close_paragraph()
            blocks.append((BlockKind.HEADING, line_start, content_end - line_start, len(heading.group(1)), None))
            continue
        if _QUOTE.match(line):
            close_paragraph()
            blocks.append((BlockKind.BLOCK_QUOTE, line_start, content_end - line_start, None, ">"))
            continue
        list_match = _LIST_ITEM.match(line)
        if list_match is not None:
            close_paragraph()
            blocks.append(
                (BlockKind.LIST_ITEM, line_start, content_end - line_start, None, list_match.group(1))
            )
            continue
        if paragraph is None:
            paragraph = (line_start, content_end)
        else:
            paragraph = (paragraph[0], content_end)

    close_paragraph()
    if open_fence is not None:
        _fence_char, _fence_length, block_start = open_fence
        # An unclosed fence runs to the end of the document (CommonMark behaviour).
        protected.append((ProtectedKind.FENCED_CODE, block_start, len(text) - block_start))
        blocks.append((BlockKind.CODE_FENCE, block_start, len(text) - block_start, None, _fence_char))
        fences.append((block_start, len(text)))


def _is_closing_fence(line: str, fence_char: str, fence_length: int) -> bool:
    stripped = line.lstrip(" ")
    if len(line) - len(stripped) > 3:
        return False
    run = len(stripped) - len(stripped.lstrip(fence_char))
    return run >= fence_length and stripped[run:].strip(" \t") == ""


def _free_segments(text: str, fences: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Segments of text outside fenced code blocks, in order."""
    segments: list[tuple[int, int]] = []
    cursor = 0
    for start, end in sorted(fences):
        if start > cursor:
            segments.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < len(text):
        segments.append((cursor, len(text)))
    return segments


# --------------------------------------------------------------------------
# inline scanning


def _scan_inline(
    text: str, start: int, end: int, protected: list[tuple[ProtectedKind, int, int]]
) -> None:
    cursor = start
    while cursor < end:
        character = text[cursor]
        if character == "\\":
            if cursor + 1 < end:
                protected.append((ProtectedKind.ESCAPED, cursor, 2))
                cursor += 2
            else:
                cursor += 1
            continue
        if character == "`":
            close = _code_span_end(text, cursor, end)
            if close is None:
                cursor = _run_end(text, cursor, end)
                continue
            protected.append((ProtectedKind.INLINE_CODE, cursor, close - cursor))
            cursor = close
            continue
        if character == "!" and cursor + 1 < end and text[cursor + 1] == "[":
            link_end = _link_end(text, cursor + 1, end)
            if link_end is not None:
                protected.append((ProtectedKind.IMAGE, cursor, link_end - cursor))
                cursor = link_end
                continue
            cursor += 1
            continue
        if character == "[":
            link_end = _link_end(text, cursor, end)
            if link_end is not None:
                protected.append((ProtectedKind.LINK, cursor, link_end - cursor))
                cursor = link_end
                continue
            cursor += 1
            continue
        if character == "<":
            autolink = _AUTOLINK.match(text, cursor, end)
            if autolink is not None:
                protected.append((ProtectedKind.AUTOLINK, cursor, autolink.end() - cursor))
                cursor = autolink.end()
                continue
        cursor += 1


def _run_end(text: str, start: int, end: int) -> int:
    cursor = start
    while cursor < end and text[cursor] == "`":
        cursor += 1
    return cursor


def _code_span_end(text: str, start: int, end: int) -> int | None:
    run = _run_end(text, start, end) - start
    cursor = start + run
    while cursor < end:
        if text[cursor] != "`":
            cursor += 1
            continue
        candidate_end = _run_end(text, cursor, end)
        if candidate_end - cursor == run:
            return candidate_end
        cursor = candidate_end
    return None


def _link_end(text: str, open_index: int, end: int) -> int | None:
    """Return the exclusive end of an inline link, image or reference link.

    Link *text* only balances brackets: an apostrophe or double quote there is
    ordinary text, not a quoting state (C-REV-03). The destination part separates
    a bare/angle destination from an optional title, so a quote or parenthesis
    inside a URL cannot hide the closing ``)`` and leave a marker unprotected.
    The returned index is one past the closing delimiter so a protected span
    covers the complete construct (C-REV-04).
    """
    close = _balanced(text, open_index, end)
    if close is None:
        return None
    after = close + 1
    if after >= end:
        return None
    if text[after] == "(":
        return _destination_end(text, after + 1, end)
    if text[after] == "[":
        label_close = _balanced(text, after, end)
        return None if label_close is None else label_close + 1
    return None


def _balanced(text: str, open_index: int, end: int) -> int | None:
    """Index of the ``]`` closing the ``[`` at ``open_index``, honouring escapes."""
    depth = 0
    cursor = open_index
    while cursor < end:
        character = text[cursor]
        if character == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if character == "[":
            depth += 1
        elif character == "]":
            depth -= 1
            if depth == 0:
                return cursor
        cursor += 1
    return None


def _destination_end(text: str, cursor: int, end: int) -> int | None:
    """Parse ``destination [title] )``; return the index after the closing paren."""
    cursor = _skip_blank(text, cursor, end)
    if cursor < end and text[cursor] == "<":
        cursor = _angle_end(text, cursor, end)
    else:
        cursor = _bare_destination_end(text, cursor, end)
    if cursor is None or cursor >= end:
        return None
    if text[cursor] == ")":
        return cursor + 1
    cursor = _skip_blank(text, cursor, end)
    if cursor >= end:
        return None
    if text[cursor] in "\"'":
        cursor = _quoted_end(text, cursor, end)
    elif text[cursor] == "(":
        cursor = _parenthesised_end(text, cursor, end)
    else:
        return None
    if cursor is None:
        return None
    cursor = _skip_blank(text, cursor, end)
    if cursor < end and text[cursor] == ")":
        return cursor + 1
    return None


def _skip_blank(text: str, cursor: int, end: int) -> int:
    while cursor < end and text[cursor] in " \t\r\n":
        cursor += 1
    return cursor


def _angle_end(text: str, cursor: int, end: int) -> int | None:
    """Return the index after ``<destination>``."""
    cursor += 1
    while cursor < end:
        character = text[cursor]
        if character == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if character == ">":
            return cursor + 1
        if character == "<":
            return None
        cursor += 1
    return None


def _bare_destination_end(text: str, cursor: int, end: int) -> int | None:
    """Return where a bare destination ends (blank at depth 0 or the closing ``)``)."""
    depth = 0
    while cursor < end:
        character = text[cursor]
        if character == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if character in " \t\r\n":
            return cursor if depth == 0 else None
        if character == "(":
            depth += 1
        elif character == ")":
            if depth == 0:
                return cursor
            depth -= 1
        cursor += 1
    return None


def _quoted_end(text: str, cursor: int, end: int) -> int | None:
    """Return the index after a quoted title (``"..."`` or ``'...'``)."""
    quote = text[cursor]
    cursor += 1
    while cursor < end:
        character = text[cursor]
        if character == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if character == quote:
            return cursor + 1
        cursor += 1
    return None


def _parenthesised_end(text: str, cursor: int, end: int) -> int | None:
    """Return the index after a parenthesised title ``(...)``."""
    depth = 0
    while cursor < end:
        character = text[cursor]
        if character == "\\" and cursor + 1 < end:
            cursor += 2
            continue
        if character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
            if depth == 0:
                return cursor + 1
        cursor += 1
    return None


# --------------------------------------------------------------------------
# markers


def _scan_markers(
    text: str,
    protected: list[tuple[ProtectedKind, int, int]],
    collection_id: str,
) -> list[tuple[int, int, str, MarkerIdentity, Ownership]]:
    ranges = sorted((start, start + length) for _kind, start, length in protected)
    found: list[tuple[int, int, str, MarkerIdentity, Ownership]] = []
    ordinal = 0
    for match in _MARKER.finditer(text):
        start, end = match.start(), match.end()
        if any(protected_start < end and start < protected_end for protected_start, protected_end in ranges):
            continue
        identity = parse_reference_marker(match.group(0))
        if identity is None:
            continue
        ownership = Ownership.LOCAL if identity.collection_id == collection_id else Ownership.FOREIGN
        found.append((ordinal, start, match.group(0), identity, ownership))
        ordinal += 1
    return found


# --------------------------------------------------------------------------
# code-point to UTF-16 conversion


def _utf16_prefix(text: str) -> list[int]:
    """Cumulative UTF-16 lengths per code-point boundary (index 0 is always 0).

    Unpaired surrogates are rejected by ``require_text`` before this runs, so a
    code point is either one UTF-16 unit (BMP) or two (astral plane).
    """
    prefix = [0] * (len(text) + 1)
    offset = 0
    for index, character in enumerate(text):
        offset += 2 if ord(character) > 0xFFFF else 1
        prefix[index + 1] = offset
    return prefix


def _to_utf16_spans(
    text: str, spans: list[tuple[ProtectedKind, int, int]],
    prefix: list[int] | None = None,
) -> tuple[ProtectedSpan, ...]:
    units = _utf16_prefix(text) if prefix is None else prefix
    ordered = sorted(spans, key=lambda item: (item[1], item[2]))
    return tuple(
        ProtectedSpan(kind, units[start], units[start + length] - units[start])
        for kind, start, length in ordered
    )


def _to_utf16_blocks(
    text: str, blocks: list[tuple[BlockKind, int, int, int | None, str | None]],
    prefix: list[int] | None = None,
) -> tuple[Block, ...]:
    units = _utf16_prefix(text) if prefix is None else prefix
    ordered = sorted(blocks, key=lambda item: (item[1], item[2]))
    return tuple(
        Block(kind, units[start], units[start + length] - units[start], level, marker)
        for kind, start, length, level, marker in ordered
    )


def _utf16_markers(
    text: str, markers: list[tuple[int, int, str, MarkerIdentity, Ownership]],
    prefix: list[int] | None = None,
) -> list[tuple[int, int, str, MarkerIdentity, Ownership]]:
    units = _utf16_prefix(text) if prefix is None else prefix
    return [
        (ordinal, units[start], marker_text, identity, ownership)
        for ordinal, start, marker_text, identity, ownership in markers
    ]
