"""Install approved, team-referenced captures without changing existing sources."""
from copy import deepcopy
from hashlib import sha256
import json
from .roster_source_matching import player_key


def apply_scanned_templates(payload,path,data):
    if not path.is_file():return
    overlay=json.loads(path.read_text(encoding='utf-8'))
    if overlay.get('format')!='nba2k17-scanned-source-install/v1':raise ValueError('Invalid scanned-source overlay')
    raw=(data.parents[2]/overlay['capture']['path']).read_bytes()
    if sha256(raw).hexdigest()!=overlay['capture']['sha256']:raise ValueError('Scanned source capture changed')
    capture=json.loads(raw)
    if capture['memory_written'] is not False or capture['executable']['sha256'].lower()!=payload['executable_sha256'].lower():
        raise ValueError('Scanned source executable/capture mismatch')
    protected={cid:entry['template_id'] for cid,entry in payload['cards'].items()}
    if protected!=overlay['protected_assignments']:raise ValueError('Existing template assignments changed; review scan scope')
    records={r['record_sha256']:r for r in capture['records']}
    pool=overlay.get('source_kind')=='ALL_PLAYERS_POOL'
    supported=set()
    if pool:
        directory_raw=(data.parents[2]/overlay['directory']['path']).read_bytes()
        if sha256(directory_raw).hexdigest()!=capture['directory_sha256'] or sha256(directory_raw).hexdigest()!=overlay['directory']['sha256']:
            raise ValueError('Pool directory evidence changed')
        directory=json.loads(directory_raw)
        if (directory['pid'],directory['process_start_token'])!=(capture['pid'],capture['process_start_token']):
            raise ValueError('Pool directory belongs to another game session')
        teams={t['index']:t for t in directory['teams']}
        # Pool-only sources require the independently team-supported record grid.
        # This validates read-only source membership, never writable destinations.
        groups=[]
        for record in sorted(capture['records'],key=lambda r:int(r['address_diagnostic'],16)):
            address=int(record['address_diagnostic'],16)
            if not groups or address-int(groups[-1][-1]['address_diagnostic'],16)>5*0x460:groups.append([])
            groups[-1].append(record)
        for group in groups:
            if sum(bool(r['team_references']) for r in group)>=50:
                supported.update(r['record_sha256'] for r in group)
    else:
        teams={t['index']:t for t in capture['teams']}
    for tid,template in overlay['templates'].items():
        if sha256(json.dumps(template,sort_keys=True).encode()).hexdigest()!=tid:raise ValueError('Scanned template hash changed')
        record=records[template['source_record_sha256']]
        if pool and (record['record_sha256'] not in supported or not record.get('stable_final_readback')
                     or not record.get('linked_final_readback') or record.get('decode_errors')):
            raise ValueError('Pool source lacks stable, complete, team-supported capture evidence')
        source=bytes.fromhex(record['raw_record_hex'])
        if len(source)!=0x460 or sha256(source).hexdigest()!=template['source_record_sha256']:raise ValueError('Invalid source record')
        if template['name']!=record['name']:raise ValueError('Source name changed')
        if set(template['defaults']) & {'attributes','badges','tendencies','hot_zones'}:raise ValueError('Source replaces card-owned data')
        if any(key in json.dumps(template) for key in ('address_diagnostic','raw_record_hex','diagnostic_target')):
            raise ValueError('Reusable source contains live addresses')
        for ref in record['team_references']:
            slot=next(s for s in teams[ref['team_index']]['slots'] if s['slot']==ref['slot'])
            if slot['address_diagnostic']!=record['address_diagnostic'] or slot['name']!=record['name']:
                raise ValueError('Source team reference mismatch')
    for cid,entry in overlay['cards'].items():
        if cid in protected:raise ValueError('Scanned source replaces an existing assignment')
        template=overlay['templates'][entry['template_id']];record=records[template['source_record_sha256']]
        if cid not in record['matching_cards'] or player_key(template['name'])!=player_key(entry['identity']['name']):
            raise ValueError('Scanned source identity/scope mismatch')
    payload['templates'].update(deepcopy(overlay['templates']))
    for tid in overlay['templates']:
        payload.setdefault('template_capture_sources',{})[tid]={
            'capture_sha256':overlay['capture']['sha256'],'executable_sha256':payload['executable_sha256'],
            'ct_sha256':payload['ct_sha256'],'source_kind':'USER_LOADED_ROSTER',
            'capture_evidence':deepcopy(overlay['capture'])}
    payload['cards'].update(deepcopy(overlay['cards']))
    payload['unresolved']=[r for r in payload['unresolved'] if str(r['source_card_id']) not in overlay['cards']]
    payload['summary'].update(matched_cards=len(payload['cards']),unresolved_cards=len(payload['unresolved']),
                              templates=len(payload['templates']),live_records=len(payload['templates']))
