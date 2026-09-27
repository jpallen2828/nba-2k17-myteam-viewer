"""Field-level buffer planning for 2K17. No process access or write permission.

The registry carries evidence and confidence separately from the codec. A
successful buffer round trip is not validation of a CT definition in the game.
"""
from hashlib import sha256
import math
import struct

from .roster_record import decode_roster_rating, encode_roster_rating


BADGE_LEVELS = {'NONE': 0, 'UNEQUIPPED': 0, 'BRONZE': 1, 'SILVER': 2,
                'GOLD': 3, 'HALL_OF_FAME': 4}


def is_unknown_badge_value(value):
    return value is None or (isinstance(value,str) and value.strip().upper() in
                             ('', 'UNKNOWN', 'MISSING', 'NOT_SET', 'NOT SET'))


def complete_badge_values(profile, registry, *, clear_unlisted=False, unknown_as_none=False):
    """Resolve the caller's badge policy without inheriting destination badges.

    The app explicitly treats unknown badges as NONE at the user's request.
    Strict callers retain the distinction between absent and unknown evidence.
    """
    explicit=dict(profile['card_specific_overrides'].get('badges',{}))
    excluded=profile.get('excluded_card_values',{})
    missing,cleared,assumed=[],[],[]
    for field in registry['fields']:
        if not field['field'].startswith('badges.'):continue
        key=field['field'].split('.',1)[1]
        if key in explicit and not is_unknown_badge_value(explicit[key]):continue
        unknown=key in explicit or field['field'] in excluded
        if unknown and unknown_as_none:
            explicit[key]='NONE'
            assumed.append(key)
            continue
        if key in explicit:
            explicit.pop(key)
            missing.append(key)
            continue
        if field['field'] in excluded or not clear_unlisted:
            missing.append(key)
        else:
            explicit[key]='NONE'
            cleared.append(key)
    policy=('UNKNOWN_AND_UNLISTED_AS_NONE' if clear_unlisted else 'UNKNOWN_AS_NONE') if unknown_as_none else (
        'CLEAR_UNLISTED_KEEP_EXPLICIT_MISSING_UNRESOLVED' if clear_unlisted else 'REQUIRE_EXPLICIT_STATES')
    return {'values':explicit,'cleared_unlisted':sorted(cleared),'assumed_unknown_none':sorted(assumed),
            'unresolved':sorted(missing),'policy':policy}


def _extent(field, record):
    offset, size = field['offset'], field['size']
    if type(offset) is not int or type(size) is not int or offset < 0 or size <= 0 or offset + size > len(record):
        raise ValueError('Field exceeds supplied buffer')
    return offset, size


def decode(record, field):
    offset, size = _extent(field, record)
    raw = record[offset:offset + size]
    codec = field['codec']
    if codec == 'utf16le':
        end = next((i for i in range(0, size, 2) if raw[i:i+2] == b'\0\0'), size)
        return raw[:end].decode('utf-16-le')
    if codec == 'float32':
        value = struct.unpack('<f', raw)[0]
        if not math.isfinite(value):
            raise ValueError('Nonfinite player value')
        return value
    value = (int.from_bytes(raw, 'little') >> field.get('shift', 0)) & ((1 << field.get('bits', size*8)) - 1)
    if codec == 'rating':
        return decode_roster_rating(value)
    if codec == 'personality':
        return bool(value)
    if codec == 'jersey':
        if value > 100:
            raise ValueError('Invalid jersey code')
        return '00' if value == 100 else str(value)
    if codec in ('uint', 'badge', 'enum'):
        return value
    raise ValueError('Unsupported codec: ' + codec)


def encode(record, field, value):
    """Modify a local buffer, preserving every bit outside this exact field."""
    offset, size = _extent(field, record)
    result = bytearray(record)
    codec = field['codec']
    if value is None:
        raise ValueError('Missing is not explicit zero/none')
    if codec == 'utf16le':
        if not isinstance(value, str) or '\0' in value:
            raise ValueError('Name must be text without embedded nulls')
        encoded = value.encode('utf-16-le')
        if len(encoded) + 2 > size:
            raise ValueError('Name exceeds field; truncation is forbidden')
        result[offset:offset+size] = encoded + bytes(size-len(encoded))
        return bytes(result)
    if codec == 'float32':
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError('Player value must be finite')
        result[offset:offset+size] = struct.pack('<f', value)
        return bytes(result)
    if codec == 'rating':
        value = encode_roster_rating(value)
    elif codec == 'badge' and isinstance(value, str):
        if value not in BADGE_LEVELS:
            raise ValueError('Unknown gameplay badge level: ' + value)
        value = BADGE_LEVELS[value]
    elif codec == 'personality':
        if value is True or value == 'PERSONALITY':
            value = 1
        elif value is False or value in ('NONE', 'UNEQUIPPED'):
            value = 0
        else:
            raise ValueError('Personality badge needs explicit equipped/none state')
    elif codec == 'jersey':
        if value == '00':
            value = 100
        elif isinstance(value, str) and value.isascii() and value.isdecimal() and str(int(value)) == value:
            value = int(value)
    elif codec == 'enum' and isinstance(value, str):
        if field.get('field') in ('identity.primary_position', 'identity.secondary_position'):
            value = value.strip().upper()
        if value not in field.get('labels', {}):
            raise ValueError('Unmapped enum label: ' + value)
        value = field['labels'][value]
    elif codec not in ('uint', 'badge', 'enum'):
        raise ValueError('Unsupported codec: ' + codec)
    bits, shift = field.get('bits', size*8), field.get('shift', 0)
    if bits <= 0 or shift < 0 or bits + shift > size*8:
        raise ValueError('Invalid packed extent')
    maximum = field.get('raw_max', (1 << bits)-1)
    if type(value) is not int or not 0 <= value <= min(maximum, (1 << bits)-1):
        raise ValueError('Value outside field domain')
    mask = ((1 << bits)-1) << shift
    prior = int.from_bytes(record[offset:offset+size], 'little')
    result[offset:offset+size] = ((prior & ~mask) | (value << shift)).to_bytes(size, 'little')
    return bytes(result)


def preview_record(destination, values, registry):
    """Compile only explicitly requested fields into reviewable buffer patches.

    Even corroborated candidates remain marked as such. Unknown/missing fields
    are returned as gaps. Nothing here can approve a live transaction.
    """
    if len(destination) != 0x460:
        raise ValueError('Expected a complete 2K17 destination shell')
    after, rows, unsupported, used_bits = bytes(destination), [], [], set()
    protected = registry.get('protected_ranges', [[0x58,0x64],[0x70,0xc8],[0x150,0x158],[0x3e0,0x3f1]])
    definitions = {f['field']: f for f in registry['fields']}
    for section, fields in values.items():
        for key, value in fields.items():
            qualified = section + '.' + key
            definition = definitions.get(qualified)
            if definition is None or definition.get('region', 'player') != 'player':
                unsupported.append({'field': qualified, 'reason': 'No canonical field mapping', 'requested': value})
                continue
            if value is None:
                unsupported.append({'field': qualified, 'reason': 'Missing value requires an explicit policy', 'requested': None})
                continue
            off, size = _extent(definition, destination)
            if any(off < end and off + size > start for start, end in protected):
                raise ValueError('Field intersects destination structure, durability or calculated output: ' + qualified)
            shift = definition.get('shift', 0)
            bits = set(range(off*8+shift, off*8+shift+definition.get('bits', size*8)))
            if used_bits & bits:
                raise ValueError('Overlapping player fields: ' + qualified)
            try:
                candidate = encode(after, definition, value)
                decoded = decode(candidate, definition)
            except (ValueError, OverflowError, struct.error) as exc:
                unsupported.append({'field': qualified, 'reason': str(exc), 'requested': value})
                continue
            used_bits |= bits
            rows.append({'field': qualified, 'requested': value, 'decoded_after': decoded,
                         'offset': off, 'size': size, 'before_hex': after[off:off+size].hex(),
                         'after_hex': candidate[off:off+size].hex(), 'confidence': definition['confidence'],
                         'evidence': definition['evidence'], 'injection_verified': False})
            after = candidate
    # Coalesce at byte granularity after all packed fields have been combined.
    # Adjacent/overlapping bytes cannot become conflicting transaction patches.
    patches, start = [], None
    for i in range(len(destination)+1):
        changed = i < len(destination) and destination[i] != after[i]
        if changed and start is None:
            start = i
        elif not changed and start is not None:
            patches.append({'offset': start, 'before_hex': destination[start:i].hex(),
                            'after_hex': after[start:i].hex(),
                            'fields': [r['field'] for r in rows if r['offset'] < i and r['offset']+r['size'] > start]})
            start = None
    return {'format': 'nba2k17-player-buffer-preview/v1', 'before_sha256': sha256(destination).hexdigest(),
            'after_sha256': sha256(after).hexdigest(), 'after_hex': after.hex(), 'fields': rows,
            'unsupported': unsupported, 'patches': patches, 'memory_write_authorized': False,
            'preserves_destination_structure': True, 'injection_verified': False}
