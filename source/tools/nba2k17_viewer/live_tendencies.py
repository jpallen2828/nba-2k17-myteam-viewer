"""Hash-bound, missing-only roster-derived tendency overlays."""
import hashlib
import json
from pathlib import Path
from copy import deepcopy


def load_live_tendencies(path):
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != 'nba2k17-live-tendencies/v1':
        raise ValueError('Unknown live tendencies format')
    capture_path = Path(data['capture_path'])
    capture = capture_path.read_bytes()
    if hashlib.sha256(capture).hexdigest() != data['capture_sha256']:
        raise ValueError('Live tendency capture hash mismatch')
    rows = {r['record_sha256']: r for r in json.loads(capture)['rows']}
    aliases = {'shot': 'shoot', 'shot_under_basket': 'under_basket_shot',
               'putback': 'putback_dunk', 'drive_right': 'drive_right_vs_left',
               'driving_step_back': 'driving_stepback', 'post_step_back_shot': 'post_stepback_shot'}
    entries = {}
    for entry in data['entries']:
        cid = str(entry['source_card_id'])
        values = entry['values']
        if cid in entries or entry.get('status') != 'LIVE_ROSTER_DERIVED':
            raise ValueError('Invalid live tendency identity/status')
        if len(values) != 84 or any(type(v) is not int or not 0 <= v <= 100 for v in values.values()):
            raise ValueError('Invalid live tendency values')
        row = rows.get(entry['source']['record_sha256'])
        if row is None or values != {aliases.get(k, k): v for k, v in row['candidate_tendencies'].items()}:
            raise ValueError('Live tendency values differ from captured record')
        entries[cid] = {**entry, 'capture_sha256': data['capture_sha256'],
                        'mapping_note': data['mapping_note']}
    return entries


def apply_live_tendencies(record, entry):
    if not entry:
        return 0
    card = record['card']
    if card.get('source', {}).get('provider') == 'user_authored':
        return 0
    if str(record['source_card_id']) != str(entry['source_card_id']) or any(
            card['identity']['values'].get(k) != v for k, v in entry['identity'].items()):
        raise ValueError('Live tendency card identity changed')
    section = card.setdefault('tendencies', {})
    values = section.setdefault('values', {})
    # This import fills wholly missing domains only. Recovered web observations
    # and any already authored numeric value take precedence over the overlay.
    observations = record.get('recovered', {}).get('source_card', {}).get('tendencies', [])
    if any(v is not None for k, v in values.items() if not k.startswith('source_')) or any(
            ob.get('value') is not None for ob in observations):
        return 0
    record.setdefault('recovered', {})['live_roster_tendencies'] = {
        **deepcopy(entry), 'original_domain_audit': deepcopy(record.get('domain_audit', {}).get('tendencies'))}
    values.update(entry['values'])
    section.setdefault('statuses', {}).update({k: 'LIVE_ROSTER_DERIVED' for k in entry['values']})
    section.setdefault('raw_labels', {}).update({k: k.replace('_', ' ').title() for k in entry['values']})
    section['missing_fields'] = [k for k in section.get('missing_fields', [])
                                 if k not in entry['values'] and not k.startswith('source_')]
    section.setdefault('provenance', []).append({
        'source_site': 'nba2k17_live_roster', 'source_card_id': record['source_card_id'],
        'source_record_sha256': entry['source']['record_sha256'],
        'capture_sha256': entry['capture_sha256'], 'source_team': entry['source']['team'],
        'source_season': entry['source']['season'], 'match_method': entry['match_method'],
        'status': 'LIVE_ROSTER_DERIVED', 'note': entry['mapping_note']})
    record.setdefault('domain_audit', {})['tendencies'] = {
        'status': 'LIVE_ROSTER_DERIVED', 'field_count': len(entry['values']),
        'missing_vs_observed_template': [], 'conflicting_observations': {}, 'out_of_range_or_nonnumeric': {}}
    record['missing_domains'] = [d for d in record.get('missing_domains', []) if d != 'tendencies']
    return len(entry['values'])
