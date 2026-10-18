import asyncio
from threading import Event, get_ident
import unittest

from pmkb.application.runtime import AsyncServiceRuntime, RuntimeClosed


class AsyncRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.runtime = AsyncServiceRuntime()
        self.addCleanup(self.stop)

    def stop(self):
        self.runtime.begin_shutdown()
        self.assertTrue(self.runtime.wait_stopped(5))

    def test_factories_use_one_background_loop(self):
        async def capture(cancel):
            return get_ident(), id(asyncio.get_running_loop())
        one = self.runtime.submit(capture).outcome.result(5)
        two = self.runtime.submit(capture).outcome.result(5)
        self.assertNotEqual(one[0], get_ident())
        self.assertEqual(one, two)

    def test_cancel_is_intent_and_does_not_hide_actual_result(self):
        started, release = Event(), Event()
        self.addCleanup(release.set)
        async def work(cancel):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return ('published', cancel.is_set())
        handle = self.runtime.submit(work)
        self.assertTrue(started.wait(3))
        handle.request_cancel()
        self.assertFalse(handle.outcome.cancel())
        self.assertFalse(handle.outcome.done())
        release.set()
        self.assertEqual(handle.outcome.result(5), ('published', True))

    def test_shutdown_drains_work_then_finalizers_and_executor(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        order = []
        async def work(cancel):
            entered.set()
            await asyncio.to_thread(release.wait, 3)
            order.append('work')
            return cancel.is_set()
        async def close():
            await asyncio.to_thread(order.append, 'closed')
        self.runtime.add_finalizer(close)
        handle = self.runtime.submit(work)
        self.assertTrue(entered.wait(3))
        self.runtime.begin_shutdown()
        self.assertFalse(self.runtime.wait_stopped(.02))
        with self.assertRaises(RuntimeClosed):
            self.runtime.submit(work)
        release.set()
        self.assertTrue(self.runtime.wait_stopped(5))
        self.assertTrue(handle.outcome.result(0))
        self.assertEqual(order, ['work', 'closed'])

    def test_sync_factory_failure_is_observable_and_loop_survives(self):
        def broken(cancel):
            raise ValueError('bad factory')
        with self.assertRaisesRegex(ValueError, 'bad factory'):
            self.runtime.submit(broken).outcome.result(5)
        async def fine(cancel): return 42
        self.assertEqual(self.runtime.submit(fine).outcome.result(5), 42)

    def test_finalizer_failure_is_reported_and_later_cleanup_runs(self):
        order = []
        async def good(): order.append('good')
        async def bad(): raise OSError('cleanup unavailable')
        self.runtime.add_finalizer(good)
        self.runtime.add_finalizer(bad)
        self.stop()
        self.assertEqual(order, ['good'])
        self.assertEqual(len(self.runtime.shutdown_errors), 1)
        self.assertIn('cleanup unavailable', str(self.runtime.shutdown_errors[0]))

    def test_cannot_register_finalizer_during_shutdown(self):
        self.runtime.begin_shutdown()
        async def close(): pass
        with self.assertRaises(RuntimeClosed):
            self.runtime.add_finalizer(close)

    def test_cancel_all_settles_without_stopping_runtime_for_business_exit_checks(self):
        entered, release = Event(), Event()
        self.addCleanup(release.set)
        async def work(cancel):
            entered.set()
            await asyncio.to_thread(release.wait, 3)
            return cancel.is_set()
        handle = self.runtime.submit(work)
        self.assertTrue(entered.wait(3))
        self.assertEqual(self.runtime.pending_count, 1)
        self.runtime.request_cancel_all()
        release.set()
        self.assertTrue(handle.outcome.result(5))
        async def check_receipt(cancel): return 'still able to resolve CB02'
        self.assertEqual(self.runtime.submit(check_receipt).outcome.result(5), 'still able to resolve CB02')

    def test_immediate_shutdown_does_not_skip_accepted_operation(self):
        async def work(cancel): return ('settled', cancel.is_set())
        handle = self.runtime.submit(work)
        self.runtime.begin_shutdown()
        self.assertTrue(self.runtime.wait_stopped(5))
        self.assertEqual(handle.outcome.result()[0], 'settled')
