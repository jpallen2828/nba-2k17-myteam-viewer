"""Hash-bound pointer-free template curation, distinct from card overrides."""
from copy import deepcopy
from hashlib import sha256
import json

from .face_ids import identity_binding
from .signature_layout import load_signature_layout,field_bits

FORMAT='nba2k17-live-roster-templates/v1'

# Explicit promotions from captured CT candidates, only after controlled edits.
SIGNATURE_PROMOTIONS={
    'chew_gum':('0/22/6/4/4',0x2bd,1,2,1,'CONTROLLED_OFF_ON_OFF_RESTORED',1),
    'shot_base':('0/22/6/0/2',0x171,1,None,None,'CONTROLLED_RAW_ID_CHANGE_RESTORED_LABEL_PARTIAL',255),
    'spin_jumper':('0/22/6/0/6',0x1f6,2,4,5,'CONTROLLED_CROSS_BYTE_IDS_RESTORED_LABEL_PARTIAL',31),
}


def load_roster_templates(path, *, data_root=None, include_validation=True):
    if not path.is_file():return None
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload.get('format')!=FORMAT:raise ValueError('Invalid roster template overlay')
    data=data_root or path.parent.parent
    for key in ('capture','ct'):
        if sha256((data/payload[key+'_path']).read_bytes()).hexdigest()!=payload[key+'_sha256']:
            raise ValueError('Roster template evidence hash mismatch: '+key)
    for tid,t in payload['templates'].items():
        if sha256(json.dumps(t,sort_keys=True).encode()).hexdigest()!=tid:
            raise ValueError('Roster template content hash mismatch')
        if any(k in t['defaults'] for k in ('attributes','badges','tendencies','hot_zones')):
            raise ValueError('Template cannot replace card-owned ratings/badges/zones/tendencies')
        # Address-bearing evidence stays in the immutable diagnostic capture,
        # never in the reusable data stored on each canonical card.
        text=json.dumps(t)
        if 'diagnostic_target' in text or 'raw_record_hex' in text or 'address_diagnostic' in text:
            raise ValueError('Reusable template contains a process address or raw record')
    for cid,e in payload['cards'].items():
        if e['template_id'] not in payload['templates']:raise ValueError('Missing template for '+cid)
    registry_path=data/'field_discovery/player-field-registry.json'
    if registry_path.is_file():
        registry=json.loads(registry_path.read_text(encoding='utf-8'))
        payload['validated_signature_promotions']={}
        for key,(uid,offset,size,shift,bits,confidence,maximum) in SIGNATURE_PROMOTIONS.items():
            field=next((f for f in registry['fields'] if f['field']=='signatures.'+key),None)
            if not field:continue
            definition=payload['candidate_definitions'][uid]
            if (registry['executable_sha256'].lower()!=payload['executable_sha256'].lower()
                    or (field['offset'],field['size'],field.get('shift'),field.get('bits'))!=(offset,size,shift,bits)
                    or (definition['context_relative_offset'],definition['bit_start'],definition['bit_width'])!=(offset,shift,bits)
                    or (bits is None and definition['byte_length']!=size)
                    or field['confidence']!=confidence or field['raw_max']!=maximum):
                raise ValueError(key+' promotion disagrees with captured layout')
            root=data.parents[2]
            for evidence in field['evidence']:
                if sha256((root/evidence['path']).read_bytes()).hexdigest()!=evidence['sha256']:
                    raise ValueError(key+' evidence changed')
            payload['validated_signature_promotions'][key]=deepcopy(field)
        if layout:=load_signature_layout(data):
            if layout['executable_sha256'].lower()!=payload['executable_sha256'].lower():
                raise ValueError('Signature layout/template build mismatch')
            mapped={f['field']:f for f in registry['fields']};checked=set()
            for candidate in layout['fields']:
                field=mapped.get(candidate['field']);uid=candidate['reference_uid']
                definition=payload['candidate_definitions'][uid]
                shift=definition['bit_start'] or 0
                bits=definition['bit_width'] or definition['byte_length']*8
                start=definition['context_relative_offset']*8+shift
                if (not field or field_bits(field)!=field_bits(candidate)
                        or field_bits(field)!=set(range(start,start+bits))):
                    raise ValueError('Signature promotion disagrees with captured CT: '+uid)
                for evidence in field['evidence']:
                    pair=(evidence['path'],evidence['sha256'])
                    if pair in checked:continue
                    if sha256((data.parents[2]/evidence['path']).read_bytes()).hexdigest()!=evidence['sha256']:
                        raise ValueError('Signature mapping evidence changed')
                    checked.add(pair)
                payload['validated_signature_promotions'][candidate['field'].split('.',1)[1]]=deepcopy(field)
            payload['signature_mapping_summary']=deepcopy(layout['summary'])
    if include_validation and path.resolve()==(data/'curation/live_roster_templates.json').resolve():
        apply_template_validation(payload,data/'curation/roster_template_validation.json',data)
        from .additional_roster_templates import apply_additional_templates
        apply_additional_templates(payload,data/'curation/additional_roster_templates.json',data)
        apply_alternate_versions(payload,data/'curation/alternate_version_templates.json')
        from .scanned_roster_templates import apply_scanned_templates
        apply_scanned_templates(payload,data/'curation/scanned_roster_templates.json',data)
        apply_scanned_templates(payload,data/'curation/all_player_pool_templates.json',data)
        from .native_roster_templates import apply_native_templates
        apply_native_templates(payload,data/'curation/native_roster_templates.json',data)
        apply_scanned_templates(payload,data/'curation/selected_roster_templates.json',data)
    return payload


def apply_alternate_versions(payload,path):
    """Explicitly approved same-player fallback, retaining exact captured sources."""
    if not path.is_file():return
    from .roster_source_matching import player_key
    overlay=json.loads(path.read_text(encoding='utf-8'))
    if overlay.get('format')!='nba2k17-approved-alternate-versions/v1':
        raise ValueError('Invalid alternate-version template overlay')
    for cid,entry in overlay['cards'].items():
        if cid in payload['cards']:
            raise ValueError('Alternate version would overwrite an existing assignment: '+cid)
        donor=payload['cards'][entry['source_card_id']]
        template=payload['templates'][entry['template_id']]
        if (donor['template_id']!=entry['template_id'] or donor['identity']!=entry['source_identity']
                or template['source_record_sha256']!=entry['source_record_sha256']
                or player_key(entry['identity']['name'])!=player_key(donor['identity']['name'])):
            raise ValueError('Alternate version source binding changed: '+cid)
        assignment=deepcopy(donor)
        assignment.update(identity=deepcopy(entry['identity']),method='USER_APPROVED_ALTERNATE_VERSION',
                          source_card_id=entry['source_card_id'],exact_card_era_verified=False)
        assignment['selection']={**assignment.get('selection',{}),
                                 'method':'USER_APPROVED_ALTERNATE_VERSION','exact_card_era_verified':False}
        payload['cards'][cid]=assignment
    payload['unresolved']=[e for e in payload['unresolved'] if str(e['source_card_id']) not in overlay['cards']]
    payload['summary'].update(matched_cards=len(payload['cards']),unresolved_cards=len(payload['unresolved']))


def apply_template_validation(payload,path,data):
    """Merge reviewed assignments only when their live references still have evidence."""
    from .roster_source_matching import team_candidates,select_source,player_key
    if not path.is_file():return payload
    supplement=json.loads(path.read_text(encoding='utf-8'))
    if (supplement.get('format')!='nba2k17-roster-template-validation/v1'
            or supplement['base_capture_sha256']!=payload['capture_sha256']):
        raise ValueError('Roster source validation capture mismatch')
    audits=[]
    for evidence in supplement['evidence']:
        raw=(data.parents[2]/evidence['path']).read_bytes()
        if sha256(raw).hexdigest()!=evidence['sha256']:raise ValueError('Roster source validation evidence hash mismatch')
        document=json.loads(raw)
        if document.get('format')=='nba2k17-live-source-validation/v1':audits.append(document)
    if len(audits)!=1 or audits[0]['source_capture_sha256']!=payload['capture_sha256']:
        raise ValueError('Missing matching live source audit')
    audit=audits[0]
    reviewed={r['card_id']:r for r in audit['cards']}
    for cid,entry in supplement['cards'].items():
        if cid not in reviewed or entry['identity']!=reviewed[cid]['identity']:
            raise ValueError('Source assignment identity differs from audit: '+cid)
        if entry['selection']['year_hint']!=reviewed[cid]['decision']['year_hint']:
            raise ValueError('Source year hint differs from reviewed card: '+cid)
        template=payload['templates'][entry['template_id']]
        if player_key(template['name'])!=player_key(entry['identity']['name']):
            raise ValueError('Source assignment names differ')
        hint={'expected':{'card_year':entry['selection']['year_hint']}}
        choice,decision=select_source(entry['identity'],team_candidates(audit['teams'],template['name']),hint)
        if (not choice or decision!=entry['selection'] or choice['template_id']!=entry['template_id']
                or choice['context']!=entry['source_context']
                or choice['team_index']!=entry['verified_team_index'] or choice['slot']!=entry['verified_slot']
                or choice['record_sha256']!=entry['source_record_sha256']
                or template['source_record_sha256']!=entry['source_record_sha256']
                or entry['source_slot_verified'] is not True
                or entry['exact_card_era_verified']!=decision['exact_card_era_verified']):
            raise ValueError('Source assignment disagrees with live reference or closest-year rule: '+cid)
        payload['cards'][cid]={**deepcopy(entry),'validation_evidence':deepcopy(supplement['evidence'])}
    payload['unresolved']=[{'source_card_id':r['card_id'],'name':r['identity']['name'],
        'year':r['identity']['card_year'],'reason':r['decision']['reason'],
        'has_any_team_slot':r['has_any_team_slot'],'available_sources':r['candidates']} for r in supplement['unresolved']]
    payload['summary'].update(matched_cards=len(payload['cards']),unresolved_cards=len(payload['unresolved']),
                              source_validation=deepcopy(supplement['summary']))
    payload['source_matching_policy']=supplement['policy']
    return payload


def materialize_template(payload,template_id):
    """Named captured defaults and candidates, without inventing gear codecs."""
    template=deepcopy(payload['templates'][template_id])
    template.update(template_id=template_id,
                    capture_sha256=payload['capture_sha256'],executable_sha256=payload['executable_sha256'],
                    ct_sha256=payload['ct_sha256'])
    if origin:=payload.get('template_capture_sources',{}).get(template_id):
        template.update(deepcopy(origin))
    template['candidate_definition_catalog']='live_roster_candidate_definitions'
    for key,field in payload.get('validated_signature_promotions',{}).items():
        uid=field['reference_uid'] if 'reference_uid' in field else SIGNATURE_PROMOTIONS[key][0]
        maximum=field['raw_max']
        value=template['ct_candidate_values'][uid]
        if type(value) is not int or not 0<=value<=maximum:
            raise ValueError('Invalid captured '+key+' value')
        template['defaults'].setdefault('signatures',{})[key]=value
        template['field_evidence']['signatures.'+key]={'confidence':field['confidence'],'evidence':deepcopy(field['evidence']),
                                                     'inferred_mapping':field.get('inferred_mapping',False),'reference_uid':uid}
    if 'signature_mapping_summary' in payload:
        template['signature_mapping_summary']=deepcopy(payload['signature_mapping_summary'])
        template['signature_mapping_summary']['visual_injection_verified']=False
    gear={}
    for key,uid,offset,length in [('shoes','0/22/7',0x124,12),('accessories','0/22/8',0x139,19)]:
        definition=payload['candidate_definitions'][uid]
        if (definition['context_relative_offset'],definition['byte_length'],definition['data_type'])!=(offset,length,'Array of byte'):
            raise ValueError('Captured gear definition changed: '+key)
        value=template['ct_candidate_values'][uid]
        if not isinstance(value,str) or len(value)!=length*2:
            raise ValueError('Captured gear block has wrong extent: '+key)
        raw=bytes.fromhex(value)
        if len(raw)!=length or raw.hex()!=value:raise ValueError('Invalid captured gear block: '+key)
        gear[key]={'raw_hex':value,'reference_uid':uid,'offset':offset,'length':length,
                   'confidence':'CT_CANDIDATE_CAPTURED_INLINE_BLOCK',
                   'individual_fields_verified':False,'none_encoding_verified':False,'write_enabled':False}
    template['captured_gear']=gear
    template['gear_mapping_summary']={'captured_blocks':2,'captured_bytes':31,
        'status':'CAPTURED_CANDIDATES_UI_EDIT_UNAVAILABLE','individual_fields_verified':False,
        'visual_injection_verified':False,
        'policy':'Preserve source bytes including zeros. No inference that zero means no gear; no live write enabled by capture.'}
    from .gear_layout import promote_template_gear
    promote_template_gear(template)
    from .speech_identity import promote_template_speech
    promote_template_speech(template)
    return template


def apply_roster_template(record,payload):
    if not payload:return False
    cid=str(record['source_card_id']); entry=payload['cards'].get(cid)
    if not entry:return False
    card=record['card']
    if identity_binding(card)!=entry['identity']:raise ValueError('Roster template card identity changed: '+cid)
    template=materialize_template(payload,entry['template_id'])
    template['match']=deepcopy(entry)
    if entry.get('method') == 'EXACT_NATIVE_MYTEAM_ROSTER':
        # This source's exact height was explicitly requested in the user's
        # reconstruction reference; do not round it back to the web label.
        exact = entry.get('card_height_cm')
        if exact is not None:
            captured = template['defaults']['identity']['height_cm']
            if abs(exact-captured)>0.001:
                raise ValueError('Card height disagrees with native body source')
            card['identity']['values']['height_cm'] = exact
            card['identity'].setdefault('statuses', {})['height_cm'] = 'USER_CONFIRMED'
    # Existing curated variants win. Fill only absent asset IDs from a matched
    # live template, enforcing the user's face == portrait rule explicitly.
    metadata=card.setdefault('gameplay_metadata',{})
    values=metadata.setdefault('values',{})
    captured_face=template['defaults'].get('gameplay_metadata',{}).get('face_id')
    if (values.get('face_id') is None and values.get('portrait_id') is None
            and type(captured_face) is int and captured_face>0):
        for key in ('face_id','portrait_id'):
            values[key]=captured_face
            metadata.setdefault('statuses',{})[key]='LIVE_ROSTER_DERIVED'
            metadata.setdefault('raw_labels',{})[key]='Matched live face ID' if key=='face_id' else 'User rule: portrait ID equals face ID'
        if 'missing_fields' in metadata:
            metadata['missing_fields']=[key for key in metadata['missing_fields'] if key not in ('face_id','portrait_id')]
        metadata.setdefault('provenance',[]).append({'status':'LIVE_ROSTER_DERIVED',
            'fields':['face_id','portrait_id'],'template_id':template['template_id'],
            'capture_sha256':template['capture_sha256'],'source_face_id':captured_face,
            'source_portrait_id':template['defaults']['gameplay_metadata'].get('portrait_id'),
            'policy':'FILL_MISSING_ONLY_PORTRAIT_EQUALS_FACE'})
    card.setdefault('raw_source',{})['live_roster_template']=template
    from .gear_layout import populate_card_gear
    populate_card_gear(card)
    return True
