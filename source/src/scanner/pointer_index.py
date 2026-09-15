"""Exact, unaligned pointer matching with shared suffix searches.

This changes search work, not its address/byte coverage. Every candidate still
requires an exact eight-byte match, including across chunk boundaries.
"""
from collections import defaultdict
import struct
from contextlib import nullcontext


class PointerMatcher:
    def __init__(self, targets):
        self.groups = defaultdict(dict)
        for target in targets:
            needle = struct.pack('<Q', target)
            self.groups[needle[3:]][needle] = target

    def find(self, raw, start):
        for suffix, needles in self.groups.items():
            position = raw.find(suffix, 3)
            while position >= 0:
                needle_start = position - 3
                target = needles.get(raw[needle_start:position + 5])
                if target is not None:
                    yield target, start + needle_start
                position = raw.find(suffix, position + 1)


class ScanFailure(ValueError):
    def __init__(self, reason, coverage, *, map_changed=False):
        self.coverage = list(coverage)
        self.map_changed = map_changed
        self.read_bytes = sum(c['read'] for c in coverage)
        super().__init__(reason)


def region_identity(region):
    return {k: getattr(region, k, None) for k in
            ('base_address', 'region_size', 'allocation_base', 'allocation_protect', 'state', 'protect', 'type')}


def scan_references(reader, targets, progress, *, chunk_size=8 * 1024 * 1024,
                    cancel_event=None, retries=2, page_size=4096, map_retries=2):
    """Restart invalidated scans; never combine references from different maps."""
    if not 0 <= map_retries <= 3:
        raise ValueError('Invalid memory map recovery bounds')
    history = []
    region_cache = {}
    targets = set(targets)
    for attempt in range(map_retries + 1):
        try:
            references, coverage = _scan_references_once(reader, targets, progress,
                chunk_size=chunk_size, cancel_event=cancel_event, retries=retries,
                page_size=page_size, region_cache=region_cache)
        except ScanFailure as exc:
            history.extend(dict(row, map_attempt=attempt, discarded=True) for row in exc.coverage)
            if not exc.map_changed or attempt == map_retries:
                raise ScanFailure(str(exc), history, map_changed=exc.map_changed) from exc
            metrics = getattr(reader, 'metrics', None)
            if metrics:
                metrics.count('ownership_map_restarts')
            progress(f'Memory map changed; refreshing changed ranges with a fresh map ({attempt + 1}/{map_retries}).')
        else:
            return references, history + [dict(row, map_attempt=attempt) for row in coverage]


def _scan_references_once(reader, targets, progress, *, chunk_size,
                          cancel_event, retries, page_size, region_cache):
    if chunk_size < 1 or page_size < 1 or not 0 <= retries <= 3:
        raise ValueError('Invalid scan recovery bounds')
    matcher = PointerMatcher(targets)
    references = {target: set() for target in targets}
    coverage, scanned, next_progress = [], 0, 256 * 1024 * 1024
    metrics = getattr(reader, 'metrics', None)
    def stage(name):
        return metrics.stage(name) if metrics else nullcontext()
    with stage('ownership_region_inventory'):
        regions = reader.readable_regions()
    identities = [tuple(region_identity(r).items()) for r in regions]
    keys = [(identities[i-1] if i else None, ident,
             identities[i+1] if i+1 < len(identities) else None)
            for i, ident in enumerate(identities)]
    valid_keys = set(keys)
    for old_key in list(region_cache):
        if old_key not in valid_keys:
            del region_cache[old_key]
    if metrics: metrics.count('exhaustive_scans')
    def check_cancel():
        if cancel_event is not None and cancel_event.is_set():
            raise ScanFailure('Ownership discovery cancelled; partial coverage is not approval.', coverage)

    def recover(address, size, region):
        for attempt in range(retries + 1):
            check_cancel()
            error = None
            with stage('ownership_memory_reads'):
                try:
                    raw = reader.read(address, size, allow_partial=True) or b''
                except OSError as exc:
                    raw, error = b'', str(exc)
            row = {'address': hex(address), 'requested': size, 'read': len(raw),
                   'retry': attempt, 'region': region_identity(region)}
            coverage.append(row)
            if error: row['error'] = error
            if metrics:
                metrics.count('ownership_bytes_received', len(raw))
                metrics.count('ownership_retries', int(attempt > 0))
            if len(raw) == size:
                yield address, raw
                return
            progress(f'Ownership read recovery at {hex(address)}: {len(raw)}/{size} bytes; retry {attempt}/{retries}')
            try:
                current = (reader.query_region(address) if hasattr(reader, 'query_region') else
                           next((r for r in reader.readable_regions() if r.base_address <= address < r.base_address+r.region_size), None))
                row['requeried_region'] = region_identity(current) if current is not None else None
                if current is None or region_identity(current) != region_identity(region):
                    raise ValueError('Allocation/protection changed; stable complete coverage cannot be established')
            except (OSError, ValueError) as exc:
                raise ScanFailure(f'Ownership unreadable span at {hex(address)} ({len(raw)}/{size} bytes): {exc}', coverage,
                                  map_changed=isinstance(exc, ValueError)) from exc
        if size <= page_size:
            raise ScanFailure(f'Ownership unreadable span at {hex(address)}: requested {size}, read {len(raw)} bytes after {retries} retries. No ownership approved.', coverage)
        split = max(page_size, (size // 2 // page_size) * page_size)
        split = min(split, size - 1)
        yield from recover(address, split, region)
        yield from recover(address + split, size - split, region)

    tail, previous_end = b'', None
    for index, region in enumerate(regions):
        check_cancel()
        # Neighbours are part of the key because a pointer may cross a region
        # boundary. Never reuse references from a resized/replaced range or
        # from either side of a changed boundary.
        cache_key = keys[index]
        if previous_end != region.base_address: tail = b''
        cached = region_cache.get(cache_key)
        if cached is not None:
            found, tail = cached
            coverage.append({'address': hex(region.base_address), 'requested': 0,
                'read': 0, 'retry': 0, 'region': region_identity(region),
                'reused_complete_region': True, 'covered_size': region.region_size})
            for target, reference in found:
                references[target].add(reference)
            previous_end = region.base_address + region.region_size
            if metrics: metrics.count('ownership_regions_reused')
            continue
        region_found = set()
        for offset in range(0, region.region_size, chunk_size):
            address = region.base_address + offset
            size = min(chunk_size, region.region_size - offset)
            for start, raw in recover(address, size, region):
                with stage('ownership_pointer_search'):
                    for target, reference in matcher.find(tail + raw, start - len(tail)):
                        references[target].add(reference)
                        region_found.add((target, reference))
                tail = (tail + raw)[-7:]
                scanned += len(raw)
                if scanned >= next_progress:
                    progress(f'Checking linked height/body ownership… {scanned // (1024 * 1024):,} MiB covered')
                    next_progress = scanned + 256 * 1024 * 1024
        previous_end = region.base_address + region.region_size
        region_cache[cache_key] = (region_found, tail)
    check_cancel()
    # A newly readable region could hold another owner. Neither disappearing
    # ranges nor newly committed ranges are silently excluded from evidence.
    if [region_identity(r) for r in reader.readable_regions()] != [region_identity(r) for r in regions]:
        raise ScanFailure('Readable memory inventory changed during discovery; complete ownership coverage is not established.', coverage, map_changed=True)
    return references, coverage
