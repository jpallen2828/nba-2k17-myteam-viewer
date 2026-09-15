"""Read-only NBA 2K17 record and reference discovery.

Candidate player-array proximity is never interpreted as team membership.
Every run rereads the selected roots and records its exact build and coverage.
"""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import struct

from .roster_record import (ROSTER_PLAYER_RECORD_SIZE, SELECTED_RECORD_POINTER_RVAS,
                            ROSTER_SELECTION_INDEX_RVAS, utf16_fixed_field)

EXPECTED_SHA256 = '86c9d5396761e64567f8ec6233bd1ccb58f483d81455d055bb5c6c9444076a35'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def identify_candidate(raw):
    if len(raw) != ROSTER_PLAYER_RECORD_SIZE:
        return None
    first = utf16_fixed_field(raw, 0x24, 0x28)
    last = utf16_fixed_field(raw, 0, 0x24)
    def valid(s):
        return len(s) < 22 and all(c.isalpha() or c in " .'-" for c in s)
    if not (first or last) or not valid(first) or not valid(last):
        return None
    rating_bytes = raw[0x3b1:0x3ef]
    if sum(x <= 222 and x % 3 == 0 for x in rating_bytes) < 60:
        return None
    if any(v > 100 for v in raw[0x3f1:0x445]):
        return None
    return {'first_name': first, 'last_name': last, 'name': (first + ' ' + last).strip(),
            'standing_layup': raw[0x3b1] // 3 + 25 if raw[0x3b1] <= 222 and raw[0x3b1] % 3 == 0 else None,
            'team': None, 'position': None, 'jersey_number': None, 'height': None,
            'player_id': None, 'team_membership_status': 'UNRESOLVED'}


def selected_records(reader):
    base = reader.main_module.base_address
    roots = {hex(r): reader.pointer(base + r) for r in SELECTED_RECORD_POINTER_RVAS}
    populated = {v for v in roots.values() if v}
    if len(populated) != 1:
        raise ValueError('Selected-record roots are absent or disagree')
    address = next(iter(populated))
    raw = reader.read_or_raise(address, ROSTER_PLAYER_RECORD_SIZE)
    identity = identify_candidate(raw)
    if identity is None or raw != reader.read_or_raise(address, len(raw)):
        raise ValueError('Selected record is invalid or changing')
    return roots, address, raw, identity


def record_candidates(reader, anchor, radius):
    rows, unstable = [], []
    for relative in range(-radius, radius + 1):
        address = anchor + relative * ROSTER_PLAYER_RECORD_SIZE
        if address < 0:
            continue
        raw = reader.read(address, ROSTER_PLAYER_RECORD_SIZE)
        identity = identify_candidate(raw) if raw else None
        if identity is None:
            continue
        if raw != reader.read(address, len(raw)):
            unstable.append(hex(address))
            continue
        rows.append({**identity, 'address': hex(address), 'relative_candidate_index': relative,
                     'record_sha256': sha(raw), 'raw_hex': raw.hex(),
                     'classification': 'STRUCTURALLY_VALID_CANDIDATE; NOT_TEAM_RESOLVED'})
    return rows, unstable


def pointer_references(reader, records, byte_budget, progress=None):
    """Search aligned references; report coverage and retain candidates as such."""
    import numpy as np
    addresses = {int(r['address'], 16): r['name'] for r in records}
    if not addresses:
        return [], [], [], 0
    low, high = min(addresses), max(addresses)
    regions = [r for r in reader.readable_regions()
               if r.category in ('main_module', 'private_memory') and r.protect & 255 in (4, 8, 64, 128)]
    # Main image first, then small allocations. Large graphics allocations get
    # lower priority; omitted spans are reported rather than claimed scanned.
    regions.sort(key=lambda r: (r.category != 'main_module', r.region_size, r.base_address))
    used, hits, coverage = 0, [], []
    for region in regions:
        offset = 0
        while offset < region.region_size and used < byte_budget:
            size = min(8 * 1024 * 1024, region.region_size - offset, byte_budget - used)
            address = region.base_address + offset
            raw = reader.read(address, size, allow_partial=True)
            used += size
            coverage.append({'address': hex(address), 'requested_bytes': size,
                             'read_bytes': len(raw) if raw else 0, 'category': region.category})
            if raw:
                start = (-address) % 8
                end = start + (len(raw) - start) // 8 * 8
                if end > start:
                    values = np.frombuffer(raw[start:end], dtype='<u8')
                    for index in np.flatnonzero((values >= low) & (values <= high)):
                        target = int(values[index])
                        if target in addresses:
                            hits.append({'reference_address': hex(address + start + int(index) * 8),
                                         'target_address': hex(target), 'name': addresses[target],
                                         'category': region.category})
            offset += size
        if progress:
            progress(used, len(hits))
        if used >= byte_budget:
            break
    hits.sort(key=lambda h: int(h['reference_address'], 16))
    clusters, group = [], []
    for hit in hits:
        if group and int(hit['reference_address'], 16) - int(group[-1]['reference_address'], 16) > 256:
            if len({h['target_address'] for h in group}) >= 3:
                clusters.append(group)
            group = []
        group.append(hit)
    if len({h['target_address'] for h in group}) >= 3:
        clusters.append(group)
    return hits, clusters, coverage, sum(r.region_size for r in regions)


def scan(reader, fingerprint, *, radius=4096, reference_budget=256 * 1024 * 1024, context=None, progress=None):
    if fingerprint['sha256'].lower() != EXPECTED_SHA256:
        raise ValueError('Unrecognized NBA 2K17 build; mapping rediscovery required')
    roots, selected, raw, identity = selected_records(reader)
    rows, unstable = record_candidates(reader, selected, radius)
    refs, groups, coverage, available = pointer_references(reader, rows, reference_budget, progress)
    after_roots, after_address, after_raw, _ = selected_records(reader)
    stable = after_roots == roots and after_address == selected and after_raw == raw
    return {'format': 'nba2k17-roster-discovery/v1', 'created_at': datetime.now(timezone.utc).isoformat(),
            'access': 'READ_ONLY', 'pid': reader.pid, 'module_base': hex(reader.main_module.base_address),
            'executable': fingerprint, 'user_context': context or {}, 'selected_roots': roots,
            'selected_player': {**identity, 'address': hex(selected), 'record_sha256': sha(raw)},
            'selection_indices_unverified': {hex(r): reader.u32(reader.main_module.base_address + r)
                                            for r in ROSTER_SELECTION_INDEX_RVAS},
            'selection_stable_through_scan': stable, 'candidate_records': rows, 'unstable_records': unstable,
            'pointer_references': refs, 'reference_clusters': groups, 'reference_scan_coverage': coverage,
            'reference_scan_available_bytes': available,
            'reference_scan_requested_bytes': sum(c['requested_bytes'] for c in coverage),
            'reference_scan_read_bytes': sum(c['read_bytes'] for c in coverage),
            'reference_scan_limitations': 'Aligned 64-bit direct pointers only; index/ID tables and unaligned references are not resolved.',
            'duplicate_names': {n: count for n, count in Counter(r['name'] for r in rows).items() if count > 1},
            'team_rosters': [], 'membership_status': 'UNRESOLVED', 'rotation_status': 'UNRESOLVED',
            'injection_ready': False,
            'blockers': ['Team membership references and rotation references not verified.',
                         'Player names and array adjacency cannot resolve a destination slot.',
                         'Position, jersey, height and player ID mappings still require validation.'] +
                        ([] if stable else ['Selection or record changed during scan; repeat before planning.'])}
