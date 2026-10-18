"""Explicit conversion for the retained UTF-16 half-open span contract.

Python indexes Unicode code points; slicing by a Qt/.NET UTF-16 index directly
would silently corrupt spans after non-BMP characters. No parser is provided.
"""


def _encoded(text: str) -> bytes:
    if not isinstance(text, str):
        raise ValueError("Expected Unicode text")
    try:
        return text.encode("utf-16-le", errors="strict")
    except UnicodeEncodeError as error:
        raise ValueError("Unpaired surrogate in text") from error


def to_utf16_offset(text: str, codepoint_offset: int) -> int:
    _encoded(text)
    if type(codepoint_offset) is not int or not 0 <= codepoint_offset <= len(text):
        raise ValueError("Code-point offset out of range")
    return len(text[:codepoint_offset].encode("utf-16-le")) // 2


def slice_utf16(text: str, start: int, length: int) -> str:
    data = _encoded(text)
    if (type(start) is not int or type(length) is not int
            or start < 0 or length < 0 or start + length > len(data) // 2):
        raise ValueError("UTF-16 span out of range")
    try:
        # Validate the start even for an empty slice inside a surrogate pair.
        data[:start * 2].decode("utf-16-le", errors="strict")
        return data[start * 2:(start + length) * 2].decode("utf-16-le", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("UTF-16 span splits a surrogate pair") from error
