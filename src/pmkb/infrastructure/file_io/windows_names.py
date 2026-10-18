"""Validation for one Windows filename component, independent of file access.

This does not claim that a path exists, is writable, or fits the target volume's
length limits. In particular it never trims or repairs a user-selected name.
"""


_FORBIDDEN = frozenset('<>:"/\\|?*')
_DEVICES = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"} | {
    f"{prefix}{digit}"
    for prefix in ("COM", "LPT")
    for digit in "123456789¹²³"
}


def validate_windows_basename(name: str) -> str:
    """Return *name* unchanged, or raise ValueError for an unsafe component.

    Device names remain reserved when followed by extensions. Superscript
    digits 1/2/3 are device-number aliases too. No filesystem is touched.
    """
    if not isinstance(name, str) or not name or name in (".", ".."):
        raise ValueError("Expected a non-empty Windows filename component")
    try:
        name.encode("utf-16-le", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("Filename contains an unpaired surrogate") from error
    if any(char in _FORBIDDEN or ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError("Filename contains a forbidden character or control")
    if name.endswith((" ", ".")):
        raise ValueError("Filename must not end in a space or period")
    # A space before an extension must not disguise a Windows device name.
    device_stem = name.split(".", 1)[0].rstrip(" ").upper()
    if device_stem in _DEVICES:
        raise ValueError("Filename is a reserved Windows device name")
    return name
