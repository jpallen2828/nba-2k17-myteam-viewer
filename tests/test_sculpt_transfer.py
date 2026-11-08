"""Offline native sculpt transfers through the real transaction and Undo paths."""
from collections import defaultdict
from copy import deepcopy
import json
import struct

import pytest

from nba2k17_viewer import body_ownership, sculpt_ownership
from nba2k17_viewer.roster_injection import RosterSession
from nba2k17_viewer.runtime_fields import runtime_definition
from nba2k17_viewer.sculpted_appearance import appearance_catalog, profile_appearance
from scanner.injection_metrics import Metrics
from scanner.injection_transaction import Region, Patch, TransactionFailure
from scanner.roster_record import SELECTED_RECORD_POINTER_RVAS


OWNER, SCULPT, BODY, SLOT = 0x10000, 0x80000, 0x90000, 0xa0000


class Memory:
    def __init__(self):
        raw = bytearray(0x460)
        raw[:8] = 'Host'.encode('utf-16-le')
        raw[0x24:0x2a] = 'Joe'.encode('utf-16-le')
        struct.pack_into('<I', raw, 0x58, 123)
        struct.pack_into('<H', raw, 0x64, 987)
        struct.pack_into('<Q', raw, 0x80, SCULPT)
        struct.pack_into('<Q', raw, 0x88, BODY)
        body = bytearray(bytes([44]) * 132)
        body[:8] = struct.pack('<ff', 200, 210)
        body[131] = 0xc0
        self.data = {OWNER: bytes(raw), SCULPT: bytes([22]) * 456 + b'sculpt-neighbor',
                     BODY: bytes(body) + b'body-neighbor', SLOT: struct.pack('<Q', OWNER),
                     **{rva: struct.pack('<Q', OWNER) for rva in SELECTED_RECORD_POINTER_RVAS}}
        self.calls = 0
        self.fail_call = None
        self.metrics = None

    def read(self, address, size):
        for base, value in self.data.items():
            if base <= address and address + size <= base + len(value):
                return value[address-base:address-base+size]
        raise OSError('Unmapped test destination')

    read_or_raise = read

    def write(self, address, value):
        self.calls += 1
        for base, raw in self.data.items():
            if base <= address and address + len(value) <= base + len(raw):
                start = address - base
                changed = bytearray(raw)
                if self.calls == self.fail_call:
                    changed[start] = value[0]
                    self.data[base] = bytes(changed)
                    raise OSError('Partial sculpt transfer')
                changed[start:start+len(value)] = value
                self.data[base] = bytes(changed)
                return
        raise OSError('Unmapped write')

    def allocation_key(self, address):
        return (address & ~0xfff, 0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def source_named(name='Yogi Ferrell'):
    return deepcopy(next(s for s in appearance_catalog()['sources'].values() if s['name'] == name))


def profile_for(source):
    return {'source_card_identity': {'name': source['name'], 'source_card_id': '20340', 'overall': 78},
            'reusable_roster_template': {
                'name': source['name'], 'source_record_sha256': source['source_record_sha256'],
                'native_sculpt_appearance': deepcopy(source),
                'defaults': {'identity': {'height_cm': 193.0}, 'appearance': {'wingspan_cm': 198.0}}}}


@pytest.fixture
def session(monkeypatch):
    import scanner.injection_transaction as transaction
    import scanner.discovery.live as live
    memory = Memory()
    monkeypatch.setattr(transaction, 'WindowsMemoryWriter', lambda pid: memory)
    monkeypatch.setattr(live, 'process_token', lambda reader: 'test-process')
    monkeypatch.setattr(body_ownership, 'scan_references',
                        lambda *args, **kwargs: ({BODY: {OWNER + 0x88}}, []))
    monkeypatch.setattr(sculpt_ownership, 'scan_references',
                        lambda *args, **kwargs: ({SCULPT: {OWNER + 0x80}}, []))
    s = RosterSession.__new__(RosterSession)
    s.reader, s.pid, s.module_base, s.process_start = memory, 1, 0, 'test-process'
    s.metrics, s.progress, s.registry = Metrics(), lambda text: None, runtime_definition()
    s.records = {OWNER: memory.data[OWNER]}
    s.reference_index = {OWNER: [{'reference': SLOT}]}
    s.body_owner_index = {BODY: [OWNER]}
    s.body_cache = body_ownership.BodyOwnershipCache(memory, 0, lambda text: None)
    s.sculpt_cache = sculpt_ownership.SculptOwnershipCache(memory, 0)
    s.appearance_cache = sculpt_ownership.SculptOwnershipCache(memory, 0, pointer_offset=0x88, block_size=132)
    s.body_cache.discover(s.records)
    s.sculpt_cache.discover(s.records)
    s.appearance_cache.discover(s.records, {BODY}, discovery_result=({BODY: {OWNER + 0x88}}, []))
    s.regions = [Region('player:' + hex(OWNER), OWNER, memory.data[OWNER]),
                 Region('slot', SLOT, memory.data[SLOT])]
    slot = {'player': OWNER, 'slot': 0, 'name': 'Joe Host', 'reference': SLOT}
    s.teams, s.references = [{'slots': [slot]}], [slot]
    s.public_scan = lambda: {'memory_source': 'offline test destination'}
    s.validate = lambda: memory.read(SLOT, 8) == struct.pack('<Q', OWNER)
    s.valid, s.blacktop_binding, s.last_run = True, None, None
    return s, memory


def prepare(s, memory, source=None, *, face=1):
    source = source if source is not None else source_named()
    profile = profile_for(source)
    resolved = {'values': {'gameplay_metadata': {'face_id': face}, 'identity': {'height_cm': 193.0}}}
    appearance = profile_appearance(profile, resolved, s.registry)
    raw = memory.read(OWNER, 0x460)
    after = raw[:100] + face.to_bytes(2, 'little') + raw[102:]
    s.records[OWNER] = raw
    s.regions = [Region('player:' + hex(OWNER), OWNER, raw), Region('slot', SLOT, memory.read(SLOT, 8))]
    s.patches = [Patch('player:' + hex(OWNER), 100, raw[100:102], after[100:102],
                       'gameplay_metadata.face_id', 'a' * 64)] if raw != after else []
    s.extra_regions = []
    s.plans = [{'destination': {'player': OWNER, 'name': 'Joe Host'},
                'card_identity': profile['source_card_identity'], 'resolved': resolved,
                'native_sculpt_appearance': appearance, 'unsupported': [],
                'after_hex': after.hex(), 'fields': [{'field': 'gameplay_metadata.face_id', 'decoded_after': face}]}]
    s._prepare_bodies([profile], 'a' * 64)
    s._prepare_sculpts('a' * 64)
    by_span = {}
    for region in s.regions + s.extra_regions:
        key = region.address, len(region.before)
        if key in by_span:
            assert by_span[key].before == region.before
        else:
            by_span[key] = region
    s.transaction_regions, s.patches = tuple(by_span.values()), tuple(s.patches)
    s.prepared_report = {'players': s.plans, 'remaining_limits': []}
    return profile


def test_saved_source_absent_from_destination_transfers_and_undo_restores_all(session, tmp_path):
    s, memory = session
    before = deepcopy(memory.data)
    source = source_named()
    prepare(s, memory, source)
    assert all(b'Yogi' not in raw for raw in memory.data.values())
    assert len(next(r.before for r in s.transaction_regions if r.name.startswith('sculpt:'))) == 456
    assert len(next(r.before for r in s.transaction_regions if r.name.startswith('body:'))) == 132
    result = s.write(tmp_path / 'injection')
    assert result['all_fields_match']
    assert memory.read(SCULPT, 456).hex() == source['sculpt_hex']
    expected_body = bytearray.fromhex(source['body_hex'])
    expected_body[:8] = struct.pack('<ff', 193, 198)
    expected_body[131] = (expected_body[131] & 0x3f) | 0xc0
    assert memory.read(BODY, 132) == bytes(expected_body)
    assert memory.data[BODY][132:] == b'body-neighbor'
    assert memory.data[SCULPT][456:] == b'sculpt-neighbor'
    assert memory.read(OWNER + 0x78, 0x18) == before[OWNER][0x78:0x90]
    checks = json.loads((tmp_path / 'injection/readback.json').read_text())
    assert any(c['field'] == 'appearance.native_sculpt' and c['bytes'] == 456 for c in checks)
    undone = s.undo()
    assert undone['restored_all_watched_regions']
    assert memory.data == before


def test_repeated_different_saved_sculpts_reuse_destination_pointers(session, tmp_path):
    s, memory = session
    pointers = memory.read(OWNER + 0x78, 0x18)
    for index, name in enumerate(('Yogi Ferrell', 'Jonathan Gibson', 'Domantas Sabonis', 'Yogi Ferrell')):
        source = source_named(name)
        prepare(s, memory, source)
        s.write(tmp_path / str(index))
        assert memory.read(SCULPT, 456).hex() == source['sculpt_hex']
        assert memory.read(OWNER + 0x78, 0x18) == pointers


def test_changed_sculpt_pointer_is_rejected_before_writing(session, tmp_path):
    s, memory = session
    prepare(s, memory)
    raw = bytearray(memory.data[OWNER]); struct.pack_into('<Q', raw, 0x80, SCULPT + 0x1000)
    memory.data[OWNER] = bytes(raw)
    with pytest.raises(ValueError, match='Watched region'):
        s.write(tmp_path / 'changed')
    assert memory.calls == 0


def test_partial_sculpt_write_rolls_back_inline_body_and_sculpt(session, tmp_path):
    s, memory = session
    before = deepcopy(memory.data)
    prepare(s, memory)
    memory.fail_call = 3
    with pytest.raises(TransactionFailure) as exc:
        s.write(tmp_path / 'partial')
    assert exc.value.report['rollback_verified']
    assert memory.data == before


def test_regular_cyberface_does_not_copy_native_sculpt_or_body_style(session, tmp_path):
    s, memory = session
    sculpt = memory.data[SCULPT]
    body_tail = memory.data[BODY][8:]
    prepare(s, memory, face=8569)
    assert not any(p.region.startswith('sculpt:') for p in s.patches)
    assert len(next(r.before for r in s.transaction_regions if r.name.startswith('body:'))) == 8
    s.write(tmp_path / 'ordinary')
    assert memory.data[SCULPT] == sculpt
    assert memory.data[BODY][8:] == body_tail


@pytest.mark.parametrize('change', ['missing', 'sculpt', 'body', 'source_identity', 'other_player', 'build'])
def test_missing_or_tampered_native_source_cannot_silently_preserve_host(change):
    source = source_named()
    profile = profile_for(source)
    resolved = {'values': {'gameplay_metadata': {'face_id': 1}}}
    registry = runtime_definition()
    stored = profile['reusable_roster_template']['native_sculpt_appearance']
    if change == 'missing':
        del profile['reusable_roster_template']['native_sculpt_appearance']
    elif change in ('sculpt', 'body'):
        stored[change + '_hex'] = 'ff' + stored[change + '_hex'][2:]
    elif change == 'source_identity':
        stored['source_record_sha256'] = '0' * 64
    elif change == 'other_player':
        profile['source_card_identity']['name'] = 'Other Player'
    else:
        registry['executable_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        profile_appearance(profile, resolved, registry)


def test_skin_type_encoding_preserves_every_adjacent_bit_and_birth_field():
    from scanner.player_codec import encode, decode
    field = next(f for f in runtime_definition()['fields'] if f['field'] == 'gameplay_metadata.skin_type')
    assert (field['offset'], field['size'], field['shift'], field['bits']) == (0x138, 1, 1, 3)
    for before_value in range(256):
        raw = bytearray([0xa5] * 0x460)
        raw[0x138] = before_value
        for skin in range(8):
            after = encode(bytes(raw), field, skin)
            assert decode(after, field) == skin
            assert ((after[0x138] ^ before_value) & 0xf1) == 0
            assert after[:0x138] == raw[:0x138] and after[0x139:] == raw[0x139:]
