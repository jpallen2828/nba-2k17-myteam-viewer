"""Inspect discovered team-reference candidates without assigning write authority.

Offsets here are NBA 2K17 hypotheses observed beside direct reference arrays in
this session. Readable references establish targets, not membership/rotation
control or capacity. Those semantics need controlled UI tests.
"""
import re
import struct

from .roster_record import ROSTER_PLAYER_RECORD_SIZE, utf16_fixed_field
from .roster_scan import identify_candidate, sha


def inspect_teams(reader, scan, *, allow_unlabeled=False):
    if reader.pid != scan['pid'] or hex(reader.main_module.base_address) != scan['module_base']:
        raise ValueError('Discovery capture belongs to another process instance')
    teams = []
    for cluster in scan['reference_clusters']:
        if not 8 <= len(cluster) <= 20:
            continue
        if len({h['target_address'] for h in cluster}) != len(cluster):
            continue
        base = int(cluster[0]['reference_address'], 16)
        token = reader.utf16le(base + 0xde, 64)
        stadium = reader.utf16le(base + 0xa0, 60)
        if allow_unlabeled:
            if token and not re.fullmatch(r'[a-z0-9_]+', token):
                continue
            if not stadium or not re.fullmatch(r'[a-z0-9_]+', stadium):
                continue
        elif not token or not re.fullmatch(r'[a-z0-9_]+_logo', token):
            continue
        table = reader.read(base, 0xa0)
        if table is None or table != reader.read(base, 0xa0):
            continue
        slots = []
        for index in range(20):
            target = struct.unpack_from('<Q', table, index * 8)[0]
            item = {'observed_order_zero_based': index, 'reference_address': hex(base + index * 8),
                    'player_address': hex(target) if target else None, 'player_id': None,
                    'player_id_status': 'UNMAPPED', 'empty_reference': not bool(target)}
            if target:
                raw = reader.read(target, ROSTER_PLAYER_RECORD_SIZE)
                if raw is None or raw != reader.read(target, ROSTER_PLAYER_RECORD_SIZE):
                    item.update(readable_stable=False, name=None)
                else:
                    # Follow the actual reference even if a conservative pool
                    # filter rejected the record; never omit an occupied slot.
                    first = utf16_fixed_field(raw, 0x24, 0x28)
                    last = utf16_fixed_field(raw, 0, 0x24)
                    item.update(readable_stable=True, name=(first + ' ' + last).strip(),
                                first_name=first, last_name=last, record_sha256=sha(raw), raw_hex=raw.hex(),
                                matches_pool_shape=identify_candidate(raw) is not None,
                                position=None, jersey_number=None, height=None)
            slots.append(item)
        # Repeated lineup references were observed at this relative location.
        # Preserve their order separately; do not call this the rotation table.
        lineups = reader.read(base + 0x788, 35 * 8)
        lineup_refs = []
        if lineups and lineups == reader.read(base + 0x788, len(lineups)):
            for i in range(35):
                ptr = struct.unpack_from('<Q', lineups, i * 8)[0]
                known = next((s for s in slots if s['player_address'] == hex(ptr)), None)
                lineup_refs.append({'order_zero_based': i, 'reference_address': hex(base + 0x788 + i * 8),
                                    'player_address': hex(ptr) if ptr else None,
                                    'name': known.get('name') if known else None})
        if allow_unlabeled and sum(s.get('readable_stable', False) and bool(s.get('first_name') or s.get('last_name')) for s in slots) < 8:
            continue
        teams.append({'team_identity_token': token or None, 'raw_identity_token': token,
                      'team_identity_status': 'TOKEN_OBSERVED' if token else 'UNRESOLVED_NO_TOKEN',
                      'stadium_token': stadium,
                      'team_id': None, 'team_id_status': 'UNMAPPED',
                      'unidentified_header_u32': {'relative_offset': '-0x2d4', 'value': reader.u32(base - 0x2d4)},
                      'reference_array_address': hex(base), 'reference_array_rva': hex(base - reader.main_module.base_address),
                      'reference_array_sha256': sha(table), 'reference_array_raw_hex': table.hex(),
                      'inspected_reference_count': 20, 'capacity': None, 'capacity_status': 'UNVERIFIED',
                      'occupied_references': sum(not s['empty_reference'] for s in slots), 'slots': slots,
                      'lineup_reference_candidates': lineup_refs,
                      'reference_resolution_status': 'DIRECT_POINTERS_READ_AND_STABILITY_CHECKED',
                      'membership_control_status': 'UNVERIFIED', 'rotation_control_status': 'UNVERIFIED',
                      'injection_ready': False})
    return teams
