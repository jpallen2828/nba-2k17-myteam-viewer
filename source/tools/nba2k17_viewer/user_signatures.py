"""Card-bound signature edits supplied by the user, with label/ID distinction."""
from copy import deepcopy
from hashlib import sha256
import json


def load_user_signatures(path, root):
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding='utf-8'))
    for evidence in payload['evidence']:
        if sha256((root / evidence['path']).read_bytes()).hexdigest() != evidence['sha256']:
            raise ValueError('User signature evidence changed: ' + evidence['path'])
    return payload['cards']


def apply_user_signatures(record, entry):
    if entry is None:
        return
    card = record['card']
    identity = card['identity']['values']
    if any(identity.get(k) != v for k, v in entry['identity'].items()):
        raise ValueError('User signature card identity changed')
    section = card.setdefault('signatures', {'values': {}, 'statuses': {}, 'raw_labels': {}})
    section.setdefault('raw_labels', {})
    live = entry.get('source_kind') == 'USER_CONFIRMED_LIVE_SCAN'
    reconstructed = entry.get('source_kind') == 'USER_AUTHORIZED_RECONSTRUCTION'
    for key, field in entry['fields'].items():
        # CSV edits fill gaps; explicitly confirmed live scans replace every value.
        if section['values'].get(key) is not None and not live:
            continue
        if live and (not field['id_resolved'] or type(field['value']) is not int):
            raise ValueError('Live signature override requires captured integer IDs')
        section['values'][key] = field['value']
        section['statuses'][key] = ('MANUAL_VERIFIED' if live else 'USER_SUPPLIED_SIGNATURE_ID' if field['id_resolved']
                                    else 'USER_SUPPLIED_ANIMATION_LABEL')
        if reconstructed:
            section['statuses'][key] = ('ESTIMATED' if field.get('estimated')
                                        else 'TRANSLATED_REFERENCE')
        section['raw_labels'][key] = field.get('label', field.get('csv_column', key))
        section['missing_fields'] = [k for k in section.get('missing_fields', []) if k != key]
    source_site = ('user_signature_reconstruction' if reconstructed else
                   'user_signature_live' if live else 'user_signature_csv')
    section['provenance'] = [p for p in section.get('provenance', [])
                             if p.get('source_site') != source_site] + [{
        'source_site': source_site, **deepcopy(entry)}]
    card.setdefault('raw_source', {})[source_site] = deepcopy(entry)
