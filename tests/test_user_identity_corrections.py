from copy import deepcopy
from nba2k17_viewer.user_identity_corrections import apply_identity_correction
from nba2k17_viewer.user_identity_corrections import apply_player_injection_corrections
import pytest


@pytest.mark.parametrize('cid,year', [('20453', 2010), ('8010', 1998)])
def test_specific_kobe_cards_update_year_and_encode_both_ids(cid, year):
    from nba2k17_scraper.models import CanonicalCard, CARD_SCHEMA
    from nba2k17_scraper.injection_exporter import build_injection_profile, resolve_player_values
    from nba2k17_viewer.runtime_fields import runtime_definition
    from scanner.player_codec import encode, decode
    card = {'schema': CARD_SCHEMA, 'canonical_card_uuid': cid,
            'source': {'source_card_id': cid, 'provider': '2kmtcentral'},
            'identity': {'values': {'name': 'Kobe Bryant', 'card_year': 1998,
                                    'represented_season': '1998'}},
            'gameplay_metadata': {'values': {'face_id': 2061, 'portrait_id': 2061,
                                             'speech_id': 777},
                                  'missing_fields': ['face_id', 'portrait_id']}}
    apply_player_injection_corrections(card)
    once = deepcopy(card)
    apply_player_injection_corrections(card)
    assert card == once
    assert card['identity']['values']['card_year'] == year
    assert card['identity']['values']['represented_season'] == str(year)
    assert card['gameplay_metadata']['values'] == {'face_id': 195, 'portrait_id': 195, 'speech_id': 777}
    assert card['gameplay_metadata']['missing_fields'] == []
    profile = build_injection_profile(CanonicalCard.from_dict(card))
    values = resolve_player_values(profile, {'gameplay_metadata': {'face_id': 9, 'portrait_id': 9}})['values']
    fields = {f['field']: f for f in runtime_definition()['fields']}
    raw = bytes([0xa5]) * 0x460
    expected = bytearray(raw)
    for field in ('face_id', 'portrait_id'):
        definition = fields['gameplay_metadata.' + field]
        assert definition['size'] == 2
        assert values['gameplay_metadata'][field] == 195
        raw = encode(raw, definition, values['gameplay_metadata'][field])
        expected[definition['offset']:definition['offset'] + definition['size']] = (195).to_bytes(definition['size'], 'little')
        assert decode(raw, definition) == 195
    assert raw == bytes(expected)


@pytest.mark.parametrize('cid,name', [('4522', 'Kobe Bryant'), ('8010', 'Other Player')])
def test_kobe_corrections_do_not_change_other_versions_or_players(cid, name):
    card = {'source': {'source_card_id': cid}, 'identity': {'values': {'name': name}},
            'gameplay_metadata': {'values': {'face_id': 2061, 'portrait_id': 2061}}}
    before = deepcopy(card)
    apply_player_injection_corrections(card)
    assert card == before


@pytest.mark.parametrize('cid,jersey', [('2395', '6'), ('8017', '6'), ('20430', '32')])
def test_erving_injection_overrides_and_encoded_readback(cid, jersey):
    from nba2k17_scraper.models import CanonicalCard, CARD_SCHEMA
    from nba2k17_scraper.injection_exporter import build_injection_profile, resolve_player_values
    from nba2k17_viewer.runtime_fields import runtime_definition
    from scanner.player_codec import encode, decode
    card = {'schema': CARD_SCHEMA, 'canonical_card_uuid': cid, 'source': {'source_card_id': cid, 'provider': '2kmtcentral'},
            'identity': {'values': {'name': 'Julius Erving', 'jersey_number': '6'},
                         'statuses': {'jersey_number': 'WEB_CARD_SPECIFIC'}},
            'gameplay_metadata': {'values': {'face_id': 766, 'portrait_id': 766, 'speech_id': 766}}}
    apply_player_injection_corrections(card)
    once = deepcopy(card)
    apply_player_injection_corrections(card)
    assert card == once
    assert card['gameplay_metadata']['values']['portrait_id'] == 766
    assert card['gameplay_metadata']['values']['speech_id'] == 766
    profile = build_injection_profile(CanonicalCard.from_dict(card))
    values = resolve_player_values(profile, {'identity': {'jersey_number': '99'},
                                            'gameplay_metadata': {'face_id': 123}})['values']
    assert values['identity']['jersey_number'] == jersey
    assert values['gameplay_metadata']['face_id'] == 2301
    fields = {field['field']: field for field in runtime_definition()['fields']}
    raw = bytes([0xa5]) * 0x460
    for key, value in [('identity.jersey_number', jersey), ('gameplay_metadata.face_id', 2301)]:
        raw = encode(raw, fields[key], value)
        assert str(decode(raw, fields[key])) == str(value)
    assert int.from_bytes(raw[0x64:0x66], 'little') == 2301

def test_explicit_dale_year_and_default_team_by_year():
    for cid, team, year in [('20259', 'Seattle SuperSonics', 1989),
                            ('8045', 'Miami Heat', 1997),
                            ('8563', 'Boston Celtics', 1986)]:
        record = {'source_card_id': cid, 'card': {'identity': {'values': {
            'team': 'Original team', 'card_year': 1994, 'name': 'unchanged'}}},
            'recovered': {'conflicts': []}}
        apply_identity_correction(record)
        values = record['card']['identity']['values']
        assert (values['team'], values['card_year']) == (team, year)
        assert values['name'] == 'unchanged'
        assert record['recovered']['conflicts'][0]['original']['team'] == 'Original team'

def test_unrelated_card_untouched():
    record = {'source_card_id': '20509'}
    original = deepcopy(record)
    apply_identity_correction(record)
    assert record == original


@pytest.mark.parametrize('name', ['Jonathan Gibson', 'Domantas Sabonis'])
def test_user_face_one_override_preserves_other_identity(name):
    from nba2k17_scraper.models import CanonicalCard, CARD_SCHEMA
    from nba2k17_scraper.injection_exporter import build_injection_profile, resolve_player_values
    from nba2k17_viewer.runtime_fields import runtime_definition
    from scanner.player_codec import encode, decode
    card = {'schema': CARD_SCHEMA, 'canonical_card_uuid': 'test-face',
            'source': {'source_card_id': 'test-face', 'provider': '2kmtcentral'},
            'identity': {'values': {'name': name}},
            'gameplay_metadata': {'values': {'face_id': 7274, 'portrait_id': 777, 'speech_id': 888}}}
    apply_player_injection_corrections(card)
    once = deepcopy(card)
    apply_player_injection_corrections(card)
    assert card == once
    assert card['gameplay_metadata']['values'] == {'face_id': 1, 'portrait_id': 777, 'speech_id': 888}
    profile = build_injection_profile(CanonicalCard.from_dict(card))
    values = resolve_player_values(profile, {'gameplay_metadata': {'face_id': 7274}})['values']
    assert values['gameplay_metadata']['face_id'] == 1
    field = next(f for f in runtime_definition()['fields'] if f['field'] == 'gameplay_metadata.face_id')
    raw = bytes([0xa5]) * 0x460
    encoded = encode(raw, field, 1)
    assert decode(encoded, field) == 1
    assert encoded[:100] == raw[:100] and encoded[102:] == raw[102:]
