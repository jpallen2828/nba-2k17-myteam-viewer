import copy
import json
import random
from types import SimpleNamespace
import pytest
from PySide6.QtWidgets import QApplication, QStackedWidget, QFrame, QPushButton
from nba2k17_viewer.blacktop import BlacktopDraftSession, eligible_role
from nba2k17_viewer.teams import TIERS, TeamSession, player_key, bonus_odds
from nba2k17_viewer.draft_autosave import DraftAutosave


@pytest.fixture(scope='module')
def rows():
    return [dict(id=f'{g}-{n}-{tier}',source_card_id=f'{g}-{n}-{tier}',name=f'Player {g} {n}',
                 position=pos,secondary_position=secondary,tier=tier,overall=80,has_roster_template=True,
                 year=2020,team='Team',image_path=None)
            for g,(pos,secondary) in enumerate([('PG','SG'),('SG','SF'),('SF','PF'),('PF','SF'),('PF','C'),('C','PF')])
            for n in range(10) for tier in TIERS]


@pytest.mark.parametrize('primary,secondary,roles',[
    ('PG','SG',{'guard'}),('SG','PG',{'guard','wing'}),('SF','PF',{'wing'}),
    ('PF','SF',{'wing'}),('PF','C',{'big'}),('C','PF',{'big'}),('PF','c',{'big'})])
def test_role_boundaries(primary,secondary,roles):
    row={'position':primary,'secondary_position':secondary}
    assert {role for role in ('guard','wing','big') if eligible_role(row,role)}==roles


@pytest.mark.parametrize('mode',['baller','default','budget','super_budget'])
@pytest.mark.parametrize('size',[1,2,3,4,5])
def test_all_sizes_and_odds_have_valid_complete_drafts_and_resume(rows,size,mode):
    s=BlacktopDraftSession(rows,random.Random(2017));s.configure(size=size);s.mode=mode
    with pytest.raises(ValueError):s.open_pack('bonus')
    for key in s.keys:
        pack=s.open_pack(key)
        assert len(pack)==5 and len({player_key(r) for r in pack})==5
        assert all(s.eligible(row,key) for row in pack)
        loaded=BlacktopDraftSession.from_data(rows,json.loads(json.dumps(s.data())))
        assert loaded.data()==s.data() and loaded.open_pack(key)==pack
        if key=='bonus':assert all(row['tier'] in dict(bonus_odds(mode)) for row in pack)
        s.pick(pack[0]['source_card_id'])
    assert s.complete and len(s.lineup())==size+1
    assert s.active_lineup()==s.lineup()
    assert len({player_key(row) for _,row in s.lineup()})==size+1
    assert BlacktopDraftSession.from_data(rows,s.data()).lineup()==s.lineup()


@pytest.mark.parametrize('solo_role',['guard','wing','big'])
def test_solo_filter_applies_to_regular_and_bonus(rows,solo_role):
    s=BlacktopDraftSession(rows,random.Random(4));s.configure(solo_role=solo_role)
    for key in s.keys:
        pack=s.open_pack(key)
        assert all(eligible_role(r,solo_role) for r in pack)
        s.pick(pack[0]['source_card_id'])
    assert len(s.active_lineup())==2


def test_five_player_flex_is_fair_and_survives_save(rows):
    counts={'wing':0,'big':0}
    for seed in range(1000):
        s=BlacktopDraftSession(rows,random.Random(seed));s.configure(size=5)
        counts[s.flex_role]+=1
        assert s.roles()==('guard','guard','wing',s.flex_role,'big')
        assert BlacktopDraftSession.from_data(rows,s.data()).flex_role==s.flex_role
    assert all(450 < count < 550 for count in counts.values())


def test_old_substitute_saves_restore_all_cards_and_settings_stay_locked(rows):
    s=BlacktopDraftSession(rows);s.configure(size=3)
    for key in s.keys:
        pack=s.open_pack(key);s.pick(pack[0]['source_card_id'])
    data=s.data();data['bonus_slot']='blacktop|2'
    restored=BlacktopDraftSession.from_data(rows,data)
    assert restored.lineup()==s.lineup() and len(restored.active_lineup())==4
    assert 'bonus_slot' not in restored.data()
    with pytest.raises(ValueError):s.configure(size=5)



@pytest.mark.parametrize('field,value',[('size',0),('size',True),('size',None),('solo_role','center'),
    ('flex_role','any'),('mode','unknown'),('pending',[])])
def test_corrupt_saved_settings_reject(rows,field,value):
    data=BlacktopDraftSession(rows).data();data[field]=value
    with pytest.raises(ValueError):BlacktopDraftSession.from_data(rows,data)


@pytest.mark.parametrize('kind',['draft','blacktop'])
def test_autosave_preserves_pending_pack_and_each_draft_has_its_own_file(rows,tmp_path,monkeypatch,kind):
    monkeypatch.setenv('NBA2K17_DRAFT_AUTOSAVE_DIR',str(tmp_path))
    cls=BlacktopDraftSession if kind=='blacktop' else TeamSession
    s=cls(rows) if kind=='blacktop' else cls(rows,'draft')
    key=s.regular_keys[0] if kind=='blacktop' else 'starter|PG'
    auto=DraftAutosave(kind)
    assert auto.save(s) is None
    pack=s.open_pack(key);path=auto.save(s)
    assert cls.load(rows,path).options==s.options
    s.pick(pack[0]['source_card_id']);assert auto.save(s)==path
    assert cls.load(rows,path).selected==s.selected
    second=DraftAutosave(kind);new_path=second.save(s)
    assert new_path!=path and new_path.exists() and path.exists()
    assert not list(tmp_path.rglob('*.tmp'))


def workspace(rows,tmp_path,monkeypatch):
    from nba2k17_viewer.team_ui import TeamWorkspace
    monkeypatch.setenv('NBA2K17_DRAFT_AUTOSAVE_DIR',str(tmp_path))
    app=QApplication.instance() or QApplication([])
    pages=QStackedWidget()
    cards={r['id']:{'badges':{'values':{}},'attributes':{'values':{'open_shot_three':80}},
                       'injection':{'source_card_identity':{'name':r['name'],'overall':r['overall']}}} for r in rows}
    owner=SimpleNamespace(pages=pages,repository=SimpleNamespace(rows=rows,cards=cards),overlay=None)
    def close():
        if owner.overlay:
            owner.overlay.hide();owner.overlay.deleteLater();owner.overlay=None
            w.pack_closed()
    owner.close_overlay=close
    owner.show_overlay=lambda panel,callback:setattr(owner,'overlay',panel)
    w=TeamWorkspace(owner,'blacktop');pages.addWidget(w);pages.resize(1030,760);pages.show();app.processEvents()
    return app,w,owner


def test_workspace_packs_autosave_and_all_visible_cards_inject(rows,tmp_path,monkeypatch):
    from nba2k17_viewer import injection_ui
    app,w,owner=workspace(rows,tmp_path,monkeypatch)
    w.team_size.setCurrentIndex(w.team_size.findData(3))
    assert not w.solo_controls.isVisible()
    for key in w.session.keys:
        w.open_pack(key)
        assert w.autosave.path and BlacktopDraftSession.load(rows,w.autosave.path).pending==key
        w.pick(w.session.options[0])
    assert w.inject_button.isEnabled()
    assert not hasattr(w,'bonus_choice')
    restored=BlacktopDraftSession.load(rows,w.autosave.path)
    assert restored.lineup()==w.session.lineup()
    received=[]
    def panel(o,lineup,**kwargs):
        received.append((lineup,kwargs));p=QFrame();p.release=lambda:None;return p
    monkeypatch.setattr(injection_ui,'InjectionPanel',panel)
    w.open_injection()
    assert received==[(w.session.active_lineup(),{'blacktop':True})]
    assert len(received[0][0])==4
    pages=owner.pages;pages.close();pages.deleteLater();app.processEvents()


def test_autosave_failure_is_visible(rows,tmp_path,monkeypatch):
    app,w,owner=workspace(rows,tmp_path,monkeypatch)
    monkeypatch.setattr(w.autosave,'save',lambda s:(_ for _ in ()).throw(OSError('disk full')))
    w.open_pack(w.session.regular_keys[0])
    assert 'Auto-save failed' in w.autosave_note.text() and 'disk full' in w.autosave_note.text()
    owner.close_overlay();owner.pages.close();owner.pages.deleteLater();app.processEvents()
