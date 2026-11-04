"""Native Qt wave-two integration evidence; temporary synthetic data only."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_editing_desktop import EditorDesktopTests
from pmkb.desktop.app import create_main_window
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPushButton
output = Path('artifacts/b-wave2').resolve()
output.mkdir(parents=True, exist_ok=True)
EditorDesktopTests.setUpClass()
case = EditorDesktopTests()
case.setUp()
try:
    case.shutdown()
    case.window = create_main_window(case.root/'config'/'collections.json',
        use_collection_storage=True, mvp_mode=True, enable_export=True, enable_references=True)
    w = case.window
    w.show()
    case.activate()
    target = case.save_production_note('目标笔记', '目标正文')
    source = case.save_production_note('来源笔记', '保存正文 ')
    with patch('pmkb.desktop.saved_panel.QInputDialog.getItem', return_value=('目标笔记', True)):
        w.insert_reference_button.click()
    marker = f'[[note:{w.editing.context.collection_id}:{target.note_id}]]'
    case.assertIn(marker, w.body.toPlainText())
    case.assertEqual(w._owned_storage.store.backlinks(target.note_id), ())
    w.save_current()
    case.pump(lambda: w._current().snapshot.base_revision == 2 and w.saved_relations.count() == 1)
    case.assertIn('目标笔记', w.saved_relations.item(0).text())
    refresh = next(b for b in w.findChildren(QPushButton)
                   if b.text() == '刷新保存版 / 应用标签')
    refresh.click()
    case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
    case.assertEqual(w.saved_relations.count(), 1)
    w.filter_input.setText('no-matching-tag')
    refresh.click()
    case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
    case.assertEqual(w.saved_notes.count(), 0)
    case.assertEqual(w.saved_relations.count(), 1)
    w.filter_input.clear()
    w.search_input.setText('no-matching-search')
    w.run_search()
    case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
    case.assertEqual(w.saved_notes.count(), 0)
    case.assertEqual(w.saved_relations.count(), 1)
    w.search_input.clear()
    refresh.click()
    case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
    case.assertEqual(w.saved_relations.count(), 1)
    w.grab().save(str(output/'saved-reference-panel.png'))
    w.body.setPlainText('未保存正文，不能进入导出文件')
    destination = case.root/'output.md'
    def confirm():
        dialog = QApplication.activeModalWidget()
        if dialog is None:
            raise RuntimeError('Expected the native saved-only confirmation')
        dialog.grab().save(str(output/'saved-only-confirmation.png'))
        dialog.buttons()[0].click()
    with patch('pmkb.desktop.editor.QFileDialog.getSaveFileName', return_value=(str(destination), '')):
        QTimer.singleShot(50, confirm)
        w.export_saved_note()
        case.pump(lambda: destination.exists())
        case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
        original = destination.read_bytes()
        case.assertIn('目标笔记', original.decode('utf-8'))
        case.assertNotIn('未保存正文', original.decode('utf-8'))
        QTimer.singleShot(50, confirm)
        w.export_saved_note()
        case.pump(lambda: 'TARGET_EXISTS' in w.statusBar().currentMessage())
    case.assertEqual(destination.read_bytes(), original)
    case.assertEqual(w.body.toPlainText(), '未保存正文，不能进入导出文件')
    w.grab().save(str(output/'refused-overwrite.png'))
    facts = dict(reference_inserted=True, saved_reference_shown=True,
                 refresh_filter_search_preserve_relations=True,
                 unsaved_reference_does_not_create_backlinks=True,
                 saved_only_export=True, overwrite_refused=True, dirty_input_retained=True,
                 deletion_hidden=not w.delete_button.isVisible(), qt_platform='windows')
    case.shutdown()
    facts.update(runtime_stopped=w.runtime.is_stopped,
                 storage_closed=all(a._closed for a in w._storage_adapters))
    (output/'result.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(facts))
finally:
    case.doCleanups()
