"""Live team-slot injection, with explicit field coverage and reversible writes.

Stored templates contain values, never source addresses. Team tables and linked
destination storage are validated anew for every process/roster binding.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import struct
import uuid
from functools import lru_cache
from scanner.injection_metrics import Metrics, measured

from nba2k17_scraper.injection_exporter import resolve_player_values, OVERRIDE_SECTIONS
from .roster_source_matching import player_key


from .runtime_fields import runtime_definition


def find_roster_files(directory):
    directory = Path(os.path.expandvars(str(directory))).expanduser().resolve()
    if not directory.is_dir():
        raise ValueError('Choose the folder containing your NBA 2K17 roster files.')
    return sorted((p for p in directory.iterdir() if p.is_file() and re.fullmatch(r'roster\d+', p.name, re.I)),
                  key=lambda p: (-p.stat().st_mtime_ns, p.name))


def default_roster_directory():
    from .install_paths import steam_roots
    candidates = []
    for root in steam_roots():
        candidates.extend(sorted((root / 'userdata').glob('*/385760/remote')))
    shared = Path(os.environ.get('PUBLIC', r'C:\Users\Public')) / 'Documents/Steam'
    candidates.extend(sorted(shared.glob('*/385760/remote')))
    for path in candidates:
        try:
            if path.is_dir() and find_roster_files(path):
                return path
        except OSError:
            continue
    return next((p for p in candidates if p.is_dir()), Path.home() / 'Documents')


def injection_workspace():
    base = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'NBA2K17MyTEAM/Injection reports'
    base.mkdir(parents=True, exist_ok=True)
    return base


def _name(raw):
    from scanner.roster_record import utf16_fixed_field
    first = utf16_fixed_field(raw, 0x24, 0x24)
    last = utf16_fixed_field(raw, 0, 0x24)
    name = (first + ' ' + last).strip()
    return name or 'Unnamed slot'


def profile_overall(profile):
    value = profile.get('source_card_identity', {}).get('overall')
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 25 <= value <= 99:
        raise ValueError('Missing or invalid saved card overall; cannot rank Blacktop injection.')
    return value


def ranked_profiles(profiles):
    # Stable ties preserve the displayed draft order.
    return sorted(profiles, key=profile_overall, reverse=True)


def live_overall(raw):
    # Build-verified native cached normalized OVR, independently validated on
    # all 1,418 team-referenced records. Read only; never force recalculation.
    value = struct.unpack_from('<f', raw, 0x150)[0]
    if not math.isfinite(value) or not .25 <= value <= 1:
        raise ValueError('Live destination overall is unavailable. Reopen the player browser and scan again.')
    return min(99, math.floor(value * 100 + .5))


def select_roster_process(processes, registry, *, progress=None, metrics=None):
    """Distinguish a loaded game from a same-named launcher/background process.

    These are bounded, read-only probes of mapped team references, not discovery
    or ownership sweeps. The chosen process still undergoes the full executable
    fingerprint, roster discovery and ownership validation in RosterSession.
    """
    from scanner.windows_process import ProcessReader
    if not processes:
        raise ValueError('NBA 2K17 is not running. Open the game and load your roster, then scan again.')
    if len(processes) == 1:
        return processes[0][0]
    if progress:
        progress(f'Found {len(processes)} NBA 2K17 processes; checking which has a loaded roster...')
    teams = registry['teams']
    probes = [teams[i] for i in sorted({0, len(teams) // 2, len(teams) - 1})]
    matches, rejected = [], []
    for pid, _ in processes:
        try:
            with ProcessReader(pid) as reader:
                reader.metrics = metrics
                if metrics:
                    metrics.count('process_candidate_probes')
                    metrics.count('process_handles_opened')
                for team in probes:
                    base = reader.main_module.base_address + team['rva']
                    if (reader.utf16le(base + 0xde, 64) != team['token']
                            or reader.utf16le(base + 0xa0, 60) != team['stadium']
                            or reader.utf16le(base + 0x24e, 36) != team['city']
                            or reader.utf16le(base + 0x21c, 50) != team['nickname']):
                        raise ValueError('verified team directory is not loaded')
                    pointers = [p for p in struct.unpack('<20Q', reader.read_or_raise(base, 160)) if p]
                    if not 8 <= len(pointers) <= 20 or len(set(pointers)) != len(pointers):
                        raise ValueError('team has no valid loaded roster references')
                    for pointer in (pointers[0], pointers[-1]):
                        _name(reader.read_or_raise(pointer, 0x460))
                matches.append(pid)
        except (OSError, ValueError, RuntimeError) as exc:
            rejected.append(f'PID {pid}: {exc}')
    if len(matches) == 1:
        if progress:
            progress(f'Using NBA 2K17 PID {matches[0]} with the loaded roster; other background processes were excluded.')
        return matches[0]
    if len(matches) > 1:
        raise ValueError('Multiple NBA 2K17 games have loaded rosters (PIDs '
                         + ', '.join(map(str, matches))
                         + '). Close the extra game instance, then scan again. No process was selected or changed.')
    raise ValueError('NBA 2K17 processes were found, but none has a readable roster matching the verified layout. '
                     'Finish loading the roster and scan again. ' + '; '.join(rejected))


def compile_stored_card(profile, destination, registry):
    """Compile a card without inheriting player values from its destination."""
    from scanner.player_codec import preview_record, complete_badge_values
    stored = profile.get('reusable_roster_template')
    if not stored:
        raise ValueError('This card has no selected reusable player template.')
    if stored['executable_sha256'].lower() != registry['executable_sha256'].lower():
        raise ValueError('The card template belongs to another game build.')
    defaults = {k: deepcopy(v) for k, v in stored['defaults'].items() if k in OVERRIDE_SECTIONS}
    # Promote the source player's captured hand values using the controlled
    # mapping. Card-authored/confirmed hand values retain override priority.
    for field in registry['fields']:
        if field['field'] in ('gameplay_metadata.dominant_hand', 'gameplay_metadata.dominant_dunk_hand'):
            key = field['field'].split('.', 1)[1]
            value = stored.get('ct_candidate_values', {}).get(field['reference_uid'])
            if type(value) is not int or not 0 <= value <= field['raw_max']:
                raise ValueError('Missing or invalid captured source ' + key)
            defaults.setdefault('gameplay_metadata', {})[key] = value
    resolved = resolve_player_values(profile, defaults, preserve_durability=True)
    from .sculpted_appearance import profile_appearance
    native_appearance = profile_appearance(profile, resolved, registry)
    if native_appearance:
        resolved['values'].setdefault('gameplay_metadata', {})['skin_type'] = native_appearance['skin_type']
    from .shoe_transfer import resolve_shoes
    resolve_shoes(resolved['values'], require_accessories=True, source_template=stored)
    # User-authorized commentary experiment: transfer the exact source identity,
    # never a face-ID guess or the destination's ID. Kept in normal field patches
    # so preflight, readback and transaction undo cover these two bytes as well.
    commentary_test = registry.get('commentary_identity_test')
    if commentary_test:
        source_id = commentary_test['source_values'].get(stored.get('source_record_sha256'))
        if type(source_id) is not int or not 0 <= source_id <= 65535:
            raise ValueError('Missing captured source identity for the commentary test.')
        resolved['values'].setdefault('gameplay_metadata', {})['commentary_identity_test'] = source_id
    # Native speech lookup reads a separate signed word at +0x200. Copy its
    # exact source bits independently of D4/D6, including the -1 sentinel.
    from .speech_identity import source_value
    resolved['values'].setdefault('gameplay_metadata', {})['speech_id'] = source_value(
        stored, registry['speech_identity'])
    badges = complete_badge_values(profile, registry, clear_unlisted=True, unknown_as_none=True)
    if badges['unresolved']:
        raise ValueError('Card badge states are explicitly unknown: ' + ', '.join(badges['unresolved']))
    resolved['values']['badges'] = badges['values']
    identity = resolved['values'].get('identity', {})
    full = identity.pop('name', None)
    template_name = (identity.get('first_name', '') + ' ' + identity.get('last_name', '')).strip()
    if full and player_key(full) != player_key(template_name):
        raise ValueError('Card identity differs from the selected player template.')
    # Reuse the catalog's established aliases without changing an era-specific
    # card name. A corrupted page title keeps the roster's exact spelling.
    if full == 'Ron Artest' and template_name == 'Metta World Peace':
        identity['first_name'], identity['last_name'] = 'Ron', 'Artest'
    name_presentation = None
    shai_name = 'Shai Gilgeous-Alexander'
    shai_key = player_key(shai_name)
    card_name = profile.get('source_card_identity', {}).get('name')
    if (player_key(full or template_name) == shai_key
            and (not card_name or player_key(card_name) == shai_key)):
        # The native surname array holds seventeen UTF-16 units plus its NUL.
        # Keep the complete authored name; only the transient injection plan
        # uses the compact surname recognized by the companion name hook.
        for key, value in (('first_name', 'Shai'), ('last_name', 'GilgeousAlexander')):
            authored_value = identity.get(key)
            identity[key] = value
            resolved['origins']['identity.' + key] = {
                'layer': 'explicit_policy', 'policy': 'SHAI_COMPACT_NATIVE_NAME',
                'authored_value': authored_value, 'storage_value': value}
        face_id = resolved['values'].get('gameplay_metadata', {}).get('face_id')
        name_presentation = {
            'policy': 'SHAI_COMPACT_NATIVE_NAME', 'authored_name': shai_name,
            'native_first_name': 'Shai', 'native_last_name': 'GilgeousAlexander',
            'native_full_name': 'Shai GilgeousAlexander',
            'requires_companion_hook': True, 'hook_face_id': 7710,
            'injected_face_id': face_id, 'hook_face_id_matches': face_id == 7710,
            'presentation_verified': False,
            'description': 'Native storage uses Shai GilgeousAlexander. The full hyphenated '
                           'menu name and jersey surname require the active companion name hook '
                           'and face ID 7710. Memory readback does not verify on-screen presentation.'}
    if 'last_name' in identity:
        # Native GetLastName resolves +0x48 before consulting the inline name.
        # Clear that selector deliberately as its own previewed/undoable field;
        # a first-name write must never touch it through an oversized buffer.
        identity['localized_last_name_crc'] = 0
        resolved['origins']['identity.localized_last_name_crc'] = {
            'layer': 'explicit_policy', 'policy': 'INLINE_SURNAME_DISPLAYS_SAVED_CARD_NAME'}
    definitions = {f['field']: f for f in registry['fields']}
    # Every card-owned rating/zone is required; absent is never destination data.
    for section in ('attributes', 'tendencies', 'hot_zones'):
        missing = [f.split('.', 1)[1] for f in definitions if f.startswith(section + '.')
                   and f.split('.', 1)[1] not in resolved['values'].get(section, {})]
        if missing:
            raise ValueError('Missing card ' + section + ': ' + ', '.join(missing))
    if len(resolved['values'].get('signatures', {})) != 44:
        raise ValueError('A complete 44-value signature template is required.')
    preview = preview_record(destination, resolved['values'], registry)
    errors = [g for g in preview['unsupported'] if g['field'] in definitions]
    if errors:
        raise ValueError('Cannot encode card fields: ' + json.dumps(errors))
    preview['resolved'] = resolved
    preview['badge_policy'] = badges
    preview['template_id'] = stored['template_id']
    preview['capture_sha256'] = stored['capture_sha256']
    preview['native_sculpt_appearance'] = native_appearance
    if name_presentation:
        preview['name_presentation'] = name_presentation
    return preview


def position_slot_assignment(source_positions, destination_positions):
    """Maximize preferred matches across the whole lineup, with stable slot ties."""
    def position(value):
        if isinstance(value, str):
            value = value.strip().upper()
            return value if value in ('PG', 'SG', 'SF', 'PF', 'C') else None
        return ('PG', 'SG', 'SF', 'PF', 'C')[value] if type(value) is int and 0 <= value < 5 else None
    sources = [tuple(position(v) for v in pair) for pair in source_positions]
    targets = [tuple(position(v) for v in pair) for pair in destination_positions]
    if len(sources) > len(targets):
        raise ValueError('Not enough occupied destination slots.')
    base = len(sources) + 1
    def score(source, target):
        p, s = source
        tp, ts = target
        if p is not None and p == tp: return base ** 4
        if s is not None and s == tp: return base ** 3
        if p is not None and p == ts: return base ** 2
        if s is not None and s == ts: return base
        return 0
    @lru_cache(None)
    def assign(index, used):
        if index == len(sources): return 0, ()
        best = None
        for slot, target in enumerate(targets):
            if used & (1 << slot): continue
            rest_score, rest = assign(index + 1, used | (1 << slot))
            candidate = (score(sources[index], target) + rest_score, (slot,) + rest)
            if best is None or candidate[0] > best[0] or (candidate[0] == best[0] and candidate[1] < best[1]):
                best = candidate
        return best
    try:
        return list(assign(0, 0)[1])
    finally:
        # Recursive cached functions form a cycle; release the search table now.
        assign.cache_clear()


def blacktop_selection(reader, module_base, teams):
    """Resolve the player-browser selection through two observed native roots.

    Blacktop and Play Now share canonical team records. These roots identify
    the selected live team, not a unique game-mode flag or a private copy.
    """
    roots = (0x2233220, 0x673d160)
    pointers = [struct.unpack('<Q', reader.read_or_raise(module_base + rva, 8))[0] for rva in roots]
    if not pointers[0] or pointers[0] != pointers[1]:
        raise ValueError('Blacktop player selection is not stable. Open the Blacktop player browser and scan again.')
    matches = [t for t in teams if t['enabled'] and not t['deferred_all_star']
               and any(s['player'] == pointers[0] for s in t['slots'])]
    if len(matches) != 1:
        raise ValueError('The selected Blacktop player does not resolve to one supported NBA team. Select a current or historic team and scan again.')
    # A second read detects selection changes during this bounded probe.
    if any(reader.read_or_raise(module_base + rva, 8) != struct.pack('<Q', pointers[0]) for rva in roots):
        raise ValueError('Blacktop selection changed while scanning. Scan again.')
    return {'team_key': matches[0]['key'], 'team': matches[0]['label'],
            'selected_player': pointers[0], 'root_rvas': list(roots)}


class RosterSession:
    def __init__(self, roster_file, *, pid=None, progress=None, metrics=None, cancel_event=None, live_mode='roster'):
        from scanner.windows_process import ProcessReader, find_processes
        from scanner.fingerprint import fingerprint_file
        from scanner.discovery.live import process_token
        self.progress = progress or (lambda message: None)
        self.metrics = metrics or Metrics()
        self.valid = True
        self.cancel_event = cancel_event
        self.compile_cache = {}
        self.invalidation_reasons = []
        self.registry = runtime_definition()
        if live_mode not in ('roster', 'blacktop'):
            raise ValueError('Unknown live injection mode')
        self.live_mode = live_mode
        self.blacktop_binding = None
        for field in self.registry['fields']:
            field['evidence'] = [{'sha256': field['evidence_sha256'], 'source': 'bundled_verified_registry'}]
        self.roster_file = Path(roster_file).resolve() if live_mode == 'roster' else None
        if live_mode == 'roster' and not self.roster_file.is_file():
            raise ValueError('The selected roster file no longer exists.')
        self.file_hash = sha256(self.roster_file.read_bytes()).hexdigest() if self.roster_file else None
        self.file_stat = self._file_stat()
        with self.metrics.stage('process_attachment'):
            processes = find_processes()
        if pid is None:
            with self.metrics.stage('process_selection'):
                pid = select_roster_process(processes, self.registry, progress=self.progress, metrics=self.metrics)
        elif pid not in [p[0] for p in processes]:
            raise ValueError('NBA 2K17 process is no longer running.')
        self.reader = ProcessReader(pid)
        self.reader.metrics = self.metrics
        with self.metrics.stage('process_attachment'):
            self.reader.__enter__()
            self.metrics.count('process_handles_opened')
        try:
            self.pid = pid
            self.process_start = process_token(self.reader)
            self.module_base = self.reader.main_module.base_address
            with self.metrics.stage('compatibility'):
                self.executable = fingerprint_file(Path(self.reader.process_info().executable_path)).to_dict()
                self.metrics.count('executable_hashes')
            if self.executable['sha256'].lower() != self.registry['executable_sha256'].lower():
                raise ValueError('This NBA 2K17 executable build has not been mapped.')
            self.refresh()
        except Exception:
            self.close()
            raise

    def close(self):
        self.valid = False
        self.reader.__exit__(None, None, None)

    def _file_stat(self):
        if self.roster_file is None:
            return None
        s = self.roster_file.stat()
        return [s.st_size, s.st_mtime_ns]

    @measured('binding_checks')
    def validate(self):
        if not self.valid:
            raise ValueError('The live session is stale. Scan the loaded roster again.')
        try:
            return self._validate_binding()
        except (OSError, ValueError) as exc:
            self.valid = False
            self.invalidation_reasons = getattr(self, 'invalidation_reasons', []) + [str(exc)]
            raise

    def _validate_binding(self):
        from scanner.discovery.live import process_token
        if process_token(self.reader) != self.process_start or self.reader.main_module.base_address != self.module_base:
            raise ValueError('The game restarted. Scan the loaded roster again.')
        if self._file_stat() != self.file_stat:
            raise ValueError('The roster file changed. Scan the loaded roster again.')
        from scanner.injection_transaction import read_regions, Region
        probes = [Region('team:' + t['key'], t['address'], t['table_bytes'] + t['identity_bytes']) for t in self.teams]
        current = read_regions(self.reader, probes)
        for t in self.teams:
            raw = current['team:' + t['key']]
            if raw[:160] != t['table_bytes']:
                raise ValueError('Live team references changed. Scan the loaded roster again.')
            if raw[160:] != t['identity_bytes']:
                raise ValueError('Team metadata changed. Scan the loaded roster again.')
        if getattr(self, 'blacktop_binding', None):
            selected = blacktop_selection(self.reader, self.module_base, self.teams)
            if selected['team_key'] != self.blacktop_binding['team_key']:
                raise ValueError('The Blacktop team selection changed. Scan Blacktop again before injecting.')
        return True

    @measured('snapshot_checks')
    def _check_regions(self, regions):
        from scanner.injection_transaction import read_regions
        try:
            current = read_regions(self.reader, regions)
            if any(current[r.name] != r.before for r in regions):
                raise ValueError('A scanned player, linked block or team changed. Scan the loaded roster again.')
            return current
        except (OSError, ValueError) as exc:
            self.valid = False
            self.invalidation_reasons = getattr(self, 'invalidation_reasons', []) + [str(exc)]
            raise

    @measured('roster_discovery')
    def refresh(self):
        from scanner.injection_transaction import Region
        from scanner.player_codec import decode
        self.progress('Reading current and historic team slots...')
        self.teams, self.records, self.references, self.regions = [], {}, [], []
        definitions = {f['field']: f for f in self.registry['fields']}
        for definition in self.registry['teams']:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise ValueError('Roster discovery cancelled before completion.')
            t = deepcopy(definition)
            base = self.module_base + t['rva']
            r = self.reader
            if (r.utf16le(base + 0xde, 64) != t['token'] or r.utf16le(base + 0xa0, 60) != t['stadium']
                    or r.utf16le(base + 0x24e, 36) != t['city'] or r.utf16le(base + 0x21c, 50) != t['nickname']):
                raise ValueError('The loaded team directory differs from the verified 2K17 layout. No writes were made.')
            table = r.read_or_raise(base, 160)
            pointers = struct.unpack('<20Q', table)
            if not 8 <= sum(bool(p) for p in pointers) <= 20 or len(set(p for p in pointers if p)) != sum(bool(p) for p in pointers):
                raise ValueError('Invalid or duplicate team roster references: ' + t['label'])
            t.update(address=base, table_bytes=table, identity_bytes=r.read_or_raise(base + 0xa0, 0x1d4), slots=[])
            self.regions.append(Region('team:' + t['key'], base - 0x2d8, r.read_or_raise(base - 0x2d8, 0xc68)))
            for index, ptr in enumerate(pointers):
                if not ptr:
                    continue
                raw = r.read_or_raise(ptr, 0x460)
                if raw != r.read_or_raise(ptr, 0x460) or (ptr in self.records and self.records[ptr] != raw):
                    raise ValueError('A player record changed while scanning.')
                self.records[ptr] = raw
                slot = {'slot': index, 'player': ptr, 'reference': base + 8 * index, 'name': _name(raw),
                        'jersey': decode(raw, definitions['identity.jersey_number']),
                        'position': decode(raw, definitions['identity.primary_position'])}
                t['slots'].append(slot)
                self.references.append({**slot, 'team_key': t['key'], 'team_label': t['label'], 'deferred': t['deferred_all_star']})
            self.teams.append(t)
        self.binding_regions = tuple(self.regions)
        self.regions += [Region('player:' + hex(ptr), ptr, raw) for ptr, raw in self.records.items()]
        self.reference_index = defaultdict(list)
        self.body_owner_index = defaultdict(list)
        for reference in self.references:
            self.reference_index[reference['player']].append(reference)
        for ptr, raw in self.records.items():
            self.body_owner_index[struct.unpack_from('<Q', raw, 0x88)[0]].append(ptr)
        from scanner.college_lookup import index_colleges
        table = self.registry.get('college_table')
        if table and table['executable_sha256'] != self.registry['executable_sha256']:
            raise ValueError('Native origin table belongs to another game build')
        self.college_labels, _ = index_colleges(self.reader, self.records,
            table=table, module_base=self.module_base)
        self.validate()
        self.expected_regions = {r.name: r.before for r in self.regions}
        self.last_run = None
        from .body_ownership import BodyOwnershipCache
        self.body_cache = BodyOwnershipCache(self.reader, self.module_base, self.progress,
            auxiliary_references=self.registry.get('body_auxiliary_references', ()))
        self.body_cache.records = self.records
        from .sculpt_ownership import SculptOwnershipCache
        self.sculpt_cache = SculptOwnershipCache(self.reader, self.module_base, self.progress)
        self.sculpt_cache.records = self.records
        self.appearance_cache = SculptOwnershipCache(self.reader, self.module_base, self.progress,
                                                    pointer_offset=0x88, block_size=132)
        self.appearance_cache.records = self.records
        # Linked values are read only for selected destinations during preflight.
        # An unreadable body on an unrelated team must not block attachment.
        self.body_values = {}
        self._check_regions(self.regions)
        self.validate()
        if getattr(self, 'live_mode', 'roster') == 'blacktop':
            self.blacktop_binding = blacktop_selection(self.reader, self.module_base, self.teams)
            self.automatic_slots([], next(t for t in self.teams if t['key'] == self.blacktop_binding['team_key']))
            self.validate()
        return self.public_scan()

    def public_scan(self):
        return {'pid': self.pid, 'process_start_token': self.process_start, 'roster_file': str(self.roster_file) if self.roster_file else None,
                'live_mode': getattr(self, 'live_mode', 'roster'), 'blacktop_binding': deepcopy(getattr(self, 'blacktop_binding', None)),
                'roster_sha256': self.file_hash, 'executable': self.executable,
                'file_binding': ('Live player-browser selection; shared Play Now roster, no saved-file binding.' if self.roster_file is None
                                 else 'User-selected loaded file; file selection does not load it in the game.'),
                'teams': [{k: v for k, v in t.items() if k not in ('table_bytes', 'identity_bytes')} for t in self.teams if t['enabled']],
                'all_team_tables': len(self.teams), 'unique_player_records': len(self.records),
                'body_ownership': self.body_cache.summary() if hasattr(self, 'body_cache') else None}

    def diagnose_ownership(self, team_key, profiles=None, *, slots=None):
        """Resolve missing ownership before Inject; preview and Undo do not discover."""
        if profiles is not None and getattr(self, 'live_mode', 'roster') == 'blacktop':
            profiles = ranked_profiles(profiles)
        self.validate()
        self.refresh_mutable_records()
        team = next((t for t in self.teams if t['key'] == team_key and t['enabled']), None)
        if team is None:
            raise ValueError('Select a scanned NBA team before diagnosing ownership.')
        bodies = {struct.unpack_from('<Q', self.records[s['player']], 0x88)[0] for s in team['slots']}
        sculpts, appearances = set(), set()
        if profiles is not None:
            check = self.preflight(profiles, team_key, slots=slots)
            if check['blockers']: raise ValueError('\n'.join(check['blockers']))
            bodies = set(check['missing_bodies'])
            sculpts = set(check.get('missing_sculpts', ()))
            appearances = set(check.get('missing_appearances', ()))
        self.body_cache.progress = self.progress
        with self.metrics.stage('diagnostic_ownership_discovery'):
            summary = self.body_cache.discover(self.records, bodies, cancel_event=self.cancel_event) if bodies else self.body_cache.summary()
            if bodies and self.body_cache.last_discovery.get('error') == 'Editor context changed during ownership discovery':
                if self.cancel_event is None or not self.cancel_event.is_set():
                    self.validate()
                    self.refresh_mutable_records()
                    self.progress('Player selection changed during the scan; repeating ownership verification automatically once...')
                    summary=self.body_cache.discover(self.records,bodies,cancel_event=self.cancel_event)
                    self.body_cache.last_discovery['automatic_context_retry']=True
        if sculpts or appearances:
            from scanner.pointer_index import scan_references
            discovered = scan_references(self.reader, sculpts | appearances, self.progress,
                                         cancel_event=self.cancel_event)
            for cache, targets in ((self.sculpt_cache, sculpts), (self.appearance_cache, appearances)):
                if targets:
                    cache.progress = self.progress
                    cache.discover(self.records, targets, cancel_event=self.cancel_event,
                                   discovery_result=discovered)
        self._check_regions(self.regions)
        self.validate()
        path = injection_workspace() / ('ownership-diagnostic-' + uuid.uuid4().hex + '.json')
        report = {'team': team['label'], **summary, 'global_ownership': summary,
                  'scan_diagnostics': self.body_cache.last_discovery,
                  'cancelled': bool(self.cancel_event is not None and self.cancel_event.is_set()),
                  'validated_bodies': sum('error' not in self.body_cache.entries.get(b, {'error': 'missing'}) for b in bodies),
                  'blocked_bodies': sum('error' in self.body_cache.entries.get(b, {'error': 'missing'}) for b in bodies),
                  'requested_bodies': len(bodies), 'bodies': [self.body_cache.describe(b) for b in sorted(bodies)],
                  'sculpts': [self.sculpt_cache.describe(p) for p in sorted(sculpts)],
                  'appearances': [self.appearance_cache.describe(p) for p in sorted(appearances)],
                  'performance': self.metrics.report(), 'memory_written': False, 'report_path': str(path)}
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
        return report

    def refresh_mutable_records(self):
        """Adopt only decoded, mapped nonstructural fields from a stable reread.

        Mapped player content, including names and appearance, may change.
        Structural references and unknown bits remain exact. Undo retains its
        original complete baseline guards.
        """
        from scanner.injection_transaction import read_regions
        from .mutable_fields import verified_mutation
        actual = read_regions(self.reader, self.regions)
        changes = {}
        for region in self.regions:
            raw = actual[region.name]
            if raw == region.before: continue
            approved = region.name.startswith('player:') and verified_mutation(region.before,raw,self.registry['fields'])
            if not approved or self.reader.read_or_raise(region.address, len(raw)) != raw:
                self.valid = False
                raise ValueError(f'Structural, unknown or unstable change in {region.name}; refresh the live roster. Ownership cannot be reused across this binding change.')
            changes[region.name] = raw
        if changes:
            self.metrics.count('verified_mutable_records_refreshed', len(changes))
            self._adopt_expected(changes)
            self.body_cache.records = self.records
        return len(changes)

    def _compile(self, profile, raw):
        cache = getattr(self, 'compile_cache', None)
        if cache is None: self.compile_cache = cache = {}
        key = sha256(json.dumps([profile, self.registry], sort_keys=True).encode() + raw).hexdigest()
        if key in cache:
            self.metrics.count('compilation_cache_hits')
            return deepcopy(cache[key])
        self.metrics.count('compilation_cache_misses')
        result = compile_stored_card(profile, raw, self.registry)
        if len(cache) >= 256: cache.pop(next(iter(cache)))
        cache[key] = deepcopy(result)
        return result

    @staticmethod
    def desired_body(profile, plan, before=None):
        stored = profile['reusable_roster_template']['defaults']
        identity = plan['resolved']['values'].get('identity', {})
        height = identity.get('height_cm', stored.get('identity', {}).get('height_cm'))
        inches = re.match(r"^(\d+)'(\d+)\"", str(identity.get('height', '')))
        if inches:
            precise = (int(inches[1]) * 12 + int(inches[2])) * 2.54
            if height is not None and abs(precise - height) > 1: raise ValueError('Conflicting card height measurements.')
            height = precise
        wingspan = stored.get('appearance', {}).get('wingspan_cm')
        if height is None or wingspan is None or not 120 <= height <= 260 or not 120 <= wingspan <= 300:
            raise ValueError('Missing or invalid captured height/wingspan.')
        measurements = struct.pack('<ff', height, wingspan)
        source = plan.get('native_sculpt_appearance')
        if not source:
            return measurements
        desired = bytearray(measurements + bytes.fromhex(source['body_hex'])[8:])
        # Native reflection defines only bits 0..29 of the final packed word.
        # Its initializer preserves the two remaining bits; so do we.
        if before is not None:
            if len(before) != 132:
                raise ValueError('Invalid destination appearance extent')
            desired[131] = (desired[131] & 0x3f) | (before[131] & 0xc0)
        return bytes(desired)

    def preflight(self, profiles, team_key, *, slots=None, live=True):
        """Collect all selected-lineup blockers; never scan process memory."""
        if getattr(self, 'live_mode', 'roster') == 'blacktop':
            profiles = ranked_profiles(profiles)
        if live:
            self.validate()
            self.refresh_mutable_records()
        blockers, missing, reasons, missing_sculpts, missing_appearances = [], [], [], [], []
        if getattr(self, 'blacktop_binding', None) and team_key != self.blacktop_binding['team_key']:
            return {'blockers': ['Destination differs from the detected Blacktop team.'], 'missing_bodies': [],
                    'message': 'Scan the selected Blacktop team again.'}
        team = next((t for t in self.teams if t['key'] == team_key and t['enabled']), None)
        if team is None:
            return {'blockers': ['Select a scanned NBA destination team.'], 'missing_bodies': [], 'message': 'Roster not scanned or destination unavailable.'}
        wanted = [s['slot'] for s in self.automatic_slots(profiles, team)] if slots is None and len(profiles)<=len(team['slots']) else list(slots or [])
        if getattr(self, 'live_mode', 'roster') == 'blacktop':
            expected = [s['slot'] for s in self.automatic_slots(profiles, team)]
            if wanted != expected:
                blockers.append('Blacktop destinations must follow highest-overall order. Scan the team again.')
        if not profiles: blockers.append('Select a lineup first.')
        if len(wanted) != len(profiles) or len(set(wanted)) != len(wanted): blockers.append('Each card requires a different occupied destination slot.')
        changed, unchecked = 0, 0
        for i, profile in enumerate(profiles):
            label = profile.get('source_card_identity', {}).get('name', f'Card {i+1}')
            slot = next((s for s in team['slots'] if i<len(wanted) and s['slot']==wanted[i]), None)
            if slot is None: blockers.append(label + ': destination slot unavailable.'); continue
            ptr = slot['player']
            protected = [r for r in self.reference_index[ptr] if not r['deferred']]
            if len(protected)!=1 or protected[0]['team_key']!=team_key:
                blockers.append(label + ': destination is shared with another protected team.')
            try:
                plan = self._compile(profile, self.records[ptr])
                source = plan.get('native_sculpt_appearance')
                if source:
                    sculpt = struct.unpack_from('<Q', self.records[ptr], 0x80)[0]
                    if not live:
                        unchecked += 1
                    elif self.reader.read_or_raise(sculpt, 456).hex() != source['sculpt_hex']:
                        try: self.sculpt_cache.require(sculpt, ptr)
                        except ValueError as exc: missing_sculpts.append(sculpt); reasons.append(label + ': ' + str(exc))
                desired = self.desired_body(profile, plan)
                body = struct.unpack_from('<Q', self.records[ptr], 0x88)[0]
                if not live and body not in self.body_values:
                    unchecked += 1
                    continue
                before = self.reader.read_or_raise(body, len(desired)) if live else self.body_values.get(body)
                if live: self.body_values[body] = before
                if before is not None and len(before) == len(desired):
                    desired = self.desired_body(profile, plan, before)
                if before != desired:
                    if self.body_owner_index[body] != [ptr]: raise ValueError('Destination body storage is shared.')
                    changed += 1
                    cache = self.appearance_cache if source else self.body_cache
                    missing_group = missing_appearances if source else missing
                    if live:
                        try: cache.require(body, ptr)
                        except ValueError as exc: missing_group.append(body); reasons.append(label + ': ' + str(exc))
                    elif 'owner' not in cache.entries.get(body, {}):
                        missing_group.append(body); reasons.append(label + ': ownership evidence missing or stale.')
            except (ValueError, OSError, KeyError) as exc: blockers.append(label + ': ' + str(exc))
        message = '\n'.join(blockers + reasons)
        if missing: message += f'\nSelected lineup: {len(set(missing))}/{changed} changed bodies need ownership checks; Inject runs these automatically (may take tens of seconds).'
        if missing_sculpts: message += f'\n{len(set(missing_sculpts))} facial sculpt blocks need ownership checks; Inject runs these automatically.'
        if missing_appearances: message += f'\n{len(set(missing_appearances))} full appearance blocks need ownership checks; Inject runs these automatically.'
        if unchecked: message += f'\nSession reusable. Preview transfer to check {unchecked} selected height/wingspan blocks; this quick check does not search process memory.'
        if not message: message = 'Ready to inject: live lineup checks passed.' if live else 'Session reusable. Preview or Inject performs fresh live checks for this lineup.'
        return {'blockers': blockers, 'missing_bodies': sorted(set(missing)), 'missing_sculpts': sorted(set(missing_sculpts)), 'ownership_reasons': reasons,
                'missing_appearances': sorted(set(missing_appearances)),
                'changed_bodies': changed, 'unchecked_bodies': unchecked, 'message': message.strip()}

    def automatic_slots(self, profiles, team):
        return self.position_matched_slots(profiles, team)

    def position_matched_slots(self, profiles, team):
        from scanner.player_codec import decode
        definitions = {f['field']: f for f in self.registry['fields']}
        positions = []
        for profile in profiles:
            identity = dict((profile.get('reusable_roster_template') or {}).get('defaults', {}).get('identity', {}))
            identity.update(profile.get('card_specific_overrides', {}).get('identity', {}))
            positions.append((identity.get('primary_position'), identity.get('secondary_position')))
        candidates = sorted(team['slots'], key=lambda s: s['slot'])
        target_positions = [tuple(decode(self.records[s['player']], definitions['identity.' + key])
                                  for key in ('primary_position', 'secondary_position')) for s in candidates]
        assignment = position_slot_assignment(positions, target_positions)
        return [candidates[i] for i in assignment]

    @measured('profile_preparation')
    def prepare(self, profiles, team_key, *, slots=None):
        from scanner.injection_transaction import Patch, Region
        from scanner.college_lookup import resolve_optional_college
        if getattr(self, 'live_mode', 'roster') == 'blacktop':
            profiles = ranked_profiles(profiles)
        self.validate()
        team = next((t for t in self.teams if t['key'] == team_key and t['enabled']), None)
        if team is None:
            raise ValueError('Select a scanned current or historic NBA team.')
        if not profiles or len(profiles) > len(team['slots']):
            raise ValueError('This lineup exceeds the occupied roster slots of the destination team.')
        wanted = [s['slot'] for s in team['slots'][:len(profiles)]] if slots is None else list(slots)
        if len(wanted) != len(profiles) or len(set(wanted)) != len(wanted):
            raise ValueError('Each card requires a different destination slot.')
        destination = [next((s for s in team['slots'] if s['slot'] == i), None) for i in wanted]
        if any(s is None for s in destination):
            raise ValueError('Only occupied, verified team slots can be replaced.')
        if slots is None:
            destination = self.automatic_slots(profiles, team)
        if len({s['player'] for s in destination}) != len(destination):
            raise ValueError('Multiple destinations refer to the same player record.')
        check = self.preflight(profiles, team_key, slots=[s['slot'] for s in destination])
        if check['blockers'] or check['missing_bodies'] or check.get('missing_sculpts') or check.get('missing_appearances'):
            raise ValueError(check['message'])
        self.progress('Preparing card fields and checking destination ownership...')
        self.patches, self.plans = [], []
        self.extra_regions = []
        self._check_regions(self.regions)
        college_labels = self.college_labels
        evidence_hash = sha256(json.dumps(self.registry, sort_keys=True).encode()).hexdigest()
        for profile, slot in zip(profiles, destination):
            aliases = self.reference_index[slot['player']]
            protected = [r for r in aliases if not r['deferred']]
            if len(protected) != 1 or protected[0]['team_key'] != team_key:
                raise ValueError('Destination is shared with another protected team: ' + slot['name'])
            raw = self.records[slot['player']]
            try:
                plan = self._compile(profile, raw)
            except ValueError as exc:
                card = profile.get('source_card_identity', {})
                raise ValueError(f"{card.get('name', 'Card')} ({card.get('source_card_id', '?')}): {exc}") from exc
            region = 'player:' + hex(slot['player'])
            after = bytearray.fromhex(plan['after_hex'])
            for p in plan['patches']:
                self.patches.append(Patch(region, p['offset'], bytes.fromhex(p['before_hex']), bytes.fromhex(p['after_hex']), ','.join(p['fields']), evidence_hash))
            origin = plan['resolved']['values'].get('identity', {}).get('origin')
            if origin:
                lookup = resolve_optional_college(self.reader, college_labels, origin)
                if lookup['resolution'] != 'PRESERVED_DESTINATION':
                    ptr = int(lookup['address_diagnostic'], 16)
                    lookup_bytes = self.reader.read_or_raise(ptr, 40)
                    if sha256(lookup_bytes).hexdigest() != lookup['label_probe_sha256']:
                        raise ValueError('College lookup changed during preparation; retry preview')
                    self.extra_regions.append(Region('college:' + hex(ptr), ptr, lookup_bytes))
                    new = struct.pack('<Q', ptr)
                    if new != raw[0x70:0x78]:
                        self.patches.append(Patch(region, 0x70, raw[0x70:0x78], new, 'identity.origin', evidence_hash))
                    after[0x70:0x78] = new
                plan['college'] = lookup
            plan.update(destination=slot, card_identity=deepcopy(profile['source_card_identity']),
                        deferred_aliases=[r for r in aliases if r['deferred']], after_hex=after.hex())
            plan['requires_mod_assets'] = profile['reusable_roster_template'].get('source_kind') == 'MODDED_ROSTER'
            plan['unsupported'] = [g for g in plan['unsupported'] if g['field'] not in ('identity.origin',)]
            if plan.get('college', {}).get('resolution') == 'PRESERVED_DESTINATION':
                plan['unsupported'].append({'field':'identity.origin',
                    'reason':'Origin unavailable in native lookup; destination origin preserved',
                    'requested':origin})
            self.plans.append(plan)
        self._prepare_bodies(profiles, evidence_hash)
        self._prepare_sculpts(evidence_hash)
        # De-duplicate shared immutable label probes; reject overlapping storage.
        spans = {}
        for region in self.regions + self.extra_regions:
            key = (region.address, len(region.before))
            previous = spans.get(key)
            if previous and previous.before != region.before:
                raise ValueError('Shared ownership probes changed during preparation')
            if previous is None:
                spans[key] = region
        regions = {r.name: r for r in spans.values()}
        self.transaction_regions = tuple(regions.values())
        self.patches = tuple(self.patches)
        self._check_regions(self.transaction_regions)
        self.validate()
        self.prepared_report = {'team': team['label'], 'team_key': team_key,
            'roster_file': str(self.roster_file) if self.roster_file else None,
            'live_mode': getattr(self, 'live_mode', 'roster'),
            'blacktop_binding': deepcopy(getattr(self, 'blacktop_binding', None)),
            'assignment_policy': 'highest_card_ovr_to_highest_destination_ovr' if getattr(self, 'live_mode', 'roster') == 'blacktop' else 'explicit_slots' if slots is not None else 'position_matching',
            'players': self.plans, 'all_team_tables': len(self.teams), 'watched_player_records': len(self.records),
            'complete_card_supported': False, 'remaining_limits': [
                'Shoes and accessories use native mappings; appearance still requires in-game validation.',
                'Captured native sculpt sources transfer their full sculpt and appearance; other custom sculpt sources remain unmapped.',
                'Game recalculation and save/reload are reported separately from byte readback.'],
            'policy': 'Replace occupied slots; preserve team/rotation references, destination structure, age and durability; All-Star isolation deferred.'}
        preserved_origins = [p['card_identity'].get('name','Card') for p in self.plans
                             if p.get('college',{}).get('resolution') == 'PRESERVED_DESTINATION']
        if preserved_origins:
            self.prepared_report['remaining_limits'].append('Origin label unavailable; existing destination origin retained for: ' + ', '.join(preserved_origins) + '.')
        if any(p['requires_mod_assets'] for p in self.plans):
            self.prepared_report['remaining_limits'].append('This lineup includes mod-roster templates; their required appearance assets have not been verified in this game installation.')
        if any(p.get('name_presentation') for p in self.plans):
            self.prepared_report['remaining_limits'].append(
                'Shai uses safely terminated native name storage. Keep Full-name presentation enabled in Game Connection and MyTEAM open for the hyphenated menu and jersey name.')
        if any(p.get('name_presentation', {}).get('hook_face_id_matches') is False for p in self.plans):
            self.prepared_report['remaining_limits'].append(
                'A Shai card uses a different face ID from the verified name companion profile; full-name menu presentation is not verified for that card.')
        return self.prepared_report

    @measured('ownership')
    def _prepare_bodies(self, profiles, evidence_hash):
        """Write measurements, or the verified native appearance extent when saved."""
        from scanner.injection_transaction import Region, Patch
        bodies = {}
        for profile, plan in zip(profiles, self.plans):
            ptr = plan['destination']['player']
            body = struct.unpack_from('<Q', self.records[ptr], 0x88)[0]
            owners = self.body_owner_index[body]
            if owners != [ptr] or body in bodies:
                desired=self.desired_body(profile,plan)
                before = self.reader.read_or_raise(body, len(desired))
                desired = self.desired_body(profile, plan, before)
                if before!=desired:
                    raise ValueError('Destination body storage is shared: ' + plan['destination']['name'])
                if body in bodies:
                    if self.desired_body(bodies[body][1],bodies[body][2],before)!=desired:
                        raise ValueError('Conflicting requested values for shared body storage')
                    plan['body']={'height_cm':struct.unpack('<f',desired[:4])[0],
                        'wingspan_cm':struct.unpack('<f',desired[4:8])[0],
                        'region':'body:'+hex(body),'address':body,'after_hex':desired.hex()}
                    plan['unsupported']=[g for g in plan['unsupported'] if g['field'] not in ('identity.height','identity.height_cm')]
                    continue
            bodies[body] = (ptr, profile, plan)
        # Unchanged linked blocks are read back but never written; they need
        # no all-memory ownership proof. Changed blocks keep that proof.
        changed_bodies = {}
        for body, (ptr, profile, plan) in bodies.items():
            stored = profile['reusable_roster_template']['defaults']
            identity = plan['resolved']['values'].get('identity', {})
            height = identity.get('height_cm', stored.get('identity', {}).get('height_cm'))
            inches = re.match(r"^(\d+)'(\d+)\"", str(identity.get('height', '')))
            if inches:
                precise = (int(inches[1]) * 12 + int(inches[2])) * 2.54
                if height is not None and abs(precise - height) > 1:
                    raise ValueError('Conflicting card height measurements.')
                height = precise
            wingspan = stored.get('appearance', {}).get('wingspan_cm')
            if height is None or wingspan is None or not 120 <= height <= 260 or not 120 <= wingspan <= 300:
                raise ValueError('Missing or invalid captured height/wingspan.')
            after = self.desired_body(profile, plan)
            before = self.reader.read_or_raise(body, len(after))
            after = self.desired_body(profile, plan, before)
            key = 'body:' + hex(body)
            self.extra_regions.append(Region(key, body, before))
            if before != after:
                fields = 'identity.height_cm,appearance.wingspan_cm' + (',appearance.native_body' if len(after) == 132 else '')
                self.patches.append(Patch(key, 0, before, after, fields, evidence_hash))
                changed_bodies[body] = (ptr, plan)
            plan['body'] = {'height_cm': struct.unpack('<f', after[:4])[0], 'wingspan_cm': struct.unpack('<f', after[4:8])[0],
                            'region': key, 'address': body, 'after_hex': after.hex()}
            plan['unsupported'] = [g for g in plan['unsupported'] if g['field'] not in ('identity.height', 'identity.height_cm')]
        for body, (ptr, plan) in changed_bodies.items():
            try:
                cache = self.appearance_cache if plan.get('native_sculpt_appearance') else self.body_cache
                self.extra_regions.extend(cache.require(body, ptr))
            except ValueError as exc:
                raise ValueError(plan['destination']['name'] + ': ' + str(exc)) from exc
        self.body_ownership = {'bodies': [(self.appearance_cache if p.get('native_sculpt_appearance') else self.body_cache).describe(b)
                                        for b, (_, p) in changed_bodies.items()],
                               'read_bytes': 0, 'exhaustive_scan_performed': False,
                               'targeted_validation_performed': True,
                               'changed_body_blocks': len(changed_bodies), 'unchanged_body_blocks': len(bodies) - len(changed_bodies),
                               'policy': 'Reuse initial ownership evidence only after targeted destination, pointer, known-owner, editor-context and allocation checks. Unknown ownership blocks the requested operation; no automatic exhaustive fallback.'}

    def _prepare_sculpts(self, evidence_hash):
        from scanner.injection_transaction import Region, Patch
        changed = []
        for plan in self.plans:
            source = plan.get('native_sculpt_appearance')
            if not source:
                continue
            owner = plan['destination']['player']
            pointer = struct.unpack_from('<Q', self.records[owner], 0x80)[0]
            before = self.reader.read_or_raise(pointer, 456)
            after = bytes.fromhex(source['sculpt_hex'])
            key = 'sculpt:' + hex(pointer)
            self.extra_regions.append(Region(key, pointer, before))
            if before != after:
                self.extra_regions.extend(self.sculpt_cache.require(pointer, owner))
                self.patches.append(Patch(key, 0, before, after, 'appearance.native_sculpt', evidence_hash))
                changed.append(self.sculpt_cache.describe(pointer))
            plan['sculpt'] = {'region': key, 'address': pointer, 'after_hex': after.hex(),
                              'source_record_sha256': source['source_record_sha256'],
                              'visual_injection_verified': False}
        self.body_ownership['sculpts'] = changed

    def begin_operation(self):
        self.metrics = self.reader.metrics = Metrics()
        self.metrics.count('validated_session_reused')
        # Refresh the OS module list once per operation; per-patch guards retain
        # the original process handle and creation token rather than PID alone.
        if hasattr(self.reader, 'refresh_modules'):
            self.reader.refresh_modules()

    @measured('linked_reference_checks')
    def validate_patch(self, patch, regions=None):
        if getattr(self, 'blacktop_binding', None):
            selected = blacktop_selection(self.reader, self.module_base, self.teams)
            if selected['team_key'] != self.blacktop_binding['team_key']:
                raise ValueError('Blacktop team changed before destination write. Scan Blacktop again.')
        if patch.region.startswith('player:'):
            from scanner.discovery.live import process_token
            if process_token(self.reader) != self.process_start:
                raise ValueError('Game process identity changed before destination write.')
            region = next(r for r in (self.transaction_regions if regions is None else regions) if r.name == patch.region)
            for reference in self.reference_index[region.address]:
                if self.reader.read_or_raise(reference['reference'], 8) != struct.pack('<Q', region.address):
                    raise ValueError('Destination team pointer changed before write.')
            raw = self.reader.read_or_raise(region.address, 0x90)
            if raw[0x58:0x64] != region.before[0x58:0x64] or raw[0x88:0x90] != region.before[0x88:0x90]:
                raise ValueError('Destination structural identity or body pointer changed before write.')
            return
        if patch.region.startswith('sculpt:'):
            pointer = int(patch.region.split(':', 1)[1], 16)
            owners = [a for a, raw in self.records.items() if struct.unpack_from('<Q', raw, 0x80)[0] == pointer]
            if len(owners) != 1:
                raise ValueError('Sculpt storage is shared by canonical players')
            self.sculpt_cache.require(pointer, owners[0])
            return
        if not patch.region.startswith('body:'):
            return
        from scanner.injection_transaction import read_regions
        # Canonical records and every discovered editor copy stay bound to
        # their structural IDs and body pointer before a linked write/rollback.
        regions = [r for r in (self.transaction_regions if regions is None else regions)
                   if r.name.startswith(('player:', 'owner-copy:'))]
        current = read_regions(self.reader, regions)
        for region in regions:
            raw = current[region.name]
            if raw[0x58:0x64] != region.before[0x58:0x64] or raw[0x88:0x90] != region.before[0x88:0x90]:
                self.valid = False
                raise ValueError('Linked body ownership changed. Scan the loaded roster again.')
        body = int(patch.region.split(':', 1)[1], 16)
        owners = self.body_owner_index.get(body, [])
        if len(owners) != 1:
            raise ValueError('Linked body ownership is ambiguous; no body write or rollback was performed.')
        cache = self.appearance_cache if len(patch.after) == 132 else self.body_cache
        cache.require(body, owners[0])

    def save_failure(self, message):
        path = injection_workspace() / ('failed-' + uuid.uuid4().hex + '.json')
        path.write_text(json.dumps({'error': message, 'session_valid': self.valid,
                                   'performance': self.metrics.report(),
                                   'ownership_diagnostics': self.body_cache.last_discovery,
                                   'invalidation_reasons': getattr(self, 'invalidation_reasons', []),
                                   'last_verified_run': str(self.last_run[0]) if self.last_run else None}, indent=2), encoding='utf-8')
        return str(path)

    @measured('session_update')
    def _adopt_expected(self, expected):
        """Advance only after verified writes/Undo; never accept outside changes."""
        from scanner.injection_transaction import Region
        from scanner.player_codec import decode
        self.regions = [Region(r.name, r.address, bytes(expected.get(r.name, r.before))) for r in self.regions]
        self.expected_regions = {r.name: r.before for r in self.regions}
        self.records = {ptr: self.expected_regions['player:' + hex(ptr)] for ptr in self.records}
        if hasattr(self, 'body_cache'): self.body_cache.records = self.records
        if hasattr(self, 'sculpt_cache'): self.sculpt_cache.records = self.records
        if hasattr(self, 'appearance_cache'): self.appearance_cache.records = self.records
        definitions = {f['field']: f for f in self.registry['fields']}
        for slot in [s for t in self.teams for s in t['slots']] + self.references:
            raw = self.records[slot['player']]
            slot.update(name=_name(raw), jersey=decode(raw, definitions['identity.jersey_number']),
                        position=decode(raw, definitions['identity.primary_position']))

    def write(self, output=None, *, restore_after=False):
        from scanner.injection_transaction import execute, WindowsMemoryWriter, Region, Patch
        from scanner.player_codec import decode
        output = Path(output) if output else injection_workspace() / ('run-' + uuid.uuid4().hex)
        output.mkdir(parents=True, exist_ok=False)
        def save(name, data):
            with self.metrics.stage('report_generation'):
                (output / name).write_text(json.dumps(data, indent=2), encoding='utf-8')
                self.metrics.count('report_files')
        save('plan.json', self.prepared_report)
        save('scan.json', self.public_scan())
        save('ownership.json', self.body_ownership)
        regions, patches = self.transaction_regions, self.patches
        expected = {r.name: bytearray(r.before) for r in regions}
        for p in patches:
            expected[p.region][p.offset:p.offset + len(p.after)] = p.after
        checks = []
        definitions = {f['field']: f for f in self.registry['fields']}
        def verify_fields(actual):
            for plan in self.plans:
                raw = actual['player:' + hex(plan['destination']['player'])]
                if raw.hex() != plan['after_hex']:
                    raise ValueError('Destination record readback differs from the prepared card.')
                for f in plan['fields']:
                    value = decode(raw, definitions[f['field']])
                    checks.append({'card': plan['card_identity'], 'field': f['field'], 'actual': value,
                                   'expected': f['decoded_after'], 'matches': value == f['decoded_after']})
                if actual[plan['body']['region']].hex() != plan['body']['after_hex']:
                    raise ValueError('Linked body readback failed.')
                if plan.get('sculpt'):
                    if actual[plan['sculpt']['region']].hex() != plan['sculpt']['after_hex']:
                        raise ValueError('Linked facial sculpt readback failed.')
                    checks.append({'card': plan['card_identity'], 'field': 'appearance.native_sculpt',
                                   'matches': True, 'bytes': 456})
                    checks.append({'card': plan['card_identity'], 'field': 'appearance.native_body',
                                   'matches': True, 'bytes': 132})
            if not all(c['matches'] for c in checks):
                raise ValueError('Field-level readback failed.')
            self.metrics.count('verified_fields', len(checks))
            self.metrics.count('verified_body_blocks', len(self.plans))
        self.progress('Writing lineup and checking every field…')
        memory = WindowsMemoryWriter(self.pid)
        memory.metrics = self.metrics
        with self.metrics.stage('process_attachment'):
            memory.__enter__()
            self.metrics.count('process_handles_opened')
        try:
            with self.metrics.stage('transaction'):
                result = execute(memory, regions, patches, validate_binding=self.validate,
                                 authorize_plan=lambda rs, ps: rs == regions and ps == patches,
                                 journal_path=output / 'write.jsonl', metrics=self.metrics,
                                 group_binding_checks=True,
                                 readback_validator=verify_fields,
                                 validate_patch=lambda patch: self.validate_patch(patch, regions))
            # Keep Undo available even if a later report write fails.
            if patches:
                self.last_run = (output, regions, patches, expected)
            self._adopt_expected(expected)
        except Exception as exc:
            try:
                save('failure.json', {'error': str(exc), 'transaction': getattr(exc, 'report', None),
                                      'performance': self.metrics.report()})
            except OSError:
                pass  # The durable transaction journal is already authoritative.
            raise
        finally:
            memory.__exit__(None, None, None)
        restored = False
        if restore_after and patches:
            self.undo(); restored = True
        elif restore_after:
            restored = True
        result.update(cards_written=len(self.plans), field_readback_count=len(checks), all_fields_match=True,
                      complete_card_injection=False, restored=restored, installed=not restore_after,
                      remaining_limits=self.prepared_report['remaining_limits'], report_directory=str(output),
                      recalculation_cache_refresh={'performed': False, 'seconds': 0,
                          'reason': 'No validated recalculation/cache path; calculated values are not forced.'})
        save('readback.json', checks)
        save('result.json', result)
        result['performance'] = self.metrics.report()
        save('performance.json', result['performance'])
        return result

    @measured('rollback')
    def undo(self):
        from scanner.injection_transaction import execute, WindowsMemoryWriter, Region, Patch
        output, regions, patches, expected = self.last_run
        reverse_regions = tuple(Region(r.name, r.address, bytes(expected[r.name])) for r in regions)
        reverse = tuple(Patch(p.region, p.offset, p.after, p.before, p.field, p.evidence_sha256) for p in reversed(patches))
        with WindowsMemoryWriter(self.pid) as memory:
            memory.metrics = self.metrics
            result = execute(memory, reverse_regions, reverse, validate_binding=self.validate,
                             authorize_plan=lambda rs, ps: rs == reverse_regions and ps == reverse,
                             journal_path=output / ('undo-' + uuid.uuid4().hex + '.jsonl'), metrics=self.metrics,
                             group_binding_checks=True,
                             validate_patch=lambda patch: self.validate_patch(patch, reverse_regions))
            # execute already verified the entire restored watched set.
        self._adopt_expected({r.name: r.before for r in regions})
        self.last_run = None
        result['restored_all_watched_regions'] = True
        return result


class BlacktopSession(RosterSession):
    """Fileless selection binding with the same field transaction and Undo."""
    def __init__(self, roster_file=None, **kwargs):
        super().__init__(None, live_mode='blacktop', **kwargs)

    def automatic_slots(self, profiles, team):
        for slot in team['slots']:
            slot['overall'] = live_overall(self.records[slot['player']])
        # Displayed OVR descending; equal OVRs keep native slot order.
        return sorted(team['slots'], key=lambda s: (-s['overall'], s['slot']))[:len(profiles)]
