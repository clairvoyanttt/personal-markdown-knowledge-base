"""One background event loop. Shutdown drains; cancellation is business intent."""
import asyncio
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field
from threading import Event, Lock, Thread, current_thread
from typing import Generic, TypeVar

T = TypeVar('T')


class RuntimeClosed(RuntimeError):
    pass


class OutcomeFuture(Future[T]):
    """Observers cannot cancel persistence or erase its actual outcome."""
    def cancel(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class OperationHandle(Generic[T]):
    outcome: OutcomeFuture[T]
    _cancel: Event = field(repr=False)

    def request_cancel(self) -> None:
        self._cancel.set()


class AsyncServiceRuntime:
    """Submit factories, not pre-created coroutines; never block Qt on outcomes.

    Factories/finalizers run on this loop and may offload synchronous IO with
    asyncio.to_thread. All child work must be awaited by its owning operation.
    Finalizers run in reverse registration order, after accepted operations.
    """
    def __init__(self) -> None:
        self._lock = Lock()
        self._accepting = True
        self._ready, self._stopped = Event(), Event()
        self._handles: dict[int, OperationHandle] = {}
        self._tasks: set[asyncio.Task] = set()
        self._finalizers: list[Callable[[], Awaitable[None]]] = []
        self._errors: list[BaseException] = []
        self._next_id = 0
        self._thread = Thread(target=self._run_loop, name='pmkb-services', daemon=False)
        self._thread.start()
        self._ready.wait()

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
            self._stopped.set()

    @property
    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    @property
    def shutdown_errors(self) -> tuple[BaseException, ...]:
        with self._lock:
            return tuple(self._errors)

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._handles)

    def request_cancel_all(self) -> None:
        """First exit phase: settle work but keep receipt checks available.

        Caller must disable new user commands while quiescing. This does not
        approve draft loss or decide whether a pending save has committed.
        """
        with self._lock:
            for handle in self._handles.values():
                handle.request_cancel()

    def submit(self, factory: Callable[[Event], Awaitable[T]]) -> OperationHandle[T]:
        if not callable(factory):
            raise TypeError('Pass a factory accepting a cancellation Event, not a coroutine.')
        handle = OperationHandle(OutcomeFuture(), Event())
        with self._lock:
            if not self._accepting:
                raise RuntimeClosed('The service runtime is shutting down.')
            operation_id = self._next_id
            self._next_id += 1
            self._handles[operation_id] = handle
            # Ordering under the same lock ensures shutdown sees every submission.
            self._loop.call_soon_threadsafe(self._start, operation_id, factory, handle)
        return handle

    def _start(self, operation_id, factory, handle) -> None:
        task = self._loop.create_task(self._execute(operation_id, factory, handle))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _execute(self, operation_id, factory, handle) -> None:
        try:
            value = await factory(handle._cancel)
        except BaseException as error:
            # Unexpected cancellation is observable, never fabricated as rollback.
            handle.outcome.set_exception(error)
        else:
            handle.outcome.set_result(value)
        finally:
            with self._lock:
                self._handles.pop(operation_id, None)

    def add_finalizer(self, close: Callable[[], Awaitable[None]]) -> None:
        if not callable(close):
            raise TypeError('Finalizer must be an async callable.')
        with self._lock:
            if not self._accepting:
                raise RuntimeClosed('Cannot register resources during shutdown.')
            self._finalizers.append(close)

    def begin_shutdown(self) -> None:
        """Nonblocking and idempotent. Keep Qt pumping until is_stopped."""
        with self._lock:
            if not self._accepting:
                return
            self._accepting = False
            for handle in self._handles.values():
                handle.request_cancel()
            self._loop.call_soon_threadsafe(lambda: self._loop.create_task(self._drain()))

    async def _drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        for close in reversed(self._finalizers):
            try:
                await close()
            except BaseException as error:
                with self._lock:
                    self._errors.append(error)
        await self._loop.shutdown_asyncgens()
        # No timeout/forced thread termination: ongoing IO must actually settle.
        await self._loop.shutdown_default_executor()
        self._loop.stop()

    def wait_stopped(self, timeout: float | None = None) -> bool:
        """For non-Qt tests/CLI only. UI must poll is_stopped instead of joining."""
        if current_thread() is self._thread:
            raise RuntimeError('The service thread cannot join itself.')
        self._thread.join(timeout)
        return not self._thread.is_alive()
