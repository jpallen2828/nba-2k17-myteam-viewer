import struct
from collections import defaultdict
from types import SimpleNamespace
import pytest
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication
from nba2k17_viewer.roster_injection import BlacktopSession, ranked_profiles, live_overall
from nba2k17_viewer import injection_ui


def record(ovr):
    raw=bytearray(0x460)
    struct.pack_into('<f',raw,0x150,ovr/100)
    struct.pack_into('<Q',raw,0x88,0x9000)
    return bytes(raw)


def session():
    s=BlacktopSession.__new__(BlacktopSession)
    s.live_mode='blacktop';s.blacktop_binding={'team_key':'t','team':'Live team'}
    slots=[{'slot':i,'player':100+i,'name':f'Host {i}'} for i in range(4)]
    s.records={100+i:record(ovr) for i,ovr in enumerate([78,94,89,96])}
    team={'key':'t','label':'Live team','nickname':'Team','enabled':True,'kind':'CURRENT','slots':slots}
    s.teams=[team];s.reference_index={100+i:[{'deferred':False,'team_key':'t'}] for i in range(4)}
    s.body_values={};s._compile=lambda p,r:{};s.desired_body=lambda p,plan:b''
    return s,team


def profiles():
    return [{'source_card_identity':{'name':name,'overall':ovr}} for name,ovr in [('Low',80),('Top',99),('Bonus',98)]]


def test_best_cards_replace_best_live_players_instead_of_position_or_native_order():
    s,team=session()
    ordered=ranked_profiles(profiles())
    slots=s.automatic_slots(ordered,team)
    assert [(p['source_card_identity']['name'],dest['slot'],dest['overall']) for p,dest in zip(ordered,slots)]==[
        ('Top',3,96),('Bonus',1,94),('Low',2,89)]
    assert s.preflight(ordered,'t',slots=[3,1,2],live=False)['blockers']==[]
    report=s.preflight(ordered,'t',slots=[0,1,2],live=False)
    assert any('highest-overall order' in b for b in report['blockers'])


def test_equal_ratings_keep_draft_order_and_native_slot_order():
    s,team=session()
    s.records={p:record(94) for p in s.records}
    team['slots'].reverse()
    ps=[{'source_card_identity':{'name':n,'overall':99}} for n in ['First','Second','Third']]
    assert ranked_profiles(ps)==ps
    assert [d['slot'] for d in s.automatic_slots(ps,team)]==[0,1,2]


@pytest.mark.parametrize('value',[float('nan'),float('inf'),-1,2,0])
def test_invalid_live_overall_does_not_fall_back_to_slot_or_name(value):
    raw=bytearray(0x460);struct.pack_into('<f',raw,0x150,value)
    with pytest.raises(ValueError,match='overall is unavailable'):live_overall(raw)


@pytest.mark.parametrize('value',[None,True,'99',float('nan'),100])
def test_invalid_source_overall_cannot_be_ranked(value):
    with pytest.raises(ValueError):ranked_profiles([{'source_card_identity':{'overall':value}}])


def test_panel_sorts_incoming_cards_and_locks_rating_ranked_destinations(tmp_path,monkeypatch):
    app=QApplication.instance() or QApplication([])
    settings=QSettings(str(tmp_path/'prefs.ini'),QSettings.Format.IniFormat)
    monkeypatch.setattr(injection_ui,'QSettings',lambda *_:settings)
    cards={str(i):{'injection':p} for i,p in enumerate(profiles())}
    owner=SimpleNamespace(repository=SimpleNamespace(cards=cards),close_overlay=lambda:None)
    panel=injection_ui.InjectionPanel(owner,[(str(i),{'id':str(i)}) for i in range(3)],blacktop=True)
    s,team=session();s.valid=True;s.close=lambda:None
    panel.session=s;panel.filter_teams()
    assert [p['source_card_identity']['overall'] for p in panel.profiles]==[99,98,80]
    assert [c.currentData() for c in panel.slot_choices]==[3,1,2]
    assert all(not c.isEnabled() for c in panel.slot_choices)
    assert '96 OVR' in panel.slot_choices[0].currentText()
    panel.release();panel.manager.shutdown();panel.deleteLater();app.processEvents()
