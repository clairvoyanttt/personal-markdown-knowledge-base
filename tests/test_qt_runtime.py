import asyncio
import os
from pathlib import Path
from threading import Event, get_ident
import tempfile
import time
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from pmkb.application.runtime import AsyncServiceRuntime
from pmkb.contracts.common import CallContext, OperationResult, ResultStatus
from pmkb.desktop.dispatcher import QtDispatcher, DispatcherClosed
from pmkb.desktop.composition import ApplicationServices
from pmkb.application.collection.headless_session import HeadlessCollectionSession
from pmkb.contracts.collections import ActivationMode, PrepareActivationRequest, CompleteActivationRequest


class QtRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.dispatcher = QtDispatcher()
        self.runtime = AsyncServiceRuntime()
        self.addCleanup(self.shutdown)

    def pump(self, predicate):
        until = time.monotonic() + 5
        while not predicate() and time.monotonic() < until:
            self.app.processEvents()
            time.sleep(.001)
        self.assertTrue(predicate(), 'Qt/background operation timed out')

    def shutdown(self):
        self.runtime.begin_shutdown()
        self.pump(lambda: self.runtime.is_stopped)
        self.assertTrue(self.runtime.wait_stopped(1))
        self.dispatcher.close()

    def test_invoke_returns_on_worker_but_callback_uses_qt_main_thread(self):
        async def work(cancel):
            return await self.dispatcher.invoke(get_ident), get_ident()
        handle = self.runtime.submit(work)
        self.pump(handle.outcome.done)
        ui, worker = handle.outcome.result()
        self.assertEqual(ui, get_ident())
        self.assertNotEqual(worker, ui)

    def test_ui_exception_propagates_to_worker_without_stopping_event_loop(self):
        def broken(): raise ValueError('UI callback failed')
        async def work(cancel): return await self.dispatcher.invoke(broken)
        handle = self.runtime.submit(work)
        self.pump(handle.outcome.done)
        with self.assertRaisesRegex(ValueError, 'UI callback failed'):
            handle.outcome.result()

    def test_delivery_uses_delivery_context_not_request_context(self):
        old, current = CallContext(None, 0), CallContext('a'*32, 1)
        values = []
        result = OperationResult(ResultStatus.OK, old, current, 'activated', None)
        self.dispatcher.deliver(result, lambda: current, values.append)
        self.pump(lambda: bool(values))
        self.assertEqual(values, [result])

    def test_stale_result_is_dropped_when_queued_callback_actually_runs(self):
        context = [CallContext('a'*32, 1)]
        values, dropped = [], []
        result = OperationResult(ResultStatus.UNCONFIRMED, context[0], context[0], None, None)
        future = self.dispatcher.deliver(result, lambda: context[0], values.append, dropped.append)
        context[0] = CallContext('b'*32, 2)
        self.pump(future.done)
        self.assertFalse(future.result())
        self.assertEqual(values, [])
        self.assertEqual(dropped, [result])

    def test_close_rejects_queued_callbacks_without_executing_them(self):
        seen = []
        future = self.dispatcher.post(lambda: seen.append('ran'))
        self.dispatcher.close()
        self.app.processEvents()
        self.assertEqual(seen, [])
        with self.assertRaises(DispatcherClosed): future.result()
        with self.assertRaises(DispatcherClosed): self.dispatcher.post(lambda: None).result()

    def test_shutdown_keeps_qt_bridge_alive_for_inflight_work(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        async def work(cancel):
            entered.set()
            await asyncio.to_thread(release.wait, 3)
            return await self.dispatcher.invoke(lambda: ('settled', cancel.is_set()))
        handle = self.runtime.submit(work)
        self.assertTrue(entered.wait(3))
        self.runtime.begin_shutdown()
        release.set()
        self.pump(lambda: self.runtime.is_stopped)
        self.assertEqual(handle.outcome.result(), ('settled', True))

    def test_composition_requires_session_and_does_not_write_config_on_start(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / 'config/identity.json'
            services = ApplicationServices.compose(HeadlessCollectionSession(), config, self.runtime)
            self.assertIs(services.runtime, self.runtime)
            self.assertFalse(config.exists())
            with self.assertRaises(ValueError):
                ApplicationServices.compose(None, config, self.runtime)

    def test_real_collection_activation_uses_qt_bridge(self):
        dispatcher = self.dispatcher
        main_thread = get_ident()
        class BridgedTestSession(HeadlessCollectionSession):
            # No editor in this component test. B supplies the real atomic draft gate.
            async def activate(self, expected, target, expected_buffers, discard_ids, cancel_signals=()):
                def install():
                    if get_ident() != main_thread: raise AssertionError('not Qt thread')
                    if any(signal.is_set() for signal in cancel_signals): return None
                    accepted, actual = self.gate.try_activate(expected, target.collection_id)
                    return actual if accepted else None
                return await dispatcher.invoke(install)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); target = root / 'collection'; target.mkdir()
            session = BridgedTestSession()
            services = ApplicationServices.compose(session, root/'config/identity.json', self.runtime)
            async def flow(cancel):
                prepared = await services.collections.prepare_activation(
                    PrepareActivationRequest(session.context, ActivationMode.CREATE, str(target)), cancel)
                return await services.collections.complete_activation(
                    CompleteActivationRequest(session.context, prepared.value.ticket_id, ()), cancel)
            handle = self.runtime.submit(flow)
            self.pump(handle.outcome.done)
            result = handle.outcome.result()
            self.assertEqual(result.status, ResultStatus.OK)
            self.assertEqual(result.delivery_context, session.context)
            self.assertTrue((target/'knowledge.sqlite').exists())

    def test_shutdown_cleans_prepared_owned_candidate_not_target_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); target = root/'collection'; target.mkdir()
            session = HeadlessCollectionSession()
            services = ApplicationServices.compose(session, root/'config/identity.json', self.runtime)
            handle = self.runtime.submit(lambda cancel: services.collections.prepare_activation(
                PrepareActivationRequest(session.context, ActivationMode.CREATE, str(target)), cancel))
            self.pump(handle.outcome.done)
            self.assertEqual(handle.outcome.result().status, ResultStatus.OK)
            self.assertTrue(tuple(target.iterdir()))
            self.runtime.begin_shutdown()
            self.pump(lambda: self.runtime.is_stopped)
            self.assertTrue(target.exists())
            self.assertEqual(tuple(target.iterdir()), ())

    def test_shutdown_preserves_published_collection_and_closes_service(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); target = root/'collection'; target.mkdir()
            session = HeadlessCollectionSession()
            services = ApplicationServices.compose(session, root/'config.json', self.runtime)
            async def flow(cancel):
                prepared = await services.collections.prepare_activation(
                    PrepareActivationRequest(session.context, ActivationMode.CREATE, str(target)), cancel)
                return await services.collections.complete_activation(
                    CompleteActivationRequest(session.context, prepared.value.ticket_id, ()), cancel)
            handle = self.runtime.submit(flow)
            self.pump(handle.outcome.done)
            self.assertEqual(handle.outcome.result().status, ResultStatus.OK)
            self.runtime.begin_shutdown()
            self.pump(lambda: self.runtime.is_stopped)
            self.assertTrue((target/'knowledge.sqlite').exists())
            result = asyncio.run(services.collections.prepare_activation(
                PrepareActivationRequest(session.context, ActivationMode.OPEN, str(target))))
            self.assertEqual(result.error.code, 'STATE_BLOCKED')

    def test_shutdown_preserves_unknown_artifacts_and_reports_inspection_needed(self):
        with tempfile.TemporaryDirectory() as temp:
            from pmkb.application.collection.ports import CollectionFailure
            root = Path(temp); target = root/'collection'; target.mkdir()
            session = HeadlessCollectionSession()
            services = ApplicationServices.compose(session, root/'config.json', self.runtime)
            def unknown(target): raise CollectionFailure('IO_FAILURE', 'uncertain publication', unknown=True)
            services.collections.backend.publish = unknown
            async def flow(cancel):
                prepared = await services.collections.prepare_activation(
                    PrepareActivationRequest(session.context, ActivationMode.CREATE, str(target)), cancel)
                return await services.collections.complete_activation(
                    CompleteActivationRequest(session.context, prepared.value.ticket_id, ()), cancel)
            handle = self.runtime.submit(flow)
            self.pump(handle.outcome.done)
            self.assertEqual(handle.outcome.result().status, ResultStatus.UNCONFIRMED)
            before = tuple(target.iterdir())
            self.runtime.begin_shutdown()
            self.pump(lambda: self.runtime.is_stopped)
            self.assertEqual(before, tuple(target.iterdir()))
            self.assertEqual(len(self.runtime.shutdown_errors), 1)
