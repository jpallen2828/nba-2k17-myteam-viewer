"""Batch diagnostics and population checks; statistics never promote verification."""
from collections import Counter, defaultdict
import json
from pathlib import Path
import struct
import uuid
import re

from .core import Journal, differences, digest, utc


def decode_record(raw, inventory):
    out = {}
    for row in inventory['fields']:
        dst = row.get('destination') or {}
        if 'offset' not in dst:
            continue
        off, codec = dst['offset'], dst.get('codec')
        if codec == 'utf16le':
            from scanner.roster_record import utf16_fixed_field
            value = utf16_fixed_field(raw, off, dst['length'])
        elif codec == 'packed':
            word = int.from_bytes(raw[off:off+dst.get('size', 1)], 'little')
            value = (word & dst['mask']) >> dst['shift']
        elif codec == 'rating':
            value = raw[off] / 3 + 25 if raw[off] <= 222 and raw[off] % 3 == 0 else None
        else:
            value = raw[off]
        out[row['field']] = {'value': value, 'status': row['status'], 'confidence': row['confidence']}
    return out


def population_report(snapshot, inventory):
    records = [bytes.fromhex(r['raw_hex']) for r in snapshot['records']]
    checks = []
    for row in inventory['fields']:
        dst = row.get('destination') or {}
        if 'offset' not in dst or dst.get('codec') == 'utf16le':
            continue
        values = [decode_record(raw, {'fields': [row]})[row['field']]['value'] for raw in records]
        counts = Counter(str(v) for v in values)
        if row['family'] == 'badges':
            valid = [v in (0, 1) if row.get('badge_kind') == 'personality' else v in range(5) for v in values]
        elif row['family'] == 'tendencies':
            valid = [v is not None and 0 <= v <= 100 for v in values]
        else:
            valid = [v is not None for v in values]
        checks.append({'field': row['field'], 'distribution': dict(counts), 'examples': len(values),
                       'invalid_domain_examples': len(values)-sum(valid), 'unique_values': len(counts),
                       'interpretation': 'DOMAIN_CONSISTENCY_ONLY_NOT_LOCATION_OR_LABEL_PROOF'})
    sharing = defaultdict(list)
    for item in snapshot.get('linked_pointer_candidates', []):
        sharing[item['diagnostic_target']].append({k: item[k] for k in ('player_address', 'pointer_offset')})
    # Offset-frequency and population distributions adapt the 2K16 comparison METHOD only.
    offsets = [{'offset': hex(i), 'distinct_values': len({r[i] for r in records})}
               for i in range(0x460)]
    return {'format': 'nba2k17-population-analysis/v1', 'created_at': utc(),
            'record_count': len(records), 'coverage': snapshot['coverage'], 'checks': checks,
            'variable_offset_survey': offsets,
            'shared_pointer_candidates': {p: owners for p, owners in sharing.items() if len(owners) > 1},
            'methods_reference': '2K16 compare_hidden_roster_snapshots.py: changed offsets, frequency, hashes; no 2K16 locations, sizes or encodings imported.',
            'verified_fields_added': [], 'next_smallest_experiment': 'Use controlled labels for unresolved fields; matching ranges/distributions alone cannot identify equal-valued fields.'}


def known_badge_constraints(root, snapshot):
    """Reuse video-observed Durant values; expose ambiguity instead of menu-order IDs."""
    folder = Path(root) / '.project/data/nba2k17/live_badges/session-2026-09-09-074423'
    mapping_path = folder / 'gameplay-candidate-map-after-tear-dropper-081923.json'
    mapping = json.loads(mapping_path.read_text())
    baseline_path = folder / mapping['baseline_file']
    if digest(baseline_path.read_bytes()) != mapping['baseline_sha256']:
        raise ValueError('Known UI evidence hash changed')
    baseline = json.loads(baseline_path.read_text())
    old = bytes.fromhex(baseline['records'][0]['raw_hex'])
    teams = [t for t in snapshot['teams'] if t['team_identity_token']=='golden_state_warriors_logo']
    players = [s for t in teams for s in t['slots'] if s.get('name')=='Kevin Durant' and s.get('readable_stable')]
    if len(players)!=1:
        return {'status':'KNOWN_UI_ANCHOR_NOT_UNIQUELY_RESOLVED','constraints':[]}
    current = bytes.fromhex(players[0]['raw_hex'])
    if current[0x448:0x459] != old[0x448:0x459]:
        return {'status':'BADGE_REGION_CHANGED_SINCE_VIDEO_DO_NOT_REUSE_LABELS','constraints':[]}
    anchors={'Acrobat':(0x44d,0),'Tear Dropper':(0x44d,3),'Hustle Rebounder':(0x454,0)}
    slots=[(off,shift) for off in range(0x448,0x459) for shift in (0,3)]
    constraints=[]
    for field in mapping['candidate_fields']:
        options=[anchors[field['name']]] if field['name'] in anchors else [s for s in slots if s not in anchors.values()]
        options=[(off,shift) for off,shift in options if ((current[off]>>shift)&7)==field['video_value']]
        constraints.append({'field':field['name'],'known_ui_level':field['video_level'],
                            'candidate_locations':[{'offset':hex(off),'shift':shift} for off,shift in options],
                            'remaining_candidates':len(options),'controlled_anchor':field['name'] in anchors})
    return {'status':'PRIOR_UI_VALUES_REUSED_ONLY_AFTER_BADGE_BYTES_MATCHED','player':'Kevin Durant',
            'source_video_sha256':mapping['video_sha256'],'baseline_sha256':digest(baseline_path.read_bytes()),
            'constraints':constraints,'verified_fields_added':[],
            'scope':'3-bit slots and 0/3 shifts are family hypotheses supported by existing anchors; equal-valued slots remain ambiguous. No new labels are proved by correlation.'}


def numeric_candidates(samples):
    """Fit numeric encodings to labelled edits; require three distinct inputs."""
    samples = [(raw, value) for raw, value in samples if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if len({v for _, v in samples}) < 3:
        return []
    xs = [v for _, v in samples]; xm = sum(xs)/len(xs)
    variance = sum((x-xm)**2 for x in xs)
    result = []
    for codec, size in [('B', 1), ('H', 2), ('f', 4)]:
        for off in range(0x460-size+1):
            ys = [struct.unpack_from('<'+codec, raw, off)[0] for raw, _ in samples]
            if any(not -1e9 < y < 1e9 for y in ys) or len(set(ys)) < 2:
                continue
            ym = sum(ys)/len(ys); scale = sum((x-xm)*(y-ym) for x, y in zip(xs, ys))/variance
            intercept = ym-scale*xm
            if abs(scale) < 1e-9:
                continue
            residual = max(abs(y-(scale*x+intercept)) for x,y in zip(xs,ys))
            if residual <= max(1e-5, max(abs(y) for y in ys)*1e-6):
                result.append({'offset': hex(off), 'codec': codec, 'scale': scale, 'intercept': intercept,
                               'max_residual': residual, 'status': 'FITTED_HYPOTHESIS_NOT_VERIFIED'})
    return result


def numeric_observation(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    levels = {'unequipped':0, 'equipped':1, 'bronze':1, 'silver':2, 'gold':3, 'hall of fame':4}
    if value in levels:
        return levels[value]  # Logical level order is a fitting hypothesis, not a stored ID assertion.
    match = re.fullmatch(r'(\d+)\s*(?:feet|foot|ft)\s*(\d+)\s*(?:inches|inch|in)', value)
    if match:
        return int(match[1])*12+int(match[2])
    match = re.fullmatch(r'(-?\d+(?:\.\d+)?)\s*(?:lb|lbs)?', value)
    return float(match[1]) if match else None


def packed_candidates(samples):
    samples = [(r, v) for r,v in samples if isinstance(v,(int,float)) and int(v)==v and 0<=v<=255]
    if len({v for _,v in samples}) < 2:
        return []
    candidates=[]
    for off in range(0x45f):
        words=[struct.unpack_from('<H',r,off)[0] for r,_ in samples]
        values=[int(v) for _,v in samples]
        minimum=max(values).bit_length() or 1
        for width in range(minimum,min(8,16)+1):
            for shift in range(17-width):
                mask=(1<<width)-1
                if all(((w>>shift)&mask)==v for w,v in zip(words,values)):
                    candidates.append({'offset':hex(off),'word_size':2,'shift':shift,'mask':hex(mask<<shift),
                                       'status':'MATCHES_OBSERVED_VALUES_WIDTH_AND_OTHER_FIELDS_STILL_UNVERIFIED'})
    return candidates


def batch_report(session, epoch):
    session = Path(session)
    journal = Journal(session / 'recorder.sqlite')
    try:
        events = journal.events(epoch)
        baselines = [e for e in events if e['kind'] == 'BASELINE']
        accepted = [e for e in events if e['kind'] == 'ACCEPTED_EDIT']
        original = journal.regions(baselines[0]) if baselines else {}
        final = journal.regions(accepted[-1]) if accepted else {}
        groups = defaultdict(list)
        labels = defaultdict(list)
        transitions = []
        for event in accepted:
            step = event['step']; regions = journal.regions(event)
            value = step.get('requested_value')
            if value is None:
                value = event.get('operator_note')
            groups[step['field']].append((regions['player'], numeric_observation(value)))
            labels[step['field']].append({'event': event['id'], 'display': step.get('requested_value'),
                                         'actual_label_or_note': event.get('operator_note'),
                                         'player_sha256': digest(regions['player']),
                                         'changed_offsets_and_values': event.get('changes_since_last_labeled_capture', {})})
            transitions.append({'event': event['id'], 'field': step['field'], 'requested': step.get('requested_value'),
                                'operator_note': event.get('operator_note'),
                                'changes': event.get('changes_since_last_labeled_capture'),
                                'acceptance': event['acceptance']})
        restoration = {k: {'exact_match': original[k] == final[k], 'differences': differences(original[k], final[k])}
                       for k in original.keys() & final.keys() if not k.startswith('editor:')}
        missing = sorted({k for k in original.keys() ^ final.keys() if not k.startswith('editor:')})
        restored = bool(restoration) and not missing and all(x['exact_match'] for x in restoration.values())
        core_restoration = {k:v for k,v in restoration.items() if not k.startswith('probe:')}
        core_restored = bool(core_restoration) and not any(not k.startswith('probe:') for k in missing) and all(x['exact_match'] for x in core_restoration.values())
        report = {'format': 'nba2k17-batch-report/v1', 'created_at': utc(), 'epoch': epoch,
                  'accepted_steps': len(accepted), 'transitions': transitions,
                  'numeric_candidates': {field: numeric_candidates(samples) for field, samples in groups.items()},
                  'packed_candidates': {field: packed_candidates(samples) for field, samples in groups.items() if field != 'batch.restore'},
                  'enum_flag_and_name_label_evidence': dict(labels),
                  'restoration': restoration, 'all_captured_regions_restored': restored,
                  'canonical_and_verified_regions_restored': core_restored,
                  'original_values': [{'field':e['step']['field'],'display':e.get('operator_note')} for e in accepted if e['step']['kind']=='original'],
                  'missing_restoration_regions': missing,
                  'batch_restore_step_captured': bool(accepted and accepted[-1]['step']['kind'] == 'restore'),
                  'skipped_steps': [e for e in events if e['kind'] == 'SKIPPED_STEP'],
                  'newly_verified_fields': [], 'write_enabled': False,
                  'unresolved': ['Fitted candidates need independent examples and representative injection.',
                                 'Linked appearance/gear blocks with unverified extents were not watched.',
                                 'Enums require actual operator labels; menu order does not establish IDs.'],
                  'next_smallest_experiment': ('Compare one independent player for each unambiguous controlled candidate; keep ambiguous same-changing fields separate.' if core_restored
                                               else 'Restore the whole captured baseline once, or reload and Rebind for semantic comparison. Do not overwrite live bytes to force equality.')}
        out = session / epoch / ('batch-report-' + str(len(events)) + '-' + uuid.uuid4().hex[:8] + '.json')
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return report, out
    finally:
        journal.close()
