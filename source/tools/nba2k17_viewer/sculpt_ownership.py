"""Session-only ownership proof for the native linked facial sculpt payload.

The caller must retain its canonical record/table snapshots through transaction
validation. This cache adds exhaustive reference discovery and targeted checks
for linked storage and noncanonical editor aliases; it never writes memory.
"""
from collections import defaultdict
from hashlib import sha256
import struct

from scanner.injection_transaction import Region, read_regions
from scanner.pointer_index import scan_references
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS


POINTER_OFFSET = 0x80
RECORD_SIZE = 0x460


def sculpt_pointer(raw):
    return struct.unpack_from('<Q', raw, POINTER_OFFSET)[0]


def structural_binding(raw):
    # Destination-owned IDs and pointers are distinct from mutable names,
    # faces, ratings and other card content.
    # +0x70 is a separately verified origin-label pointer that normal card
    # injection may replace. The remaining linked pointers stay destination-owned.
    return raw[0x58:0x64], raw[0x78:0xc8]


class SculptOwnershipCache:
    def __init__(self, reader, module_base, progress=None, *, pointer_offset=0x80, block_size=456):
        if (pointer_offset, block_size) not in ((0x80, 456), (0x88, 132)):
            raise ValueError('Unverified native sculpt extent')
        self.reader, self.module_base = reader, module_base
        self.progress = progress or (lambda message: None)
        self.block_size = block_size
        self.pointer_offset = pointer_offset
        self.records, self.entries = {}, {}
        self.last_discovery = {}
        self.canonical_allocations = {}

    def pointer(self, raw):
        return struct.unpack_from('<Q', raw, self.pointer_offset)[0]

    def _context_regions(self, targets):
        regions = {}
        for rva in SELECTED_RECORD_POINTER_RVAS:
            address = self.module_base + rva
            raw = self.reader.read_or_raise(address, 8)
            regions[address] = Region('sculpt-ownership-root:' + hex(rva), address, raw)
            pointer = struct.unpack('<Q', raw)[0]
            if not pointer or pointer in self.records:
                continue
            record = self.reader.read_or_raise(pointer, RECORD_SIZE)
            owners = [a for a, value in self.records.items()
                      if structural_binding(value) == structural_binding(record)]
            if len(owners) == 1:
                kind = 'sculpt-owner-copy:'
            elif self.pointer(record) in targets:
                raise ValueError('Selected editor has unverified sculpt ownership')
            else:
                kind = 'sculpt-owner-context:'
            regions[pointer] = Region(kind + hex(pointer), pointer, record)
        return tuple(regions.values())

    def _allocations(self, spans):
        allocations = {}
        for address, size in spans:
            if address <= 0 or size <= 0:
                raise ValueError('Invalid sculpt or owner address')
            end = address + size - 1
            first, last = (self.reader.allocation_key(p) for p in (address, end))
            # VirtualQueryEx describes the remaining range from the queried
            # page, so BaseAddress/RegionSize may differ across a page boundary.
            if len(first) == 6 and len(last) == 6:
                if (first[0], *first[3:]) != (last[0], *last[3:]):
                    raise ValueError('Sculpt or owner extent crosses incompatible allocations')
            elif first != last:
                raise ValueError('Sculpt or owner extent crosses incompatible allocations')
            allocations[address], allocations[end] = first, last
        return allocations

    def discover(self, records, targets=None, *, cancel_event=None, discovery_result=None):
        self.records = dict(records)
        owners = defaultdict(list)
        for address, raw in self.records.items():
            owners[self.pointer(raw)].append(address)
        targets = set(owners if targets is None else targets)
        for pointer in targets:
            self.entries[pointer] = {'error': 'Sculpt ownership discovery did not finish'}
        if not targets:
            self.last_discovery = {'targets': 0, 'read_bytes': 0, 'complete': True, 'coverage': []}
            return self.summary()
        coverage = []
        try:
            contexts = self._context_regions(targets)
            self.progress('Checking ownership of selected facial sculpt storage...')
            kwargs = {'cancel_event': cancel_event} if cancel_event is not None else {}
            if discovery_result is None:
                references, coverage = scan_references(self.reader, {p for p in targets if p}, self.progress, **kwargs)
            else:
                # The caller may share a single complete scan between the two
                # verified linked payloads. It must never supply partial scans.
                references, coverage = discovery_result
                if not {p for p in targets if p}.issubset(references):
                    raise ValueError('Shared discovery omitted requested linked targets')
                if cancel_event is not None and cancel_event.is_set():
                    raise ValueError('Linked ownership discovery cancelled')
            if read_regions(self.reader, contexts) != {r.name: r.before for r in contexts}:
                raise ValueError('Editor context changed during sculpt ownership discovery')
            self.canonical_allocations = {a: self.reader.allocation_key(a) for a in self.records}
        except (OSError, ValueError) as exc:
            coverage = getattr(exc, 'coverage', coverage)
            for pointer in targets:
                self.entries[pointer] = {'error': str(exc)}
            self.last_discovery = {'targets': len(targets), 'error': str(exc), 'complete': False,
                                   'read_bytes': sum(c.get('read', 0) for c in coverage), 'coverage': coverage}
            return self.summary()
        for pointer in targets:
            try:
                canonical = owners.get(pointer, [])
                if not pointer or len(canonical) != 1:
                    raise ValueError('Sculpt storage is missing or shared by canonical players')
                owner = canonical[0]
                baseline = self.records[owner]
                if structural_binding(self.reader.read_or_raise(owner, RECORD_SIZE)) != structural_binding(baseline):
                    raise ValueError('Canonical sculpt binding changed during discovery')
                refs = references.get(pointer, set())
                if owner + self.pointer_offset not in refs:
                    raise ValueError('Canonical sculpt reference was absent from complete scan')
                watched = {r.address: r for r in contexts}
                for reference in sorted(refs):
                    address = reference - self.pointer_offset
                    if address == owner:
                        continue
                    if address in self.records:
                        raise ValueError('Sculpt reference belongs to another canonical player')
                    raw = self.reader.read_or_raise(address, RECORD_SIZE)
                    if (structural_binding(raw) != structural_binding(baseline)
                            or self.pointer(raw) != pointer
                            or self.reader.read_or_raise(address, RECORD_SIZE) != raw):
                        raise ValueError('Unresolved sculpt owner at ' + hex(reference))
                    watched[address] = Region('sculpt-owner-copy:' + hex(address), address, raw)
                allocations = self._allocations([(pointer, self.block_size), (owner, RECORD_SIZE),
                                                *((r.address, len(r.before)) for r in watched.values())])
                block = self.reader.read_or_raise(pointer, self.block_size)
                if self.reader.read_or_raise(pointer, self.block_size) != block:
                    raise ValueError('Sculpt payload changed during discovery')
                self.entries[pointer] = {'owner': owner, 'binding': structural_binding(baseline),
                                         'references': tuple(sorted(refs)), 'watched': tuple(watched.values()),
                                         'allocations': allocations}
            except (OSError, ValueError) as exc:
                self.entries[pointer] = {'error': str(exc)}
        self.last_discovery = {'targets': len(targets), 'complete': True,
                               'read_bytes': sum(c.get('read', 0) for c in coverage), 'coverage': coverage}
        return self.summary()

    def require(self, pointer, owner):
        entry = self.entries.get(pointer, {'error': 'No sculpt ownership evidence exists'})
        reason = entry.get('error')
        if reason is None:
            try:
                if entry['owner'] != owner:
                    raise ValueError('Canonical sculpt owner changed')
                raw = self.reader.read_or_raise(owner, RECORD_SIZE)
                if structural_binding(raw) != entry['binding'] or self.pointer(raw) != pointer:
                    raise ValueError('Destination structural identity or sculpt pointer changed')
                for address, allocation in entry['allocations'].items():
                    if self.reader.allocation_key(address) != allocation:
                        raise ValueError('Sculpt or owner allocation changed')
                actual = read_regions(self.reader, entry['watched'])
                renewed = []
                from .mutable_fields import verified_mutation
                from .runtime_fields import runtime_definition
                fields = runtime_definition()['fields']
                for region in entry['watched']:
                    new = actual[region.name]
                    if new != region.before:
                        if region.name.startswith('sculpt-ownership-root:'):
                            selected = struct.unpack('<Q', new)[0]
                            baseline = self.records.get(selected)
                            if baseline is None:
                                raise ValueError('Selected editor changed to an undiscovered copy')
                            fresh = self.reader.read_or_raise(selected, RECORD_SIZE)
                            if (structural_binding(fresh) != structural_binding(baseline)
                                    or self.reader.allocation_key(selected) != self.canonical_allocations.get(selected)):
                                raise ValueError('Selected canonical editor binding changed')
                        elif region.name.startswith('sculpt-owner-context:'):
                            raise ValueError('Unrelated editor context changed')
                        elif not verified_mutation(region.before, new, fields):
                            raise ValueError('Sculpt alias changed outside verified mutable fields')
                        if self.reader.read_or_raise(region.address, len(new)) != new:
                            raise ValueError('Editor changed during sculpt ownership validation')
                    renewed.append(Region(region.name, region.address, new))
                entry['watched'] = tuple(renewed)
                return entry['watched']
            except (OSError, ValueError) as exc:
                reason = str(exc)
                self.entries[pointer] = {'error': reason}
        raise ValueError(f'Cannot change facial sculpt for destination {hex(owner)} '
                         f'(sculpt {hex(pointer)}): {reason}. Refresh ownership before injecting.')

    def summary(self):
        return {'validated_sculpts': sum('error' not in e for e in self.entries.values()),
                'blocked_sculpts': sum('error' in e for e in self.entries.values()),
                'last_discovery': {k: v for k, v in self.last_discovery.items() if k != 'coverage'}}

    def describe(self, pointer):
        entry = self.entries.get(pointer, {})
        return {'sculpt': hex(pointer), 'owner': hex(entry['owner']) if 'owner' in entry else None,
                'error': entry.get('error'), 'references': [hex(x) for x in entry.get('references', ())],
                'watched': [{'name': r.name, 'address': hex(r.address), 'size': len(r.before),
                             'sha256': sha256(r.before).hexdigest()} for r in entry.get('watched', ())]}
