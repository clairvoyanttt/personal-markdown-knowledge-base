"""Final in-memory activation gate, not a collection/file lifecycle service."""
import re
from threading import Lock

from pmkb.contracts.common import CallContext


class ActiveContextGate:
    def __init__(self, *, max_generation: int = (1 << 63) - 1) -> None:
        if type(max_generation) is not int or max_generation < 1:
            raise ValueError("max_generation must be a positive integer")
        self._lock = Lock()
        self._current = CallContext(None, 0)
        self._max_generation = max_generation

    @property
    def current(self) -> CallContext:
        with self._lock:
            return self._current

    def is_current(self, delivery: CallContext) -> bool:
        if not self._valid_context(delivery):
            return False
        with self._lock:
            return self._current == delivery

    @staticmethod
    def _valid_context(context: CallContext) -> bool:
        return (isinstance(context, CallContext)
                and type(context.context_generation) is int
                and 0 <= context.context_generation <= (1 << 63) - 1
                and (context.collection_id is None
                     or (isinstance(context.collection_id, str)
                         and re.fullmatch(r"[0-9a-f]{32}", context.collection_id) is not None)))

    def try_activate(self, expected: CallContext, target_collection_id: str) -> tuple[bool, CallContext]:
        """Call only after target/draft/publication/registration checks succeed.

        The coordinator must call this at the UI activation boundary; this gate
        neither verifies a ticket nor saves/discards a draft or performs IO.
        """
        if not self._valid_context(expected):
            raise ValueError("Invalid expected context")
        if not isinstance(target_collection_id, str) or not re.fullmatch(r"[0-9a-f]{32}", target_collection_id):
            raise ValueError("Expected a 32-character lowercase hexadecimal ID")
        with self._lock:
            if expected != self._current:
                return False, self._current
            if self._current.context_generation >= self._max_generation:
                raise OverflowError("Context generation cannot wrap")
            self._current = CallContext(target_collection_id, self._current.context_generation + 1)
            return True, self._current
