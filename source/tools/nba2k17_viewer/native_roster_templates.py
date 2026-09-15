"""Pointer-free source templates decoded from the shipped 2K17 roster asset."""
from copy import deepcopy
from hashlib import sha256
import json


def apply_native_templates(payload, path, data):
    if not path.is_file():
        return
    overlay = json.loads(path.read_text('utf-8'))
    if overlay.get('format') != 'nba2k17-native-source-install/v1':
        raise ValueError('Invalid native roster source overlay')
    capture_bytes = (data.parents[2] / overlay['capture']['path']).read_bytes()
    if sha256(capture_bytes).hexdigest() != overlay['capture']['sha256']:
        raise ValueError('Native roster source evidence changed')
    capture = json.loads(capture_bytes)
    if (capture['memory_written'] is not False or
            capture['executable_sha256'].lower() != payload['executable_sha256'].lower()):
        raise ValueError('Native roster source belongs to another executable')
    raw = bytes.fromhex(capture['raw_record_hex'])
    if len(raw) != 0x460 or sha256(raw).hexdigest() != capture['source_record_sha256']:
        raise ValueError('Invalid decoded native source image')
    for tid, template in overlay['templates'].items():
        if sha256(json.dumps(template, sort_keys=True).encode()).hexdigest() != tid:
            raise ValueError('Native source template hash changed')
        if template['source_record_sha256'] != capture['source_record_sha256']:
            raise ValueError('Native source template does not match decoded evidence')
        if set(template['defaults']) & {'attributes', 'badges', 'tendencies', 'hot_zones'}:
            raise ValueError('Native source would replace card-owned ratings')
        payload['templates'][tid] = deepcopy(template)
        payload.setdefault('template_capture_sources', {})[tid] = {
            'capture_sha256': overlay['capture']['sha256'],
            'executable_sha256': payload['executable_sha256'], 'ct_sha256': payload['ct_sha256'],
            'source_kind': 'NATIVE_SERIALIZED_ROSTER', 'capture_evidence': deepcopy(overlay['capture'])}
    for cid, assignment in overlay['cards'].items():
        if cid in payload['cards']:
            raise ValueError('Native source would replace an existing card assignment')
        if assignment['template_id'] not in overlay['templates']:
            raise ValueError('Missing native source template')
        payload['cards'][cid] = deepcopy(assignment)
    payload['unresolved'] = [r for r in payload['unresolved'] if str(r['source_card_id']) not in overlay['cards']]
    payload['summary'].update(matched_cards=len(payload['cards']), unresolved_cards=len(payload['unresolved']),
                              templates=len(payload['templates']))
