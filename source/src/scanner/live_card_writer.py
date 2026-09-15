"""Build-bound occupied-slot transactions. UI validation is a separate milestone.

The mapped-field round trip always restores. Explicit mapped-field installation
leaves verified writes applied and reports every remaining gap. Neither mode
claims complete-card support while donor-owned fields remain unresolved.
"""
from collections import Counter
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import struct
import re

from .discovery.live import LiveSource, process_token
from .injection_scope import partition_references, POLICY, DEFERRED_TEAMS
from .injection_transaction import Region, Patch, execute, WindowsMemoryWriter
from .player_codec import decode, preview_record, complete_badge_values
from .roster_record import utf16_fixed_field
from .college_lookup import index_colleges, resolve_college, read_label


def name_of(raw):
    return (utf16_fixed_field(raw, 0x24, 0x28)+' '+utf16_fixed_field(raw, 0, 0x24)).strip()


def verify_evidence(root, registry):
    seen = set()
    for field in registry['fields']:
        if not field.get('evidence'):
            raise ValueError('Field has no evidence: '+field['field'])
        for evidence in field['evidence']:
            pair = (evidence['path'], evidence['sha256'])
            if pair in seen:
                continue
            if sha256((root/evidence['path']).read_bytes()).hexdigest() != evidence['sha256']:
                raise ValueError('Changed mapping evidence: '+evidence['path'])
            seen.add(pair)


def compile_values(profile, source, destination, registry):
    from nba2k17_scraper.injection_exporter import resolve_player_values
    defaults = {}
    for field in registry['fields']:
        section, key = field['field'].split('.', 1)
        if section == 'badges' or field.get('region', 'player') != 'player':
            continue
        value = decode(source, field)
        if field['codec'] == 'enum':
            labels = [k for k, v in field.get('labels', {}).items() if v == value]
            if len(labels) == 1:
                value = labels[0]
        defaults.setdefault(section, {})[key] = value
    stored = profile.get('reusable_roster_template')
    if stored:
        if stored['executable_sha256'].lower() != registry['executable_sha256'].lower():
            raise ValueError('Stored player template belongs to a different executable build')
        supported = {f['field'] for f in registry['fields']}
        for section, fields in stored['defaults'].items():
            for key, value in fields.items():
                if section != 'badges' and section+'.'+key in supported:
                    defaults.setdefault(section,{})[key] = value
    resolved = resolve_player_values(profile, defaults, preserve_durability=True)
    if stored:
        for field, origin in resolved['origins'].items():
            if origin['layer'] == 'template':
                origin.update(template_id=stored['template_id'],capture_sha256=stored['capture_sha256'])
    badges = complete_badge_values(profile, registry, clear_unlisted=True)
    resolved['values']['badges'] = badges['values']
    for key in badges['cleared_unlisted']:
        resolved['origins']['badges.'+key] = {'layer': 'explicit_policy', 'policy': badges['policy']}
    # Full name is a projection of two existing fields, not a third memory field.
    identity = resolved['values'].get('identity', {})
    projected = []
    if 'name' in identity:
        actual = (identity.get('first_name', '')+' '+identity.get('last_name', '')).strip()
        if actual == identity['name']:
            projected.append({'field': 'identity.name', 'value': identity.pop('name'),
                              'via': ['identity.first_name', 'identity.last_name']})
    preview = preview_record(destination, resolved['values'], registry)
    gaps = list(preview['unsupported'])
    gaps += [{'field': 'badges.'+key, 'reason': 'Authored value explicitly missing; absence is not established'}
             for key in badges['unresolved']]
    gaps += [
        {'field': 'gear.shoes', 'reason': 'CT inline +0x124 length 12; individual IDs/color bits and none values lack a controlled accepted edit'},
        {'field': 'gear.accessories', 'reason': 'CT inline +0x139 length 19; individual ID/color masks and none values lack a controlled accepted edit'},
        {'field': 'appearance.linked_body', 'reason': 'Height/wingspan at pointer(+0x88)+0/+4 observed; remaining body/sculpt extent at +0x80/+0x88 is not established'},
        {'field': 'identity.remaining_template_vitals', 'reason': 'Handedness, years pro and remaining player flags need independent encoding evidence; age/date fields are excluded by user policy'},
    ]
    signature_fields=[f for f in registry['fields'] if f['field'].startswith('signatures.')]
    inferred=[f['field'] for f in signature_fields if f.get('inferred_mapping')]
    if len(signature_fields)!=44:
        gaps.append({'field':'signatures.remaining','reason':f'Only {len(signature_fields)} of 44 signature fields are mapped'})
    elif inferred:
        gaps.append({'field':'signatures.visual_validation','reason':'All 44 fields are transferable; 39 mappings are user-approved inferences and await in-game visual/save-reload validation'})
    return {**preview, 'resolution': resolved, 'badge_policy': badges, 'projected_fields': projected,
            'signature_mapping_summary':{'mapped_fields':len(signature_fields),'inferred_fields':inferred,
                                         'visual_injection_verified':False},
            'remaining_gaps': gaps, 'complete_card_supported': not gaps}


class LiveCardSession:
    def __init__(self, root, binding, registry):
        self.root, self.binding, self.registry = Path(root), binding, registry
        verify_evidence(self.root, registry)
        if registry['executable_sha256'].lower() != binding['executable_sha256'].lower():
            raise ValueError('Registry/build mismatch')
        self.source = LiveSource(binding)
        self.reader = self.source.reader

    def close(self):
        self.source.close()

    def prepare(self, profile, source_team, source_name, destination_team, destination_name, ownership_audit=None):
        if destination_team in DEFERRED_TEAMS:
            raise ValueError('All-Star destinations are deferred')
        self.source.sample()
        self.records, self.references, watched = {}, [], {}
        self.tables = {}
        for team in self.binding['teams']:
            base = int(team['address'], 16)
            table = self.reader.read_or_raise(base, 160)
            self.tables[base] = table
            watched['team:'+hex(base)] = Region('team:'+hex(base), base-0x2d8,
                                              self.reader.read_or_raise(base-0x2d8, 0xc68))
            for slot, ptr in enumerate(struct.unpack('<20Q', table)):
                if not ptr:
                    continue
                raw = self.reader.read_or_raise(ptr, 0x460)
                if ptr in self.records and self.records[ptr] != raw:
                    raise ValueError('Record changed while resolving references')
                self.records[ptr] = raw
                self.references.append({'team': team['token'], 'slot': slot,
                                        'reference': base+slot*8, 'player': ptr, 'name': name_of(raw)})
        def resolve(team, name):
            matches = [r for r in self.references if r['team'] == team and r['name'] == name]
            if len(matches) != 1:
                raise ValueError('Missing/ambiguous live team and player: '+team+' / '+name)
            return matches[0]
        self.template = resolve(source_team, source_name)
        self.destination = resolve(destination_team, destination_name)
        if self.template['player'] == self.destination['player']:
            raise ValueError('Source template cannot be its own destination')
        aliases = [r for r in self.references if r['player'] == self.destination['player']]
        protected, deferred = partition_references(aliases, defer_all_stars=True)
        if len(protected) != 1 or protected[0] != self.destination:
            raise ValueError('Destination is shared outside the two deferred All-Star teams')
        for ptr, raw in self.records.items():
            key = 'player:'+hex(ptr)
            watched[key] = Region(key, ptr, raw)
        # Watch relevant linked windows, without writing an unverified block or
        # claiming that these bounded windows cover complete sculpt storage.
        linked = []
        for role, ref in [('source', self.template), ('destination', self.destination)]:
            raw = self.records[ref['player']]
            for offset, length in [(0x80, 32), (0x88, 8)]:
                ptr = struct.unpack_from('<Q', raw, offset)[0]
                owners = [hex(p) for p, block in self.records.items()
                          if struct.unpack_from('<Q', block, offset)[0] == ptr]
                linked.append({'role': role, 'pointer_offset': hex(offset), 'target_diagnostic': hex(ptr),
                               'observed_record_owners': owners, 'watched_length': length, 'written': False,
                               'complete_block_extent_verified': False})
                key = 'linked-probe:'+hex(ptr)
                if key not in watched:
                    watched[key] = Region(key, ptr, self.reader.read_or_raise(ptr, length))
        self.regions = tuple(watched.values())
        self.before = self.records[self.destination['player']]
        self.plan = compile_values(profile, self.records[self.template['player']], self.before, self.registry)
        evidence_hash = sha256(json.dumps(self.registry, sort_keys=True).encode()).hexdigest()
        self.region_key = 'player:'+hex(self.destination['player'])
        self.patches = tuple(Patch(self.region_key, p['offset'], bytes.fromhex(p['before_hex']),
                                  bytes.fromhex(p['after_hex']), ','.join(p['fields']), evidence_hash)
                             for p in self.plan['patches'])
        self.readback_definitions = {f['field']: f for f in self.registry['fields']}
        self.readback_regions = {}
        self.lookup_resolution = None
        self._extend_runtime_fields(profile, evidence_hash, ownership_audit)
        # Second read confirms every snapshot is stable before it can be used.
        self.validate_binding()
        for region in self.regions:
            if self.reader.read_or_raise(region.address, len(region.before)) != region.before:
                raise ValueError('Snapshot changed before transaction: '+region.name)
        self.plan.update(scope_policy=POLICY, source=deepcopy(self.template), destination=deepcopy(self.destination),
                         destination_aliases=aliases, deferred_all_star_references=deferred,
                         watched_records=len(self.records), watched_regions=len(self.regions), linked_windows=linked,
                         encoded_counts=dict(Counter(f['field'].split('.')[0] for f in self.plan['fields'])))
        self.plan['transaction_patches'] = [{'region': p.region, 'offset': p.offset, 'before_hex': p.before.hex(),
                                             'after_hex': p.after.hex(), 'field': p.field,
                                             'evidence_sha256': p.evidence_sha256} for p in self.patches]
        return self.plan

    def _extend_runtime_fields(self, profile, evidence_hash, ownership_audit):
        """Only two explicitly established pointer operations, never opaque copy."""
        identity = profile['card_specific_overrides'].get('identity', {})
        origin = identity.get('origin')
        if origin is not None:
            folder = self.root/'.project/data/nba2k17/field_discovery/ct-reload-2026-09-09/checkpoints'
            paths = [folder/name for name in ('curry-vitals-baseline.json', 'curry-college-dayton-accepted.json', 'curry-vitals-restored.json')]
            evidence = [{'path': str(p.relative_to(self.root)), 'sha256': sha256(p.read_bytes()).hexdigest()} for p in paths]
            if not json.loads(paths[-1].read_text(encoding='utf-8'))['canonical_restored']:
                raise ValueError('College restoration evidence is incomplete')
            labels, _ = index_colleges(self.reader, self.records)
            self.lookup_resolution = resolve_college(self.reader, labels, origin)
            ptr = int(self.lookup_resolution['address_diagnostic'], 16)
            lookup = self.reader.read_or_raise(ptr, 40)
            self.regions += (Region('college-lookup', ptr, lookup),)
            before, after = self.before[0x70:0x78], struct.pack('<Q', ptr)
            if before != after:
                self.patches += (Patch(self.region_key, 0x70, before, after, 'identity.origin', evidence_hash),)
            player_after = bytearray.fromhex(self.plan['after_hex'])
            player_after[0x70:0x78] = after
            self.plan['after_hex'] = player_after.hex()
            self.plan['after_sha256'] = sha256(player_after).hexdigest()
            self.plan['fields'].append({'field': 'identity.origin', 'requested': origin, 'decoded_after': origin,
                'offset': 0x70, 'size': 8, 'before_hex': before.hex(), 'after_hex': after.hex(),
                'confidence': 'CONTROLLED_FULL_POINTER_DAVIDSON_DAYTON_RESTORED', 'evidence': evidence,
                'injection_verified': False, 'pointer_policy': 'FRESH_LABEL_LOOKUP_NOT_CAPTURED_ADDRESS'})
            self.plan['remaining_gaps'] = [g for g in self.plan['remaining_gaps'] if g['field'] != 'identity.origin']
            self.plan['college_resolution'] = self.lookup_resolution
        if ownership_audit is None:
            return
        audit = json.loads(Path(ownership_audit).read_text(encoding='utf-8'))
        if (audit['binding']['process_start_token'] != self.binding['process_start_token']
                or audit['destination_address_diagnostic'] != hex(self.destination['player'])
                or audit['destination_before_sha256'] != sha256(self.before).hexdigest()
                or audit['gaps'] or not audit['destination_unchanged']):
            raise ValueError('Linked ownership audit does not match fresh destination')
        body = struct.unpack_from('<Q', self.before, 0x88)[0]
        owners = []
        for hit in audit['references'][hex(body)]:
            address = int(hit['address'], 16)
            if self.reader.pointer(address) != body:
                raise ValueError('A linked-buffer reference changed since the audit')
            record = self.reader.read_or_raise(address-0x88, 0x460)
            if name_of(record) != self.destination['name'] or record[0x58:0x60] != self.before[0x58:0x60]:
                raise ValueError('Body buffer has a different or unresolved player owner')
            owners.append(hex(address-0x88))
            key = 'owner-copy:'+hex(address-0x88)
            if address-0x88 not in {r.address for r in self.regions}:
                self.regions += (Region(key, address-0x88, record),)
        if not owners:
            raise ValueError('No body ownership references')
        source_body = struct.unpack_from('<Q', self.records[self.template['player']], 0x88)[0]
        if source_body == body:
            raise ValueError('Source and destination share body storage')
        shared = [p for p, raw in self.records.items() if struct.unpack_from('<Q', raw, 0x88)[0] == body]
        if shared != [self.destination['player']]:
            raise ValueError('Body storage shared with another canonical player')
        source_bytes = self.reader.read_or_raise(source_body, 8)
        source_height, wingspan = struct.unpack('<ff', source_bytes)
        stored = profile.get('reusable_roster_template')
        if stored:
            source_height = stored['defaults'].get('identity',{}).get('height_cm',source_height)
            wingspan = stored['defaults'].get('appearance',{}).get('wingspan_cm',wingspan)
        height = identity.get('height_cm', source_height)
        inches = re.match(r"^(\d+)'(\d+)\"", str(identity.get('height', '')))
        height_policy = 'EXPLICIT_CM_OR_TEMPLATE'
        if inches:
            precise = (int(inches[1])*12+int(inches[2]))*2.54
            if abs(precise-height) > 1:
                raise ValueError('Contradictory card height measurements')
            height, height_policy = precise, 'EXPLICIT_INCHES_OVER_ROUNDED_CM'
        if not 120 <= height <= 260 or not 120 <= wingspan <= 300:
            raise ValueError('Body values outside supported human range')
        folder = self.root/'.project/data/nba2k17/field_discovery/curry-injection-2026-09-09'
        evidence_paths = [folder/'checkpoints/curry-height64-accepted.json', folder/'ct-height-reload-restoration.json']
        if not json.loads(evidence_paths[-1].read_text(encoding='utf-8'))['all_watched_canonical_team_and_linked_windows_restored']:
            raise ValueError('Height/wingspan baseline restoration is not established')
        evidence = [{'path': str(p.relative_to(self.root)), 'sha256': sha256(p.read_bytes()).hexdigest()} for p in evidence_paths]
        body_key = 'linked-probe:'+hex(body)
        body_before = next(r.before for r in self.regions if r.name == body_key)
        for offset, field, value in [(0, 'identity.height_cm', height), (4, 'appearance.wingspan_cm', wingspan)]:
            new = struct.pack('<f', value)
            if body_before[offset:offset+4] != new:
                self.patches += (Patch(body_key, offset, body_before[offset:offset+4], new, field, evidence_hash),)
            definition = {'field': field, 'offset': offset, 'size': 4, 'codec': 'float32'}
            self.readback_definitions[field] = definition
            self.readback_regions[field] = (body, 8)
            self.plan['fields'].append({'field': field, 'requested': value, 'decoded_after': struct.unpack('<f', new)[0],
                'region': body_key, 'offset': offset, 'size': 4, 'before_hex': body_before[offset:offset+4].hex(),
                'after_hex': new.hex(), 'confidence': 'CONTROLLED_LINKED_HEIGHT_WINGSPAN_FAMILY_RELOAD_RESTORED',
                'evidence': evidence, 'injection_verified': False})
        self.plan['remaining_gaps'] = [g for g in self.plan['remaining_gaps'] if g['field'] not in ('identity.height', 'identity.height_cm')]
        self.plan['body_transfer'] = {'height_policy': height_policy, 'height_cm': height, 'wingspan_cm': wingspan,
                                    'verified_extent_used': 8, 'direct_reference_owners': owners,
                                    'audit_sha256': sha256(Path(ownership_audit).read_bytes()).hexdigest()}

    def validate_binding(self):
        r, b = self.reader, self.binding
        if process_token(r) != b['process_start_token'] or hex(r.main_module.base_address) != b['module_base']:
            raise ValueError('Process instance changed')
        st = Path(b['roster_file']).stat()
        if [st.st_size, st.st_mtime_ns] != b['roster_stat']:
            raise ValueError('Roster file changed')
        for team in b['teams']:
            base = int(team['address'], 16)
            if r.utf16le(base+0xde, 64) != team['token'] or r.read_or_raise(base, 160) != self.tables[base]:
                raise ValueError('Team identity or live roster references changed')
        # Referenced records may change their names during injection. Identity
        # was resolved before writing; their current slots must still point to
        # the same snapshot, and transaction verification protects all bytes.
        for ref in (self.template, self.destination):
            if r.pointer(ref['reference']) != ref['player']:
                raise ValueError('Resolved slot target changed')
        if self.lookup_resolution:
            label, raw = read_label(r, int(self.lookup_resolution['address_diagnostic'], 16))
            if label != self.lookup_resolution['label'] or sha256(raw).hexdigest() != self.lookup_resolution['label_probe_sha256']:
                raise ValueError('Shared college label changed')
        return True

    def round_trip(self, output):
        return self._write(output, restore_after=True)

    def install_mapped_fields(self, output):
        """Explicit partial installation; keep successful writes, report gaps."""
        return self._write(output, restore_after=False)

    def _write(self, output, *, restore_after):
        output = Path(output)
        if output.exists():
            raise FileExistsError('Use a new diagnostic run directory')
        output.mkdir(parents=True)
        (output/'plan.json').write_text(json.dumps(self.plan, indent=2), encoding='utf-8')
        (output/'binding.json').write_text(json.dumps(self.binding, indent=2), encoding='utf-8')
        # Equality with the prepared tuple prevents a caller substituting
        # arbitrary addresses/spans via a permissive approval callback.
        def approve(regions, patches):
            return regions == self.regions and patches == self.patches
        expected = {r.name: bytearray(r.before) for r in self.regions}
        for patch in self.patches:
            expected[patch.region][patch.offset:patch.offset+len(patch.after)] = patch.after
        with WindowsMemoryWriter(self.binding['pid']) as memory:
            report = execute(memory, self.regions, self.patches, validate_binding=self.validate_binding,
                             authorize_plan=approve, journal_path=output/'write.jsonl')
            after = bytes.fromhex(self.plan['after_hex'])
            checks = []
            verified = False
            restore = None
            try:
                readback = memory.read(self.destination['player'], 0x460)
                for field in self.plan['fields']:
                    key = field['field']
                    if key == 'identity.origin':
                        actual, _ = read_label(self.reader, struct.unpack_from('<Q', readback, 0x70)[0])
                    else:
                        region = self.readback_regions.get(key)
                        field_buffer = memory.read(*region) if region else readback
                        actual = decode(field_buffer, self.readback_definitions[key])
                    checks.append({'field': field['field'], 'requested': field['requested'],
                                   'expected_decoded': field['decoded_after'], 'actual': actual,
                                   'matches': actual == field['decoded_after']})
                if readback != after or not all(c['matches'] for c in checks):
                    raise ValueError('Field-level readback mismatch')
                (output/'readback.json').write_text(json.dumps(checks, indent=2), encoding='utf-8')
                self.validate_binding()
                if not all(memory.read(r.address,len(r.before)) == bytes(expected[r.name]) for r in self.regions):
                    raise ValueError('Watched regions changed during field-level readback')
                verified = True
            finally:
                if restore_after or not verified:
                    restored_regions = tuple(Region(r.name, r.address, bytes(expected[r.name]))
                                             for r in self.regions)
                    reverse = tuple(Patch(p.region, p.offset, p.after, p.before, p.field, p.evidence_sha256)
                                    for p in reversed(self.patches))
                    restore = execute(memory, restored_regions, reverse, validate_binding=self.validate_binding,
                                      authorize_plan=lambda rs, ps: rs == restored_regions and ps == reverse,
                                      journal_path=output/'restore.jsonl')
            exact = all(memory.read(r.address, len(r.before)) == r.before for r in self.regions) if restore_after else False
            if restore_after and not exact:
                raise ValueError('Restoration differs from complete watched baseline')
        report.update(status='MAPPED_FIELDS_WRITTEN_READ_BACK_AND_RESTORED' if restore_after else 'MAPPED_FIELDS_INSTALLED',
                      complete_card_injection=False, installed_in_game=not restore_after,
                      field_readback_count=len(checks), all_fields_match=all(c['matches'] for c in checks),
                      restored_all_watched_regions=exact, watched_regions=len(self.regions),
                      restore_status=restore['status'] if restore else 'NOT_REQUESTED_INSTALL_LEFT_APPLIED', remaining_gaps=self.plan['remaining_gaps'],
                      deferred_all_star_references=self.plan['deferred_all_star_references'])
        (output/'result.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        return report
