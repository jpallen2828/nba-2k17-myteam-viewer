"""Lossless portable storage of repeated template provenance."""
from copy import deepcopy
from hashlib import sha256
import json


def evidence_key(value):
    return sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def templates(record):
    card=record['card']
    for value in (card.get('raw_source',{}).get('live_roster_template'),
                  card.get('injection',{}).get('reusable_roster_template')):
        if value is not None:yield value


def compact_template_evidence(record,pool):
    for template in templates(record):
        compact_source_template(template,pool)


def compact_source_template(template,pool):
    if 'field_evidence' not in template:return template
    evidence=template.pop('field_evidence');key=evidence_key(evidence)
    if key not in pool:pool[key]=evidence
    template['field_evidence_ref']=key
    return template


def validate_evidence_pool(pool):
    if not isinstance(pool,dict):raise ValueError('Invalid template evidence pool')
    for key,value in pool.items():
        if evidence_key(value)!=key:raise ValueError('Template evidence pool hash mismatch')
    return pool


def expand_template_evidence(record,pool):
    for template in templates(record):
        expand_source_template(template,pool)
    return record


def expand_source_template(template,pool):
    key=template.pop('field_evidence_ref',None)
    if key is None:return template
    if key not in pool or 'field_evidence' in template:
        raise ValueError('Missing or ambiguous template evidence')
    template['field_evidence']=deepcopy(pool[key])
    return template
