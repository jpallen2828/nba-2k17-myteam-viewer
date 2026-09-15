"""Apply explicit, missing-only tendency estimates without ML runtime dependencies."""
from copy import deepcopy
import hashlib
import json

FORMAT = 'nba2k17-attribute-badge-tendency-estimates/v1'
STATUS = 'ESTIMATED_ATTRIBUTES_BADGES'


def fingerprint(card):
    evidence = {k: card.get(k, {}) for k in ('identity', 'attributes', 'badges')}
    return hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_tendency_estimates(path):
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('format') != FORMAT or len(set(data['fields'])) != 84:
        raise ValueError('Invalid tendency estimate format/fields')
    entries = {}
    for entry in data['entries']:
        cid = str(entry['source_card_id'])
        if cid in entries or entry.get('status') != STATUS:
            raise ValueError('Invalid tendency estimate identity/status')
        if set(entry['values']) != set(data['fields']) or any(
                type(v) is not int or not 0 <= v <= 100 for v in entry['values'].values()):
            raise ValueError('Invalid tendency estimate values')
        entries[cid] = entry
    return entries


def apply_tendency_estimate(record, entry):
    if not entry:
        return 0
    card = record['card']
    if card.get('source', {}).get('provider') == 'user_authored':
        return 0
    section = card.get('tendencies', {})
    if any(v is not None for k, v in section.get('values', {}).items() if not k.startswith('source_')):
        return 0
    observations = {}
    for ob in record.get('recovered', {}).get('source_card', {}).get('tendencies', []):
        if ob.get('value') is None:
            continue
        key, value = ob['key'], ob['value']
        if key not in entry['values'] or type(value) not in (int, float) or not 0 <= value <= 100:
            raise ValueError('Unsupported existing tendency observation')
        if key in observations and observations[key]['value'] != value:
            raise ValueError('Conflicting existing tendency observations')
        observations[key] = ob
    if str(record['source_card_id']) != str(entry['source_card_id']) or fingerprint(card) != entry['input_sha256']:
        raise ValueError('Stale tendency estimate: identity, attributes, or badges changed')
    section = card.setdefault('tendencies', {})
    section.setdefault('values', {}).update(entry['values'])
    section.setdefault('statuses', {}).update({k: STATUS for k in entry['values']})
    for key, ob in observations.items():
        section['values'][key] = ob['value']
        section['statuses'][key] = 'REPORTED_ARTICLE_OBSERVATION'
        section.setdefault('provenance', []).append(deepcopy(ob))
    section.setdefault('raw_labels', {}).update({k: k.replace('_', ' ').title() for k in entry['values']})
    section['missing_fields'] = [k for k in section.get('missing_fields', [])
                                 if k not in entry['values'] and not k.startswith('source_')]
    section.setdefault('provenance', []).append({'status': STATUS, 'method': entry['method'],
        'training_sha256': entry['training_sha256'], 'input_sha256': entry['input_sha256']})
    record['tendency_estimation'] = deepcopy({k: v for k, v in entry.items() if k != 'values'})
    record['tendency_estimation']['estimated_field_count'] = len(entry['values']) - len(observations)
    record['tendency_estimation']['preserved_observation_fields'] = sorted(observations)
    record['tendency_estimation']['original_domain_audit'] = deepcopy(record.get('domain_audit', {}).get('tendencies'))
    record.setdefault('domain_audit', {})['tendencies'] = {'status': STATUS, 'field_count': len(entry['values']),
        'missing_vs_observed_template': [], 'conflicting_observations': {}, 'out_of_range_or_nonnumeric': {}}
    record['missing_domains'] = [d for d in record.get('missing_domains', []) if d != 'tendencies']
    return len(entry['values']) - len(observations)
