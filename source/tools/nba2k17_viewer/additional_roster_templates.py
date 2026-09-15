"""Append hash-bound source captures without replacing existing card sources."""
from copy import deepcopy
from hashlib import sha256
import json

from .roster_source_matching import player_key,select_source

FORMAT='nba2k17-additional-roster-templates/v1'


def apply_additional_templates(payload,path,data):
    if not path.is_file():return payload
    overlay=json.loads(path.read_text(encoding='utf-8'))
    if overlay.get('format')!=FORMAT:raise ValueError('Invalid additional source overlay')
    documents={}
    for key,item in overlay['files'].items():
        raw=(data/item['path']).read_bytes()
        if sha256(raw).hexdigest()!=item['sha256']:raise ValueError('Additional source evidence hash mismatch: '+key)
        documents[key]=json.loads(raw)
    scope=documents['scope'];capture=documents['capture'];sources=documents['templates'];directory=documents['directory']
    protected=scope['protected_existing_assignments'];eligible=scope['eligible_cards']
    if set(protected)!=set(payload['cards']) or any(payload['cards'][cid]['template_id']!=tid for cid,tid in protected.items()):
        raise ValueError('Previously assigned source set changed; renew the missing-only scope')
    if set(overlay['cards'])&set(protected) or not set(overlay['cards'])<=set(eligible):
        raise ValueError('Additional source attempts to replace a protected or out-of-scope card')
    if (capture['scope_sha256']!=overlay['files']['scope']['sha256']
            or capture['directory_sha256']!=overlay['files']['directory']['sha256']
            or sources['capture_sha256']!=overlay['files']['capture']['sha256']
            or sources['ct_sha256']!=payload['ct_sha256']
            or sources['candidate_definitions']!=payload['candidate_definitions']
            or capture['binding']['executable']['sha256'].lower()!=payload['executable_sha256'].lower()
            or capture['binding']!=directory['binding'] or capture['memory_written'] is not False):
        raise ValueError('Additional source capture/build/layout binding mismatch')
    targets={v['player_key'] for v in eligible.values()};records={r['template_id']:r for r in capture['records']}
    teams={t['team_index']:t for t in directory['teams']}
    candidates={}
    for tid,template in sources['templates'].items():
        if tid in payload['templates'] or sha256(json.dumps(template,sort_keys=True).encode()).hexdigest()!=tid:
            raise ValueError('Additional source template collision or content hash mismatch')
        if player_key(template['name']) not in targets:raise ValueError('Additional capture contains an out-of-scope player')
        if set(template['defaults'])&{'attributes','badges','tendencies','hot_zones'}:
            raise ValueError('Additional template includes card-owned data')
        text=json.dumps(template)
        if any(key in text for key in ('address_diagnostic','diagnostic_target','raw_record_hex')):
            raise ValueError('Additional reusable template contains live addresses or a whole record')
        record=records[tid];raw=bytes.fromhex(record['raw_record_hex'])
        if len(raw)!=0x460 or sha256(raw).hexdigest()!=template['source_record_sha256'] or record['record_sha256']!=template['source_record_sha256']:
            raise ValueError('Additional source record hash/extent mismatch')
        for ref in record['team_references']:
            team=teams[ref['team_index']]
            slot=next((s for s in team['slots'] if s['slot']==ref['slot']),None)
            if not slot or slot['address_diagnostic']!=record['address_diagnostic'] or slot['name']!=template['name']:
                raise ValueError('Additional source is not at its captured team reference')
            context=overlay['contexts'].get(str(ref['team_index']))
            if context:candidates.setdefault(player_key(template['name']),[]).append({'template_id':tid,
                'team_index':ref['team_index'],'slot':ref['slot'],'context':context,'record_sha256':record['record_sha256']})
    reviewed_contexts={str(c['team_index']):c['source_context'] for c in documents['contexts']['contexts']}
    undated=set(map(str,documents['contexts']['additional_undated_team_indices']))
    for index,context in overlay['contexts'].items():
        if index in undated:
            team=teams[int(index)]
            if context['season'] is not None or context['team']!=team['city']+' '+team['nickname']:
                raise ValueError('Undated mod team context changed')
        elif context!=reviewed_contexts.get(index):raise ValueError('Mod context differs from reviewed evidence')
    years=json.loads((data/'curation/year_audit.json').read_text(encoding='utf-8'))
    for cid,entry in overlay['cards'].items():
        identity=eligible[cid]['identity']
        if entry['identity']!={k:identity.get(k) for k in ('name','card_year','represented_season','team')}:
            raise ValueError('Additional source card identity changed')
        choice,decision=select_source(identity,candidates.get(eligible[cid]['player_key'],[]),years.get(cid))
        if (not choice or decision!=entry['selection'] or entry['template_id']!=choice['template_id']
                or entry['source_context']!=choice['context'] or entry['verified_team_index']!=choice['team_index']
                or entry['verified_slot']!=choice['slot'] or entry['source_record_sha256']!=choice['record_sha256']
                or entry['source_slot_verified'] is not True):
            raise ValueError('Additional source selection disagrees with its live reference or closest-year rule')
    # Commit only after all scope, evidence and reference checks pass.
    payload['templates'].update(deepcopy(sources['templates']))
    origins=payload.setdefault('template_capture_sources',{})
    for tid in sources['templates']:
        origins[tid]={'capture_sha256':sources['capture_sha256'],'executable_sha256':capture['binding']['executable']['sha256'],
            'ct_sha256':sources['ct_sha256'],'source_kind':'MODDED_ROSTER',
            'roster_sha256':capture['binding']['roster_sha256'],'capture_evidence':deepcopy(overlay['files']['capture'])}
    payload['cards'].update(deepcopy(overlay['cards']))
    payload['unresolved']=deepcopy(overlay['unresolved'])
    payload['summary'].update(matched_cards=len(payload['cards']),unresolved_cards=len(payload['unresolved']),
        templates=len(payload['templates']),live_records=len(payload['templates']),
        additional_capture=deepcopy(overlay['summary']))
    return payload
