"""Exact-source speech IDs; no live addresses, face guesses or native calls."""
from copy import deepcopy
from functools import lru_cache
from hashlib import sha256
import json
from pathlib import Path

FIELD = 'gameplay_metadata.speech_id'


@lru_cache(maxsize=1)
def catalog():
    raw = (Path(__file__).parent / 'assets/source-speech-identities.json').read_bytes()
    data = json.loads(raw)
    if (data.get('format') != 'nba2k17-source-speech-identities/v1'
            or (data['field']['offset'], data['field']['size']) != (0x200, 2)):
        raise ValueError('Invalid source speech identity catalog')
    return data, sha256(raw).hexdigest()


def register_fields(registry):
    data, digest = catalog()
    if registry['executable_sha256'].lower() != data['executable_sha256'].lower():
        raise ValueError('Speech identity catalog executable mismatch')
    field = {'field': FIELD, 'region': 'player', 'offset': 0x200, 'size': 2,
             'codec': 'uint', 'raw_max': 65535,
             'confidence': data['mapping_status'], 'injection_verified': False,
             'evidence_sha256': digest,
             'evidence': [{'sha256': digest, 'source': 'source-speech-identities.json'}]}
    registry['fields'] = [f for f in registry['fields'] if f['field'] != FIELD] + [field]
    registry['speech_identity'] = deepcopy(data)


def source_value(template, definition=None):
    data = definition if definition is not None else catalog()[0]
    if template.get('executable_sha256', '').lower() != data['executable_sha256'].lower():
        raise ValueError('Source speech identity belongs to another game build')
    source_hash = template.get('source_record_sha256')
    value = data['source_values'].get(source_hash)
    if type(value) is not int or not 0 <= value <= 65535:
        raise ValueError('Missing captured source speech ID; destination speech will not be inherited.')
    override = data.get('user_overrides', {}).get(source_hash)
    if override is not None:
        if (override.get('captured_value') != value or type(override.get('value')) is not int
                or not 0 <= override['value'] <= 65535 or not override.get('authorization')):
            raise ValueError('Invalid or mismatched user speech identity override')
        value = override['value']
    # The codec writes all 16 bits; 0xFFFF remains native -1, not zero.
    return value


def promote_template_speech(template):
    data, digest = catalog()
    if template.get('source_record_sha256') not in data['source_values']:
        return False  # Browsing remains possible; injection reports the missing source.
    value = source_value(template, data)
    source_hash = template['source_record_sha256']
    template.setdefault('captured_identity', {})['speech_id_u16'] = data['source_values'][source_hash]
    template.setdefault('defaults', {}).setdefault('gameplay_metadata', {})['speech_id'] = value
    template.setdefault('field_evidence', {})[FIELD] = {
        'confidence': data['mapping_status'], 'source_record_sha256': template['source_record_sha256'],
        'evidence': [{'sha256': digest, 'source': 'source-speech-identities.json'}],
        'auditory_verified': False}
    if override := data.get('user_overrides', {}).get(source_hash):
        template['field_evidence'][FIELD]['user_override'] = deepcopy(override)
    return True
