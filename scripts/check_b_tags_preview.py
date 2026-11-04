"""Native Qt development evidence for B tags and opt-in saved preview.

Run with PYTHONPATH=src; scripts use only temporary synthetic collections.
Not a human acceptance record or a packaged-install test.
"""
import json
import os
from pathlib import Path
import sys

os.environ['QT_QPA_PLATFORM'] = 'windows'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tests'))
from test_editing_desktop import EditorDesktopTests
from pmkb.desktop.app import create_main_window
from pmkb.contracts.collections import LeaveChoice
from unittest.mock import patch

output = Path('artifacts/b-tags-preview').resolve()
output.mkdir(parents=True, exist_ok=True)
EditorDesktopTests.setUpClass()
case = EditorDesktopTests()
case.setUp()
try:
    case.shutdown()
    case.window = create_main_window(case.root / 'config' / 'collections.json',
        use_collection_storage=True, mvp_mode=True, enable_preview=True)
    w = case.window
    w.show()
    case.activate()
    body = '# 标题\n\n段落 English 😀\n\n1. 第一项\n2. 第二项\n\n[链接](https://example.com)\n\n> 引用\n\n```\n<x>\n```'
    body = body.replace('\\n', '\n')
    case.save_production_note('笔记 A', body)
    w.tag_input.setText('AI')
    w.add_tag()
    w.save_current()
    case.pump(lambda: w._current().snapshot.base_revision == 2)
    case.save_production_note('笔记 B', '第二篇保存版')
    w.tag_input.setText('other')
    w.add_tag()
    w.save_current()
    case.pump(lambda: w._current().snapshot.base_revision == 2)
    def filtered(tag, expected):
        w.filter_input.setText(tag)
        w.refresh_saved()
        case.pump(lambda: w.runtime.pending_count == 0 and w.dispatcher.pending_count == 0)
        case.assertEqual(w.saved_notes.count(), 1)
        case.assertEqual(w.saved_notes.item(0).text(), expected)
    filtered('other', '笔记 B')
    w.tags.setCurrentRow(0)
    w.remove_tag()
    filtered('other', '笔记 B')  # Unsaved removal does not affect saved filtering.
    w.grab().save(str(output / 'tags-saved-filter.png'))
    filtered('AI', '笔记 A')
    with patch.object(w, '_ask_leave', return_value=LeaveChoice.DISCARD):
        w.open_saved_item(w.saved_notes.item(0))
        case.pump(lambda: w._current().snapshot.name == '笔记 A')
    case.pump(lambda: '<x>' in w.preview.toPlainText())
    case.assertEqual(w.body.toPlainText(), body)
    w.grab().save(str(output / 'six-structure-preview.png'))
    w.body.setPlainText('未保存修改，不得替代保存版预览')
    case.pump(lambda: '未保存修改不更新预览' in w.preview.toPlainText())
    case.assertIn('<x>', w.preview.toPlainText())
    w.grab().save(str(output / 'unsaved-warning.png'))
    facts = dict(tags_two_saved_notes=True, unsaved_tag_filter_unchanged=True,
                 six_preview_structures=True, source_unchanged_before_edit=True,
                 unsaved_preview_warning=True, qt_platform='windows')
    case.shutdown()
    facts.update(runtime_stopped=w.runtime.is_stopped,
                 storage_closed=all(a._closed for a in w._storage_adapters))
    (output / 'result.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(facts))
finally:
    case.doCleanups()
