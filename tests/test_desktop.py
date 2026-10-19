import os
import unittest
import asyncio
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
# These checks exercise A's no-editor shell; B's real editor has its own guards
# and integration checks in test_editing_desktop.py.
from pmkb.desktop.app import create_startup_window as create_main_window


class DesktopStartupTests(unittest.TestCase):
    def test_window_runs_event_loop_and_closes_without_business_services(self):
        app = QApplication.instance() or QApplication([])
        window = create_main_window()
        window.show()
        QTimer.singleShot(20, window.close)
        self.assertEqual(app.exec(), 0)
        self.assertFalse(window.isVisible())

    def test_close_keeps_qt_alive_until_worker_finishes_ui_callback(self):
        app = QApplication.instance() or QApplication([])
        window = create_main_window()
        self.addCleanup(window.close)
        window.show()
        started, release = Event(), Event()
        async def work(cancel):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return await window.dispatcher.invoke(lambda: 'UI finished')
        handle = window.runtime.submit(work)
        self.assertTrue(started.wait(3))
        window.close()
        self.assertTrue(window.isVisible())
        QTimer.singleShot(20, release.set)
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(app.quit)
        watchdog.start(5000)
        app.exec()
        watchdog.stop()
        self.assertFalse(window.isVisible())
        self.assertTrue(window.runtime.is_stopped)
        self.assertEqual(handle.outcome.result(0), 'UI finished')

    def test_close_delivers_already_queued_results_before_destroying_dispatcher(self):
        app = QApplication.instance() or QApplication([])
        window = create_main_window()
        window.show()
        applied = []
        delivery = window.dispatcher.post(lambda: applied.append('confirmed outcome'))
        window.runtime.begin_shutdown()
        self.assertTrue(window.runtime.wait_stopped(3))  # No work awaits Qt in this test.
        window.close()
        window._check_shutdown()  # Deliberately check stop before queued signal delivery.
        self.assertFalse(delivery.done())
        self.assertTrue(window.isVisible())
        watchdog = QTimer()
        watchdog.setSingleShot(True)
        watchdog.timeout.connect(app.quit)
        watchdog.start(5000)
        app.exec()
        watchdog.stop()
        self.assertEqual(applied, ['confirmed outcome'])
        self.assertIsNone(delivery.result(0))
        self.assertFalse(window.isVisible())
