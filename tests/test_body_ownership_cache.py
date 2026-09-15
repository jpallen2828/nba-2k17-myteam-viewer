from pathlib import Path
import struct
import sys
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'src')]
from nba2k17_viewer import body_ownership as module
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS


def record(last, body, index):
    raw = bytearray(0x460)
    name = last.encode('utf-16-le'); raw[:len(name)] = name
    raw[0x24:0x2a] = 'Joe'.encode('utf-16-le')
    struct.pack_into('<I', raw, 0x58, index)
    struct.pack_into('<Q', raw, 0x88, body)
    return bytes(raw)


class Reader:
    def __init__(self):
        self.records = {0x10000: record('First', 0x80000, 1), 0x20000: record('Second', 0x81000, 2)}
        self.data = {**self.records, 0x40000: self.records[0x10000],
                     0x80000: struct.pack('<ff', 190, 200), 0x81000: struct.pack('<ff', 200, 210),
                     **{rva: struct.pack('<Q', 0x10000) for rva in SELECTED_RECORD_POINTER_RVAS}}
        self.allocations = {}
        self.exhaustive_calls = 0

    def read(self, address, size):
        for base, value in self.data.items():
            if base <= address and address + size <= base + len(value):
                return value[address-base:address-base+size]
        raise OSError('Unmapped test address')

    read_or_raise = read

    def allocation_key(self, address):
        return (address & ~0xfff, self.allocations.get(address, 0))

    def readable_regions(self):
        raise AssertionError('Routine ownership validation must never enumerate process memory')


@pytest.fixture
def cache(monkeypatch):
    reader = Reader()
    def scan(r, targets, progress):
        r.exhaustive_calls += 1
        refs = {0x80000: {0x10088, 0x40088}, 0x81000: {0x20088}}
        return {b: refs[b] for b in targets}, [{'read': 100, 'requested': 100, 'address': '0x10000'}]
    monkeypatch.setattr(module, 'scan_references', scan)
    cache = module.BodyOwnershipCache(reader, 0, lambda _: None)
    cache.discover(reader.records)
    return cache, reader


def test_initial_discovery_is_reused_without_a_routine_sweep(cache):
    cache, reader = cache
    assert cache.summary()['validated_bodies'] == 2
    for _ in range(3):
        regions = cache.require(0x80000, 0x10000)
        assert any(r.name == 'owner-copy:0x40000' for r in regions)
        cache.require(0x81000, 0x20000)
    assert reader.exhaustive_calls == 1


@pytest.mark.parametrize('change', ['body_pointer', 'owner_identity', 'known_copy', 'allocation'])
def test_changed_evidence_blocks_only_affected_body_and_never_falls_back(cache, change):
    cache, reader = cache
    address = 0x40000 if change == 'known_copy' else 0x10000
    if change == 'allocation':
        reader.allocations[0x80000] = 1
    else:
        raw = bytearray(reader.data[address])
        offset = {'body_pointer': 0x88, 'owner_identity': 0x58, 'known_copy': 0x300}[change]
        raw[offset] ^= 1; reader.data[address] = bytes(raw)
    with pytest.raises(ValueError, match='Cannot change height/wingspan.*Inject automatically'):
        cache.require(0x80000, 0x10000)
    cache.require(0x81000, 0x20000)
    assert reader.exhaustive_calls == 1


def test_canonical_editor_context_change_receives_targeted_validation(cache):
    cache, reader = cache
    root = SELECTED_RECORD_POINTER_RVAS[0]
    reader.data[root] = struct.pack('<Q', 0x20000)
    cache.require(0x80000, 0x10000)
    reader.data[root] = struct.pack('<Q', 0x10000)
    cache.require(0x80000, 0x10000)
    assert reader.exhaustive_calls == 1


def test_verified_card_identity_changes_do_not_discard_structural_owner(cache):
    cache, reader = cache
    # Runtime's full expected-record check independently approves own writes.
    raw = bytearray(reader.data[0x10000]); raw[:10] = 'Curry'.encode('utf-16-le')
    reader.data[0x10000] = bytes(raw)
    cache.require(0x80000, 0x10000)
    assert reader.exhaustive_calls == 1


def test_failed_diagnostic_revokes_old_approval_without_touching_other_entries(cache, monkeypatch):
    cache, reader = cache
    def fail(*args):
        raise ValueError('Unreadable diagnostic span')
    monkeypatch.setattr(module, 'scan_references', fail)
    cache.discover(reader.records, {0x80000})
    with pytest.raises(ValueError, match='Unreadable diagnostic span'):
        cache.require(0x80000, 0x10000)
    cache.require(0x81000, 0x20000)


def test_shared_or_unrecognized_owner_is_not_approved(cache, monkeypatch):
    cache, reader = cache
    monkeypatch.setattr(module, 'scan_references', lambda *a: ({0x80000: {0x10088, 0x20088}}, []))
    cache.discover(reader.records, {0x80000})
    with pytest.raises(ValueError, match='Unresolved body owner'):
        cache.require(0x80000, 0x10000)
    cache.require(0x81000, 0x20000)


def test_missing_entry_blocks_height_instead_of_skipping_or_scanning(cache):
    cache, reader = cache
    with pytest.raises(ValueError, match='No ownership evidence exists'):
        cache.require(0x99999, 0x10000)
    assert reader.exhaustive_calls == 1


@pytest.mark.parametrize('base', [0, 0x140000000])
def test_controlled_auxiliary_alias_rebases_exactly_and_is_watched(monkeypatch, base):
    reader = Reader()
    owner, body, address = base + 0x10000, base + 0x80000, base + 0x90000
    raw = record('West', body, 1)
    auxiliary = bytearray(312)
    auxiliary[:8] = b'NoahData'
    struct.pack_into('<Q', auxiliary, 288, body)
    normalized = bytearray(auxiliary)
    struct.pack_into('<Q', normalized, 288, body-base)
    reader.records = {owner: raw}
    reader.data = {owner:raw, body:struct.pack('<ff',183,190), address:bytes(auxiliary),
                   **{base+rva:struct.pack('<Q',owner) for rva in SELECTED_RECORD_POINTER_RVAS}}
    definition = {'rva':0x90000,'owner_rva':0x10000,'size':312,'body_offset':288,
                  'rebase_offsets':[288],'normalized_hex':normalized.hex()}
    monkeypatch.setattr(module,'scan_references',lambda *args:({body:{owner+0x88,address+288}},[]))
    cache = module.BodyOwnershipCache(reader,base,lambda _:None,auxiliary_references=[definition])
    cache.discover(reader.records)
    watched = cache.require(body,owner)
    assert any(r.name.startswith('body-alias:') and r.before==bytes(auxiliary) for r in watched)
    # Like the game's own height edit, changing the body leaves the alias intact.
    reader.data[body] = struct.pack('<ff',190,200)
    cache.require(body,owner)
    auxiliary[40] ^= 1
    reader.data[address] = bytes(auxiliary)
    with pytest.raises(ValueError,match='auxiliary body reference changed'):
        cache.require(body,owner)
    cache.discover(reader.records)
    with pytest.raises(ValueError,match='auxiliary body reference changed'):
        cache.require(body,owner)


def test_new_session_cannot_inherit_live_addresses_or_old_approvals(cache):
    _, reader = cache
    new_cache = module.BodyOwnershipCache(reader, 0, lambda _: None)
    with pytest.raises(ValueError, match='No ownership evidence exists'):
        new_cache.require(0x80000, 0x10000)


def test_unknown_new_editor_copy_still_requires_discovery(cache):
    cache,reader=cache
    reader.data[0x50000]=reader.records[0x10000]
    reader.data[SELECTED_RECORD_POINTER_RVAS[0]]=struct.pack('<Q',0x50000)
    with pytest.raises(ValueError,match='undiscovered copy'):cache.require(0x80000,0x10000)
    assert reader.exhaustive_calls==1


def test_known_copy_mapped_tendency_edit_can_be_renewed(cache):
    from nba2k17_viewer.runtime_fields import runtime_definition
    from scanner.player_codec import encode
    cache,reader=cache
    field=next(f for f in runtime_definition()['fields'] if f['field']=='tendencies.touches')
    reader.data[0x40000]=encode(reader.data[0x40000],field,75)
    watched=cache.require(0x80000,0x10000)
    assert next(r.before for r in watched if r.address==0x40000)==reader.data[0x40000]
    assert reader.exhaustive_calls==1


def test_failed_scan_retains_bytes_and_retries_in_diagnostics(cache,monkeypatch):
    from scanner.pointer_index import ScanFailure
    cache,reader=cache
    def fail(*args,**kwargs):raise ScanFailure('persistent unreadable span',[{'read':500,'retry':0},{'read':2,'retry':1}])
    monkeypatch.setattr(module,'scan_references',fail)
    cache.discover(reader.records,{0x80000})
    assert cache.last_discovery['read_bytes']==502 and cache.last_discovery['retries']==1
    assert cache.last_discovery['complete'] is False
    with pytest.raises(ValueError,match='persistent unreadable'):cache.require(0x80000,0x10000)
