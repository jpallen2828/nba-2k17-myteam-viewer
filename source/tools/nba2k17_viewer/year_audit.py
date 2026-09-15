"""Evidence-backed year review, applied after original artwork identity checks."""
from copy import deepcopy
import json


def load_year_audit(path):
    if not path.is_file():
        return {}
    entries = json.loads(path.read_text(encoding='utf-8'))
    for cid, entry in entries.items():
        if not cid.isdigit() or not entry.get('reason') or not entry.get('status'):
            raise ValueError('Invalid year audit entry')
        if set(entry['expected']) != {'team', 'card_year'}:
            raise ValueError('Year audit requires exact prior identity')
        if set(entry['updates']) - {'team', 'card_year', 'represented_season'}:
            raise ValueError('Year audit cannot modify gameplay or other identity fields')
    return entries


def apply_year_audit(record, entries):
    entry = entries.get(str(record['source_card_id']))
    if entry is None:
        return
    section = record['card']['identity']
    values = section['values']
    if any(values.get(k) != v for k, v in entry['expected'].items()):
        raise ValueError(f"Year audit prior identity mismatch: {record['source_card_id']}")
    updates = entry['updates']
    original = {k: values.get(k) for k in updates}
    values.update(updates)
    statuses = section.setdefault('statuses', {})
    for key in updates:
        statuses[key] = entry['status']
    if entry['status'].startswith('UNRESOLVED'):
        statuses['card_year'] = entry['status']
        statuses['represented_season'] = entry['status']
        reviews = record.setdefault('review_domains', [])
        if 'identity_metadata' not in reviews:
            reviews.append('identity_metadata')
    missing = section.setdefault('missing_fields', [])
    for key, value in updates.items():
        if value is None and key not in missing:
            missing.append(key)
        elif value is not None and key in missing:
            missing.remove(key)
    record['recovered']['conflicts'].append({
        'kind': 'user_requested_year_audit', 'original': original,
        'corrected': deepcopy(updates), 'status': entry['status'],
        'resolution': entry['reason'],
        'candidate_season_end_years': entry['candidate_season_end_years'],
        'source_urls': entry['source_urls'],
        'dataset_sha256': entry['dataset_sha256'],
    })
