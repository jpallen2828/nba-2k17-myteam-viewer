"""Native gear readers for pointer-free captured player templates."""
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def shoe_model_labels():
    return json.loads((Path(__file__).parent/'assets/native-shoe-models.json').read_text('utf-8'))['models']


@lru_cache(maxsize=1)
def native_gear_layout():
    payload=json.loads((Path(__file__).parent/'assets/native-gear-layout.json').read_text('utf-8'))
    if payload.get('format')!='nba2k17-native-gear-layout/v1':
        raise ValueError('Invalid native gear layout')
    return payload


def decode_gear(raw):
    if len(raw)!=0x460:raise ValueError('Expected a complete player record')
    values={}
    for field in native_gear_layout()['fields']:
        offset,size=field['offset'],field['size']
        value=(int.from_bytes(raw[offset:offset+size],'little')>>field['shift'])&((1<<field['bits'])-1)
        if field['signed'] and value&(1<<(field['bits']-1)):value-=1<<field['bits']
        values[field['field'].split('.',1)[1]]=value
    return values


def captured_gear_values(template):
    layout=native_gear_layout()
    if template['executable_sha256'].lower()!=layout['executable_sha256'].lower():
        raise ValueError('Gear layout belongs to another game executable')
    raw=bytearray(0x460);covered=set()
    for block in template.get('captured_gear',{}).values():
        offset,length=block['offset'],block['length'];data=bytes.fromhex(block['raw_hex'])
        if len(data)!=length or offset<0 or offset+length>len(raw):raise ValueError('Invalid gear capture extent')
        raw[offset:offset+length]=data;covered.update(range(offset,offset+length))
    for field in layout['fields']:
        if not set(range(field['offset'],field['offset']+field['size']))<=covered:
            raise ValueError('Captured source lacks '+field['field'])
    return decode_gear(raw)


def promote_template_gear(template):
    values=captured_gear_values(template)
    template.setdefault('defaults',{})['gear']=values
    template['gear_mapping_summary']={'status':'NATIVE_GETTER_SETTER_LAYOUT',
        'individual_fields_verified':True,'fields':len(values),
        'visual_injection_verified':False,'write_enabled':False,
        'colorways_included':False}
    return values


def populate_card_gear(card):
    template=card.get('raw_source',{}).get('live_roster_template')
    if not template or not template.get('captured_gear'):return False
    values=promote_template_gear(template)
    gear=card.setdefault('gear',{});stored=gear.setdefault('values',{});statuses=gear.setdefault('statuses',{})
    for key,value in values.items():
        # Authored overrides retain priority, including explicit zero / None.
        if statuses.get(key)=='CUSTOM_AUTHORED':continue
        if stored.get(key) is not None and statuses.get(key) not in ('MISSING','LIVE_ROSTER_DERIVED'):continue
        stored[key]=value;statuses[key]='LIVE_ROSTER_DERIVED'
        gear.setdefault('raw_labels',{})[key]=key.replace('_',' ').title()
    gear['missing_fields']=[key for key in gear.get('missing_fields',[]) if stored.get(key) is None]
    return True
