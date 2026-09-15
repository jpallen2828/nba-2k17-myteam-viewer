"""Visible, explicit Studio option values, with source defaults resolved first."""
from copy import deepcopy
from functools import lru_cache
import json
from pathlib import Path


@lru_cache(maxsize=1)
def option_schema():
    from nba2k17_viewer import runtime_fields
    return json.loads((Path(runtime_fields.__file__).parent/'assets/studio-player-fields.json').read_text('utf-8'))


def complete_options(data, defaults=None):
    defaults = defaults or {}
    for section in ('signatures', 'gear'):
        target=data.setdefault(section, {'values':{}, 'statuses':{}})
        values=target.setdefault('values', {}); statuses=target.setdefault('statuses', {})
        for entry in option_schema()[section]:
            key=entry['key']
            if values.get(key) is not None:
                continue
            inherited=defaults.get(section, {}).get(key)
            if inherited is not None and statuses.get(key)!='CUSTOM_AUTHORED':
                values[key]=inherited; statuses[key]='LIVE_ROSTER_DERIVED'
            else:
                values[key]=0; statuses[key]='CUSTOM_AUTHORED'
        target['missing_fields']=[k for k in target.get('missing_fields', []) if values.get(k) is None]
    return data


def studio_data(card):
    from .document import SECTIONS
    data={s:deepcopy(card.get(s, {'values':{}, 'statuses':{}})) for s in SECTIONS}
    defaults=(card.get('raw_source', {}).get('live_roster_template') or {}).get('defaults', {})
    return complete_options(data, defaults)
