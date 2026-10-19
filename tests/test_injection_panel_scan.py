"""Read-only scan UI regressions: no confirmation gate, retained results, errors."""
from pathlib import Path
from types import SimpleNamespace
import sys
import time

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / 'tools'), str(Path(__file__).resolve().parents[1] / 'src')]
from PySide6.QtCore import QSettings, QThread
from PySide6.QtWidgets import QApplication
from nba2k17_viewer import injection_ui


def make_panel(tmp_path, monkeypatch, with_card=False):
    app = QApplication.instance() or QApplication([])
    path = tmp_path / 'roster0003'; path.write_bytes(b'test roster')
    settings = QSettings(str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat)
    settings.setValue('directory', str(tmp_path))
    monkeypatch.setattr(injection_ui, 'QSettings', lambda *_: settings)
    cards = {'curry': {'injection': {'source_card_identity': {'name': 'Stephen Curry', 'overall': 99, 'tier': 'Pink Diamond'}}}}
    owner = SimpleNamespace(repository=SimpleNamespace(cards=cards), close_overlay=lambda: None)
    return app, injection_ui.InjectionPanel(owner, [(0, {'id': 'curry'})] if with_card else [])


def finish(app, panel):
    deadline = time.monotonic() + 5
    while panel.busy and time.monotonic() < deadline:
        app.processEvents(); time.sleep(.005)
    app.processEvents()
    assert not panel.busy


class Session:
    def __init__(self, path, progress):
        progress('Reading live team referencesâ€¦')
        self.closed = False
        self.records = {100: b''}
        self.teams = [{'enabled': True, 'kind': 'CURRENT', 'label': 'Washington Wizards', 'nickname': 'Wizards',
                       'key': 'team-27', 'slots': [{'slot': 0, 'name': 'John Wall'}]}]
    def public_scan(self):
        return {'teams': self.teams}
    def close(self):
        self.closed = True
    def begin_operation(self):pass
    def validate(self):
        if self.closed:raise ValueError('Closed session')
        return True
    def refresh_mutable_records(self):return 0


def test_scan_without_checkbox_and_refresh_preserves_result(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch)
    monkeypatch.setattr(injection_ui, 'RosterSession', Session)
    assert panel.scan_button.isEnabled()
    panel.scan_button.click()
    assert not panel.scan_button.isEnabled() and 'Scanning' in panel.status.text()
    finish(app, panel)
    assert panel.team.currentData() == 'team-27'
    assert 'Resolved 1 NBA teams' in panel.status.text()
    session = panel.session
    panel.find_button.click()
    assert panel.session is session and not session.closed
    assert panel.team.currentData() == 'team-27'
    assert panel.scan_button.isEnabled()
    panel.release()


def test_error_is_visible_and_retry_enabled(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise ValueError('NBA 2K17 is not running')
    monkeypatch.setattr(injection_ui, 'RosterSession', fail)
    panel.scan_button.click(); finish(app, panel)
    assert panel.status.text() == 'Stopped: NBA 2K17 is not running'
    assert panel.scan_button.isEnabled() and panel.team.count() == 0


def test_completion_runs_on_gui_thread_and_reports_callback_failure(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch)
    def done(value):
        assert QThread.currentThread() == app.thread()
        raise ValueError('Cannot display team results')
    panel.start(lambda progress: None, done); finish(app, panel)
    assert panel.status.text() == 'Stopped: Cannot display team results'
    assert panel.scan_button.isEnabled()


def test_missing_folder_disables_scan_and_explains_why(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch)
    panel.directory.setText(str(tmp_path / 'missing'))
    panel.find_files()
    assert not panel.scan_button.isEnabled()
    assert 'folder' in panel.status.text()


def test_editing_folder_cannot_scan_stale_file_selection(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch)
    panel.directory.textEdited.emit(str(tmp_path / 'another'))
    assert panel.roster.count() == 0 and not panel.scan_button.isEnabled()
    assert 'Find rosters' in panel.status.text()


def test_current_teams_sorted_by_nickname_historic_order_preserved(tmp_path, monkeypatch):
    from nba2k17_viewer.roster_injection import runtime_definition
    app, panel = make_panel(tmp_path, monkeypatch)
    teams = [{**t, 'slots': []} for t in runtime_definition()['teams']]
    panel.session = SimpleNamespace(teams=teams)
    panel.filter_teams()
    current = sorted((t for t in teams if t['enabled'] and t['kind'] == 'CURRENT'), key=lambda t:t['nickname'].casefold())
    assert [panel.team.itemData(i) for i in range(panel.team.count())] == [t['key'] for t in current]
    assert [t['nickname'] for t in current][:6] == ['76ers', 'Bucks', 'Bulls', 'Cavaliers', 'Celtics', 'Clippers']
    panel.kind.setCurrentIndex(panel.kind.findData('HISTORIC'))
    assert [panel.team.itemData(i) for i in range(panel.team.count())] == [t['key'] for t in teams if t['enabled'] and t['kind'] == 'HISTORIC']


class WritableSession(Session):
    def __init__(self, path, progress):
        super().__init__(path, progress)
        self.calls = []
        self.last_run = None
        self.teams.insert(0, {'enabled': True, 'kind': 'CURRENT', 'label': 'Philadelphia 76ers',
                             'nickname': '76ers', 'key': 'team-0', 'slots': [{'slot': 0, 'name': 'Joel Embiid'}]})
        self.teams[-1]['slots'].append({'slot': 1, 'name': 'Bradley Beal'})

    def begin_operation(self):
        self.calls.append('begin_operation')

    def prepare(self, profiles, key, *, slots):
        self.calls.append(('prepare', key, slots))
        if None in slots:
            raise ValueError('Selected slot is unavailable')
        team = next(t for t in self.teams if t['key'] == key)
        return {'players': [{'destination': next(s for s in team['slots'] if s['slot'] == slots[0]),
                             'requires_mod_assets': False, 'fields': [1, 2]}], 'remaining_limits': ['Gear retained.']}

    def diagnose_ownership(self, key, profiles, *, slots=None):
        return {'blocked_bodies': 0}

    def write(self):
        self.calls.append('write')
        self.last_run = ('report-directory',)
        return {'cards_written': 1, 'field_readback_count': 2, 'report_directory': 'report-directory'}


def test_scan_enables_direct_inject_and_checks_selected_slots_before_write(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    monkeypatch.setattr(injection_ui, 'RosterSession', WritableSession)
    assert not panel.inject_button.isEnabled()
    panel.inject(); assert not panel.busy
    panel.scan_button.click(); finish(app, panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    assert panel.inject_button.isEnabled() and not panel.prepared
    assert not hasattr(panel, 'partial')
    panel.inject_button.click()
    assert not panel.inject_button.isEnabled()
    finish(app, panel)
    assert panel.session.calls == ['begin_operation', ('prepare', 'team-27', [1]), 'write']
    assert 'Injected 1 card(s)' in panel.status.text()
    assert panel.table.item(0, 2).text() == 'Bradley Beal'
    assert panel.undo_button.isEnabled()
    panel.release()


def test_rescan_preserves_team_and_custom_slots_even_after_scan_failure(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    monkeypatch.setattr(injection_ui, 'RosterSession', WritableSession)
    panel.scan_button.click(); finish(app, panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    previous = panel.session
    panel.scan_button.click(); finish(app, panel)
    assert previous.closed and panel.session is not previous
    assert panel.team.currentData() == 'team-27'
    assert panel.slot_choices[0].currentData() == 1
    assert panel.table.item(0, 2).text() == 'Bradley Beal'
    def fail(*args, **kwargs):
        raise ValueError('Roster reload in progress')
    monkeypatch.setattr(injection_ui, 'RosterSession', fail)
    panel.scan_button.click(); finish(app, panel)
    assert not panel.inject_button.isEnabled()
    monkeypatch.setattr(injection_ui, 'RosterSession', WritableSession)
    panel.scan_button.click(); finish(app, panel)
    assert panel.team.currentData() == 'team-27'
    assert panel.slot_choices[0].currentData() == 1
    panel.release()


def test_missing_previous_team_does_not_silently_select_76ers(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    monkeypatch.setattr(injection_ui, 'RosterSession', WritableSession)
    panel.scan_button.click(); finish(app, panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    class MissingTeam(WritableSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.teams = self.teams[:1]
    monkeypatch.setattr(injection_ui, 'RosterSession', MissingTeam)
    panel.scan_button.click(); finish(app, panel)
    assert panel.team.currentIndex() == -1 and not panel.inject_button.isEnabled()
    assert 'previous team is unavailable' in panel.status.text()
    panel.release()


def test_missing_slot_is_not_substituted_and_failed_check_never_writes(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    monkeypatch.setattr(injection_ui, 'RosterSession', WritableSession)
    panel.scan_button.click(); finish(app, panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    monkeypatch.setattr(injection_ui, 'RosterSession', Session)
    panel.scan_button.click(); finish(app, panel)
    assert panel.team.currentData() == 'team-27'
    assert panel.slot_choices[0].currentIndex() == -1
    assert panel.table.item(0, 2).text() == 'Unavailable'
    # Keep the fresh scan's actual slots, but use the guarded fake writer.
    panel.session.calls = []
    panel.session.begin_operation = lambda: panel.session.calls.append('begin_operation')
    panel.session.prepare = lambda *a, **k: WritableSession.prepare(panel.session, *a, **k)
    panel.session.diagnose_ownership = lambda *a, **k: {'blocked_bodies': 0}
    panel.session.write = lambda: panel.session.calls.append('write')
    panel.inject_button.click(); finish(app, panel)
    assert panel.session.calls == ['begin_operation', ('prepare', 'team-27', [None])]
    assert panel.status.text() == 'Stopped: Selected slot is unavailable'
    assert panel.inject_button.isEnabled()
    panel.release()


def test_inject_runs_ownership_before_prepare_and_preserves_team_selection(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    class DiagnosticSession(WritableSession):
        def diagnose_ownership(self, key, profiles, *, slots=None):
            self.calls.append(('diagnostic', key))
            return {'team': 'Washington Wizards', 'validated_bodies': 2, 'blocked_bodies': 0,
                    'report_path': 'diagnostic.json'}
    monkeypatch.setattr(injection_ui, 'RosterSession', DiagnosticSession)
    panel.scan_button.click(); finish(app, panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'))
    panel.slot_choices[0].setCurrentIndex(1)
    assert panel.session.calls == []
    assert not hasattr(panel, 'ownership_button')
    panel.inject_button.click(); finish(app, panel)
    assert panel.session.calls == ['begin_operation', ('diagnostic', 'team-27'), ('prepare', 'team-27', [1]), 'write']
    assert panel.team.currentData() == 'team-27' and panel.slot_choices[0].currentData() == 1
    assert 'Injected 1 card(s)' in panel.status.text()
    panel.release()


def test_unknown_ownership_stops_write_without_releasing_other_valid_destinations(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    class BlockedBodySession(WritableSession):
        def prepare(self, *args, **kwargs):
            self.calls.append('blocked_body')
            raise ValueError('Cannot change height/wingspan: known owner changed. Diagnose ownership (slow).')
    monkeypatch.setattr(injection_ui, 'RosterSession', BlockedBodySession)
    panel.scan_button.click(); finish(app, panel)
    session = panel.session
    panel.inject_button.click(); finish(app, panel)
    assert session.calls == ['begin_operation', 'blocked_body']
    assert panel.session is session and not session.closed
    assert not hasattr(panel, 'ownership_button') and 'height/wingspan' in panel.status.text()
    panel.release()


def test_cancelling_automatic_ownership_never_prepares_or_writes(tmp_path, monkeypatch):
    app, panel = make_panel(tmp_path, monkeypatch, with_card=True)
    class CancelledSession(WritableSession):
        def diagnose_ownership(self, key, profiles, *, slots=None):
            self.calls.append('ownership')
            panel.manager.cancel()
            return {'cancelled': True}
    monkeypatch.setattr(injection_ui, 'RosterSession', CancelledSession)
    panel.scan_button.click(); finish(app, panel)
    panel.inject_button.click(); finish(app, panel)
    assert panel.session.calls == ['begin_operation', 'ownership']
    assert 'cancelled before any game data' in panel.status.text()
    assert not panel.manager.writing


def test_reopening_retains_session_team_slots_and_undo_without_scan(tmp_path,monkeypatch):
    app,panel=make_panel(tmp_path,monkeypatch,with_card=True)
    monkeypatch.setattr(injection_ui,'RosterSession',WritableSession)
    panel.scan();finish(app,panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'));panel.slot_choices[0].setCurrentIndex(1)
    session=panel.session;session.last_run=('durable-run',)
    panel.release();assert not session.closed
    monkeypatch.setattr(injection_ui,'RosterSession',lambda *a,**k: (_ for _ in ()).throw(AssertionError('no rescan')))
    reopened=injection_ui.InjectionPanel(panel.owner,[(0,{'id':'curry'})]);finish(app,reopened)
    assert reopened.session is session and reopened.team.currentData()=='team-27'
    assert reopened.slot_choices[0].currentData()==1 and reopened.undo_button.isEnabled()
    reopened.release();assert panel.owner.injection_manager.shutdown() and session.closed


def test_changed_lineup_reuses_session_but_not_previous_slot_assignment(tmp_path,monkeypatch):
    app,panel=make_panel(tmp_path,monkeypatch,with_card=True)
    monkeypatch.setattr(injection_ui,'RosterSession',WritableSession)
    panel.scan();finish(app,panel)
    panel.team.setCurrentIndex(panel.team.findData('team-27'));panel.slot_choices[0].setCurrentIndex(1)
    session=panel.session;panel.release()
    panel.owner.repository.cards['curry']['injection']['source_card_identity']['overall']=98
    reopened=injection_ui.InjectionPanel(panel.owner,[(0,{'id':'curry'})]);finish(app,reopened)
    assert reopened.session is session and reopened.slot_choices[0].currentData()==0
    reopened.release();reopened.manager.shutdown()


def test_released_worker_result_does_not_touch_panel_or_close_shared_session(tmp_path,monkeypatch):
    from threading import Event
    app,panel=make_panel(tmp_path,monkeypatch)
    started,finish_job=Event(),Event();completed=[]
    def work(progress):started.set();finish_job.wait(2);return 1
    panel.start(work,completed.append);assert started.wait(2)
    assert not panel.manager.reserve()
    panel.release();finish_job.set();finish(app,panel)
    assert completed==[] and panel.manager.cancel_event.is_set()
