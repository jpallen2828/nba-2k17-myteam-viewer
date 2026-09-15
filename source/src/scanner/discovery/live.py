"""Read-only discovery epochs and watched regions; no native write API."""
from bisect import bisect_right
import ctypes
from ctypes import wintypes
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import uuid

from scanner.fingerprint import fingerprint_file
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS, utf16_fixed_field
from scanner.roster_scan import EXPECTED_SHA256
from scanner.team_references import inspect_teams
from scanner.windows_process import ProcessReader
from .core import digest, utc


def process_token(reader):
    times = [wintypes.FILETIME() for _ in range(4)]
    fn = ctypes.WinDLL('kernel32', use_last_error=True).GetProcessTimes
    fn.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    fn.restype = wintypes.BOOL
    if not fn(reader.handle, *(ctypes.byref(t) for t in times)):
        raise ctypes.WinError(ctypes.get_last_error())
    return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)


def expand_teams(reader, scan):
    """Test 2K17-observed spacing; verify every token/table, never assume membership."""
    initial = inspect_teams(reader, scan)
    if not initial:
        raise ValueError('No candidate team directory found')
    anchor = int(initial[0]['reference_array_address'], 16)
    clusters = []
    rejected = []
    for i in range(-512, 513):
        base = anchor + i * 0xc68
        token = reader.utf16le(base + 0xde, 64)
        stadium = reader.utf16le(base + 0xa0, 60)
        if token is None or (token and not re.fullmatch(r'[a-z0-9_]+', token)):
            continue
        if not stadium or not re.fullmatch(r'[a-z0-9_]+', stadium):
            continue
        table = reader.read(base, 160)
        if not table or table != reader.read(base, 160):
            continue
        pointers = struct.unpack('<20Q', table)
        if not pointers[0] or not 8 <= sum(bool(x) for x in pointers) <= 20:
            rejected.append({'address': hex(base), 'token': token, 'reason': 'outside current table-shape hypothesis'})
            continue
        clusters.append([{'reference_address': hex(base + n * 8), 'target_address': hex(ptr)}
                         for n, ptr in enumerate(pointers) if ptr])
    expanded = inspect_teams(reader, {**scan, 'reference_clusters': clusters}, allow_unlabeled=True)
    unique = {t['reference_array_address']: t for t in initial + expanded}
    return list(unique.values()), rejected


def prepare(root, session, roster_file, team, player, family, inventory, linked_specs=None):
    from .inventory import make_plan
    root, session = Path(root).resolve(), Path(session).resolve()
    session.mkdir(parents=True, exist_ok=True)
    epoch = 'epoch-' + uuid.uuid4().hex[:12]
    folder = session / epoch; folder.mkdir()
    scan_file = folder / 'roster-scan.json'
    subprocess.run([sys.executable, str(root / '.project/scripts/scan_live_roster.py'), '--output', str(scan_file),
                    '--reference-mib', '512', '--radius', '4096', '--roster-name', str(roster_file),
                    '--team', team, '--player', player], check=True, capture_output=True, text=True)
    scan = json.loads(scan_file.read_text())
    if not scan['selection_stable_through_scan']:
        raise ValueError('Selection changed during discovery; repeat prepare')
    with ProcessReader(scan['pid']) as reader:
        teams, rejected = expand_teams(reader, scan)
        targets = [t for t in teams if t['team_identity_token'] == team]
        if len(targets) != 1:
            raise ValueError('Chosen team token is missing or ambiguous')
        slots = [s for s in targets[0]['slots'] if s.get('name') == player and s.get('readable_stable')]
        if len(slots) != 1:
            raise ValueError('Chosen player is missing or ambiguous within chosen team')
        target = slots[0]
        if target['player_address'] != scan['selected_player']['address']:
            raise ValueError('Selected editor player is not the resolved team player')
        records = {s['player_address']: s for t in teams for s in t['slots'] if s.get('readable_stable')}
        all_records = {r['address']: r for r in scan['candidate_records']}
        for address, rec in records.items():
            all_records[address] = {**rec, 'address': address}
        regions = reader.readable_regions()
        starts = [r.base_address for r in regions]
        candidates = []
        for address, rec in all_records.items():
            raw = bytes.fromhex(rec['raw_hex'])
            for offset in range(0x50, 0xc8, 8):
                ptr = struct.unpack_from('<Q', raw, offset)[0]
                index = bisect_right(starts, ptr) - 1
                if index >= 0 and ptr < regions[index].end_address:
                    candidates.append({'player_address': address, 'pointer_offset': hex(offset),
                                       'diagnostic_target': hex(ptr), 'region_bytes_remaining': regions[index].end_address-ptr,
                                       'status': 'POINTER_CANDIDATE_EXTENT_AND_OWNERSHIP_UNVERIFIED'})
        token = process_token(reader)
        # Fresh complete player reads, not merely candidate hashes from the broad scan.
        for rec in all_records.values():
            address = int(rec['address'], 16)
            raw = reader.read_or_raise(address, 0x460)
            if raw != reader.read_or_raise(address, 0x460):
                raise ValueError('Roster changed while snapshotting; repeat discovery')
            rec['raw_hex'], rec['record_sha256'] = raw.hex(), digest(raw)
    roster_file = Path(roster_file).resolve()
    binding = {'format': 'nba2k17-discovery-epoch/v1', 'created_at': utc(), 'epoch': epoch,
               'pid': scan['pid'], 'process_start_token': token, 'module_base': scan['module_base'],
               'executable_sha256': scan['executable']['sha256'], 'player_address': target['player_address'],
               'player_name': player, 'team_token': team, 'slot_reference': target['reference_address'],
               'roster_file': str(roster_file), 'roster_sha256': digest(roster_file.read_bytes()),
               'roster_stat': [roster_file.stat().st_size, roster_file.stat().st_mtime_ns],
               'teams': [{'token': t.get('raw_identity_token', t['team_identity_token']),
                          'stadium': t['stadium_token'], 'address': t['reference_array_address']} for t in teams],
               'linked_specs': linked_specs or [], 'family': family, 'write_enabled': False,
               'rebind_policy': 'Pause before every roster load/restart and Rebind afterward, even if all bytes/addresses appear unchanged.'}
    team_addresses = {t['reference_array_address'] for t in teams}
    binding['linked_probe_windows'] = [{'pointer_offset': int(c['pointer_offset'], 16), 'length': 256,
                                        'status': 'UNVERIFIED_WINDOW_NOT_COMPLETE_BLOCK'}
                                       for c in candidates if c['player_address'] == target['player_address']
                                       and c['region_bytes_remaining'] >= 256 and c['diagnostic_target'] not in team_addresses]
    snapshot = {'format': 'nba2k17-discovery-roster-snapshot/v1', 'created_at': utc(), 'binding': binding,
                'teams': teams, 'records': list(all_records.values()), 'linked_pointer_candidates': candidates,
                'coverage': {'team_candidates': len(teams), 'unique_team_referenced_records': len(records),
                             'total_record_candidates': len(all_records), 'rejected_team_hypotheses': rejected,
                             'complete_loaded_roster_proven': False,
                             'limitations': ['Directory extent/count and 20-slot capacity are not verified.',
                                             'Record pool scan is bounded; candidates without a team reference remain unresolved.',
                                             'No unverified linked block lengths are captured or replayed.']}}
    for name, payload in [('binding.json', binding), ('roster-snapshot.json', snapshot), ('plan.json', make_plan(inventory, family))]:
        (folder / name).write_text(json.dumps(payload, indent=2), encoding='utf-8')
    (session / 'current.json').write_text(json.dumps({'epoch': epoch}), encoding='utf-8')
    return folder


def next_batch(session, family, inventory, *, team=None, player=None, checkpoint_output=None):
    """Reuse the validated discovery for the same loaded roster, not another scan."""
    from .inventory import make_plan
    from .analysis import batch_report
    session = Path(session)
    previous = json.loads((session / 'current.json').read_text())['epoch']
    binding = json.loads((session / previous / 'binding.json').read_text())
    report_epoch = binding.get('restored_previous_epoch', previous)
    report, _ = batch_report(session, report_epoch)
    source = LiveSource(binding)
    target_update = {}
    try:
        current, metadata = source.sample()
        if team or player:
            team = team or binding['team_token']; player = player or binding['player_name']
            teams = [t for t in binding['teams'] if t['token'] == team]
            if len(teams) != 1:
                raise ValueError('Requested team is missing or ambiguous in this discovery')
            base = int(teams[0]['address'],16)
            pointers = struct.unpack('<20Q',source.reader.read_or_raise(base,160))
            matches = []
            for i, address in enumerate(pointers):
                if not address: continue
                raw = source.reader.read_or_raise(address,0x460)
                name = (utf16_fixed_field(raw,0x24,0x28)+' '+utf16_fixed_field(raw,0,0x24)).strip()
                if name == player and raw == source.reader.read_or_raise(address,0x460):
                    matches.append((i,address))
            if len(matches)!=1:
                raise ValueError('Requested player is missing or ambiguous within team')
            index,address=matches[0]
            if source.reader.pointer(source.reader.main_module.base_address+SELECTED_RECORD_POINTER_RVAS[0]) != address:
                raise ValueError('Select the requested player in the game before binding')
            target_update = {'player_name':player,'player_address':hex(address),'team_token':team,
                             'slot_reference':hex(base+index*8),'linked_specs':[],
                             'linked_probe_windows':[], 'new_player_linked_scope':'Investigate this player\'s pointers before adding complete linked blocks'}
        elif not metadata['canonical_selection_matches']:
            raise ValueError('Select the bound player before starting the next batch')
    finally:
        source.close()
    external_restored = False
    if checkpoint_output is not None:
        # Computer Use checkpoints have their own durable journal. Trust its
        # before-images and fresh bytes, not merely a restoration report flag.
        from .core import Journal
        journal = Journal(Path(checkpoint_output) / 'recorder.sqlite')
        try:
            history = journal.events(previous)
            baselines = [e for e in history if e['kind'] == 'BASELINE']
            restored = [e for e in history if e.get('capture_kind') == 'restored']
            if baselines and restored and history[-1]['id'] == restored[-1]['id']:
                original, final = journal.regions(baselines[0]), journal.regions(restored[-1])
                keys = [k for k in original if not k.startswith(('editor:', 'probe:'))]
                external_restored = all(final.get(k) == original[k] for k in keys)
                if external_restored:
                    source = LiveSource(binding)
                    try:
                        fresh, _ = source.sample()
                        for key in keys:
                            raw = (source.reader.read_or_raise(int(key.split(':',1)[1],16),len(original[key]))
                                   if key.startswith(('roster:', 'roster-probe:')) else fresh.get(key))
                            if raw != original[key]:
                                external_restored = False
                                break
                    finally:
                        source.close()
        finally:
            journal.close()
        if not external_restored:
            raise ValueError('Automated batch lacks a latest restored checkpoint with matching fresh before-images')
    if not external_restored and (not report['batch_restore_step_captured'] or not report['canonical_and_verified_regions_restored']):
        from .core import Journal
        journal = Journal(session / 'recorder.sqlite')
        try:
            history = journal.events(previous)
            baselines = [e for e in history if e['kind'] == 'BASELINE']
            unchanged_unstarted = bool(baselines) and not any(e['kind'] in ('ACCEPTED_EDIT', 'SKIPPED_STEP') for e in history)
            if unchanged_unstarted:
                baseline = journal.regions(baselines[0])
                unchanged_unstarted = all(current.get(k) == v for k,v in baseline.items() if not k.startswith(('editor:', 'probe:')))
            if not unchanged_unstarted:
                raise ValueError('Finish and restore the current batch before starting another family; use Rebind after a roster load')
        finally:
            journal.close()
    epoch = 'epoch-' + uuid.uuid4().hex[:12]
    folder = session / epoch; folder.mkdir()
    binding.update(epoch=epoch, family=family, created_at=utc(),
                   source_discovery_epoch=binding.get('source_discovery_epoch', previous))
    binding.update(target_update)
    binding.pop('restored_previous_epoch', None)
    (folder / 'binding.json').write_text(json.dumps(binding, indent=2), encoding='utf-8')
    plan = make_plan(inventory, family)
    (folder / 'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    (folder / 'checklist.md').write_text('# ' + family + '\n\n' + '\n'.join(f'{i}. {s["instruction"]}' for i,s in enumerate(plan['steps'],1)), encoding='utf-8')
    (session / 'current.json').write_text(json.dumps({'epoch': epoch}), encoding='utf-8')
    return folder


class LiveSource:
    def __init__(self, binding):
        self.binding = binding
        self.reader = ProcessReader(binding['pid']); self.reader.open()
        fp = fingerprint_file(Path(self.reader.process_info().executable_path)).to_dict()
        if fp['sha256'].lower() != EXPECTED_SHA256 or fp['sha256'] != binding['executable_sha256']:
            self.close(); raise ValueError('Build changed; rebind required')
        if process_token(self.reader) != binding['process_start_token'] or hex(self.reader.main_module.base_address) != binding['module_base']:
            self.close(); raise ValueError('Process instance changed; rebind required')
        self.target = int(binding['player_address'], 16)
        self.link_targets = None

    def sample(self):
        r, b = self.reader, self.binding
        roster = Path(b['roster_file']); st = roster.stat()
        if [st.st_size, st.st_mtime_ns] != b['roster_stat']:
            raise ValueError('Roster file changed; pause and Rebind')
        raw = r.read_or_raise(self.target, 0x460)
        result = {'player': raw}; addresses = {'player': hex(self.target)}
        references = []
        for team in b['teams']:
            base = int(team['address'], 16)
            if r.utf16le(base + 0xde, 64) != team['token']:
                raise ValueError('Team identity changed; Rebind')
            if team.get('stadium') and r.utf16le(base + 0xa0, 60) != team['stadium']:
                raise ValueError('Team stadium token changed; Rebind')
            name = 'team:' + team['address']
            table = r.read_or_raise(base, 160)
            if self.target in struct.unpack('<20Q', table):
                references.append(team['token'])
            # Capture known team neighborhoods, including all rotation candidate bytes.
            result[name] = r.read_or_raise(base - 0x2d8, 0xc68)
            addresses[name] = hex(base - 0x2d8)
        if not references:
            raise ValueError('Player no longer referenced by the discovered teams; Rebind')
        roots = {}
        for rva in SELECTED_RECORD_POINTER_RVAS:
            ptr = r.pointer(r.main_module.base_address + rva)
            roots[hex(rva)] = hex(ptr) if ptr else None
            if ptr and ptr != self.target:
                block = r.read(ptr, 0x460)
                if block is not None:
                    result['editor:' + hex(rva)] = block
                    addresses['editor:' + hex(rva)] = hex(ptr)
        links = {}
        for spec in b.get('linked_specs', []):
            evidence = Path(spec['evidence_file'])
            if spec.get('status') != 'CAPTURE_EXTENT_VERIFIED' or digest(evidence.read_bytes()) != spec['evidence_sha256']:
                raise ValueError('Linked capture extent lacks matching evidence')
            off, length = int(spec['pointer_offset']), int(spec['length'])
            if not 0 <= off <= 0x458 or not 0 < length <= 1048576:
                raise ValueError('Invalid linked capture definition')
            ptr = struct.unpack_from('<Q', raw, off)[0]
            key = 'linked:' + spec['name']; links[key] = ptr
            result[key] = r.read_or_raise(ptr, length); addresses[key] = hex(ptr)
        if self.link_targets is not None and links != self.link_targets:
            raise ValueError('Linked allocation changed; investigate ownership and Rebind')
        self.link_targets = links
        probe_meta = []
        for spec in b.get('linked_probe_windows', []):
            ptr = struct.unpack_from('<Q', raw, spec['pointer_offset'])[0]
            key = 'probe:' + hex(spec['pointer_offset'])
            block = r.read(ptr, spec['length']) if ptr else None
            stable = block is not None and block == r.read(ptr, spec['length'])
            probe_meta.append({'pointer_offset': hex(spec['pointer_offset']), 'diagnostic_target': hex(ptr),
                               'readable_stable': stable, 'status': 'UNVERIFIED_WINDOW_NOT_COMPLETE_BLOCK'})
            if stable:
                result[key] = block; addresses[key] = hex(ptr)
        for name, block in result.items():
            if not name.startswith(('editor:', 'probe:')) and block != r.read_or_raise(int(addresses[name], 16), len(block)):
                raise ValueError('Regions changing during read; capture withheld')
        return result, {'diagnostic_addresses_never_replay': addresses, 'selected_roots': roots,
                        'canonical_selection_matches': roots.get(hex(SELECTED_RECORD_POINTER_RVAS[0])) == hex(self.target),
                        'team_references_to_player': references, 'linked_targets_diagnostic': links,
                        'unverified_linked_window_probes': probe_meta,
                        'linked_regions_missing': not bool(b.get('linked_specs'))}

    def close(self):
        self.reader.close()
