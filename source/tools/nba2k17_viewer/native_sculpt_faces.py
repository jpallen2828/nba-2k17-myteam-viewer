"""Captured native face selectors, independent of destination roster contents."""
from functools import lru_cache
import json
from pathlib import Path
import re

from .face_ids import normalized_name


@lru_cache(maxsize=1)
def native_sculpt_face_catalog():
    payload = json.loads((Path(__file__).parent / 'assets/native-sculpt-faces.json').read_text(encoding='utf-8'))
    if (payload.get('format') != 'nba2k17-native-sculpt-faces/v1'
            or payload.get('field') != {'field': 'gameplay_metadata.face_id', 'offset': 100, 'size': 2, 'codec': 'uint'}
            or not re.fullmatch('[0-9a-f]{64}', payload.get('capture_sha256', ''))):
        raise ValueError('Invalid captured native face catalog')
    for key, entry in payload['players'].items():
        if (key != normalized_name(entry['name']) or entry['face_id'] != 1
                or not entry['known_card_ids'] or not entry['compatible_source_record_sha256']
                or any(not re.fullmatch('[0-9a-f]{64}', value)
                       for value in [entry['source_record_sha256'], *entry['compatible_source_record_sha256']])
                or any(type(value) is not int or not 0 <= value <= 65535
                       for value in entry['replaced_face_ids'])):
            raise ValueError('Invalid captured native face assignment: ' + key)
    return payload


def native_sculpt_face_entry(card):
    """Match a reviewed card or a saved same-player source, never a live name."""
    entry = native_sculpt_face_catalog()['players'].get(normalized_name(
        card.get('identity', {}).get('values', {}).get('name') or ''))
    if entry is None:
        return None
    cid = str(card.get('source', {}).get('source_card_id', ''))
    template = card.get('raw_source', {}).get('live_roster_template') or {}
    source_match = (normalized_name(template.get('name') or '') == normalized_name(entry['name'])
                    and template.get('source_record_sha256') in entry['compatible_source_record_sha256'])
    if cid not in entry['known_card_ids'] and not source_match:
        return None
    # A deliberately different face can be a supplied mod asset. Only replace
    # the recorded stale selectors; do not turn a later cyberface into a sculpt.
    face = card.get('gameplay_metadata', {}).get('values', {}).get('face_id')
    if face is not None and face not in entry['replaced_face_ids']:
        return None
    return entry


def apply_native_sculpt_face(card):
    entry = native_sculpt_face_entry(card)
    if entry is None:
        return False
    catalog = native_sculpt_face_catalog()
    section = card.setdefault('gameplay_metadata', {})
    section.setdefault('values', {})['face_id'] = 1
    section.setdefault('statuses', {})['face_id'] = 'LIVE_ROSTER_DERIVED'
    section['missing_fields'] = [key for key in section.get('missing_fields', []) if key != 'face_id']
    section['provenance'] = [item for item in section.get('provenance', [])
                             if item.get('source_site') != 'native_sculpt_face_scan']
    section['provenance'].append({
        'source_site': 'native_sculpt_face_scan', 'field': 'face_id', 'value': 1,
        'date': catalog['captured_date'], 'capture_sha256': catalog['capture_sha256'],
        'source_record_sha256': entry['source_record_sha256'],
        'reason': 'Stable native roster source uses the sculpted-player face selector 1.',
    })
    return True
