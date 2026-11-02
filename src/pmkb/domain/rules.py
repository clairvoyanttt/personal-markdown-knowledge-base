"""Pure, database-free input rules shared by services and storage adapters.

This module deliberately depends on nothing but the standard library: no Qt, no
sqlite3, no filesystem. Callers must not treat Python's dynamic typing as input
validation; every public entry point here re-checks runtime types.
"""
from __future__ import annotations


class RuleViolation(ValueError):
    """A rejected input, carrying the shared business error codes.

    ``code`` reuses the design's error vocabulary (INVALID_NAME, INVALID_TAG,
    TAG_LIMIT, INVALID_ARGUMENT...) so callers can build an AppError without
    parsing the message. It is never a SQL error or a stack trace.
    """

    def __init__(self, code: str, reason: str, action: str = "Correct the input and retry.") -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason
        self.action = action


MAX_VERSION = 2**63 - 1
"""Largest accepted saved revision / collection revision (signed 64-bit)."""


def is_version(value: object, minimum: int = 0) -> bool:
    """True only for a real int in ``minimum..MAX_VERSION``; ``bool`` is rejected."""
    return type(value) is int and minimum <= value <= MAX_VERSION


def require_version(value: object, label: str, minimum: int = 0) -> int:
    if not is_version(value, minimum):
        raise RuleViolation("INVALID_ARGUMENT", f"{label} must be an integer version in range.")
    return value


def require_text(value: object, label: str = "Text") -> str:
    """Reject non-text and unpaired surrogates before any character-level rule."""
    if not isinstance(value, str):
        raise RuleViolation("INVALID_ARGUMENT", f"{label} must be Unicode text.")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as error:
        raise RuleViolation("INVALID_ARGUMENT", f"{label} contains an unpaired surrogate.") from error
    return value
