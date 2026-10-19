import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
import os

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication
from pmkb.application.collection.context_gate import ActiveContextGate
from pmkb.application.collection.ports import PreparedCollection
from pmkb.application.editing.service import EditingService
from pmkb.contracts.collections import ActivationMode
from pmkb.contracts.notes import CreateDraftRequest, ApplyDraftEditRequest
from pmkb.desktop.editor_session import EditorSession
from pmkb.desktop.dispatcher import QtDispatcher

A, B = 'a' * 32, 'b' * 32


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.app = QApplication.instance() or QApplication([])
        self.gate = ActiveContextGate()
        self.gate.try_activate(self.gate.current, A)
        self.editing = EditingService(self.gate)
        self.dispatcher = QtDispatcher()
        self.addCleanup(self.dispatcher.close)
        self.session = EditorSession(self.editing, self.dispatcher)
        self.target = PreparedCollection('C:/synthetic-target', B, ActivationMode.OPEN)
        self.view = self.editing.create(CreateDraftRequest(self.gate.current, 'keep')).value

    def test_changed_draft_rejects_activation_and_retains_old_context(self):
        before = self.session.buffers()
        s = self.view.snapshot
        self.editing.edit(ApplyDraftEditRequest(self.gate.current, s.buffer_id, s.edit_version,
                                               s.name, 'newer input', s.tags))
        context = self.editing.install_collection(self.gate.current, self.target, before, (s.buffer_id,))
        self.assertIsNone(context)
        self.assertEqual(self.gate.current.collection_id, A)
        self.assertEqual(self.editing.view(s.buffer_id).snapshot.body, 'newer input')

    def test_missing_discard_and_cancel_do_not_drop_drafts(self):
        old = self.gate.current
        before = self.session.buffers()
        self.assertIsNone(self.editing.install_collection(old, self.target, before, ()))
        signal = Event()
        signal.set()
        self.assertIsNone(self.editing.install_collection(old, self.target, before,
            (self.view.snapshot.buffer_id,), (signal,)))
        self.assertEqual(len(self.editing.views()), 1)

    def test_real_qt_bridge_installs_and_discards_only_at_activation_boundary(self):
        from pmkb.application.runtime import AsyncServiceRuntime
        runtime = AsyncServiceRuntime()
        old = self.gate.current
        handle = runtime.submit(lambda cancel: self.session.activate(old, self.target,
            self.session.buffers(), (self.view.snapshot.buffer_id,), (cancel,)))
        import time
        deadline = time.monotonic() + 3
        try:
            while not handle.outcome.done() and time.monotonic() < deadline:
                self.app.processEvents()
            context = handle.outcome.result(0)
            self.assertEqual(context.collection_id, B)
            self.assertEqual(context.context_generation, old.context_generation + 1)
            self.assertEqual(self.session.directory, self.target.directory)
            self.assertFalse(self.editing.views())
        finally:
            runtime.begin_shutdown()
            runtime.wait_stopped(3)

    def test_same_collection_reactivation_still_increments_generation(self):
        target = PreparedCollection('C:/same', A, ActivationMode.OPEN)
        old = self.gate.current
        context = self.editing.install_collection(old, target, self.session.buffers(),
                                                   (self.view.snapshot.buffer_id,))
        self.assertEqual(context.context_generation, old.context_generation + 1)
