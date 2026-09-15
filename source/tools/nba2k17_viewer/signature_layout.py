"""Explicitly approved inferred signature layout, separate from UI verification."""
from copy import deepcopy
from hashlib import sha256
import json

FORMAT='nba2k17-signature-layout/v1'
INFERRED='INFERRED_CT_LAYOUT_CONTROLLED_ANCHORS_ROSTER_CORROBORATED'


def field_bits(field):
    start=field['offset']*8+field.get('shift',0)
    return set(range(start,start+field.get('bits',field['size']*8)))


def load_signature_layout(data):
    path=data/'curation/signature_layout.json'
    if not path.is_file():return None
    layout=json.loads(path.read_text(encoding='utf-8'))
    if layout['format']!=FORMAT or layout['authorization']['policy']!='USER_APPROVED_INFERRED_SIGNATURE_LAYOUT':
        raise ValueError('Unknown signature layout policy')
    root=data.parents[2]
    for evidence in layout['evidence']:
        if sha256((root/evidence['path']).read_bytes()).hexdigest()!=evidence['sha256']:
            raise ValueError('Signature layout evidence changed: '+evidence['path'])
    seen=set();names=set();uids=set()
    for field in layout['fields']:
        bits=field_bits(field)
        if (seen&bits or field['field'] in names or field['reference_uid'] in uids
                or not field['field'].startswith('signatures.') or not bits
                or max(bits)>=0x460*8 or min(bits)<0):
            raise ValueError('Invalid/overlapping signature layout')
        seen|=bits;names.add(field['field']);uids.add(field['reference_uid'])
    if len(names)!=44:raise ValueError('Incomplete signature layout')
    layout['overlay_evidence']={'path':path.relative_to(root).as_posix(),'sha256':sha256(path.read_bytes()).hexdigest()}
    return layout


def merge_signature_registry(fields,layout,executable_sha256):
    if layout['executable_sha256'].lower()!=executable_sha256.lower():
        raise ValueError('Signature layout belongs to a different executable')
    original={f['field']:f for f in fields}
    occupied={bit:f['field'] for f in fields for bit in field_bits(f)}
    for candidate in layout['fields']:
        name=candidate['field'];bits=field_bits(candidate)
        conflicts={occupied[b] for b in bits if b in occupied and occupied[b]!=name}
        if conflicts:raise ValueError('Signature overlaps existing fields: '+name+' / '+str(sorted(conflicts)))
        if name in original:
            field=original[name]
            if field_bits(field)!=bits:raise ValueError('Signature disagrees with controlled mapping: '+name)
            field.update(reference_uid=candidate['reference_uid'],display_label=candidate['display_label'],
                         inferred_mapping=False)
        else:
            field=deepcopy(candidate)
            field.update(region='player',codec='enum',confidence=INFERRED,inferred_mapping=True,
                         write_enabled=False,injection_verified=False,
                         evidence=[layout['overlay_evidence'],*deepcopy(layout['evidence'])])
            fields.append(field)
        occupied.update({b:name for b in bits})
    return fields
