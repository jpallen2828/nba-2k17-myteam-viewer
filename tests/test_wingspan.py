"""Calibration, saved authoring, and real offline transaction coverage."""
from copy import deepcopy
import json
import struct
import zipfile

import pytest
from PIL import Image

from nba2k17_viewer.wingspan import custom_length, slider_scale, format_length
from nba2k17_viewer.roster_injection import RosterSession
from nba2k17_card_studio.document import Document, SECTIONS, save_document, open_document
from nba2k17_card_studio.option_defaults import studio_data
from scanner.injection_transaction import Region, TransactionFailure
from test_sculpt_transfer import session, prepare, OWNER, BODY


@pytest.mark.parametrize('height,slider,measured', [
    (190.5,0,180.77723693847656), (190.5,50,200.8636016845703), (190.5,100,220.94996643066406),
    (231.13999938964844,0,219.34304809570312), (231.13999938964844,50,243.71450805664062),
    (231.13999938964844,100,268.0859680175781)])
def test_formula_matches_captured_edited_lengths(height, slider, measured):
    assert abs(custom_length(height, slider)-measured)<0.001


@pytest.mark.parametrize('bad', [-1,101,50.5,True,'50',float('nan')])
def test_invalid_slider_refused(bad):
    with pytest.raises(ValueError): custom_length(190.5,bad)


def test_display_carries_inches_at_foot_boundary():
    assert format_length(182.8799)=='182.88 cm  /  6′ 0.0″'
    assert format_length(custom_length(190.5,50))=='200.86 cm  /  6′ 7.1″'


def test_persist_export_resolve_zero_and_original_metadata(tmp_path):
    from nba2k17_scraper.injection_exporter import resolve_player_values, _section_overrides
    from types import SimpleNamespace
    # Source length is metadata; it must never replace an explicit slider zero.
    card={'identity':{'values':{'wingspan_value':0},'statuses':{'wingspan_value':'CUSTOM_AUTHORED'}},
          'raw_source':{'live_roster_template':{'defaults':{'appearance':{'wingspan_cm':191.77}}}}}
    before=deepcopy(card); doc=Document(data=studio_data(card))
    assert card==before
    path=tmp_path/'custom.2k17custom'; save_document(path,doc,Image.new('RGBA',(4,4)))
    restored=open_document(path)
    assert restored.data['identity']['values']['wingspan_value']==0
    assert restored.data['identity']['values']['source_wingspan_cm']==191.77
    with zipfile.ZipFile(path) as archive:
        data=json.loads(archive.read('data.json'))['data']
    record=SimpleNamespace(**{s:SimpleNamespace(values=data[s]['values'],statuses=data[s]['statuses']) for s in SECTIONS})
    overrides=_section_overrides(record)
    assert overrides['identity']['wingspan_value']==0
    assert 'source_wingspan_cm' not in overrides['identity']
    profile={'card_specific_overrides':overrides,'override_evidence':{s:{k:{} for k in vs} for s,vs in overrides.items()}}
    assert resolve_player_values(profile,{})['values']['identity']['wingspan_value']==0


@pytest.fixture
def editor(monkeypatch):
    monkeypatch.setenv('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication
    from nba2k17_card_studio.editor_workspace import DataEditor
    app=QApplication.instance() or QApplication([])
    doc=Document(); doc.data['identity']['values'].update(height='6\'3"',height_cm=190.5,source_wingspan_cm=191.77)
    presets={'cards':[],'schema':{s:{} for s in SECTIONS}}
    widget=DataEditor(doc,presets); widget.resize(1000,850); widget.show(); app.processEvents()
    yield widget
    widget.close(); widget.deleteLater(); app.processEvents()


def test_studio_live_units_height_restore_and_no_implicit_50(editor):
    assert '191.77 cm' in editor.wingspan_length.text()
    assert not editor.wingspan_slider.isEnabled()
    assert 'wingspan_value' not in editor.document.authored_data()['identity']['values']
    editor.wingspan_mode.setCurrentIndex(1)
    assert '200.86 cm' in editor.wingspan_length.text()
    editor.wingspan_slider.setValue(100)
    assert '220.95 cm' in editor.wingspan_length.text()
    assert editor.document.data['identity']['values']['wingspan_value']==100
    editor.changed('identity','height','7\'7"')
    assert '268.09 cm' in editor.wingspan_length.text()
    editor.wingspan_spin.setValue(0)
    assert editor.wingspan_slider.value()==0 and '219.34 cm' in editor.wingspan_length.text()
    editor.wingspan_restore.click()
    assert '191.77 cm' in editor.wingspan_length.text()
    assert 'wingspan_value' not in editor.document.authored_data()['identity']['values']
    editor.populate()
    assert editor.wingspan_mode.currentText()=='Original'


def prepare_custom(s,memory,slider=100,face=8569):
    profile=prepare(s,memory,face=face)
    s.plans[0]['resolved']['values']['identity']['wingspan_value']=slider
    s.extra_regions=[];s.patches=[p for p in s.patches if p.region.startswith('player:')]
    s._prepare_bodies([profile],'a'*64)
    # This test focuses on body and inline transfer; the full sculpt path is
    # already independently covered by test_sculpt_transfer.
    s.transaction_regions=tuple({(r.address,len(r.before)):r for r in s.regions+s.extra_regions}.values())
    s.patches=tuple(s.patches)
    s.prepared_report={'players':s.plans,'remaining_limits':[]}
    return profile


@pytest.mark.parametrize('slider', [0,50,100])
def test_custom_write_readback_journal_and_undo_preserve_body_neighbors(session,tmp_path,slider):
    s,memory=session; before=deepcopy(memory.data)
    prepare_custom(s,memory,slider)
    patches=[p for p in s.patches if p.region.startswith('body:')]
    assert [(p.offset,len(p.after)) for p in patches]==[(0,8),(0x60,4)]
    assert s.plans[0]['body']['wingspan_value']==slider
    result=s.write(tmp_path/'custom')
    assert result['all_fields_match']
    assert struct.unpack('<f',memory.read(BODY+4,4))[0]==custom_length(193,slider)
    assert struct.unpack('<f',memory.read(BODY+0x60,4))[0]==pytest.approx(slider_scale(slider))
    assert memory.data[BODY][8:0x60]==before[BODY][8:0x60]
    assert memory.data[BODY][0x64:]==before[BODY][0x64:]
    checks=json.loads((tmp_path/'custom/readback.json').read_text())
    assert any(c['field']=='appearance.wingspan_scale' and c['matches'] for c in checks)
    assert 'appearance.wingspan_scale' in (tmp_path/'custom/write.jsonl').read_text()
    assert s.undo()['restored_all_watched_regions']
    assert memory.data==before


def test_partial_scale_write_rolls_back_all_fields(session,tmp_path):
    s,memory=session; before=deepcopy(memory.data)
    prepare_custom(s,memory)
    memory.fail_call=3
    with pytest.raises(TransactionFailure) as exc:s.write(tmp_path/'failed')
    assert exc.value.report['rollback_verified']
    assert memory.data==before


def test_custom_cannot_bypass_shared_body_ownership(session):
    s,memory=session; profile=prepare(s,memory,face=8569)
    s.plans[0]['resolved']['values']['identity']['wingspan_value']=100
    s.body_owner_index[BODY]=[OWNER,OWNER+0x1000]
    with pytest.raises(ValueError,match='shared'):s._prepare_bodies([profile],'a'*64)
    assert memory.calls==0


def test_repeated_custom_values_use_card_height_without_compounding(session,tmp_path):
    s,memory=session
    for index,slider in enumerate((100,0,50)):
        prepare_custom(s,memory,slider)
        assert s.write(tmp_path/str(index))['all_fields_match']
        assert struct.unpack('<f',memory.read(BODY+4,4))[0]==custom_length(193,slider)


def test_custom_native_sculpt_keeps_saved_body_style(session):
    s,memory=session;profile=prepare(s,memory,face=1)
    plan=s.plans[0];plan['resolved']['values']['identity']['wingspan_value']=0
    before=memory.read(BODY,132)
    expected=bytearray.fromhex(plan['native_sculpt_appearance']['body_hex'])
    expected[:8]=struct.pack('<ff',193,custom_length(193,0))
    struct.pack_into('<f',expected,0x60,0.9)
    expected[131]=(expected[131]&0x3f)|(before[131]&0xc0)
    assert RosterSession.desired_body(profile,plan,before)==bytes(expected)


def test_old_cards_keep_source_length_and_custom_extremes_are_valid():
    profile={'reusable_roster_template':{'defaults':{'identity':{'height_cm':190.5},'appearance':{'wingspan_cm':191.77}}}}
    plan={'resolved':{'values':{'identity':{}}}}
    assert RosterSession.desired_body(profile,plan)==struct.pack('<ff',190.5,191.77)
    for height in (120,260):
        for slider in (0,100):
            plan['resolved']['values']['identity']={'height_cm':height,'wingspan_value':slider}
            assert len(RosterSession.desired_body(profile,plan,bytes(132)))==132
