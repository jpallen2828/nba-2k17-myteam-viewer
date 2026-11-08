import pytest
from nba2k17_scraper.injection_exporter import normalize_player_id, resolve_player_values
from scanner.player_codec import encode, decode

FIELD = {'offset': 0x64, 'size': 2, 'codec': 'uint', 'raw_max': 65535}

@pytest.mark.parametrize('value', ['389', ' 389 ', '00389', 389])
def test_editor_decimal_face_id_encodes_without_touching_neighbors(value):
    before = bytes([0xAB]) * 0x460
    after = encode(before, FIELD, normalize_player_id('gameplay_metadata', 'face_id', value))
    assert decode(after, FIELD) == 389
    assert before[:0x64] == after[:0x64] and before[0x66:] == after[0x66:]

@pytest.mark.parametrize('value', ['65536', '-1', '389.5', '389x', '３８９', '', None, True, 389.5])
def test_invalid_ids_still_fail_the_native_codec(value):
    with pytest.raises(ValueError):
        encode(bytes(0x460), FIELD, normalize_player_id('gameplay_metadata', 'face_id', value))

def test_saved_legacy_profile_resolves_numeric_ids_and_preserves_source():
    profile = {'card_specific_overrides': {'gameplay_metadata': {'face_id': '389'}},
               'override_evidence': {'gameplay_metadata': {'face_id': {}}}}
    result = resolve_player_values(profile, {'gameplay_metadata': {'face_id': 1335}})
    assert result['values']['gameplay_metadata']['face_id'] == 389
    assert profile['card_specific_overrides']['gameplay_metadata']['face_id'] == '389'
    assert normalize_player_id('identity', 'jersey_number', '00') == '00'
    assert normalize_player_id('gameplay_metadata', 'dominant_hand', 'Right') == 'Right'
