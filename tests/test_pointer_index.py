from pathlib import Path
import random
import struct
import sys
from types import SimpleNamespace
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from scanner.pointer_index import PointerMatcher, scan_references


def exhaustive(raw, targets, start=0):
    found = set()
    for target in targets:
        needle = struct.pack('<Q', target)
        offset = raw.find(needle)
        while offset >= 0:
            found.add((target, start + offset))
            offset = raw.find(needle, offset + 1)
    return found


def test_shared_search_matches_exhaustive_at_every_alignment_and_overlaps():
    targets = [0, 0x101010101010101, 0x7ff76587c570, 0x7ff76587c5f4, 0x123456789abc]
    rng = random.Random(17)
    raw = bytearray(rng.randbytes(10000))
    for i in range(100):
        offset = rng.randrange(len(raw) - 8)
        raw[offset:offset + 8] = struct.pack('<Q', rng.choice(targets))
    raw[100:130] = b'\0' * 30
    assert set(PointerMatcher(targets).find(bytes(raw), 0x1000)) == exhaustive(bytes(raw), targets, 0x1000)


def test_every_chunk_boundary_and_unaligned_pointer_is_covered():
    targets = [0x7ff76587c570, 0x7ff76587c5f4]
    for offset in range(1, 64):
        data = bytearray(100)
        data[offset:offset+8] = struct.pack('<Q', targets[offset % 2])
        reader = SimpleNamespace(readable_regions=lambda: [SimpleNamespace(base_address=1000, region_size=len(data))],
                                 read=lambda address, size, **kw: bytes(data[address-1000:address-1000+size]))
        references, coverage = scan_references(reader, targets, lambda _: None, chunk_size=16)
        assert {(target, address) for target, values in references.items() for address in values} == exhaustive(data, targets, 1000)
        assert sum(c['read'] for c in coverage) == len(data)


def test_unreadable_span_never_yields_successful_ownership():
    reader = SimpleNamespace(readable_regions=lambda: [SimpleNamespace(base_address=1000, region_size=100)],
                             read=lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match='unreadable span'):
        scan_references(reader, [0x7ff76587c570], lambda _: None)


def test_transient_reads_retry_and_large_failures_split_without_losing_boundaries():
    data=bytearray(128); target=0x7ff76587c570
    for offset in (5,31,63,95):data[offset:offset+8]=struct.pack('<Q',target)
    region=SimpleNamespace(base_address=1000,region_size=len(data))
    calls={}
    def read(address,size,**kw):
        calls[address,size]=calls.get((address,size),0)+1
        if size>32 or calls[address,size]==1:return bytes(data[address-1000:address-1000+3])
        return bytes(data[address-1000:address-1000+size])
    reader=SimpleNamespace(readable_regions=lambda:[region],query_region=lambda a:region,read=read)
    refs,coverage=scan_references(reader,[target],lambda _:None,chunk_size=128,page_size=16)
    assert {(t,a) for t,values in refs.items() for a in values}==exhaustive(data,[target],1000)
    assert max(calls.values())<=3 and any(c['retry'] for c in coverage)


def test_failure_preserves_partial_diagnostics_and_changed_ranges_fail_closed():
    from scanner.pointer_index import ScanFailure
    region=SimpleNamespace(base_address=1000,region_size=100)
    reader=SimpleNamespace(readable_regions=lambda:[region],query_region=lambda a:None,
        read=lambda address,size,**kw: b'x'*size if address==1000 else b'x')
    with pytest.raises(ScanFailure) as error:
        scan_references(reader,[1],lambda _:None,chunk_size=50)
    assert error.value.read_bytes==153
    assert {row['map_attempt'] for row in error.value.coverage} == {0, 1, 2}
    assert error.value.coverage[-1]['address']==hex(1050)
    assert error.value.coverage[-1]['requeried_region'] is None


def test_adjacent_regions_match_cross_boundary_and_inventory_changes_reject():
    from scanner.pointer_index import ScanFailure
    target=0x123456789abcdef;data=b'xxx'+struct.pack('<Q',target)+b'yyy'
    regions=[SimpleNamespace(base_address=1000,region_size=7),SimpleNamespace(base_address=1007,region_size=7)]
    reader=SimpleNamespace(readable_regions=lambda:regions,read=lambda a,n,**kw:data[a-1000:a-1000+n])
    refs,_=scan_references(reader,[target],lambda _:None,chunk_size=2)
    assert refs[target]=={1003}
    calls=[]
    def inventory():
        calls.append(1);return regions if len(calls)==1 else regions[:1]
    reader.readable_regions=inventory
    with pytest.raises(ScanFailure,match='inventory changed'):scan_references(reader,[target],lambda _:None,map_retries=0)


def test_restart_discards_old_references_and_covers_new_allocations():
    target=0x123456789abcdef
    old=SimpleNamespace(base_address=1000,region_size=16)
    new=SimpleNamespace(base_address=2000,region_size=16)
    calls=[]
    def inventory():
        calls.append(1)
        return [old] if len(calls)==1 else [new]
    reader=SimpleNamespace(readable_regions=inventory,
        read=lambda a,n,**kw: struct.pack('<Q',target)+b'x'*8)
    refs,coverage=scan_references(reader,[target],lambda _:None)
    assert refs[target]=={2000}
    assert coverage[0]['discarded']
    assert coverage[-1]['map_attempt']==1


def test_shrunken_region_recovers_with_fresh_full_inventory():
    target=0x123456789abcdef
    old=SimpleNamespace(base_address=1000,region_size=32)
    new=SimpleNamespace(base_address=1000,region_size=16)
    calls=[]
    def inventory():
        calls.append(1)
        return [old] if len(calls)==1 else [new]
    reader=SimpleNamespace(readable_regions=inventory,query_region=lambda a:new,
        read=lambda a,n,**kw: struct.pack('<Q',target)+b'x'*8)
    refs,coverage=scan_references(reader,[target],lambda _:None)
    assert refs[target]=={1000}
    assert len(calls)==3
    assert coverage[0]['discarded'] and coverage[-1]['read']==16


def test_cancelled_scan_never_returns_partial_approval():
    from threading import Event
    from scanner.pointer_index import ScanFailure
    event=Event();event.set()
    reader=SimpleNamespace(readable_regions=lambda:[SimpleNamespace(base_address=1000,region_size=100)])
    with pytest.raises(ScanFailure,match='cancelled'):scan_references(reader,[1],lambda _:None,cancel_event=event)


def test_recovery_reuses_only_regions_with_unchanged_neighbours():
    target=0x123456789abcdef
    stable=[SimpleNamespace(base_address=a,region_size=16) for a in (1000,2000,3000)]
    old=SimpleNamespace(base_address=4000,region_size=16)
    new=SimpleNamespace(base_address=5000,region_size=16)
    inventories=[]; reads=[]
    def inventory():
        inventories.append(1)
        return stable + [old if len(inventories)==1 else new]
    def read(a,n,**kw):
        reads.append(a)
        return struct.pack('<Q',target)+b'x'*8
    reader=SimpleNamespace(readable_regions=inventory,read=read)
    refs,coverage=scan_references(reader,[target],lambda _:None)
    assert refs[target]=={1000,2000,3000,5000}
    assert reads.count(1000)==reads.count(2000)==1
    assert reads.count(3000)==2  # Changed neighbour forces boundary recheck.
    assert any(row.get('reused_complete_region') for row in coverage)
