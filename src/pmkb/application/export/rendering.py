"""FR09 text replacement over C-authorized UTF-16 source spans only.

ExportReference and ReferenceKind are D-internal adapter inputs, NOT an agreed
API05/API07 contract. C must supply the classified saved-version occurrences
from its shared parser and saved target projection. This module neither finds
markers nor decides collection ownership, target existence, or code protection.
No database, Qt, network, or filesystem access occurs here.
"""

from dataclasses import dataclass
from enum import Enum
import re
from string import punctuation
from typing import Iterable

from pmkb.domain.text_spans import slice_utf16


class ReferenceKind(Enum):
    VALID = "VALID"
    DELETED = "DELETED"
    MISSING = "MISSING"
    FOREIGN_COLLECTION = "FOREIGN_COLLECTION"


@dataclass(frozen=True)
class ExportReference:
    """One authorized occurrence, not a newly parsed marker.

    target_name is the saved name or trusted historical display hint supplied
    by C; the renderer never looks up a matching name in another collection.
    """

    start_utf16: int
    length_utf16: int
    marker_text: str
    kind: ReferenceKind
    target_name: str | None = None


_MARKER = re.compile(r"\[\[note:[0-9a-f]{32}:[0-9a-f]{32}\]\]")
_ESCAPED = frozenset(punctuation)


def _display_name(name: str | None, *, required: bool) -> str:
    if name is None and not required:
        return "未知目标"
    if not isinstance(name, str) or not name:
        raise ValueError("A valid reference requires its saved target name")
    try:
        name.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("Target name contains an unpaired surrogate") from error
    if any(ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029" for char in name):
        raise ValueError("Target name must be single-line text without controls")
    # CommonMark allows backslash escaping of every ASCII punctuation character.
    # This also prevents entity/HTML or nested marker syntax from target names.
    return "".join("\\" + char if char in _ESCAPED else char for char in name)


def _replacement(item: ExportReference) -> str:
    match item.kind:
        case ReferenceKind.VALID:
            name = _display_name(item.target_name, required=True)
            return f"内部引用：{name}（仅供识别，导出后不支持应用内跳转）"
        case ReferenceKind.DELETED:
            name = _display_name(item.target_name, required=False)
            return f"失效内部引用：{name}（目标已删除，无法跳转）"
        case ReferenceKind.MISSING:
            name = _display_name(item.target_name, required=False)
            return f"失效内部引用：{name}（目标标识不存在或不可用，无法跳转）"
        case ReferenceKind.FOREIGN_COLLECTION:
            name = _display_name(item.target_name, required=False)
            return f"失效内部引用：{name}（引用来自其他集合，无法跳转）"
        case _:
            raise ValueError("Unknown reference classification")


def render_export_markdown(saved_body: str, references: Iterable[ExportReference]) -> str:
    """Replace only checked, non-overlapping spans; preserve all other text.

    This is not a parser or a complete export service. Calling with no supplied
    occurrences does not scan for markers. It intentionally leaves code, links,
    and images unchanged when C omits those protected spans. Names are escaped
    into ordinary text; arbitrary reader auto-link extensions are not promised.
    """
    if not isinstance(saved_body, str):
        raise ValueError("Expected saved Markdown text")
    # Existing domain conversion rejects unpaired surrogates even outside spans.
    slice_utf16(saved_body, 0, 0)
    try:
        occurrences = tuple(references)
    except TypeError as error:
        raise ValueError("Expected authorized reference occurrences") from error
    checked: list[tuple[ExportReference, str]] = []
    for item in occurrences:
        if not isinstance(item, ExportReference):
            raise ValueError("Expected an ExportReference adapter input")
        if type(item.start_utf16) is not int or type(item.length_utf16) is not int or item.length_utf16 <= 0:
            raise ValueError("Reference span must use integer UTF-16 units and be non-empty")
        if not isinstance(item.marker_text, str) or not _MARKER.fullmatch(item.marker_text):
            raise ValueError("Authorized span must contain a complete literal reference marker")
        if slice_utf16(saved_body, item.start_utf16, item.length_utf16) != item.marker_text:
            raise ValueError("Authorized marker does not match the original saved source")
        checked.append((item, _replacement(item)))
    checked.sort(key=lambda pair: pair[0].start_utf16)
    encoded = saved_body.encode("utf-16-le", errors="strict")
    chunks: list[str] = []
    cursor = 0
    for item, replacement in checked:
        if item.start_utf16 < cursor:
            raise ValueError("Authorized reference spans must not overlap")
        chunks.append(encoded[cursor * 2:item.start_utf16 * 2].decode("utf-16-le"))
        chunks.append(replacement)
        cursor = item.start_utf16 + item.length_utf16
    chunks.append(encoded[cursor * 2:].decode("utf-16-le"))
    return "".join(chunks)
