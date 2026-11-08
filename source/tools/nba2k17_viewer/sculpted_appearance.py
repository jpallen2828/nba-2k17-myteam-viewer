"""Portable native sculpt sources; destination addresses are resolved per session."""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

from .native_sculpt_faces import native_sculpt_face_entry


@lru_cache(maxsize=1)
def appearance_catalog():
    payload = json.loads((Path(__file__).parent / 'assets/native-sculpt-appearance.json').read_text(encoding='utf-8'))
    if (payload.get('format') != 'nba2k17-native-sculpt-appearance/v1'
            or payload.get('layout') != {'sculpt_pointer_offset': 128, 'sculpt_size': 456,
                                         'body_pointer_offset': 136, 'body_size': 132}):
        raise ValueError('Unsupported native sculpt layout')
    for key, source in payload['sources'].items():
        if key != source['source_record_sha256']:
            raise ValueError('Sculpt source identity mismatch')
        if type(source.get('skin_type')) is not int or not 0 <= source['skin_type'] <= 7:
            raise ValueError('Invalid native skin type')
        for block, size in (('sculpt', 456), ('body', 132)):
            raw = bytes.fromhex(source[block + '_hex'])
            if len(raw) != size or sha256(raw).hexdigest() != source[block + '_sha256']:
                raise ValueError('Invalid saved sculpt payload')
    return payload


def register_fields(registry):
    field = {'field': 'gameplay_metadata.skin_type', 'region': 'player', 'offset': 0x138,
             'size': 1, 'codec': 'uint', 'shift': 1, 'bits': 3, 'raw_max': 7,
             'confidence': 'NATIVE_TYPED_REFLECTION_AND_STABLE_ROSTER_CAPTURE',
             'evidence_sha256': appearance_catalog()['executable_sha256'].lower(),
             'evidence': [{'source': 'native PLAYERDATA SkinType descriptor RVA 0x1F46F90',
                           'sha256': appearance_catalog()['executable_sha256'].lower()}],
             'injection_verified': False}
    if not any(f['field'] == field['field'] for f in registry['fields']):
        registry['fields'].append(field)


def apply_sculpted_appearance(card):
    entry = native_sculpt_face_entry(card)
    template = card.get('raw_source', {}).get('live_roster_template')
    if not entry or not template:
        return False
    catalog = appearance_catalog()
    source = catalog['sources'][entry['source_record_sha256']]
    template['native_sculpt_appearance'] = deepcopy(source)
    return True


def profile_appearance(profile, resolved, registry):
    """Validate saved payload exactly; no fallback to a live source player."""
    template = profile.get('reusable_roster_template') or {}
    source = template.get('native_sculpt_appearance')
    if resolved['values'].get('gameplay_metadata', {}).get('face_id') != 1:
        return None
    identity = profile.get('source_card_identity', {})
    expected_entry = native_sculpt_face_entry({
        'source': {'source_card_id': identity.get('source_card_id')},
        'identity': {'values': identity},
        'gameplay_metadata': {'values': {'face_id': 1}},
        'raw_source': {'live_roster_template': template}})
    if not source:
        if expected_entry:
            raise ValueError('Missing saved native sculpt data; reload this card from the updated database')
        return None
    catalog = appearance_catalog()
    if catalog['executable_sha256'].lower() != registry['executable_sha256'].lower():
        raise ValueError('Native sculpt layout belongs to another game build')
    expected = catalog['sources'].get(source.get('source_record_sha256'))
    if source != expected:
        raise ValueError('Saved native sculpt data differs from its verified capture')
    from .face_ids import normalized_name
    if normalized_name(profile['source_card_identity'].get('name', '')) != normalized_name(source['name']):
        raise ValueError('Native sculpt source belongs to another player')
    return deepcopy(source)
