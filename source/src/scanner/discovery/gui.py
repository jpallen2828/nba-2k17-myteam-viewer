"""Local batch controls and a registered acceptance hotkey; no game UI automation."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import threading
import uuid

from PySide6.QtCore import QTimer, QObject, Signal
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
                               QLabel, QPushButton, QLineEdit, QListWidget, QPlainTextEdit, QComboBox, QCheckBox)

from .analysis import batch_report, decode_record
from .core import Journal, Recorder, SessionLease
from .live import LiveSource, prepare, next_batch


class Hotkey(QObject):
    pressed = Signal()
    status = Signal(str)

    def __init__(self):
        super().__init__()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread_id = None
        self.registered = False

    def run(self):
        user32 = ctypes.WinDLL('user32', use_last_error=True)
        self.thread_id = ctypes.WinDLL('kernel32').GetCurrentThreadId()
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        registered = user32.RegisterHotKey(None, 17017, 0x4000 | 0x2 | 0x1, 0x77)
        if not registered:
            self.status.emit('Global hotkey unavailable; use Capture accepted edit here.')
            return
        self.registered = True
        self.status.emit('Ctrl + Alt + F8 registered: capture accepted edit while the game is foreground.')
        try:
            message = wintypes.MSG()
            while not self.stop.wait(0.04):
                while user32.PeekMessageW(ctypes.byref(message), None, 0x312, 0x312, 1):
                    if message.wParam == 17017:
                        self.pressed.emit()
        finally:
            user32.UnregisterHotKey(None, 17017)
            self.registered = False


class Window(QMainWindow):
    def __init__(self, root, session, inventory, source_factory=LiveSource, enable_hotkey=True):
        super().__init__()
        self.root, self.session, self.inventory = Path(root), Path(session), inventory
        self.lease = SessionLease(self.session)
        self.source_factory = source_factory
        self.journal = Journal(self.session / 'recorder.sqlite')
        self.source = None
        self.paused = False
        self.report_epoch = None
        font_path = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/segoeui.ttf'
        if font_path.is_file():
            QFontDatabase.addApplicationFont(str(font_path))
        self.setFont(QFont('Segoe UI', 10))
        self.setStyleSheet('QWidget { background:#101b27; color:#edf2f7; } '
                           'QPushButton { background:#234057; padding:8px; border:1px solid #42617a; border-radius:4px; } '
                           'QLineEdit,QPlainTextEdit,QListWidget { background:#0b141e; padding:7px; border:1px solid #344b61; } '
                           'QListWidget::item { padding:5px; } QListWidget::item:selected { background:#295878; }')
        self.setWindowTitle('NBA 2K17 — Batch Field Recorder (read only)')
        self.resize(1030, 820)
        host = QWidget(); self.setCentralWidget(host); layout = QVBoxLayout(host)
        self.summary = QLabel(); self.summary.setWordWrap(True); layout.addWidget(self.summary)
        family_row = QHBoxLayout(); layout.addLayout(family_row)
        self.family_picker = QComboBox()
        self.family_picker.addItems(['identity','attributes','tendencies','badges','hot_zones','signatures','gear','gameplay_metadata','team','appearance'])
        family_row.addWidget(self.family_picker)
        next_button = QPushButton('Start chosen family (reuse discovery)'); next_button.clicked.connect(self.start_family)
        family_row.addWidget(next_button)
        self.sound = QCheckBox('Sound after capture'); self.sound.setChecked(True); family_row.addWidget(self.sound)
        self.instructions = QLabel('One field at a time. Accept in the game, then Ctrl+Alt+F8. No chat replies between steps.\n'
                                   'Original values and enum labels go in Notes. Restore the complete baseline once at the end.\n'
                                   'Pause before EVERY roster load or restart; use Rebind afterward, even if addresses look unchanged.')
        self.instructions.setWordWrap(True); layout.addWidget(self.instructions)
        self.step_label = QLabel(); self.step_label.setWordWrap(True); layout.addWidget(self.step_label)
        self.notes = QLineEdit(); self.notes.setPlaceholderText('Original value / exact enum label / coupled change / skip reason')
        layout.addWidget(self.notes)
        self.custom_field = QLineEdit()
        self.custom_field.setPlaceholderText('Optional extra field: e.g. attributes.left_ankle_durability. Clear to follow the checklist.')
        layout.addWidget(self.custom_field)
        controls = QHBoxLayout(); layout.addLayout(controls)
        for label, handler in [('Capture accepted edit', self.mark), ('Skip with note', self.skip),
                               ('Pause / resume', self.toggle), ('Rebind after load', self.rebind), ('Original values', self.originals), ('Batch report', self.report)]:
            button = QPushButton(label); button.clicked.connect(handler); controls.addWidget(button)
        self.status = QLabel('Starting…'); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.hotkey_status = QLabel('Capture button is available.'); layout.addWidget(self.hotkey_status)
        self.list = QListWidget(); self.list.setWordWrap(True); layout.addWidget(self.list, 1)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumHeight(140); layout.addWidget(self.log)
        self.load_epoch()
        self.notes.textChanged.connect(self.save_draft)
        self.custom_field.textChanged.connect(self.save_draft)
        self.timer = QTimer(self); self.timer.timeout.connect(self.tick); self.timer.start(250)
        self.hotkey = None
        if enable_hotkey:
            self.hotkey = Hotkey(); self.hotkey.pressed.connect(self.mark); self.hotkey.status.connect(self.hotkey_status.setText)
            self.hotkey.thread.start()

    def load_epoch(self):
        self.epoch = json.loads((self.session / 'current.json').read_text())['epoch']
        folder = self.session / self.epoch
        self.binding = json.loads((folder / 'binding.json').read_text())
        self.plan = json.loads((folder / 'plan.json').read_text())
        self.recorder = Recorder(self.journal, self.epoch, self.plan['steps'], lambda raw: decode_record(raw, self.inventory))
        draft = self.journal.get('draft:' + self.epoch, {})
        if draft.get('cursor') == self.recorder.progress['cursor']:
            self.notes.setText(draft.get('note', '')); self.custom_field.setText(draft.get('field', ''))
        else:
            self.notes.clear(); self.custom_field.clear()
        try:
            self.source = self.source_factory(self.binding)
        except Exception as exc:
            self.paused = True; self.status.setText(str(exc))
        self.list.clear()
        for i, step in enumerate(self.plan['steps'], 1):
            self.list.addItem(f'{i}. {step["instruction"]}')
        self.summary.setText(f'{self.binding["player_name"]} • {self.binding["team_token"]} • {self.plan["family"]}\n'
                             f'Evidence: {self.session} • epoch {self.epoch}')
        self.refresh_step()

    def save_draft(self):
        self.journal.set('draft:' + self.epoch, {'cursor':self.recorder.progress['cursor'],
                                               'note':self.notes.text(), 'field':self.custom_field.text()})

    def refresh_step(self):
        cursor = self.recorder.progress['cursor']
        if cursor < len(self.plan['steps']):
            self.list.setCurrentRow(cursor)
            self.step_label.setText(f'CURRENT STEP {cursor+1}/{len(self.plan["steps"])} — {self.plan["steps"][cursor]["instruction"]}')
        else:
            self.step_label.setText('BATCH COMPLETE — generate the batch report.')

    def tick(self):
        if self.paused or self.source is None:
            return
        try:
            regions, metadata = self.source.sample()
            if self.recorder.pending and not metadata.get('canonical_selection_matches'):
                self.status.setText('Select the bound player before labeling. Watching continues; acceptance capture is withheld.')
                return
            old_cursor = self.recorder.progress['cursor']
            old_event = self.recorder.progress['last_event']
            self.status.setText(self.recorder.sample(regions, metadata))
            if old_cursor != self.recorder.progress['cursor']:
                self.log.appendPlainText(f'Captured {self.plan["steps"][old_cursor]["id"]}')
                self.notes.clear(); self.refresh_step()
            elif old_event != self.recorder.progress['last_event'] and self.custom_field.text():
                self.log.appendPlainText('Captured extra field: ' + self.custom_field.text())
                self.notes.clear()
            if old_event and old_event != self.recorder.progress['last_event'] and self.sound.isChecked():
                import winsound
                winsound.MessageBeep(winsound.MB_OK)
        except Exception as exc:
            self.status.setText(str(exc))
            # A changing read is retried; structural/file failures require explicit rebind.
            if 'changing during read' not in str(exc):
                self.paused = True
                self.journal.append(self.epoch, 'PAUSED_INVALID_BINDING', details={'reason': str(exc)})

    def mark(self):
        if self.paused:
            self.status.setText('Paused. Resume or Rebind before capturing.'); return
        try:
            custom = None
            if self.custom_field.text().strip():
                custom = {'id': 'custom-' + uuid.uuid4().hex, 'field': self.custom_field.text().strip(),
                          'kind': 'custom', 'requested_value': None, 'requires_note': True,
                          'instruction': 'Operator-labeled extra field; exact displayed value/label is in Notes'}
            self.recorder.request_mark(self.notes.text(), custom)
            self.status.setText('Accepted-edit label queued; waiting for stable canonical capture.')
        except Exception as exc:
            self.status.setText(str(exc))

    def skip(self):
        if not self.notes.text().strip() or self.recorder.pending:
            self.status.setText('Enter a skip reason; finish any pending capture first.'); return
        cursor = self.recorder.progress['cursor']
        if cursor >= len(self.plan['steps']): return
        progress = {**self.recorder.progress, 'cursor': cursor+1}
        self.journal.append(self.epoch, 'SKIPPED_STEP', details={'step': self.plan['steps'][cursor], 'reason': self.notes.text()},
                            state={self.recorder.key: progress})
        self.recorder.progress = progress; self.notes.clear(); self.refresh_step()

    def toggle(self):
        self.paused = not self.paused
        self.status.setText('Paused; progress saved. Rebind after any load/restart.' if self.paused else 'Watching resumed.')

    def rebind(self):
        self.paused = True
        old_epoch = self.epoch
        old_recorder = self.recorder
        if self.source:
            self.source.close(); self.source = None
        self.status.setText('Rediscovering current process, selected player and roster references…')
        QApplication.processEvents()
        try:
            prepare(self.root, self.session, self.binding['roster_file'], self.binding['team_token'],
                    self.binding['player_name'], self.plan['family'], self.inventory, self.binding.get('linked_specs'))
            self.load_epoch()
            if self.source is None: raise ValueError('Rebinding source failed')
            regions, metadata = self.source.sample()
            cursor = old_recorder.progress['cursor']
            old_step = old_recorder.steps[cursor] if cursor < len(old_recorder.steps) else None
            kind = 'ACCEPTED_EDIT' if old_step and old_step['kind'] == 'restore' else 'REBIND_CHECKPOINT'
            details = {'step': old_step or {'field': 'batch.rebind', 'kind': 'rebind'},
                       'acceptance': 'OPERATOR_REBOUND_AFTER_LOAD', 'new_epoch': self.epoch,
                       'operator_note': 'Rebind button; fresh discovery, no saved addresses replayed',
                       'diagnostics': metadata, 'decoded': decode_record(regions['player'], self.inventory)}
            self.journal.append(old_epoch, kind, regions, details)
            if kind == 'ACCEPTED_EDIT':
                self.binding['restored_previous_epoch'] = old_epoch
                (self.session / self.epoch / 'binding.json').write_text(json.dumps(self.binding, indent=2), encoding='utf-8')
            self.report_epoch = old_epoch
            self.paused = True
            self.status.setText('Rebound and captured. Use Batch report for the previous batch; resume only when starting this new epoch.')
        except Exception as exc:
            self.status.setText('Rebind failed: ' + str(exc))

    def start_family(self):
        self.paused = True
        try:
            next_batch(self.session, self.family_picker.currentText(), self.inventory)
            if self.source: self.source.close()
            self.source = None
            self.load_epoch(); self.report_epoch = None; self.notes.clear(); self.custom_field.clear()
            self.paused = self.source is None
        except Exception as exc:
            self.status.setText(str(exc))

    def report(self):
        try:
            report, path = batch_report(self.session, self.report_epoch or self.epoch)
            self.log.appendPlainText(f'Report saved: {path}\nAccepted: {report["accepted_steps"]}; canonical/verified-region restoration: {report["canonical_and_verified_regions_restored"]}; all windows: {report["all_captured_regions_restored"]}; newly verified: 0 (injection gate pending).')
        except Exception as exc:
            self.status.setText('Report failed: ' + str(exc))

    def originals(self):
        lines = [f'{e["step"]["field"]}: {e.get("operator_note", "")}' for e in self.journal.events(self.epoch)
                 if e['kind']=='ACCEPTED_EDIT' and e.get('step',{}).get('kind')=='original']
        self.log.setPlainText('Recorded original values:\n' + '\n'.join(lines))

    def closeEvent(self, event):
        self.timer.stop()
        if self.hotkey:
            self.hotkey.stop.set(); self.hotkey.thread.join(timeout=2)
        if self.source:
            self.source.close()
        self.journal.close()
        self.lease.close()
        event.accept()


def run(root, session, inventory):
    app = QApplication.instance() or QApplication([])
    window = Window(root, session, inventory); window.show()
    # Start-Process hides the console through STARTUPINFO; explicitly reveal
    # only our Qt window after its first ShowWindow has consumed that flag.
    if os.name == 'nt':
        def reveal():
            user32 = ctypes.WinDLL('user32')
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow(wintypes.HWND(int(window.winId())), 5)
        QTimer.singleShot(100, reveal)
    return app.exec()
