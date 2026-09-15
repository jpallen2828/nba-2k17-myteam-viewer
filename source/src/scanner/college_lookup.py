"""Resolve 2K17 college labels from current canonical records, read-only.

Controlled Davidson/Dayton edits established a full pointer at player +0x70.
The CT's two-byte college value is not portable. Lookup entries are shared
immutable data: never edit them or serialize their address as a template ID.
This module resolves candidates, not permission to reassign a native pointer.
"""
import struct
from hashlib import sha256


def read_label(reader, address):
    # A bounded string probe, not an assertion of the complete entry extent.
    raw = reader.read_or_raise(address,40)
    if raw != reader.read_or_raise(address,40):
        raise ValueError('College label changed during read')
    if b'\0' not in raw:
        raise ValueError('Unterminated college label')
    label=raw.split(b'\0',1)[0].decode('ascii')
    if not label or any(ord(c)<32 or ord(c)>126 for c in label):
        raise ValueError('Unsupported college label encoding')
    return label,raw


def index_colleges(reader, records, *, table=None, module_base=None):
    """records must be freshly read canonical records from this process binding."""
    labels,unresolved={},[]
    for record_address,raw in records.items():
        if len(raw)!=0x460:raise ValueError('Incomplete player record')
        ptr=struct.unpack_from('<Q',raw,0x70)[0]
        if not ptr:continue
        try:
            label,probe=read_label(reader,ptr)
        except (ValueError,UnicodeError,OSError) as exc:
            unresolved.append({'record_address_diagnostic':hex(record_address),'reason':str(exc)})
            continue
        entry=labels.setdefault(label,{}).setdefault(ptr,{'label':label,'address_diagnostic':hex(ptr),
            'label_probe_sha256':sha256(probe).hexdigest(),'record_references':[]})
        entry['record_references'].append(hex(record_address))
    if table is not None and module_base is not None:
        # The complete native table is build-bound and independently anchored
        # to live roster pointers. It includes schools no player currently uses.
        base = module_base + table['rva']
        size = table['stride'] * len(table['labels'])
        try:
            raw = reader.read_or_raise(base, size)
            if raw != reader.read_or_raise(base, size):
                raise ValueError('Native origin table changed during read')
            entries = {}
            for i, expected in enumerate(table['labels']):
                offset = i * table['stride']
                probe = raw[offset:offset+40]
                if len(probe) != 40 or probe.split(b'\0',1)[0] != expected.encode('ascii'):
                    raise ValueError('Native origin table does not match verified layout')
                entries[base+offset] = (expected, probe)
            anchors = sum(address in entries and entries[address][0] == label
                          for label, candidates in labels.items() for address in candidates)
            if anchors < 2:
                raise ValueError('Native origin table is not anchored to the loaded roster')
            for address, (label, probe) in entries.items():
                entry = labels.setdefault(label, {}).setdefault(address, {
                    'label':label,'address_diagnostic':hex(address),
                    'label_probe_sha256':sha256(probe).hexdigest(),'record_references':[]})
                entry['native_table_verified'] = True
        except (OSError, ValueError) as exc:
            unresolved.append({'reason': str(exc), 'native_table': True})
    return labels,unresolved


def resolve_college(reader, labels, label):
    if not isinstance(label,str) or not label:raise ValueError('College requires an exact label')
    candidates=labels.get(label,{})
    native = {a:e for a,e in candidates.items() if e.get('native_table_verified')}
    if len(native) == 1:
        candidates = native
    if len(candidates)!=1:raise ValueError('College label missing or ambiguous in current roster')
    address,entry=next(iter(candidates.items()))
    current,probe=read_label(reader,address)
    if current!=label or sha256(probe).hexdigest()!=entry['label_probe_sha256']:
        raise ValueError('College lookup changed; rebind')
    return {**entry,'memory_write_authorized':False,'resolution':'FRESH_SHARED_LOOKUP_LABEL',
            'persistence_policy':'Store label; re-resolve each process/roster binding; never persist pointer as an ID'}


def resolve_optional_college(reader, labels, label):
    """Unavailable descriptive metadata must not block mapped player fields."""
    try:
        return resolve_college(reader, labels, label)
    except (ValueError, OSError, UnicodeError) as exc:
        return {'label': label, 'resolution': 'PRESERVED_DESTINATION',
                'memory_write_authorized': False, 'reason': str(exc)}
