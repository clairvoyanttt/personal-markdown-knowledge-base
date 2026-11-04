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

    def render_current_preview(self):
        # Synthetic adapter exercises B's asynchronous Qt delivery, not C parsing.
        class Renderer:
            async def render(self, context, snapshot, cancel):
                return '<p>OLD_COLLECTION_PREVIEW</p>'
        self.window.previews.renderer = Renderer()
        self.window._refresh()
        self.window._preview_timer.stop()
        self.window._request_preview()
        self.pump(lambda: self.window.preview.toPlainText() == 'OLD_COLLECTION_PREVIEW'
                  and self.window.runtime.pending_count == 0
                  and self.window.dispatcher.pending_count == 0)

    def test_export_saved_only_and_reject_edit_after_confirmation(self):
        import asyncio
        from threading import Event
        from pmkb.application.export.service import SavedExportNote

        started, release = Event(), Event()

        class Source:
            name = '课程/笔记'
            reads = 0

            async def read_saved(self, context, note_id, cancel):
                self.reads += 1
                started.set()
                while not release.is_set() and not cancel.is_set():
                    await asyncio.sleep(.001)
                return SavedExportNote(context, note_id, 1, 2, self.name, 'saved body', ())

        self.activate()
        storage = MemoryStorage()
        self.window.editing.storage = storage
        source = Source()
        self.window.export_source = source
        loaded = asyncio.run(self.window.editing.load_note(
            LoadNoteRequest(self.window.editing.context, 'b' * 32)))
        self.window._active_buffer = loaded.value.snapshot.buffer_id
        self.window._refresh(refresh_text=True)
        self.window.name.setText('未保存的新标题')
        self.window._apply_name()
        self.window.body.setPlainText('unsaved text')
        destination = self.root / 'saved.md'

        def accept(dialog):
            dialog.buttons()[0].click()

        with patch.object(QMessageBox, 'exec', accept), \
             patch('pmkb.desktop.editor.QFileDialog.getSaveFileName') as choose_file:
            self.window.export_button.click()
            try:
                self.pump(started.is_set)
                self.window.body.setPlainText('edited while reading')
            finally:
                release.set()
            self.pump(lambda: '草稿或集合状态已变化' in self.window.statusBar().currentMessage())
            choose_file.assert_not_called()

        for title, expected in [('课程/笔记', 'note-课程_笔记.md'), ('CON', 'note-CON.md'),
                                ('复习.md', 'note-复习.md.md')]:
            with self.subTest(title=title):
                source.name = title
                with patch.object(QMessageBox, 'exec', accept), \
                     patch('pmkb.desktop.editor.QFileDialog.getSaveFileName',
                           return_value=('', '')) as choose_file:
                    self.window.statusBar().clearMessage()
                    self.window.export_button.click()
                    self.pump(lambda: self.window.statusBar().currentMessage() == '操作已取消。')
                    self.assertEqual(choose_file.call_args.args[2], expected)
                self.assertFalse(destination.exists())

        def choose(*args, **kwargs):
            self.window.body.setPlainText('edited while choosing')
            return str(destination), ''

        with patch.object(QMessageBox, 'exec', accept), \
             patch('pmkb.desktop.editor.QFileDialog.getSaveFileName', side_effect=choose):
            self.window.statusBar().clearMessage()
            self.window.export_button.click()
            self.pump(lambda: '草稿或集合状态已变化' in self.window.statusBar().currentMessage())
        self.assertFalse(destination.exists())

        with patch.object(QMessageBox, 'exec', accept), \
             patch('pmkb.desktop.editor.QFileDialog.getSaveFileName', return_value=(str(destination), '')) as choose_file:
            reads = source.reads
            self.window.export_button.click()
            self.pump(lambda: self.window.statusBar().currentMessage().startswith('已导出'))
            self.assertEqual(choose_file.call_args.args[2], 'note-复习.md.md')
            self.assertEqual(source.reads, reads + 1)
            self.assertEqual(destination.read_bytes(), b'saved body')
            self.window.export_button.click()
            self.pump(lambda: 'TARGET_EXISTS' in self.window.statusBar().currentMessage())
        self.assertEqual(destination.read_bytes(), b'saved body')
        self.assertEqual(self.window.body.toPlainText(), 'edited while choosing')
        self.assertEqual(self.window._current().snapshot.name, '未保存的新标题')
        self.assertTrue(self.window._current().is_dirty)
        self.assertEqual(storage.calls, [])

    def test_exit_during_saved_export_read_never_opens_file_chooser(self):
        import asyncio
        from threading import Event
        from pmkb.application.export.service import SavedExportNote

        started, cancelled = Event(), Event()

        class Source:
            async def read_saved(self, context, note_id, cancel):
                started.set()
                while not cancel.is_set():
                    await asyncio.sleep(.001)
                cancelled.set()
                return SavedExportNote(context, note_id, 1, 2, 'saved name', 'saved body', ())

        self.activate()
        self.window.editing.storage = MemoryStorage()
        self.window.export_source = Source()
        loaded = asyncio.run(self.window.editing.load_note(
            LoadNoteRequest(self.window.editing.context, 'b' * 32)))
        self.window._active_buffer = loaded.value.snapshot.buffer_id
        self.window._refresh(refresh_text=True)
        destination = self.root / 'must-not-export.md'

        with patch.object(QMessageBox, 'exec', lambda dialog: dialog.buttons()[0].click()), \
             patch('pmkb.desktop.editor.QFileDialog.getSaveFileName',
                   return_value=(str(destination), '')) as choose_file:
            self.window.export_button.click()
            self.pump(started.is_set)
            self.window.close()
            self.pump(lambda: self.window.runtime.is_stopped and not self.window.isVisible())
            choose_file.assert_not_called()
        self.assertTrue(cancelled.is_set())
        self.assertFalse(destination.exists())

    def test_production_storage_and_preview_on_real_collection(self):
        from pmkb.desktop.markdown_preview import SharedMarkdownPreview
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
            preview_renderer=SharedMarkdownPreview(), use_collection_storage=True)
        self.window.show()
        self.activate()
        self.window.new_draft()
        self.window.name.setText('saved')
        self.window._apply_name()
        self.window.body.setPlainText('# title\n\n> quote\n\n- item\n\ntext `code` <img src=x>\n\n```\n<tag>\n```')
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 1)
        self.pump(lambda: '<img src=x>' in self.window.preview.toPlainText())
        old = self.window._owned_storage
        self.activate('second')
        self.assertIn(old, self.window._storage_adapters)
        self.assertIsNot(self.window.editing.storage, old)
        self.assertFalse(self.window.editing.views())

    def test_storage_rebind_needs_no_runtime_submit_and_finalizer_closes_all(self):
        import asyncio
        self.activate()
        self.window._bind_collection_storage()
        first = self.window._owned_storage
        first.close()
        with patch.object(self.window.runtime, 'submit', side_effect=RuntimeError('closed')) as submit:
            self.window._bind_collection_storage()
            submit.assert_not_called()
        second = self.window._owned_storage
        self.assertIs(self.window.editing.storage, second)
        self.assertIsNot(first, second)
        asyncio.run(self.window._close_collection_storage())
        self.assertTrue(first._closed)
        self.assertTrue(second._closed)

    def test_saved_list_search_reopen_and_real_export_source(self):
        import asyncio
        from threading import Event
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
                                        use_collection_storage=True)
        self.window.show()
        self.activate()
        self.window.new_draft()
        self.window.name.setText('searchable')
        self.window._apply_name()
        self.window.body.setPlainText('saved content')
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 1)
        self.pump(lambda: self.window.saved_notes.count() == 1)
        note_id = self.window._current().snapshot.note_id
        self.window.body.setPlainText('unsaved input')
        self.window.open_saved_item(self.window.saved_notes.item(0))
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.body.toPlainText(), 'unsaved input')
        saved = asyncio.run(self.window.export_source.read_saved(self.window.editing.context, note_id, Event()))
        self.assertEqual(saved.saved_body, 'saved content')
        self.window.search_input.setText('not found')
        self.window.refresh_saved()
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.saved_notes.count(), 0)

    def production_window(self):
        from pmkb.desktop.markdown_preview import SharedMarkdownPreview
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
            preview_renderer=SharedMarkdownPreview(), use_collection_storage=True)
        self.window.show()
        return self.activate()

    def save_production_note(self, name, body):
        self.window.new_draft()
        self.window.name.setText(name)
        self.window._apply_name()
        self.window.body.setPlainText(body)
        self.window.save_current()
        self.pump(lambda: not self.window.editing.busy
                  and self.window._current().snapshot.base_revision is not None)
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        return self.window._current().snapshot

    def test_real_delete_cancel_dirty_failure_and_unknown_commit_check(self):
        from pmkb.infrastructure.storage.writing import CommitResult, CommitState
        self.production_window()
        note = self.save_production_note('delete target', 'body')
        self.window.saved_notes.setCurrentRow(0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Cancel):
            self.window.delete_saved()
        self.assertIsNotNone(self.window._owned_storage.store.load_note(note.note_id))
        self.window.body.setPlainText('dirty input')
        self.window.delete_saved()
        self.assertIsNone(self.window._delete_attempt)
        self.assertEqual(self.window.body.toPlainText(), 'dirty input')
        self.window.save_current()
        self.pump(lambda: not self.window.editing.busy and self.window._current().snapshot.base_revision == 2)
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        store = self.window._owned_storage.store
        self.window.saved_notes.setCurrentRow(0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
             patch.object(store, 'delete', return_value=CommitResult(CommitState.NOT_COMMITTED, None, None)):
            self.window.delete_saved()
            self.pump(lambda: not self.window._delete_running)
        self.assertIsNone(self.window._delete_attempt)
        self.assertIsNotNone(store.load_note(note.note_id))
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.window.saved_notes.setCurrentRow(0)
        real = store.delete
        def lost_receipt(**kwargs):
            real(**kwargs)
            return CommitResult(CommitState.UNRESOLVED, None, None)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
             patch.object(store, 'delete', side_effect=lost_receipt):
            self.window.delete_saved()
            self.pump(lambda: not self.window._delete_running)
        self.assertTrue(self.window.editing.has_unconfirmed_save)
        self.assertIsNotNone(self.window._current())  # Retained until evidence is confirmed.
        self.window.new_draft()
        self.assertEqual(len(self.window.editing.views()), 1)
        self.window.check_saved_delete()
        self.pump(lambda: self.window._delete_attempt is None)
        self.assertFalse(self.window.editing.has_unconfirmed_save)
        self.assertEqual(self.window.editing.views(), ())
        self.assertIsNone(store.load_note(note.note_id))

    def test_real_saved_references_backlinks_export_and_reopen(self):
        first = self.production_window()
        target = self.save_production_note('target', '# target')
        context = self.window.editing.context
        marker = f'[[note:{context.collection_id}:{target.note_id}]]'
        source = self.save_production_note('source', 'reference ' + marker)
        item = next(self.window.saved_notes.item(i) for i in range(self.window.saved_notes.count())
                    if self.window.saved_notes.item(i).text() == 'source')
        self.window.open_saved_item(item)
        self.pump(lambda: self.window.saved_relations.count() == 1)
        self.assertIn('target', self.window.saved_relations.item(0).text())
        self.window.open_saved_item(self.window.saved_relations.item(0))
        self.pump(lambda: self.window._current().snapshot.note_id == target.note_id
                  and self.window.saved_relations.count() == 1)
        self.assertIn('source', self.window.saved_relations.item(0).text())
        destination = self.root / 'real-export.md'
        def accept(dialog):
            dialog.buttons()[0].click()
        with patch.object(QMessageBox, 'exec', accept), \
             patch('pmkb.desktop.editor.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
            self.window.export_saved_note()
            self.pump(lambda: destination.exists())
            self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(destination.read_text(encoding='utf-8'), '# target')
        old = self.window._owned_storage
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD):
            self.activate('other')
        self.pump(lambda: old._closed)
        self.window.open_collection(ActivationMode.OPEN, first)
        self.pump(lambda: not self.window._switching and self.window.saved_notes.count() == 2)
        item = next(self.window.saved_notes.item(i) for i in range(2)
                    if self.window.saved_notes.item(i).text() == 'source')
        self.window.open_saved_item(item)
        self.pump(lambda: self.window._current() is not None and
                  self.window._current().snapshot.note_id == source.note_id)
        self.assertEqual(self.window.body.toPlainText(), 'reference ' + marker)

    def test_delete_missing_receipt_stays_unknown_and_exit_cancel_retains_input(self):
        from pmkb.infrastructure.storage.writing import CommitResult, CommitState
        self.production_window()
        note = self.save_production_note('not deleted', 'keep me')
        store = self.window._owned_storage.store
        self.window.saved_notes.setCurrentRow(0)
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.StandardButton.Yes), \
             patch.object(store, 'delete', return_value=CommitResult(CommitState.UNRESOLVED, None, None)):
            self.window.delete_saved()
            self.pump(lambda: not self.window._delete_running)
        self.window.check_saved_delete()
        self.pump(lambda: not self.window._delete_running)
        self.assertTrue(self.window.editing.external_pending)
        self.assertIsNotNone(store.load_note(note.note_id))
        with patch.object(QMessageBox, 'warning', return_value=QMessageBox.StandardButton.Cancel):
            self.window.close()
            self.pump(lambda: not self.window._exit_waiting)
        self.assertTrue(self.window.isVisible())
        self.assertEqual(self.window.body.toPlainText(), 'keep me')
        # Later positive evidence resolves only the original operation.
        _, _, nid, revision, command, operation = self.window._delete_attempt
        store.delete(note_id=nid, expected_revision=revision, command_id=command, operation_id=operation)
        self.window.check_saved_delete()
        self.pump(lambda: self.window._delete_attempt is None)

    def test_unsaved_reference_preview_resolves_saved_target_and_protects_code(self):
        self.production_window()
        target = self.save_production_note('saved target', 'body')
        cid = self.window.editing.context.collection_id
        marker = f'[[note:{cid}:{target.note_id}]]'
        self.window.new_draft()
        self.window.body.setPlainText(marker + '\n\n`' + marker + '`')
        self.pump(lambda: '内部引用：saved target' in self.window.preview.toPlainText())
        self.assertIn(marker, self.window.preview.toPlainText())
        self.assertEqual(self.window._owned_storage.store.backlinks(target.note_id), ())
        self.assertEqual(self.window._current().snapshot.body, marker + '\n\n`' + marker + '`')

    def test_stale_list_callback_never_replaces_newer_search(self):
        import asyncio
        from threading import Event
        self.production_window()
        self.save_production_note('alpha', 'first body')
        started, release = Event(), Event()
        store = self.window._owned_storage.store
        real = store.search
        def controlled(**kwargs):
            if kwargs['keyword'] == 'alpha':
                started.set()
                release.wait(3)
            return real(**kwargs)
        with patch.object(store, 'search', side_effect=controlled):
            self.window.search_input.setText('alpha')
            self.window.refresh_saved()
            self.pump(started.is_set)
            self.window.search_input.setText('no-match')
            self.window.refresh_saved()
            self.pump(lambda: self.window.runtime.pending_count == 1)
            release.set()
            self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.saved_notes.count(), 0)

    def test_empty_search_invalid_filter_and_unresolved_save_message(self):
        from pmkb.contracts.common import OperationResult
        from pmkb.contracts.commit_resolution import CommitCheckReply, CommitCheckState
        self.production_window()
        self.save_production_note('alpha', 'body')
        self.window.search_input.setText('')
        self.window.run_search()
        self.assertEqual(self.window.saved_notes.count(), 0)
        self.assertIn('不能为空', self.window.statusBar().currentMessage())
        self.window.filter_input.setText('\tAI')
        self.window.refresh_saved()
        self.assertEqual(self.window.saved_notes.count(), 0)
        self.assertIn('INVALID_TAG', self.window.statusBar().currentMessage())
        context = self.window.editing.context
        reply = CommitCheckReply('a'*32, 'b'*32, 0, CommitCheckState.UNRESOLVED, None, None)
        self.window.filter_input.clear()
        self.window._saved(OperationResult(ResultStatus.OK, context, context, reply, None))
        self.assertIn('核实尚无结论', self.window.statusBar().currentMessage())

    def test_delete_confirmation_invalidated_by_new_edit(self):
        self.production_window()
        note = self.save_production_note('target', 'saved text')
        self.window.saved_notes.setCurrentRow(0)
        def confirm(*args):
            self.window.body.setPlainText('new input during confirmation')
            return QMessageBox.StandardButton.Yes
        with patch.object(QMessageBox, 'question', side_effect=confirm):
            self.window.delete_saved()
        self.assertIsNone(self.window._delete_attempt)
        self.assertIsNotNone(self.window._owned_storage.store.load_note(note.note_id))
        self.assertEqual(self.window.body.toPlainText(), 'new input during confirmation')

    def test_mvp_hidden_scope_and_guarded_saved_selection(self):
        from pmkb.contracts.common import OperationResult, AppError
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
                                        use_collection_storage=True, mvp_mode=True)
        self.window.show()
        self.activate()
        first = self.save_production_note('first', 'first saved')
        second = self.save_production_note('second', 'second saved')
        self.assertFalse(self.window.preview.isVisible())
        self.assertTrue(self.window.tags.isVisible())
        self.assertFalse(self.window.export_button.isVisible())
        self.assertFalse(self.window.saved_relations.isVisible())
        self.assertFalse(self.window.delete_button.isVisible())
        self.assertTrue(self.window.filter_input.isVisible())
        self.assertIsNone(self.window.previews.renderer)
        self.assertIsNone(self.window.export_source)
        self.assertTrue(self.window.name.isReadOnly())
        self.window.body.setPlainText('second unsaved')
        def first_item():
            return next(self.window.saved_notes.item(i) for i in range(self.window.saved_notes.count())
                        if self.window.saved_notes.item(i).text() == 'first')
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.CANCEL):
            self.window.open_saved_item(first_item())
        self.assertEqual(self.window.body.toPlainText(), 'second unsaved')
        failure = OperationResult(ResultStatus.ERROR, self.window.editing.context,
            self.window.editing.context, None, AppError('IO_FAILURE', 'controlled save failure', 'retry'))
        async def fail(*args):
            return failure
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.SAVE), \
             patch.object(self.window.session, 'save_buffer', side_effect=fail):
            self.window.open_saved_item(first_item())
            self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.body.toPlainText(), 'second unsaved')
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD):
            self.window.open_saved_item(first_item())
            self.pump(lambda: self.window._current().snapshot.note_id == first.note_id)
        self.assertEqual(self.window.body.toPlainText(), 'first saved')
        self.window.body.setPlainText('first changed')
        second_item = next(self.window.saved_notes.item(i) for i in range(self.window.saved_notes.count())
                           if self.window.saved_notes.item(i).text() == 'second')
        with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.SAVE):
            self.window.open_saved_item(second_item)
            self.pump(lambda: self.window._current().snapshot.note_id == second.note_id)
        self.assertEqual(self.window.body.toPlainText(), 'second saved')
        self.assertEqual(self.window._owned_storage.store.load_note(first.note_id).body, 'first changed')
        old_context = self.window.editing.context
        self.window.open_collection(ActivationMode.CREATE, self.root / 'not-allowed')
        self.assertEqual(self.window.editing.context, old_context)
        self.assertFalse((self.root / 'not-allowed').exists())

    def test_storage_shutdown_completes_before_new_binding(self):
        from threading import Event
        self.production_window()
        old = self.window._owned_storage
        started, release, finished = Event(), Event(), Event()
        real_close, real_bind = old.close, self.window._bind_collection_storage
        target = self.root / 'serial-target'
        target.mkdir()
        def close():
            started.set()
            release.wait(3)
            real_close()
            finished.set()
        def bind():
            self.assertTrue(finished.is_set())
            real_bind()
        try:
            with patch.object(old, 'close', side_effect=close), \
                 patch.object(self.window, '_bind_collection_storage', side_effect=bind) as binding:
                self.window.open_collection(ActivationMode.CREATE, target)
                self.pump(started.is_set)
                binding.assert_not_called()
                self.assertTrue(self.window._switching)
                release.set()
                self.pump(lambda: not self.window._switching)
                binding.assert_called_once()
        finally:
            release.set()

    def test_final_activation_rejection_restores_usable_old_storage(self):
        first = self.production_window()
        note = self.save_production_note('keep', 'old')
        old, context = self.window._owned_storage, self.window.editing.context
        real = self.window.editing.install_collection
        def reject_after_edit(*args, **kwargs):
            self.assertTrue(old._closed)
            self.window.body.setPlainText('input before final install')
            return real(*args, **kwargs)
        target = self.root / 'rejected-real-target'
        target.mkdir()
        with patch.object(self.window.editing, 'install_collection', side_effect=reject_after_edit):
            self.window.open_collection(ActivationMode.CREATE, target)
            self.pump(lambda: not self.window._switching)
        self.assertEqual(self.window.editing.context, context)
        self.assertEqual(self.window.session.directory, str(first.resolve()))
        self.assertFalse(self.window._owned_storage._closed)
        self.assertEqual(self.window.body.toPlainText(), 'input before final install')
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 2)
        self.assertEqual(self.window._owned_storage.store.load_note(note.note_id).body,
                         'input before final install')

    def test_mvp_search_reads_saved_title_body_and_reports_no_results(self):
        from pmkb.infrastructure.storage.writing import CommitResult, CommitState
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
                                        use_collection_storage=True, mvp_mode=True)
        self.window.show()
        self.activate()
        note = self.save_production_note('中文title', 'saved English body\n😀')
        self.window.body.setPlainText('unsaved keyword')
        self.window.search_input.setText('keyword')
        self.window.run_search()
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.saved_notes.count(), 0)
        self.assertIn('没有找到', self.window.saved_state.text())
        store = self.window._owned_storage.store
        with patch.object(store, 'save', return_value=CommitResult(CommitState.NOT_COMMITTED, None, None)):
            self.window.save_current()
            self.pump(lambda: not self.window.editing.busy)
        self.assertEqual(self.window.body.toPlainText(), 'unsaved keyword')
        self.assertTrue(self.window._current().is_dirty)
        self.assertEqual(store.load_note(note.note_id).body, 'saved English body\n😀')
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 2)
        self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
        self.assertEqual(self.window.saved_notes.count(), 1)
        for word in ('中文title', 'keyword'):
            self.window.search_input.setText(word)
            self.window.run_search()
            self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
            self.assertEqual(self.window.saved_notes.count(), 1)

    def test_mvp_tags_saved_only_filter_and_failed_save(self):
        from pmkb.infrastructure.storage.writing import CommitResult, CommitState
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
                                        use_collection_storage=True, mvp_mode=True)
        self.window.show()
        self.activate()
        first = self.save_production_note('first', 'body one')
        self.window.tag_input.setText('AI')
        self.window.add_tag()
        self.window.tag_input.setText('AI')
        self.window.add_tag()
        self.window.tag_input.setText('ai')
        self.window.add_tag()
        self.assertEqual(self.window._current().snapshot.tags, ('AI', 'ai'))
        def filtered(tag):
            self.window.search_input.clear()
            self.window.filter_input.setText(tag)
            self.window.refresh_saved()
            self.pump(lambda: self.window.runtime.pending_count == 0 and self.window.dispatcher.pending_count == 0)
            return self.window.saved_notes.count()
        self.assertEqual(filtered('AI'), 0)
        store = self.window._owned_storage.store
        with patch.object(store, 'save', return_value=CommitResult(CommitState.NOT_COMMITTED, None, None)):
            self.window.save_current()
            self.pump(lambda: not self.window.editing.busy)
        self.assertEqual(self.window._current().snapshot.tags, ('AI', 'ai'))
        self.assertEqual(store.load_note(first.note_id).tags, ())
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 2)
        self.assertEqual(filtered('AI'), 1)
        self.assertEqual(filtered('ai'), 1)
        self.window.filter_input.clear()
        self.save_production_note('second', 'body two')
        self.window.tag_input.setText('other')
        self.window.add_tag()
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 2)
        self.assertEqual(filtered('other'), 1)
        self.assertEqual(self.window.saved_notes.item(0).text(), 'second')
        self.window.tags.setCurrentRow(0)
        self.window.remove_tag()
        self.assertEqual(filtered('other'), 1)
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 3)
        self.assertEqual(filtered('other'), 0)
        self.window.filter_input.clear()
        for tag in ('', 'x'*31, '\tbad'):
            self.window.tag_input.setText(tag)
            self.window.add_tag()
            self.assertEqual(self.window._current().snapshot.tags, ())
        self.window.tag_input.setText('中'*30)
        self.window.add_tag()
        self.assertEqual(self.window._current().snapshot.tags, ('中'*30,))
        self.window.tags.setCurrentRow(0)
        self.window.remove_tag()
        for index in range(20):
            self.window.tag_input.setText(f't{index}')
            self.window.add_tag()
        self.window.tag_input.setText('overflow')
        self.window.add_tag()
        self.assertEqual(len(self.window._current().snapshot.tags), 20)
        self.assertEqual(self.window.tag_input.text(), 'overflow')

    def test_mvp_saved_preview_switch_limit_and_heartbeat(self):
        import asyncio
        from threading import Event
        self.shutdown()
        self.window = create_main_window(self.root / 'config' / 'collections.json',
            use_collection_storage=True, mvp_mode=True, enable_preview=True)
        self.window.show()
        self.activate()
        self.assertTrue(self.window.preview.isVisible())
        self.assertFalse(self.window.export_button.isVisible())
        self.assertFalse(self.window.saved_relations.isVisible())
        self.assertFalse(self.window.delete_button.isVisible())
        body = '# Heading\n\nparagraph\n\n1. item\n\n[link](https://example.com)\n\n> quote\n\n```\n<x>\n```'
        note = self.save_production_note('preview', body)
        self.pump(lambda: 'Heading' in self.window.preview.toPlainText())
        self.assertIn('<x>', self.window.preview.toPlainText())
        self.assertEqual(self.window.body.toPlainText(), body)
        self.window.body.setPlainText('UNSAVED_PRIVATE_TEXT')
        self.pump(lambda: '未保存修改不更新预览' in self.window.preview.toPlainText())
        self.assertIn('Heading', self.window.preview.toPlainText())
        self.assertNotIn('UNSAVED_PRIVATE_TEXT', self.window.preview.toPlainText())
        self.assertEqual(self.window._owned_storage.store.load_note(note.note_id).body, body)
        self.window.body.setPlainText('x' * (200 * 1024 + 1))
        self.window.save_current()
        self.pump(lambda: self.window._current().snapshot.base_revision == 2)
        self.pump(lambda: '超过 200 KB' in self.window.preview.toPlainText())
        # Force a slow worker parse while Qt and the service loop keep running.
        self.window.body.setPlainText('new saved content')
        renderer = self.window.previews.renderer
        started, release, heartbeat = Event(), Event(), Event()
        real_render = renderer._render
        def delayed(*args):
            started.set()
            release.wait(3)
            return real_render(*args)
        try:
            with patch.object(renderer, '_render', side_effect=delayed):
                self.window.save_current()
                self.pump(lambda: self.window._current().snapshot.base_revision == 3)
                self.pump(started.is_set)
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, heartbeat.set)
                self.pump(heartbeat.is_set)
                async def worker_heartbeat(cancel):
                    return 1
                handle = self.window.runtime.submit(worker_heartbeat)
                self.pump(handle.outcome.done)
                self.assertEqual(handle.outcome.result(), 1)
                release.set()
                self.pump(lambda: self.window.preview.toPlainText() == 'new saved content')
        finally:
            release.set()
        self.assertEqual(self.window.body.toPlainText(), 'new saved content')

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
        self.render_current_preview()
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
        self.assertEqual(self.window.preview.toPlainText(), 'OLD_COLLECTION_PREVIEW')

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
        self.render_current_preview()
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
        self.assertEqual(self.window.preview.toPlainText(), 'OLD_COLLECTION_PREVIEW')

    def test_completed_preview_cleared_before_and_after_late_activation_outcome(self):
        self.activate()
        self.window.new_draft()
        self.window.body.setPlainText('old collection draft')
        self.render_current_preview()
        old_context = self.window.editing.context
        target = self.root / 'preview-target'
        target.mkdir()
        activated, held = self.window._activated, []
        try:
            with patch.object(self.window, '_ask_leave', return_value=LeaveChoice.DISCARD), \
                 patch.object(self.window, '_activated', side_effect=held.append):
                self.window.open_collection(ActivationMode.CREATE, target)
                self.pump(lambda: bool(held))
            # Final model/view installation completed; outcome is still withheld.
            self.assertTrue(self.window._switching)
            self.assertNotEqual(self.window.editing.context, old_context)
            self.assertEqual(self.window.location.text(), str(target.resolve()))
            self.assertIsNone(self.window._active_buffer)
            self.assertEqual(self.window.editing.views(), ())
            self.assertEqual(self.window.preview.toPlainText(), '请选择草稿。')
        finally:
            for result in held:
                activated(result)
        self.assertFalse(self.window._switching)
        self.assertEqual(self.window.preview.toPlainText(), '请选择草稿。')

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
