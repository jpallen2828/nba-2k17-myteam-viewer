import struct
import pytest

from nba2k17_viewer import sculpt_ownership as module
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS


def record(name, pointer, index):
    raw = bytearray(0x460)
    raw[:len(name.encode('utf-16-le'))] = name.encode('utf-16-le')
    raw[0x24:0x2a] = 'Joe'.encode('utf-16-le')
    struct.pack_into('<I', raw, 0x58, index)
    struct.pack_into('<Q', raw, 0x80, pointer)
    struct.pack_into('<Q', raw, 0x88, 0x90000 + index * 0x1000)
    return bytes(raw)


class Reader:
    def __init__(self):
        self.records = {0x10000: record('First', 0x80000, 1),
                        0x20000: record('Second', 0x81000, 2)}
        self.data = {**self.records, 0x40000: self.records[0x10000],
                     0x80000: bytes(456), 0x81000: bytes(456),
                     **{rva: struct.pack('<Q', 0x10000) for rva in SELECTED_RECORD_POINTER_RVAS}}
        self.allocations = {}
        self.scans = 0

    def read(self, address, size):
        for base, value in self.data.items():
            if base <= address and address + size <= base + len(value):
                return value[address-base:address-base+size]
        raise OSError('Unmapped test address')

    read_or_raise = read

    def allocation_key(self, address):
        return (address & ~0xfff, self.allocations.get(address, 0))


@pytest.fixture
def cache(monkeypatch):
    reader = Reader()
    def scan(r, targets, progress, **kwargs):
        r.scans += 1
        refs = {0x80000: {0x10080, 0x40080}, 0x81000: {0x20080}}
        return {p: refs[p] for p in targets}, [{'read': 100}]
    monkeypatch.setattr(module, 'scan_references', scan)
    cache = module.SculptOwnershipCache(reader, 0)
    cache.discover(reader.records)
    return cache, reader


def test_discovery_is_session_scoped_and_reused(cache):
    cache, reader = cache
    assert cache.summary()['validated_sculpts'] == 2
    for _ in range(3):
        regions = cache.require(0x80000, 0x10000)
        assert any(r.name == 'sculpt-owner-copy:0x40000' for r in regions)
    assert reader.scans == 1
    fresh = module.SculptOwnershipCache(reader, 0)
    with pytest.raises(ValueError, match='No sculpt ownership evidence'):
        fresh.require(0x80000, 0x10000)


@pytest.mark.parametrize('change', ['sculpt_pointer', 'other_pointer', 'structural_id', 'alias_pointer',
                                  'alias_unknown', 'allocation_start', 'allocation_end'])
def test_changed_evidence_revokes_only_affected_sculpt(cache, change):
    cache, reader = cache
    if change.startswith('allocation'):
        reader.allocations[0x80000 if change.endswith('start') else 0x80000 + 455] = 1
    else:
        address = 0x40000 if change.startswith('alias') else 0x10000
        offset = {'sculpt_pointer': 0x80, 'other_pointer': 0x88, 'structural_id': 0x58,
                  'alias_pointer': 0x80, 'alias_unknown': 0x300}[change]
        raw = bytearray(reader.data[address]); raw[offset] ^= 1
        reader.data[address] = bytes(raw)
    with pytest.raises(ValueError, match='Cannot change facial sculpt'):
        cache.require(0x80000, 0x10000)
    cache.require(0x81000, 0x20000)
    assert reader.scans == 1


def test_display_name_and_mapped_alias_content_are_not_ownership(cache):
    cache, reader = cache
    raw = bytearray(reader.data[0x40000]); raw[:10] = 'Curry'.encode('utf-16-le')
    reader.data[0x40000] = bytes(raw)
    watched = cache.require(0x80000, 0x10000)
    assert next(r.before for r in watched if r.address == 0x40000) == bytes(raw)
    raw = bytearray(reader.data[0x10000]); raw[:10] = 'Other'.encode('utf-16-le')
    reader.data[0x10000] = bytes(raw)
    cache.require(0x80000, 0x10000)
    assert reader.scans == 1


def test_equal_names_do_not_allow_shared_canonical_storage(cache):
    cache, reader = cache
    reader.records[0x20000] = record('First', 0x80000, 2)
    reader.data[0x20000] = reader.records[0x20000]
    cache.discover(reader.records, {0x80000})
    with pytest.raises(ValueError, match='shared by canonical players'):
        cache.require(0x80000, 0x10000)


def test_unexpected_reference_and_failed_scan_do_not_keep_approval(cache, monkeypatch):
    cache, reader = cache
    monkeypatch.setattr(module, 'scan_references', lambda *a: ({0x80000: {0x10080, 0x20080}}, []))
    cache.discover(reader.records, {0x80000})
    with pytest.raises(ValueError, match='another canonical player'):
        cache.require(0x80000, 0x10000)
    def fail(*args):
        raise ValueError('Scan incomplete')
    monkeypatch.setattr(module, 'scan_references', fail)
    cache.discover(reader.records, {0x80000})
    with pytest.raises(ValueError, match='Scan incomplete'):
        cache.require(0x80000, 0x10000)


def test_selected_root_accepts_known_canonical_but_not_unknown_copy(cache):
    cache, reader = cache
    root = SELECTED_RECORD_POINTER_RVAS[0]
    reader.data[root] = struct.pack('<Q', 0x20000)
    cache.require(0x80000, 0x10000)
    reader.data[root] = struct.pack('<Q', 0x50000)
    with pytest.raises(ValueError, match='undiscovered copy'):
        cache.require(0x80000, 0x10000)


def test_unrelated_editor_is_watched_without_becoming_owner(cache):
    cache, reader = cache
    reader.data[0x60000] = record('Old', 0x99000, 999)
    reader.data[SELECTED_RECORD_POINTER_RVAS[0]] = struct.pack('<Q', 0x60000)
    cache.discover(reader.records, {0x80000})
    assert any(r.name == 'sculpt-owner-context:0x60000' for r in cache.require(0x80000, 0x10000))
    changed = bytearray(reader.data[0x60000]); changed[0] ^= 1
    reader.data[0x60000] = bytes(changed)
    with pytest.raises(ValueError, match='Unrelated editor context changed'):
        cache.require(0x80000, 0x10000)


def test_extent_guards_allow_page_boundary_with_same_allocation(cache):
    cache, reader = cache
    reader.allocation_key = lambda a: (0x80000, a & ~0xfff, 0x2000 - (a & 0x1000), 0x1000, 4, 0x20000)
    assert len(cache._allocations([(0x80ff0, 456)])) == 2
    reader.allocation_key = lambda a: (a & ~0xfff, a & ~0xfff, 0x1000, 0x1000, 4, 0x20000)
    with pytest.raises(ValueError, match='crosses incompatible allocations'):
        cache._allocations([(0x80ff0, 456)])


def test_payload_mutations_do_not_invalidate_pointer_ownership(cache):
    cache, reader = cache
    reader.data[0x80000] = bytes([55]) * 456
    cache.require(0x80000, 0x10000)
    with pytest.raises(ValueError, match='Unverified native sculpt extent'):
        module.SculptOwnershipCache(reader, 0, block_size=52)


def test_separately_authorized_origin_pointer_is_not_a_sculpt_binding(cache):
    cache, reader = cache
    raw = bytearray(reader.data[0x10000])
    struct.pack_into('<Q', raw, 0x70, 0x12340000)
    reader.data[0x10000] = bytes(raw)
    cache.require(0x80000, 0x10000)


def test_full_body_uses_separate_exact_pointer_extent_and_shared_scan(cache, monkeypatch):
    _, reader = cache
    body = 0x91000
    reader.data[body] = bytes(132)
    appearance = module.SculptOwnershipCache(reader, 0, pointer_offset=0x88, block_size=132)
    # No extra sweep is allowed: the root supplied the combined complete scan.
    monkeypatch.setattr(module, 'scan_references', lambda *a, **kw: pytest.fail('Duplicate scan'))
    appearance.discover(reader.records, {body}, discovery_result=(
        {body: {0x10088, 0x40088}, 0x80000: {0x10080, 0x40080}}, [{'read': 100}]))
    assert appearance.require(body, 0x10000)
    reader.allocations[body + 131] = 1
    with pytest.raises(ValueError, match='allocation changed'):
        appearance.require(body, 0x10000)


def test_incomplete_shared_scan_never_approves(cache):
    cache, reader = cache
    cache.discover(reader.records, {0x80000}, discovery_result=({}, []))
    with pytest.raises(ValueError, match='omitted requested linked targets'):
        cache.require(0x80000, 0x10000)
