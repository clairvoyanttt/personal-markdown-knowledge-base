"""Run the real Qt entry point with synthetic data and record a native-window smoke test.

Run manually on Windows with PYTHONPATH=src. Outputs remain under artifacts/.
This is automated runtime evidence, not V01-V16 product acceptance.
"""
import os
os.environ['QT_QPA_PLATFORM'] = 'windows'
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from PySide6.QtCore import QTimer
from pmkb.desktop import app
from pmkb.contracts.collections import ActivationMode
original = app.create_main_window
output = Path('artifacts/b-runtime-check').resolve()
with TemporaryDirectory(prefix='pmkb-native-') as temp:
    root = Path(temp)
    target = root/'collection'
    target.mkdir()
    refs = {}
    def factory(**kwargs):
        w = original(root/'config'/'collections.json', **kwargs)
        refs['window'] = w
        timer = QTimer(w)
        timer.setInterval(30)
        state = {'step': 0, 'ticks': 0}
        def step():
            state['ticks'] += 1
            if state['ticks'] > 400:
                timer.stop()
                refs['facts'] = {'error': 'Native Qt flow timed out'}
                w._ask_leave = lambda view: __import__('pmkb.contracts.collections', fromlist=['LeaveChoice']).LeaveChoice.DISCARD
                w.close()
                return
            if state['step'] == 0:
                w.open_collection(ActivationMode.CREATE, target)
                state['step'] = 1
            elif state['step'] == 1 and not w._switching:
                w.new_draft()
                w.name.setText('真实窗口检查')
                w._apply_name()
                w.body.setPlainText('# 标题\n\n1. 第一项\n2. 第二项\n\n[链接](https://example.com)\n\n> 引用\n\n`code`')
                w.tag_input.setText('AI')
                w.add_tag()
                w.save_current()
                state['step'] = 2
            elif (state['step'] == 2 and w._current().snapshot.base_revision == 1
                  and w.saved_notes.count() == 1 and not w.runtime.pending_count
                  and not w.dispatcher.pending_count and '第一项' in w.preview.toPlainText()):
                w.grab().save(str(output/'native-window.png'))
                refs['facts'] = {'saved_revision': 1, 'saved_rows': 1,
                                 'preview': w.preview.toPlainText(), 'qt_platform': 'windows'}
                timer.stop()
                w.close()
        timer.timeout.connect(step)
        timer.start()
        refs['timer'] = timer
        return w
    app.create_main_window = factory
    code = app.main()
    w = refs['window']
    facts = refs['facts']
    facts.update(exit_code=code, runtime_stopped=w.runtime.is_stopped,
                 storage_closed=all(a._closed for a in w._storage_adapters))
    (output/'native-result.json').write_text(json.dumps(facts,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(facts,ensure_ascii=True))
    raise SystemExit(0 if code == 0 and facts.get('saved_revision') == 1 and facts['runtime_stopped'] and facts['storage_closed'] else 1)
