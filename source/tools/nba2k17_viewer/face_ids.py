"""Persistent evidence-backed face assignments; these are not memory-write tests."""
from copy import deepcopy
import hashlib
import json
import re
import unicodedata

FORMAT = 'nba2k17-face-ids/v1'
STATUS = 'USER_SUPPLIED_FACE_ID'
PORTRAIT_STATUS = 'USER_CONFIRMED_FACE_PORTRAIT_RULE'
PORTRAIT_POLICY = 'User instruction: every player portrait_id equals the selected face_id, including season variants.'
IDENTITY_KEYS = ('name', 'card_year', 'represented_season', 'team')


def normalized_name(name):
    # Some source pages accidentally include the page title in the player name.
    name = re.sub(r'\s*\(\d+\)\s*-\s*MyTEAM.*$', '', name, flags=re.I)
    return re.sub('[^a-z0-9]', '', unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode().lower())


def identity_binding(card):
    return {key: card['identity']['values'].get(key) for key in IDENTITY_KEYS}


def load_face_ids(path):
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    corrections = path.with_name('face_id_corrections.json')
    if corrections.is_file():
        supplement = json.loads(corrections.read_text(encoding='utf-8'))
        payload['cards'].update(supplement['cards'])
        payload['evidence_files'].extend(supplement['evidence_files'])
    if payload.get('format') != FORMAT:
        raise ValueError('Invalid face ID overlay format')
    for source in payload['evidence_files']:
        evidence = path.parent / source['path']
        if hashlib.sha256(evidence.read_bytes()).hexdigest() != source['sha256']:
            raise ValueError('Face ID evidence hash mismatch: ' + str(evidence))
    for cid, entry in payload['cards'].items():
        if entry['face_id'] is None and entry.get('confidence') == 'UNRESOLVED':
            pass
        elif type(entry['face_id']) is not int or not 0 <= entry['face_id'] <= 65535:
            raise ValueError('Invalid face ID for ' + cid)
        if not entry.get('source_entries') or not entry.get('reason'):
            raise ValueError('Face ID assignment lacks source evidence: ' + cid)
    return payload['cards']


def apply_face_id(record, entry):
    if entry is None:
        apply_portrait_id(record)
        return
    card = record['card']
    if identity_binding(card) != entry['identity']:
        raise ValueError('Face ID card identity changed: ' + str(record['source_card_id']))
    section = card.setdefault('gameplay_metadata', {})
    section.setdefault('values', {})['face_id'] = entry['face_id']
    mod_source = entry.get('source_kind') == 'MODDED_LIVE_ROSTER'
    source_site = 'modded_live_roster_face_id' if mod_source else 'user_supplied_face_id_list'
    section.setdefault('statuses', {})['face_id'] = 'LIVE_ROSTER_DERIVED' if mod_source else STATUS
    if entry['face_id'] is None:
        for field in ('face_id', 'portrait_id'):
            section['values'][field] = None
            section['statuses'][field] = 'UNRESOLVED'
            if field not in section.setdefault('missing_fields', []):
                section['missing_fields'].append(field)
        section['provenance'] = [p for p in section.get('provenance', [])
                                 if p.get('source_site') != 'user_face_portrait_policy']
    section.setdefault('raw_labels', {})['face_id'] = (
        'Captured mod roster face ID' if mod_source else 'User-supplied NBA 2K17 face ID')
    if entry['face_id'] is not None:
        section['missing_fields'] = [key for key in section.get('missing_fields', []) if key != 'face_id']
    provenance = {'source_site': source_site, 'field': 'face_id',
                  'source_card_id': str(record['source_card_id']), **deepcopy(entry)}
    # Reapplying the same curation is idempotent, including its provenance.
    section['provenance'] = [p for p in section.get('provenance', [])
                             if p.get('source_site') not in
                             {'user_supplied_face_id_list', 'modded_live_roster_face_id'}] + [provenance]
    card.setdefault('raw_source', {})['face_id_curation'] = deepcopy(entry)
    apply_portrait_id(record)


def apply_portrait_id(record):
    """Apply the user's global ID relationship; never invent an unknown face."""
    section = record['card'].get('gameplay_metadata', {})
    face_id = section.get('values', {}).get('face_id')
    if face_id is None:
        return
    if type(face_id) is not int or not 0 <= face_id <= 65535:
        raise ValueError('Invalid face ID for portrait policy')
    section['values']['portrait_id'] = face_id
    section.setdefault('statuses', {})['portrait_id'] = PORTRAIT_STATUS
    section.setdefault('raw_labels', {})['portrait_id'] = 'Portrait ID (same as face ID)'
    section['missing_fields'] = [key for key in section.get('missing_fields', []) if key != 'portrait_id']
    section['provenance'] = [p for p in section.get('provenance', [])
                             if p.get('source_site') != 'user_face_portrait_policy'] + [{
        'source_site': 'user_face_portrait_policy', 'field': 'portrait_id',
        'source_field': 'gameplay_metadata.face_id', 'value': face_id,
        'rule': PORTRAIT_POLICY, 'date': '2026-09-09'}]
