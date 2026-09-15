"""Session-scoped linked-body ownership evidence and targeted validity checks.

Only explicit discover() performs exhaustive memory search. Ordinary roster
discovery and prepare/write/Undo never call it. No cache is loaded from disk.
"""
from collections import defaultdict
from hashlib import sha256
import struct
from scanner.injection_transaction import Region, read_regions
from scanner.pointer_index import scan_references
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS


def identity(raw):
    """Structural record binding, never a real-player/name authentication.

    +0x58..+0x63 is protected by the injector. A matching body pointer is
    checked separately, as are other canonical owners and allocations.
    """
    return raw[0x58:0x64]


class BodyOwnershipCache:
    def __init__(self, reader, module_base, progress, *, auxiliary_references=()):
        self.reader, self.module_base, self.progress = reader, module_base, progress
        self.entries = {}
        self.last_discovery = {}
        self.records = {}
        self.canonical_allocations = {}
        self.auxiliary_references = tuple(auxiliary_references)

    def auxiliary_region(self, reference, body, owner):
        """Recognize only an exact build-bound alias verified by an in-game edit.

        This is a 0x138-byte auxiliary entry, not a 0x460-byte roster owner.
        Unknown entries still fail closed. Every byte is watched through write
        and undo; this method never authorizes modification of the entry.
        """
        for definition in self.auxiliary_references:
            address = self.module_base + definition['rva']
            if (reference != address + definition['body_offset']
                    or owner != self.module_base + definition['owner_rva']):
                continue
            raw = self.reader.read_or_raise(address, definition['size'])
            expected = bytearray.fromhex(definition['normalized_hex'])
            for offset in definition['rebase_offsets']:
                value = struct.unpack_from('<Q', expected, offset)[0]
                struct.pack_into('<Q', expected, offset, value + self.module_base)
            if (raw != bytes(expected)
                    or struct.unpack_from('<Q', raw, definition['body_offset'])[0] != body
                    or self.reader.read_or_raise(address, len(raw)) != raw):
                raise ValueError('Verified auxiliary body reference changed')
            return Region('body-alias:' + hex(address), address, raw)
        return None

    def context_regions(self, records):
        regions = []
        for rva in SELECTED_RECORD_POINTER_RVAS:
            address = self.module_base + rva
            raw = self.reader.read_or_raise(address, 8)
            regions.append(Region('ownership-root:' + hex(rva), address, raw))
            pointer = struct.unpack('<Q', raw)[0]
            # Canonical records are already watched, including their identity
            # and body pointer. Noncanonical selected buffers need their own
            # before image because they may be rebound without moving a root.
            if pointer and pointer not in records:
                record = self.reader.read_or_raise(pointer, 0x460)
                body = struct.unpack_from('<Q', record, 0x88)[0]
                if not any(identity(record) == identity(value) and value[0x88:0x90] == record[0x88:0x90]
                           for value in records.values()):
                    raise ValueError('The selected editor buffer is not tied to a scanned roster player')
                if not body:
                    raise ValueError('The selected editor buffer has no body pointer')
                regions.append(Region('owner-copy:' + hex(pointer), pointer, record))
        return tuple({r.name: r for r in regions}.values())

    def discover(self, records, targets=None, *, cancel_event=None):
        self.records = records
        owners = defaultdict(list)
        for address, raw in records.items():
            owners[struct.unpack_from('<Q', raw, 0x88)[0]].append(address)
        targets = set(owners if targets is None else targets)
        # A failed refresh must never leave old approval for these targets.
        for body in targets:
            self.entries[body] = {'error': 'Ownership discovery did not finish'}
        coverage = []
        try:
            roots = self.context_regions(records)
            self.progress('Slow ownership discovery: scanning the entire readable process for unresolved selected bodies...')
            kwargs = {'cancel_event': cancel_event} if cancel_event is not None else {}
            references, coverage = scan_references(self.reader, {b for b in targets if b}, self.progress, **kwargs)
            from .mutable_fields import verified_mutation
            from .runtime_fields import runtime_definition
            renewed=[]
            for region in roots:
                raw=self.reader.read_or_raise(region.address,len(region.before))
                if raw!=region.before:
                    if (not region.name.startswith('owner-copy:')
                            or not verified_mutation(region.before,raw,runtime_definition()['fields'])
                            or self.reader.read_or_raise(region.address,len(raw))!=raw):
                        raise ValueError('Editor context changed during ownership discovery')
                renewed.append(Region(region.name,region.address,raw))
            roots=tuple(renewed)
        except (OSError, ValueError) as exc:
            coverage = getattr(exc, 'coverage', coverage)
            for body in targets:
                self.entries[body] = {'error': str(exc)}
            self.last_discovery = {'targets': len(targets), 'error': str(exc), 'read_bytes': sum(c['read'] for c in coverage),
                                   'coverage': coverage, 'complete': False,
                                   'retries': sum(c.get('retry',0)>0 for c in coverage)}
            return self.summary()
        self.canonical_allocations = {address: self.reader.allocation_key(address) for address in records}
        allocation_cache = {}
        for body in targets:
            try:
                canonical = owners.get(body, [])
                if not body or len(canonical) != 1:
                    raise ValueError('Body storage is missing or shared by canonical roster records')
                owner = canonical[0]
                baseline = records[owner]
                refs = references.get(body, set())
                if owner + 0x88 not in refs:
                    raise ValueError('The canonical body reference was not found in the ownership scan')
                copies = {}
                for reference in sorted(refs):
                    auxiliary = self.auxiliary_region(reference, body, owner)
                    if auxiliary is not None:
                        copies[auxiliary.address] = auxiliary
                        continue
                    address = reference - 0x88
                    raw = records.get(address)
                    if raw is None:
                        raw = self.reader.read_or_raise(address, 0x460)
                    if identity(raw) != identity(baseline) or raw[0x88:0x90] != struct.pack('<Q', body):
                        raise ValueError('Unresolved body owner at ' + hex(reference))
                    if address not in records:
                        copies[address] = Region('owner-copy:' + hex(address), address, raw)
                watched = tuple({r.name: r for r in (*roots, *copies.values())}.values())
                allocations = {}
                for address in {body, owner, *(r.address for r in watched)}:
                    if address not in allocation_cache:
                        allocation_cache[address] = self.reader.allocation_key(address)
                    allocations[address] = allocation_cache[address]
                self.entries[body] = {'owner': owner, 'structural_id': baseline[0x58:0x64],
                    'references': tuple(sorted(refs)), 'watched': watched, 'allocations': allocations}
            except (OSError, ValueError) as exc:
                self.entries[body] = {'error': str(exc)}
        self.last_discovery = {'targets': len(targets), 'read_bytes': sum(c['read'] for c in coverage), 'coverage': coverage,
                               'complete': True, 'retries': sum(c.get('retry',0)>0 for c in coverage)}
        return self.summary()

    def summary(self):
        return {'validated_bodies': sum('error' not in e for e in self.entries.values()),
                'blocked_bodies': sum('error' in e for e in self.entries.values()),
                'last_discovery': {k: v for k, v in self.last_discovery.items() if k != 'coverage'}}

    def require(self, body, owner):
        metrics = getattr(self.reader, 'metrics', None)
        entry = self.entries.get(body, {'error': 'No ownership evidence exists for this destination'})
        reason = entry.get('error')
        if reason is None:
            try:
                if entry['owner'] != owner:
                    raise ValueError('Canonical body owner changed')
                raw = self.reader.read_or_raise(owner, 0x460)
                if raw[0x58:0x64] != entry['structural_id'] or raw[0x88:0x90] != struct.pack('<Q', body):
                    raise ValueError('Destination identity or linked body pointer changed')
                for address, expected in entry['allocations'].items():
                    if self.reader.allocation_key(address) != expected:
                        raise ValueError('A body or owner allocation changed at ' + hex(address))
                actual = read_regions(self.reader, entry['watched'])
                renewed = []
                from .mutable_fields import verified_mutation
                from .runtime_fields import runtime_definition
                for region in entry['watched']:
                    new = actual[region.name]
                    if new != region.before:
                        if region.name.startswith('body-alias:'):
                            raise ValueError('Verified auxiliary body reference changed')
                        elif region.name.startswith('ownership-root:'):
                            pointer = struct.unpack('<Q', new)[0]
                            baseline = self.records.get(pointer)
                            if baseline is None:
                                raise ValueError('Selected editor context changed to an undiscovered copy; explicit ownership discovery is required')
                            target = self.reader.read_or_raise(pointer, 0x460)
                            if (target[0x58:0x64] != baseline[0x58:0x64] or target[0x88:0x90] != baseline[0x88:0x90]
                                    or self.reader.allocation_key(pointer) != self.canonical_allocations.get(pointer)):
                                raise ValueError('Selected canonical context identity, body or allocation changed')
                        elif not verified_mutation(region.before, new, runtime_definition()['fields']):
                            raise ValueError('A known owner changed outside verified mutable fields')
                        if self.reader.read_or_raise(region.address, len(new)) != new:
                            raise ValueError('Selected editor context changed during targeted validation')
                        metrics = getattr(self.reader, 'metrics', None)
                        if metrics: metrics.count('ownership_targeted_renewals')
                    renewed.append(Region(region.name, region.address, new))
                entry['watched'] = tuple(renewed)
                if metrics: metrics.count('ownership_cache_hits')
                return entry['watched']
            except (OSError, ValueError) as exc:
                reason = str(exc)
                # Invalidate only this entry. A later return to old bytes is
                # insufficient to renew evidence without explicit discovery.
                self.entries[body] = {'error': reason}
                if metrics: metrics.count('ownership_cache_invalidations')
        if metrics: metrics.count('ownership_cache_misses')
        raise ValueError(f'Cannot change height/wingspan for destination {hex(owner)} (body {hex(body)}): {reason}. '
                         'Inject automatically checks ownership for this lineup. If discovery failed, keep the roster open and retry Inject. No fields were skipped.')

    def describe(self, body):
        entry = self.entries.get(body, {})
        return {'body': hex(body), 'owner': hex(entry['owner']) if 'owner' in entry else None,
                'error': entry.get('error'), 'references': [hex(x) for x in entry.get('references', ())],
                'watched': [{'name': r.name, 'address': hex(r.address), 'size': len(r.before),
                             'sha256': sha256(r.before).hexdigest()} for r in entry.get('watched', ())]}
