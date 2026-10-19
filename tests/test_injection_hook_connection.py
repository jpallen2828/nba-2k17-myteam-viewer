"""Injection and the game monitor share one confirmed roster and operation lock."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtWidgets import QApplication, QTableWidgetItem

from nba2k17_viewer import injection_ui
from nba2k17_viewer.hook_roster_sync import sync_roster
from nba2k17_viewer.injection_session import SessionManager
from test_injection_panel_scan import WritableSession, finish


class Connection(QObject):
    snapshot_changed = Signal(object)
    activity_changed = Signal(bool)

    def __init__(self, manager, roster_file=''):
        super().__init__()
        self.manager = manager
        self.roster_file = roster_file
        self.active = False
        self.selections = []

    def set_roster_path(self, value):
        self.roster_file = str(value)
        self.selections.append((self.roster_file, self.manager.busy))

    def activity(self, value):
        self.active = value
        self.activity_changed.emit(value)


class BoundSession(WritableSession):
    def __init__(self, path, progress=lambda _: None, *, pid=52, cancel_event=None):
        super().__init__(path, progress)
        self.roster_file = Path(path).resolve()
        self.pid, self.process_start = pid, 'birth'
        self.valid = True
        self.cancel_event = cancel_event

    def close(self):
        super().close()
        self.valid = False

    def public_scan(self):
        return {'teams': self.teams, 'roster_file': str(self.roster_file)}


def panel_fixture(tmp_path, monkeypatch, *, existing=False, confirmed=False):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'roster0003'; path.write_bytes(b'roster')
    settings = QSettings(str(tmp_path / 'injection.ini'), QSettings.Format.IniFormat)
    settings.setValue('directory', str(tmp_path))
    monkeypatch.setattr(injection_ui, 'QSettings', lambda *_: settings)
    monkeypatch.setattr(injection_ui, 'RosterSession', BoundSession)
    manager = SessionManager()
    if existing:
        manager.session = BoundSession(path)
        manager.session.last_run = ('undo-journal',)
    connection = Connection(manager, str(path) if confirmed else '')
    cards = {'card': {'injection': {'source_card_identity': {'name': 'Incoming', 'overall': 90, 'tier': 'Amethyst'}}}}
    owner = SimpleNamespace(repository=SimpleNamespace(cards=cards), injection_manager=manager,
                            game_connection=connection, close_overlay=lambda: None)
    panel = injection_ui.InjectionPanel(owner, [(0, {'id': 'card'})])
    return app, panel, connection, path


def synchronized(connection, panel, *, changed=False, rebound=False):
    connection.snapshot_changed.emit({'sync': {'state': 'ready', 'changed': changed,
                                     'rebound': rebound, 'scan': panel.session.public_scan()}})


def test_opening_panel_does_not_confirm_first_discovered_file(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch)
    assert panel.roster.currentData() == str(path)
    assert connection.roster_file == '' and connection.selections == []
    panel.find_files()
    assert connection.roster_file == '' and not connection.selections
    panel.release()


def test_manual_scan_confirms_path_under_reservation_and_monitor_preserves_undo(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch)
    panel.scan(); finish(app, panel)
    assert connection.selections == [(str(path), True)]
    original = panel.session
    original.last_run = ('saved-undo',)
    result = sync_roster(panel.manager, connection.roster_file,
                         {'pid': 52, 'token': 'birth', 'supported': True},
                         session_factory=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No rebind needed')))
    assert result['state'] == 'ready' and not result['rebound']
    assert panel.session is original and original.last_run == ('saved-undo',)
    panel.release(); panel.manager.shutdown()


def test_existing_manual_session_seeds_blank_monitor_without_discard(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True)
    assert connection.roster_file == str(path)
    assert not panel.session.closed and panel.session.last_run == ('undo-journal',)
    assert panel.undo_button.isEnabled()
    panel.release(); panel.manager.shutdown()


def test_monitor_activity_blocks_scan_preview_injection_and_undo(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    connection.activity(True)
    assert not panel.scan_button.isEnabled() and not panel.inject_button.isEnabled()
    assert not panel.prepare_button.isEnabled() and not panel.undo_button.isEnabled()
    before = panel.session
    panel.scan(); panel.inject(); panel.undo()
    assert panel.session is before and not panel.busy and not panel.manager.busy
    assert before.calls == []
    connection.activity(False)
    assert panel.scan_button.isEnabled() and panel.inject_button.isEnabled() and panel.undo_button.isEnabled()
    panel.release(); panel.manager.shutdown()


def test_unchanged_refresh_preserves_success_status_and_transfer_table(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    panel.status.setText('Injected 1 card; every field matched readback.')
    panel.table.setItem(0, 3, QTableWidgetItem('312 fields verified'))
    panel.prepared = True
    connection.activity(True)
    synchronized(connection, panel)
    connection.activity(False)
    assert panel.status.text() == 'Injected 1 card; every field matched readback.'
    assert panel.table.item(0, 3).text() == '312 fields verified' and panel.prepared
    panel.release(); panel.manager.shutdown()


def test_changed_refresh_updates_names_but_preserves_manual_slot_selection(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    panel.session.teams[-1]['slots'][1]['name'] = 'Injected player'
    connection.activity(True)
    synchronized(connection, panel, changed=True)
    connection.activity(False)
    assert panel.team.currentData() == 'team-27'
    assert panel.slot_choices[0].currentData() == 1
    assert panel.table.item(0, 2).text() == 'Injected player'
    assert panel.session.last_run == ('undo-journal',)
    assert panel.undo_button.isEnabled()
    panel.release(); panel.manager.shutdown()


def test_connection_page_rebind_updates_injection_selection_and_manual_slots(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    other = tmp_path / 'roster0008'; other.write_bytes(b'new')
    panel.manager.discard()
    panel.manager.session = BoundSession(other)
    connection.roster_file = str(other)
    assert not panel.can_inject()
    connection.activity(True)
    synchronized(connection, panel, changed=True, rebound=True)
    connection.activity(False)
    assert panel.roster.currentData() == str(other)
    assert panel.team.currentData() == 'team-27' and panel.slot_choices[0].currentData() == 1
    assert panel.can_inject() and not panel.undo_button.isEnabled()
    panel.release(); panel.manager.shutdown()


def test_changing_roster_while_reserved_never_closes_transaction_handle(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    other = tmp_path / 'roster0008'; other.write_bytes(b'new')
    panel.find_files()
    original = panel.session
    panel.manager.reserve(writing=True)
    panel.roster.setCurrentIndex(panel.roster.findData(str(other)))
    assert panel.session is original and not original.closed
    assert connection.roster_file == str(other)
    assert not panel.can_inject() and not panel.undo_button.isEnabled()
    panel.manager.run(lambda _: None, lambda _: None)
    result = sync_roster(panel.manager, connection.roster_file,
                         {'pid': 52, 'token': 'birth', 'supported': True}, session_factory=BoundSession)
    connection.snapshot_changed.emit({'sync': result})
    assert original.closed and panel.roster.currentData() == str(other)
    assert panel.can_inject()
    panel.release(); panel.manager.shutdown()


def test_missing_selected_file_clears_confirmation_instead_of_guessing_another(tmp_path, monkeypatch):
    app, panel, connection, path = panel_fixture(tmp_path, monkeypatch, existing=True, confirmed=True)
    other = tmp_path / 'roster0008'; other.write_bytes(b'new')
    path.unlink()
    panel.find_files()
    assert connection.roster_file == ''
    assert panel.session is None and not panel.can_inject()
    panel.release(); panel.manager.shutdown()
