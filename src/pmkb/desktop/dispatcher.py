"""Queued synchronous UI callbacks; no widgets are touched on worker threads."""
import asyncio
from collections.abc import Callable
from threading import Lock
from typing import TypeVar

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal, Slot
from pmkb.application.runtime import OutcomeFuture
from pmkb.contracts.common import CallContext, OperationResult

T = TypeVar('T')


class DispatcherClosed(RuntimeError):
    pass


class QtDispatcher(QObject):
    _requested = Signal(object, object)

    def __init__(self) -> None:
        app = QCoreApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError('Create the dispatcher on the Qt application thread.')
        super().__init__()
        self._lock = Lock()
        self._closed = False
        self._pending: set[OutcomeFuture] = set()
        self._executing = 0
        self._requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    def post(self, callback: Callable[[], T]) -> OutcomeFuture[T]:
        """Always queued, including submissions from the Qt thread itself."""
        future = OutcomeFuture()
        with self._lock:
            if self._closed:
                future.set_exception(DispatcherClosed('Qt dispatcher is closed.'))
                return future
            self._pending.add(future)
        self._requested.emit(callback, future)
        return future

    async def invoke(self, callback: Callable[[], T]) -> T:
        # Cancellation must not silently cancel a queued activation/commit callback.
        # Side-effect callbacks must recheck context, buffer versions and cancel signals.
        return await asyncio.shield(asyncio.wrap_future(self.post(callback)))

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending) + self._executing

    @Slot(object, object)
    def _execute(self, callback, future) -> None:
        with self._lock:
            if future not in self._pending:
                return
            self._pending.remove(future)
            self._executing += 1
        try:
            value = callback()
        except BaseException as error:
            future.set_exception(error)
        else:
            future.set_result(value)
        finally:
            with self._lock:
                self._executing -= 1

    def deliver(self, result: OperationResult[T], current_context: Callable[[], CallContext],
                apply: Callable[[OperationResult[T]], None],
                stale: Callable[[OperationResult[T]], None] | None = None) -> OutcomeFuture[bool]:
        """Check at the actual UI delivery boundary, including activation results.

        Dropped results remain on OperationHandle.outcome; dropping presentation
        never undoes IO or resolves UNCONFIRMED. Do not log note bodies in stale().
        """
        def accept():
            if result.delivery_context != current_context():
                if stale is not None:
                    stale(result)
                return False
            apply(result)
            return True
        return self.post(accept)

    def close(self) -> None:
        """Only after runtime drain and all needed UI deliveries; call on Qt thread."""
        if QThread.currentThread() != self.thread():
            raise RuntimeError('Close the dispatcher on its Qt thread.')
        with self._lock:
            self._closed = True
            pending, self._pending = self._pending, set()
        for future in pending:
            future.set_exception(DispatcherClosed('Qt dispatcher closed before delivery.'))
