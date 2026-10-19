"""Blacktop binds to live canonical slots, with no saved roster dependency."""
import struct
from types import SimpleNamespace
import pytest
from nba2k17_viewer.roster_injection import blacktop_selection, BlacktopSession
from nba2k17_viewer.injection_session import SessionManager
from nba2k17_viewer.hook_roster_sync import sync_roster
from nba2k17_viewer import injection_ui
from test_injection_panel_scan import WritableSession, finish
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QStackedWidget


class Roots:
    def __init__(self):
        self.values = {0x2233220: 100, 0x673d160: 100}
    def read_or_raise(self, address, size):
        return struct.pack('<Q', self.values[address])


def teams():
    return [dict(enabled=True, deferred_all_star=False, key='team-26', label='Golden State Warriors',
                 slots=[dict(player=100), dict(player=200)]),
            dict(enabled=False, deferred_all_star=True, key='team-29', label='All-Stars', slots=[dict(player=100)])]


def test_selection_uses_pointer_membership_and_excludes_all_star_alias():
    r = Roots()
    assert blacktop_selection(r, 0, teams())['team_key'] == 'team-26'
    r.values = {k:200 for k in r.values}
    assert blacktop_selection(r, 0, teams())['team_key'] == 'team-26'


@pytest.mark.parametrize('kind', ['zero', 'disagree', 'unmapped', 'shared', 'unstable'])
def test_ambiguous_and_changing_selections_fail_closed(kind):
    r, ts = Roots(), teams()
    if kind == 'zero': r.values = {k:0 for k in r.values}
    if kind == 'disagree': r.values[0x2233220] = 200
    if kind == 'unmapped': r.values = {k:300 for k in r.values}
    if kind == 'shared': ts.append({**ts[0], 'key':'team-1'})
    if kind == 'unstable':
        original = r.read_or_raise
        calls=[]
        def read(a, n):
            calls.append(a)
            return original(a,n) if len(calls)<3 else struct.pack('<Q', 200)
        r.read_or_raise=read
    with pytest.raises(ValueError): blacktop_selection(r, 0, ts)


def test_live_binding_rejects_team_changes_and_stale_tables(monkeypatch):
    from scanner.discovery import live
    monkeypatch.setattr(live, 'process_token', lambda r:'birth')
    s=BlacktopSession.__new__(BlacktopSession)
    s.reader=Roots(); s.reader.main_module=SimpleNamespace(base_address=0)
    s.module_base=0; s.process_start='birth'; s.roster_file=None; s.file_stat=None
    s.teams=teams()[:1]
    s.teams[0].update(address=400, table_bytes=b'a'*160, identity_bytes=b'b'*20)
    roots_read=s.reader.read_or_raise
    s.reader.read_or_raise=lambda a,n: (b'a'*160+b'b'*20)[:n] if a==400 else roots_read(a,n)
    s.reader.read=lambda a,n:s.reader.read_or_raise(a,n)
    s.blacktop_binding=blacktop_selection(s.reader,0,s.teams)
    assert s._validate_binding()
    s.blacktop_binding['team_key']='different'
    with pytest.raises(ValueError, match='team selection changed'):s._validate_binding()
    s.reader.read_or_raise=lambda a,n:b'x'*n
    with pytest.raises(ValueError, match='team references changed'):s._validate_binding()


def test_each_patch_checks_blacktop_team_before_touching_destination():
    s=BlacktopSession.__new__(BlacktopSession)
    s.reader=Roots();s.module_base=0;s.teams=teams()
    s.blacktop_binding={'team_key':'different'}
    with pytest.raises(ValueError,match='Blacktop team changed before destination write'):
        s.validate_patch(SimpleNamespace(region='player:0x64'))


class LiveSession(WritableSession):
    live_mode='blacktop'
    def __init__(self, path=None, progress=lambda _:None, **kwargs):
        assert path is None
        super().__init__(path,progress)
        self.roster_file=None;self.pid=52;self.process_start='birth';self.valid=True
        self.blacktop_binding={'team_key':'team-27','team':'Washington Wizards'}
    def public_scan(self):
        return {'teams':self.teams,'roster_file':None,'live_mode':'blacktop'}


def panel(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    settings=QSettings(str(tmp_path/'prefs.ini'),QSettings.Format.IniFormat)
    settings.setValue('directory',str(tmp_path/'no-rosters'))
    monkeypatch.setattr(injection_ui,'QSettings',lambda *_:settings)
    monkeypatch.setattr(injection_ui,'BlacktopSession',LiveSession)
    owner=SimpleNamespace(repository=SimpleNamespace(cards={'a':{'injection':{
        'source_card_identity':{'name':'Incoming','tier':'Diamond','overall':95}}}}),close_overlay=lambda:None)
    p=injection_ui.InjectionPanel(owner,[(0,{'id':'a'})],blacktop=True)
    return app,p


def test_fileless_scan_detects_team_injects_and_monitor_keeps_undo(tmp_path,monkeypatch):
    app,p=panel(tmp_path,monkeypatch)
    assert p.scan_button.isEnabled() and not p.roster.isEnabled()
    p.scan();finish(app,p)
    assert p.team.count()==1 and p.team.currentData()=='team-27'
    p.inject();finish(app,p)
    assert p.session.calls==['begin_operation',('prepare','team-27',[0]),'write']
    assert p.undo_button.isEnabled()
    before=p.session
    result=sync_roster(p.manager,'',{'pid':52,'token':'birth','supported':True})
    assert result['state']=='ready' and not result['rebound']
    assert p.session is before and p.session.last_run
    p.release();p.manager.shutdown()


def test_monitor_does_not_rebind_blacktop_after_game_replacement(tmp_path,monkeypatch):
    app,p=panel(tmp_path,monkeypatch)
    p.scan();finish(app,p)
    old=p.session
    result=sync_roster(p.manager,'',{'pid':99,'token':'new','supported':True})
    assert result['state']=='error' and p.session is None and old.closed
    p.release();p.manager.shutdown()


@pytest.mark.parametrize('mode',['custom','random','draft'])
def test_other_modes_do_not_have_blacktop_injection_buttons(mode):
    from nba2k17_viewer.team_ui import TeamWorkspace
    from nba2k17_viewer.desktop import STYLE
    app=QApplication.instance() or QApplication([])
    app.setStyleSheet(STYLE)
    pages=QStackedWidget();owner=SimpleNamespace(pages=pages,repository=SimpleNamespace(rows=[]),cache=None)
    w=TeamWorkspace(owner,mode);pages.addWidget(w);pages.resize(1030,700);pages.show();app.processEvents()
    from PySide6.QtWidgets import QPushButton
    assert not hasattr(w, 'blacktop_button')
    assert not any('blacktop' in b.text().lower() for b in w.findChildren(QPushButton))
    pages.close();pages.deleteLater();app.processEvents()
