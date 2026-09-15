"""Apply user-requested, missing-only card details with retained provenance."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

FORMAT = 'nba2k17-card-completions/v1'
LEVELS = ('BRONZE', 'SILVER', 'GOLD', 'HALL_OF_FAME')


def attribute_fingerprint(card):
    return hashlib.sha256(json.dumps(card['attributes']['values'], sort_keys=True).encode()).hexdigest()


def load_completions(path):
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('format') != FORMAT:
        raise ValueError('Invalid card completion format')
    return payload['cards']


def apply_completion(record, entry):
    if not entry:
        return
    card = record['card']
    if attribute_fingerprint(card) != entry['attribute_sha256']:
        raise ValueError('Card completion attributes changed: ' + str(record['source_card_id']))
    identity = card['identity']['values']
    if identity['name'] != entry['name'] or identity['overall'] != entry['overall']:
        raise ValueError('Card completion identity changed')
    for domain in ('tendencies', 'badges'):
        additions = entry.get(domain, {})
        section = card.setdefault(domain, {})
        for key, value in additions.items():
            if domain == 'tendencies' and (type(value) is not int or not 0 <= value <= 100):
                raise ValueError('Invalid tendency completion')
            if domain == 'badges' and (value not in LEVELS or key.startswith('source_')):
                raise ValueError('Invalid badge completion')
            if section.setdefault('values', {}).get(key) is None:
                section['values'][key] = value
                section.setdefault('statuses', {})[key] = 'USER_REQUESTED_ESTIMATE'
                section.setdefault('raw_labels', {})[key] = key.replace('_', ' ').title()
        section['missing_fields'] = [k for k in section.get('missing_fields', []) if section['values'].get(k) is None]
        if additions:
            record['missing_domains'] = [d for d in record.get('missing_domains', []) if d != domain]
    if entry.get('tendencies'):
        record.setdefault('domain_audit', {})['tendencies'] = {
            'status': 'USER_COMPLETED', 'field_count': sum(v is not None and not k.startswith('source_') for k,v in card['tendencies']['values'].items()),
            'missing_vs_observed_template': [], 'conflicting_observations': {}, 'out_of_range_or_nonnumeric': {}}
    if entry.get('badges'):
        audit = record.setdefault('badge_audit', {})
        named = Counter(v for v in card['badges']['values'].values() if v in LEVELS)
        declared = audit.get('declared_main_counts', {})
        if any(named[level] != declared.get(level, 0) for level in LEVELS):
            raise ValueError('Completed badge counts do not match card totals')
        audit['status'] = 'NAMED_TIERS_MATCH_SOURCE_COUNTS'
        audit['named_main_counts'] = dict(named)
    if jersey := entry.get('jersey'):
        if not any(a.get('sha256') == jersey['art_sha256'] or a.get('content_hash') == jersey['art_sha256'] for a in record.get('assets', [])):
            raise ValueError('Jersey transcription artwork changed')
        if identity.get('jersey_number') is None:
            identity['jersey_number'] = jersey['number']
            card['identity'].setdefault('statuses', {})['jersey_number'] = 'VISUALLY_VERIFIED_CARD_ART'
    if hand := entry.get('shooting_hand'):
        if hand['value'] not in ('Left', 'Right') or not hand.get('source_url'):
            raise ValueError('Shooting hand requires a confirmed profile')
        section = card.setdefault('gameplay_metadata', {})
        if section.setdefault('values', {}).get('dominant_hand') is None:
            section['values']['dominant_hand'] = hand['value']
            section.setdefault('statuses', {})['dominant_hand'] = 'CONFIRMED_PLAYER_PROFILE'
            section.setdefault('raw_labels', {})['dominant_hand'] = 'Shooting Hand'
    record['card_completion'] = deepcopy(entry)
