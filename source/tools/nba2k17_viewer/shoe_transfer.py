"""Native model/asset pairs. No colorway UI or process writes here."""
from functools import lru_cache
from hashlib import sha256
import json
import re
from pathlib import Path


@lru_cache(maxsize=1)
def catalog():
    path=Path(__file__).parent/'assets/shoe-transfer-catalog.json'
    raw=path.read_bytes()
    return json.loads(raw),sha256(raw).hexdigest()


def register_fields(registry):
    data,digest=catalog()
    if registry['executable_sha256'].lower()!=data['executable_sha256'].lower():
        raise ValueError('Shoe catalog executable mismatch')
    for side,shift,asset_offset in [('home',0,0x128),('away',14,0x12c)]:
        for key,offset,size,bits,bitshift in [('shoe_'+side,0x124,4,14,shift),
                                             ('shoe_'+side+'_asset',asset_offset,4,32,0)]:
            field={'field':'gear.'+key,'region':'player','offset':offset,'size':size,
                   'bits':bits,'shift':bitshift,'codec':'uint','raw_max':(1<<bits)-1,
                   'confidence':'NATIVE_SETTER_AND_CAPTURED_ASSET_PAIR',
                   'evidence_sha256':digest,'evidence':[{'sha256':digest,'source':'shoe-transfer-catalog.json'}],
                   'injection_verified':False}
            registry['fields']=[f for f in registry['fields'] if f['field']!=field['field']]+[field]
    from .gear_layout import native_gear_layout
    gear_digest=sha256((Path(__file__).parent/'assets/native-gear-layout.json').read_bytes()).hexdigest()
    for native in native_gear_layout()['fields']:
        if native['field'] in ('gear.shoe_home','gear.shoe_away'):continue
        field={k:native[k] for k in ('field','offset','size','shift','bits')}
        field.update(region='player',codec='uint',raw_max=max(native['valid_option_ids']),
                     confidence='NATIVE_ACCESSORY_GETTER_SETTER',evidence_sha256=gear_digest,
                     evidence=[{'sha256':gear_digest,'source':'native-gear-layout.json'}],injection_verified=False)
        registry['fields']=[f for f in registry['fields'] if f['field']!=field['field']]+[field]


def resolve_shoes(values, *, require_accessories=False, source_template=None):
    data,_=catalog()
    gear=values.setdefault('gear',{})
    for side in ('home','away'):
        key='shoe_'+side
        model=gear.get(key)
        if isinstance(model,str):
            normalize=lambda value:re.sub('[^a-z0-9]','',value.lower())
            matches=[int(k) for k,v in data['models'].items() if normalize(v['label'])==normalize(model)]
            if len(matches)==1:model=matches[0];gear[key]=model
        if type(model) is not int or str(model) not in data['models']:
            # Preserve a captured native zero-key fallback, never destination
            # bytes or an authored unknown asset guess (e.g. stock model 1095).
            block=(source_template or {}).get('captured_gear',{}).get('shoes',{})
            raw=bytes.fromhex(block.get('raw_hex',''))
            if (type(model) is int and len(raw)==12 and block.get('offset')==0x124
                    and (source_template or {}).get('source_record_sha256')
                    and (source_template or {}).get('executable_sha256','').lower()==data['executable_sha256'].lower()):
                shift=0 if side=='home' else 14
                start=4 if side=='home' else 8
                captured=(int.from_bytes(raw[:4],'little')>>shift)&0x3fff
                if model==captured and raw[start:start+4]==b'\0'*4:
                    gear[key+'_asset']=0
                    continue
            raise ValueError('Missing or unmapped source '+key+'; destination shoes will not be inherited.')
        chosen=data['models'][str(model)]
        asset=chosen['asset_key']
        if data['loaded_asset_models'].get(str(asset))!=model:
            raise ValueError('Shoe asset/model mismatch for '+key)
        gear[key+'_asset']=asset
    from .gear_layout import native_gear_layout
    if not require_accessories:return
    for field in native_gear_layout()['fields']:
        if 'valid_option_ids' not in field:continue
        key=field['field'].split('.',1)[1]
        if type(gear.get(key)) is not int or gear[key] not in field['valid_option_ids']:
            raise ValueError('Missing or invalid source accessory '+key)
