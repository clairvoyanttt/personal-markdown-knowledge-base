import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from pmkb.desktop.app import create_main_window


class DesktopStartupTests(unittest.TestCase):
    def test_window_runs_event_loop_and_closes_without_business_services(self):
        app = QApplication.instance() or QApplication([])
        window = create_main_window()
        window.show()
        QTimer.singleShot(20, window.close)
        self.assertEqual(app.exec(), 0)
        self.assertFalse(window.isVisible())
