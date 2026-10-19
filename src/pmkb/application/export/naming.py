"""Pure D2 filename rules; no saved-note lookup, UI, or file publication.

Callers must supply the saved title, not a draft title. These helpers do not
constitute the pending API07 export-service contract.
"""

from pathlib import PureWindowsPath

from pmkb.infrastructure.file_io.windows_names import validate_windows_basename


_REPLACED = frozenset('<>:"/\\|?*')


def default_export_name(saved_name: str) -> str:
    """Generate the D2 default, always appending the export extension once.

    A title that already ends in .md therefore produces .md.md. The saved note
    itself is never modified. Input business validation still belongs to C.
    """
    if not isinstance(saved_name, str):
        raise ValueError("Expected a saved note name")
    try:
        saved_name.encode("utf-16-le", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("Saved name contains an unpaired surrogate") from error
    if any(ord(char) < 32 or ord(char) == 127 or char in "\u2028\u2029" for char in saved_name):
        raise ValueError("Saved name must not contain controls or line separators")
    safe_title = "".join("_" if char in _REPLACED else char for char in saved_name)
    safe_title = safe_title.rstrip(" .")
    result = f"note-{safe_title}.md" if safe_title else "note.md"
    return validate_windows_basename(result)


def selected_export_name(name: str) -> str:
    """Validate a selected basename; append .md only if no extension exists.

    The directory is a separate concern. Unlike the default title conversion,
    invalid user choices are rejected rather than silently sanitized.
    """
    validate_windows_basename(name)
    if name.lower().endswith(".md"):
        return name
    if PureWindowsPath(name).suffix:
        raise ValueError("Export filename must end in .md")
    return validate_windows_basename(name + ".md")
