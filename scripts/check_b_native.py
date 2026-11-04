"""Automated native-Qt MVP smoke: save, exit, start a second process, read back.

Run on Windows with PYTHONPATH=src. Evidence is written under artifacts/.
This is development verification, not a signed human acceptance record.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

os.environ['QT_QPA_PLATFORM'] = 'windows'
OUTPUT = Path('artifacts/b-runtime-check').resolve()
OUTPUT.mkdir(parents=True, exist_ok=True)
parser = argparse.ArgumentParser()
parser.add_argument('--phase', choices=('write', 'read'))
parser.add_argument('--root', type=Path)
args = parser.parse_args()
if args.phase is None:
    with TemporaryDirectory(prefix='pmkb-native-') as temp:
        for phase in ('write', 'read'):
            completed = subprocess.run([sys.executable, __file__, '--phase', phase, '--root', temp])
            if completed.returncode:
                raise SystemExit(completed.returncode)
    raise SystemExit(0)

from PySide6.QtCore import QTimer
from pmkb.desktop import app
from pmkb.contracts.collections import ActivationMode, LeaveChoice

root = args.root
target = root / 'collection'
if args.phase == 'write':
    target.mkdir()
expected = "# 中文标题\nEnglish / 符号 😀\n第二行\n"
refs = {}
original = app.create_main_window

def factory(**kwargs):
    w = original(root / 'config' / 'collections.json', **kwargs)
    refs['window'] = w
    timer = QTimer(w)
    timer.setInterval(30)
    state = {'step': 0, 'ticks': 0}
    def step():
        state['ticks'] += 1
        if state['ticks'] > 400:
            refs['facts'] = {'error': 'Native Qt flow timed out'}
            timer.stop()
            w._ask_leave = lambda view: LeaveChoice.DISCARD
            w.close()
            return
        if state['step'] == 0:
            mode = ActivationMode.CREATE if args.phase == 'write' else ActivationMode.OPEN
            w.open_collection(mode, target)
            state['step'] = 1
        elif state['step'] == 1 and not w._switching:
            if args.phase == 'write':
                w.new_draft()
                w.name.setText('真实窗口检查')
                w._apply_name()
                w.body.setPlainText(expected)
                w.save_current()
                state['step'] = 2
            elif w.saved_notes.count() == 1:
                w.open_saved_item(w.saved_notes.item(0))
                state['step'] = 2
        elif (state['step'] == 2 and w._current() is not None
              and w._current().snapshot.base_revision == 1
              and w.saved_notes.count() == 1 and not w.runtime.pending_count
              and not w.dispatcher.pending_count):
            exact = w.body.toPlainText() == expected
            w.grab().save(str(OUTPUT / f'mvp-{args.phase}.png'))
            refs['facts'] = {'phase': args.phase, 'saved_revision': 1, 'saved_rows': 1,
                             'exact_body': exact, 'preview_hidden': not w.preview.isVisible(),
                             'export_hidden': not w.export_button.isVisible(),
                             'tags_hidden': not w.tags.isVisible(), 'qt_platform': 'windows'}
            timer.stop()
            w.close()
    timer.timeout.connect(step)
    timer.start()
    refs['timer'] = timer
    return w

app.create_main_window = factory
code = app.main()
w = refs['window']
facts = refs.get('facts', {'error': 'No evidence'})
facts.update(exit_code=code, runtime_stopped=w.runtime.is_stopped,
             storage_closed=all(a._closed for a in w._storage_adapters))
(OUTPUT / f'mvp-{args.phase}.json').write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(facts, ensure_ascii=True))
raise SystemExit(0 if code == 0 and facts.get('exact_body') and facts['runtime_stopped']
                 and facts['storage_closed'] else 1)
