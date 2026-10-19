"""Real Qt/temporary collection integration. No user notes/config are used."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QCoreApplication, QEvent, Qt, QUrl
from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtTest import QTest
from pmkb.desktop.app import create_main_window
from pmkb.contracts.collections import ActivationMode, LeaveChoice
from pmkb.contracts.notes import ApplyDraftEditRequest, LoadNoteRequest
from pmkb.contracts.common import ResultStatus
from test_editing_service import MemoryStorage


class EditorDesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.window = create_main_window(self.root / 'config' / 'collections.json')
        self.window.show()
        self.addCleanup(self.shutdown)

    def pump(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(.001)
        self.assertTrue(predicate(), 'Qt operation timed out')

    def shutdown(self):
        if not self.window.runtime.is_stopped:
            with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD), \
                 patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Discard):
                self.window.close()
                self.pump(lambda: self.window.runtime.is_stopped and not self.window.isVisible())
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def activate(self, name='collection'):
        directory = self.root / name
        directory.mkdir()
        self.window.open_collection(ActivationMode.CREATE, directory)
        self.pump(lambda: not self.window._switching)
        self.assertEqual(self.window.session.directory, str(directory.resolve()))
        self.assertTrue((directory / 'knowledge.sqlite').is_file())
        return directory

    def test_startup_has_no_config_write_or_fake_saved_rows(self):
        self.assertFalse((self.root / 'config').exists())
        self.assertFalse(self.window.new_button.isEnabled())
        self.assertEqual(self.window.drafts.count(), 0)
        self.assertIn('等待 C', self.window.preview.toPlainText())

    def test_real_collection_draft_edit_tags_and_missing_storage_save(self):
        self.activate()
        self.window.new_draft()
        self.window.name.setText('中文😀')
        self.window._apply_name()
        self.window.body.setPlainText('# 标题\n\n正文😀')
        self.window.tag_input.setText(' AI ')
        self.window.add_tag()
        view = self.window._current()
        self.assertEqual(view.snapshot.name, '中文😀')
        self.assertEqual(view.snapshot.body, '# 标题\n\n正文😀')
        self.assertEqual(view.snapshot.tags, ('AI',))
        self.assertTrue(view.is_dirty)
        self.window.save_current()
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertIn('STATE_BLOCKED', self.window.statusBar().currentMessage())
        self.assertTrue(self.window._current().is_dirty)

    def test_invalid_tag_keeps_raw_input_and_accepted_tags(self):
        self.activate()
        self.window.new_draft()
        self.window.tag_input.setText('x' * 31)
        self.window.add_tag()
        self.assertEqual(self.window.tag_input.text(), 'x' * 31)
        self.assertEqual(self.window._current().snapshot.tags, ())

    def test_cancel_close_retains_draft_and_runtime_accepts_work(self):
        self.activate()
        self.window.new_draft()
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.CANCEL):
            self.window.close()
            self.pump(lambda: not self.window._exit_waiting)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.runtime.is_stopped)
        self.assertEqual(len(self.window.editing.views()), 1)

    def test_discard_and_switch_runs_A_coordinator_with_real_B_drafts(self):
        self.activate()
        self.window.new_draft()
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD):
            self.activate('second')
        self.assertFalse(self.window.editing.views())
        self.assertEqual(self.window.editing.context.context_generation, 2)

    def test_failed_save_during_switch_keeps_original_drafts_and_collection(self):
        first = self.activate()
        self.window.new_draft()
        self.window.body.setPlainText('retain this input when saving fails')
        before = self.window._current()
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.SAVE):
            directory = self.root / 'second'
            directory.mkdir()
            self.window.open_collection(ActivationMode.CREATE, directory)
            self.pump(lambda: not self.window._switching)
        self.assertEqual(self.window.session.directory, str(first.resolve()))
        self.assertEqual(len(self.window.editing.views()), 1)
        self.assertEqual(self.window._current(), before)
        self.assertEqual(self.window.body.toPlainText(), before.snapshot.body)
        self.assertTrue(self.window.body.isEnabled())
        self.assertIn('STATE_BLOCKED', self.window.statusBar().currentMessage())

    def test_rename_does_not_normalize_untouched_saved_markdown(self):
        from pmkb.application.editing.ports import SavedNote
        self.activate()
        storage = MemoryStorage()
        async def load(context, note_id, cancel):
            return SavedNote(note_id, 1, 'old', 'line 1\r\nline 2\r\n', ())
        storage.load_note = load
        self.window.editing.storage = storage
        import asyncio
        result = asyncio.run(self.window.editing.load_note(LoadNoteRequest(
            self.window.editing.context, 'b' * 32)))
        self.window._active_buffer = result.value.snapshot.buffer_id
        self.window._refresh(refresh_text=True)
        self.window.name.setText('renamed')
        self.window._apply_name()
        self.assertEqual(self.window._current().snapshot.body, 'line 1\r\nline 2\r\n')

    def test_preview_refuses_automatic_remote_resource_loading(self):
        self.assertIsNone(self.window.preview.loadResource(2, QUrl('https://example.com/private.png')))
        self.assertFalse(self.window.preview.openExternalLinks())

    def test_late_preview_cannot_replace_newer_draft_preview(self):
        from pmkb.application.editing.preview import PreviewContent
        from pmkb.contracts.common import OperationResult
        self.activate()
        self.window.new_draft()
        old = self.window._current().snapshot
        self.window.body.setPlainText('newer text')
        self.window.preview.setPlainText('current preview')
        result = OperationResult(ResultStatus.OK, self.window.editing.context, self.window.editing.context,
            PreviewContent(old.buffer_id, old.edit_version, '<p>old text</p>'), None)
        self.window._preview_ready(result)
        self.assertEqual(self.window.preview.toPlainText(), 'current preview')

    def test_typing_preserves_untouched_crlf_and_nonbreaking_space(self):
        self.activate()
        self.window.new_draft()
        s = self.window._current().snapshot
        self.window.editing.edit(ApplyDraftEditRequest(self.window.editing.context,
            s.buffer_id, s.edit_version, 'name', '中文😀\r\nsecond\u00a0line\r\n', ()))
        self.window._refresh(refresh_text=True)
        cursor = self.window.body.textCursor()
        cursor.setPosition(0)
        cursor.insertText('prefix ')
        self.assertEqual(self.window._current().snapshot.body, 'prefix 中文😀\r\nsecond\u00a0line\r\n')

    def test_unknown_exit_dialog_edit_invalidates_permission(self):
        self.activate()
        self.window.new_draft()
        self.window.name.setText('name')
        self.window._apply_name()
        storage = MemoryStorage()
        from pmkb.application.editing.ports import SaveEvidenceState
        storage.state = SaveEvidenceState.UNRESOLVED
        self.window.editing.storage = storage
        self.window.save_current()
        self.pump(lambda: self.window.editing.has_unconfirmed_save
                  and self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        def confirm_after_edit(*args):
            view = self.window._current()
            s = view.snapshot
            self.window.editing.edit(ApplyDraftEditRequest(self.window.editing.context,
                s.buffer_id, s.edit_version, s.name, 'new after displayed confirmation', s.tags))
            return QMessageBox.StandardButton.Discard
        with patch.object(QMessageBox, 'warning', side_effect=confirm_after_edit):
            self.window.close()
            self.pump(lambda: not self.window._exit_waiting)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.runtime.is_stopped)
        self.assertIn('STATE_BLOCKED', self.window.statusBar().currentMessage())

    def test_relative_config_is_rejected_before_background_runtime_starts(self):
        with patch('pmkb.desktop.app.AsyncServiceRuntime') as runtime:
            with self.assertRaises(ValueError):
                create_main_window(Path('relative-config.json'))
            runtime.assert_not_called()

    def test_waiting_for_worker_can_cancel_exit_without_closing_later(self):
        import asyncio
        from threading import Event
        started, release = Event(), Event()
        async def work(cancel):
            started.set()
            await asyncio.to_thread(release.wait, 3)
            return 'settled'
        handle = self.window.runtime.submit(work)
        self.assertTrue(started.wait(2))
        try:
            self.window.close()
            self.assertTrue(hasattr(self.window, 'cancel_exit_button'))
            self.assertTrue(self.window.cancel_exit_button.isVisible())
            self.window.cancel_exit_button.click()
            self.assertFalse(self.window._exit_waiting)
            self.assertTrue(self.window.centralWidget().isEnabled())
        finally:
            release.set()
            self.pump(handle.outcome.done)
        for _ in range(20):
            self.app.processEvents()
            time.sleep(.001)
        self.assertTrue(self.window.isVisible())
        self.assertFalse(self.window.runtime.is_stopped)

    def test_cancelled_exit_drops_late_close_permission(self):
        from pmkb.contracts.common import OperationResult
        from pmkb.contracts.commit_resolution import CloseUnconfirmedReply
        self.window._exit_waiting = False
        result = OperationResult(ResultStatus.OK, self.window.editing.context,
            self.window.editing.context, CloseUnconfirmedReply(True, True, ()), None)
        with patch.object(self.window.editing, 'exit_permit_current', return_value=True):
            self.window._exit_permission(result)
        self.assertFalse(self.window._closing)

    def test_close_prompt_treats_note_name_as_plain_text(self):
        self.activate()
        self.window.new_draft()
        s = self.window._current().snapshot
        self.window.editing.edit(ApplyDraftEditRequest(self.window.editing.context,
            s.buffer_id, s.edit_version, '<b>literal name</b>', '', ()))
        formats = []
        def cancel(dialog):
            formats.append(dialog.textFormat())
            return QMessageBox.StandardButton.Cancel
        with patch.object(QMessageBox, 'exec', cancel), \
             patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            self.window._ask_leave(self.window._current())
        self.assertEqual(formats, [Qt.TextFormat.PlainText])

    def test_check_not_committed_is_explained_as_unsaved_in_ui(self):
        from pmkb.contracts.common import OperationResult
        from pmkb.contracts.commit_resolution import CommitCheckReply, CommitCheckState
        reply = CommitCheckReply('a' * 32, 'b' * 32, 0, CommitCheckState.NOT_COMMITTED, None, None)
        result = OperationResult(ResultStatus.OK, self.window.editing.context,
            self.window.editing.context, reply, None)
        self.window._saved(result)
        self.assertIn('未提交', self.window.statusBar().currentMessage())

    def test_cancelled_worker_result_is_consumed_without_unhandled_ui_callback(self):
        import asyncio
        async def cancelled(cancel):
            raise asyncio.CancelledError()
        self.window._submit(cancelled, lambda result: self.fail('No successful result exists'))
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertIn('操作异常', self.window.statusBar().currentMessage())

    def test_stale_preview_failure_does_not_replace_current_status(self):
        from pmkb.contracts.common import OperationResult, AppError
        self.activate()
        self.window.new_draft()
        old = self.window._current().snapshot
        self.window.body.setPlainText('new input')
        self.window.statusBar().showMessage('current operation')
        failed = OperationResult(ResultStatus.ERROR, self.window.editing.context,
            self.window.editing.context, None, AppError('IO_FAILURE', 'old preview failed', 'retry'))
        self.window._preview_ready(failed, (old.buffer_id, old.edit_version))
        self.assertEqual(self.window.statusBar().currentMessage(), 'current operation')

    def test_queued_input_between_model_install_and_activated_has_consistent_view(self):
        first = self.activate()
        self.window.new_draft()
        self.window.name.setText('old note')
        self.window._apply_name()
        self.window.body.setPlainText('old text selected for discard')
        old_buffer = self.window._active_buffer
        old_context = self.window.editing.context
        target = self.root / 'handoff-target'
        target.mkdir()
        install = self.window.editing.install_collection
        activated = self.window._activated
        held, probes, errors = [], [], []

        def probe():
            # This is a queued Qt callback, not a worker-thread/widget shortcut.
            # The model is installed, but the _activated result is held back.
            state = {
                'context': self.window.editing.context,
                'buffers': self.window.editing.views(),
                'active': self.window._active_buffer,
                'location': self.window.location.text(),
                'body_enabled': self.window.body.isEnabled(),
                'name_enabled': self.window.name.isEnabled(),
                'tag_enabled': self.window.tag_input.isEnabled(),
                'body_before': self.window.body.toPlainText(),
            }
            # Real Qt key events must be rejected by disabled editors. Exercise
            # queued shortcuts and input handlers too; none may read old_buffer.
            for widget in (self.window.body, self.window.name, self.window.tag_input):
                QTest.keyClicks(widget, 'queued input')
            for handler in (self.window.save_shortcut.activated.emit, self.window._apply_text,
                            self.window._apply_name, self.window.add_tag,
                            self.window.remove_tag, self.window._request_preview):
                try:
                    handler()
                except Exception as error:
                    errors.append(error)
            state['body_after'] = self.window.body.toPlainText()
            state['name_after'] = self.window.name.text()
            state['tag_after'] = self.window.tag_input.text()
            probes.append(state)

        def install_then_queue(*args, **kwargs):
            context = install(*args, **kwargs)
            if context is not None:
                self.window.dispatcher.post(probe)
            return context

        try:
            with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD), \
                 patch.object(self.window, '_activated', side_effect=held.append), \
                 patch.object(self.window.editing, 'install_collection', side_effect=install_then_queue), \
                 patch('sys.excepthook', side_effect=lambda kind, error, tb: errors.append(error)):
                self.window.open_collection(ActivationMode.CREATE, target)
                self.pump(lambda: bool(held) and bool(probes))
            state = probes[0]
            self.assertNotEqual(state['context'], old_context)
            self.assertEqual(state['buffers'], ())
            self.assertEqual(errors, [])
            self.assertIsNone(state['active'], f'Old active buffer {old_buffer} survived installation')
            self.assertEqual(state['location'], str(target.resolve()))
            self.assertFalse(state['body_enabled'])
            self.assertFalse(state['name_enabled'])
            self.assertFalse(state['tag_enabled'])
            self.assertEqual(state['body_before'], '')
            self.assertEqual(state['body_after'], '')
            self.assertEqual(state['name_after'], '')
            self.assertEqual(state['tag_after'], '')
        finally:
            for result in held:
                activated(result)
        # Once the result completes, new input is accepted into a new draft in
        # the target collection and survives subsequent state-only refreshes.
        self.window.new_draft()
        QTest.keyClicks(self.window.body, 'new collection text')
        accepted = self.window._current().snapshot
        self.window._refresh()
        self.assertEqual(self.window._current().snapshot.body, 'new collection text')
        self.assertEqual(self.window.body.toPlainText(), accepted.body)
        self.assertTrue((first / 'knowledge.sqlite').is_file())

    def test_cancel_switch_keeps_original_model_and_editor(self):
        first = self.activate()
        self.window.new_draft()
        self.window.body.setPlainText('keep on cancelled switch')
        before, context = self.window._current(), self.window.editing.context
        target = self.root / 'cancelled-target'
        target.mkdir()
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.CANCEL):
            self.window.open_collection(ActivationMode.CREATE, target)
            self.pump(lambda: not self.window._switching)
        self.assertEqual(self.window.editing.context, context)
        self.assertEqual(self.window._current(), before)
        self.assertEqual(self.window.body.toPlainText(), before.snapshot.body)
        self.assertEqual(self.window.location.text(), str(first.resolve()))
        self.assertTrue(self.window.body.isEnabled())

    def test_last_qt_activation_version_rejection_keeps_new_input_and_published_target(self):
        first = self.activate()
        self.window.new_draft()
        self.window.body.setPlainText('original ')
        context, buffer_id = self.window.editing.context, self.window._active_buffer
        target = self.root / 'rejected-target'
        target.mkdir()
        install = self.window.editing.install_collection
        def edit_before_final_check(*args, **kwargs):
            self.window.body.moveCursor(self.window.body.textCursor().MoveOperation.End)
            QTest.keyClicks(self.window.body, 'arrived before final check')
            return install(*args, **kwargs)
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD), \
             patch.object(self.window.editing, 'install_collection', side_effect=edit_before_final_check):
            self.window.open_collection(ActivationMode.CREATE, target)
            self.pump(lambda: not self.window._switching)
        self.assertEqual(self.window.editing.context, context)
        self.assertEqual(self.window._active_buffer, buffer_id)
        self.assertEqual(self.window._current().snapshot.body, 'original arrived before final check')
        self.assertEqual(self.window.body.toPlainText(), self.window._current().snapshot.body)
        self.assertEqual(self.window.location.text(), str(first.resolve()))
        self.assertTrue(self.window.body.isEnabled())
        self.assertTrue((target / 'knowledge.sqlite').is_file())
