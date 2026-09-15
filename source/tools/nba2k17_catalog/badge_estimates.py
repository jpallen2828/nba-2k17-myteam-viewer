"""Read-only, evidence-bound application of user-requested badge estimates.

The overlay is not scraped evidence. It never changes source counts, raw pages,
the recovery queue, or an existing non-null badge. Changed source data makes an
entry stale, so subsequently recovered badges always win.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

FORMAT = 'nba2k17-same-player-badge-estimates/v1'
STATUS = 'CUSTOM_AUTHORED'
LEVELS = ('BRONZE', 'SILVER', 'GOLD', 'HALL_OF_FAME')


def default_overlay():
    root = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[3]
    return root / '.project/data/nba2k17/curation/badge_estimates.json'


def card_key(card):
    source = card.get('source') or {}
    return f"{source.get('primary_site')}:{source.get('source_card_id')}"


def evidence_fingerprint(card):
    # Jersey transcriptions and changing PNG availability are unrelated to this
    # decision. Identity, attributes, and *all* badge evidence are not.
    identity = {k: v for k, v in card.get('identity', {}).get('values', {}).items() if k != 'jersey_number'}
    body = {'key': card_key(card), 'identity': identity,
            'attributes': card.get('attributes', {}).get('values', {}),
            'badges': {k: card.get('badges', {}).get(k, {}) for k in ('values', 'statuses')}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def load_overlay(path=None):
    path = Path(path) if path is not None else default_overlay()
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload.get('format') != FORMAT:
        raise ValueError(f'Unsupported badge estimate overlay: {path}')
    return payload.get('cards', {})


def apply_estimate(card, entries):
    """Return a copy with estimated missing fields, plus the applied audit entry."""
    entry = entries.get(card_key(card))
    if not entry or entry.get('input_fingerprint') != evidence_fingerprint(card):
        return card, None
    values = card.get('badges', {}).get('values', {})
    estimates = entry.get('estimated_values', {})
    if not estimates or any(values.get(k) is not None for k in estimates):
        return card, None
    if any(v not in (*LEVELS, 'PERSONALITY') or k.startswith('source_') for k, v in estimates.items()):
        raise ValueError('Invalid estimated badge entry')
    result = deepcopy(card)
    section = result.setdefault('badges', {})
    for key, value in estimates.items():
        section.setdefault('values', {})[key] = value
        section.setdefault('statuses', {})[key] = STATUS
        section.setdefault('raw_labels', {})[key] = key.replace('_', ' ').title()
    section['missing_fields'] = [k for k in section.get('missing_fields', []) if k not in estimates]
    section.setdefault('provenance', []).append({
        'source_site': 'manual', 'source_card_id': result['source']['source_card_id'],
        'source_label': 'ESTIMATED SAME-PLAYER BADGES — not recovered source data',
        'parser_version': entry['method'], 'snapshot_sha256': entry['input_fingerprint'],
        'note': f"User-directed estimate; reference {entry['parent']['key']}. Original source counts and missing-detail markers retained.",
    })
    result.setdefault('raw_source', {})['badge_estimation'] = entry
    return result, entry
