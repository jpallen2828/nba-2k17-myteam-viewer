from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import struct
from typing import Any, Callable, Iterable

from .constants import MAX_SEARCH_RESULTS, SCAN_CHUNK_BYTES
from .models import MemoryRegion
from .windows_process import ProcessReader


NUMERIC_TYPES: dict[str, str] = {
    "u8": "<B", "i8": "<b",
    "u16": "<H", "i16": "<h",
    "u32": "<I", "i32": "<i",
    "u64": "<Q", "i64": "<q",
    "float32": "<f", "float64": "<d",
    "pointer": "<Q",
}
STRING_TYPES = {"ascii": "ascii", "utf8": "utf-8", "utf16le": "utf-16-le"}


def is_canonical_user_pointer(value: int) -> bool:
    """Return whether a value is in the canonical user-mode x64 address range."""
    return 0x10000 <= int(value) <= 0x00007FFFFFFFFFFF


def region_for_address(regions: list[MemoryRegion], address: int, size: int = 1) -> MemoryRegion | None:
    for region in regions:
        if region.base_address <= address and address + size <= region.end_address:
            return region
        if region.base_address > address:
            return None
    return None


@dataclass(slots=True)
class SearchHit:
    address: int
    data_type: str
    value: Any
    raw_hex: str
    region_base: int | None = None
    region_category: str | None = None
    module_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["address_hex"] = f"0x{self.address:X}"
        if self.region_base is not None:
            data["region_base_hex"] = f"0x{self.region_base:X}"
        return data


@dataclass(slots=True)
class SearchResults:
    data_type: str
    query: str
    hits: list[SearchHit]
    scanned_bytes: int
    scanned_regions: int
    read_failures: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "data_type": self.data_type,
            "query": self.query,
            "hits": [hit.to_dict() for hit in self.hits],
            "scanned_bytes": self.scanned_bytes,
            "scanned_regions": self.scanned_regions,
            "read_failures": self.read_failures,
            "truncated": self.truncated,
        }


def parse_numeric(value: str, data_type: str):
    if data_type.startswith("float"):
        return float(value)
    return int(value.strip(), 0)


def encode_query(value: str, data_type: str) -> bytes:
    if data_type in NUMERIC_TYPES:
        return struct.pack(NUMERIC_TYPES[data_type], parse_numeric(value, data_type))
    if data_type in STRING_TYPES:
        return value.encode(STRING_TYPES[data_type])
    if data_type == "bytes":
        cleaned = value.replace("0x", "").replace(" ", "").replace("-", "")
        if not cleaned or len(cleaned) % 2:
            raise ValueError("Byte sequence must contain complete hexadecimal bytes")
        return bytes.fromhex(cleaned)
    raise ValueError(f"Unsupported search type: {data_type}")


def decode_value(raw: bytes, data_type: str):
    if data_type in NUMERIC_TYPES:
        return struct.unpack(NUMERIC_TYPES[data_type], raw)[0]
    if data_type in STRING_TYPES:
        return raw.decode(STRING_TYPES[data_type], errors="replace")
    return raw.hex().upper()


def find_all(data: bytes, needle: bytes, alignment: int = 1, base_address: int = 0) -> Iterable[int]:
    if not needle:
        return
    cursor = 0
    while True:
        offset = data.find(needle, cursor)
        if offset < 0:
            return
        absolute = base_address + offset
        if alignment <= 1 or absolute % alignment == 0:
            yield offset
        cursor = offset + 1


class MemoryScanner:
    def __init__(self, process: ProcessReader):
        self.process = process

    def exact_scan(
        self,
        query: str,
        data_type: str,
        *,
        regions: list[MemoryRegion] | None = None,
        alignment: int = 1,
        max_results: int = MAX_SEARCH_RESULTS,
        cancel_event=None,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> SearchResults:
        needle = encode_query(query, data_type)
        selected = regions or self.process.readable_regions()
        total_bytes = sum(region.region_size for region in selected)
        scanned_bytes = 0
        failures = 0
        hits: list[SearchHit] = []
        seen_addresses: set[int] = set()
        truncated = False
        for region_index, region in enumerate(selected, 1):
            offset = 0
            overlap = max(0, len(needle) - 1)
            while offset < region.region_size:
                if cancel_event is not None and cancel_event.is_set():
                    return SearchResults(data_type, query, hits, scanned_bytes, region_index - 1, failures, True)
                read_size = min(SCAN_CHUNK_BYTES, region.region_size - offset)
                address = region.base_address + offset
                data = self.process.read(address, read_size, allow_partial=True)
                if not data:
                    failures += 1
                else:
                    for found in find_all(data, needle, alignment, address):
                        hit_address = address + found
                        if hit_address in seen_addresses:
                            continue
                        seen_addresses.add(hit_address)
                        hits.append(SearchHit(
                            address=hit_address,
                            data_type=data_type,
                            value=decode_value(needle, data_type),
                            raw_hex=needle.hex().upper(),
                            region_base=region.base_address,
                            region_category=region.category,
                            module_name=region.module_name,
                        ))
                        if len(hits) >= max_results:
                            truncated = True
                            break
                scanned_bytes += len(data) if data else read_size
                if progress:
                    progress(scanned_bytes, total_bytes, len(hits))
                if truncated:
                    break
                advance = read_size - overlap if read_size > overlap else read_size
                if advance <= 0:
                    break
                offset += advance
            if truncated:
                break
        return SearchResults(data_type, query, hits, scanned_bytes, len(selected), failures, truncated)

    def refine_live(
        self,
        previous: SearchResults,
        mode: str,
        exact_value: str | None = None,
        *,
        cancel_event=None,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> SearchResults:
        data_type = previous.data_type
        if data_type not in NUMERIC_TYPES:
            raise ValueError("Live candidate refinement currently supports numeric and pointer types")
        size = struct.calcsize(NUMERIC_TYPES[data_type])
        exact = parse_numeric(exact_value, data_type) if exact_value is not None else None
        hits: list[SearchHit] = []
        failures = 0
        for index, old_hit in enumerate(previous.hits, 1):
            if cancel_event is not None and cancel_event.is_set():
                break
            raw = self.process.read(old_hit.address, size)
            if raw is None:
                failures += 1
                continue
            current = decode_value(raw, data_type)
            if _matches(mode, old_hit.value, current, exact):
                hits.append(SearchHit(
                    address=old_hit.address,
                    data_type=data_type,
                    value=current,
                    raw_hex=raw.hex().upper(),
                    region_base=old_hit.region_base,
                    region_category=old_hit.region_category,
                    module_name=old_hit.module_name,
                ))
            if progress and (index % 1000 == 0 or index == len(previous.hits)):
                progress(index, len(previous.hits), len(hits))
        return SearchResults(data_type, f"refine:{mode}", hits, len(previous.hits) * size,
                             previous.scanned_regions, failures, False)

    def pointer_xrefs(
        self,
        target: int,
        *,
        depth: int = 1,
        regions: list[MemoryRegion] | None = None,
        alignment: int = 8,
        max_per_level: int = 64,
        cancel_event=None,
        progress: Callable[[int, int, int], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Find a bounded, shallow graph of readable pointers to a known address."""
        if not is_canonical_user_pointer(target):
            raise ValueError(f"0x{target:X} is not a canonical user-mode x64 pointer")
        depth = min(3, max(1, int(depth)))
        selected = sorted(regions or self.process.readable_regions(), key=lambda item: item.base_address)
        if region_for_address(selected, target) is None:
            raise ValueError(f"Target 0x{target:X} is not in committed readable memory")
        edges: list[dict[str, Any]] = []
        frontier = [target]
        seen_targets = {target}
        total_per_scan = sum(region.region_size for region in selected)
        completed_scans = 0
        for level in range(1, depth + 1):
            next_frontier: list[int] = []
            current_frontier = frontier[:max_per_level]
            level_base = completed_scans
            for target_index, pointed_to in enumerate(current_frontier):
                if cancel_event is not None and cancel_event.is_set():
                    return edges

                def update(done: int, _total: int, candidates: int) -> None:
                    if progress:
                        progress((level_base + target_index) * total_per_scan + done,
                                 max(1, (level_base + len(current_frontier)) * total_per_scan), candidates)

                results = self.exact_scan(
                    hex(pointed_to), "pointer", regions=selected, alignment=alignment,
                    max_results=max_per_level, cancel_event=cancel_event, progress=update,
                )
                completed_scans += 1
                for hit in results.hits:
                    source_region = region_for_address(selected, hit.address, 8)
                    if source_region is None:
                        continue
                    edge = {
                        "level": level,
                        "source_address": hit.address,
                        "source_address_hex": f"0x{hit.address:X}",
                        "target_address": pointed_to,
                        "target_address_hex": f"0x{pointed_to:X}",
                        "source_region": source_region.to_dict(),
                        "validated": True,
                    }
                    edges.append(edge)
                    if hit.address not in seen_targets:
                        seen_targets.add(hit.address)
                        next_frontier.append(hit.address)
            if not next_frontier:
                break
            frontier = next_frontier[:max_per_level]
        return edges

    def inspect_neighborhood(self, address: int, radius: int = 0x100) -> dict[str, Any]:
        regions = sorted(self.process.readable_regions(), key=lambda item: item.base_address)
        region = region_for_address(regions, address)
        if region is None:
            raise ValueError(f"Address 0x{address:X} is not currently readable")
        start = max(region.base_address, address - max(0, radius))
        end = min(region.end_address, address + max(0, radius))
        data = self.process.read(start, end - start, allow_partial=True) or b""
        pointer_raw = self.process.read(address, 8)
        pointer_value = struct.unpack("<Q", pointer_raw)[0] if pointer_raw else None
        center_offset = address - start
        interpretations: dict[str, Any] = {}
        for data_type, format_code in NUMERIC_TYPES.items():
            size = struct.calcsize(format_code)
            if center_offset + size > len(data):
                continue
            value = struct.unpack_from(format_code, data, center_offset)[0]
            interpretations[data_type] = {
                "value": str(value) if isinstance(value, float) and not math.isfinite(value) else value,
                "raw_hex": data[center_offset:center_offset + size].hex().upper(),
            }
        hex_rows = []
        for offset in range(0, len(data), 16):
            row = data[offset:offset + 16]
            hex_rows.append({
                "address": start + offset,
                "address_hex": f"0x{start + offset:X}",
                "hex": " ".join(f"{value:02X}" for value in row),
                "ascii": "".join(chr(value) if 0x20 <= value <= 0x7E else "." for value in row),
            })
        return {
            "address": address,
            "address_hex": f"0x{address:X}",
            "start": start,
            "start_hex": f"0x{start:X}",
            "size": len(data),
            "region": region.to_dict(),
            "raw_hex": data.hex().upper(),
            "hex_rows": hex_rows,
            "interpretations_at_address": interpretations,
            "pointer_value": pointer_value,
            "pointer_value_hex": f"0x{pointer_value:X}" if pointer_value is not None else None,
            "pointer_target_readable": bool(pointer_value is not None and is_canonical_user_pointer(pointer_value)
                                            and region_for_address(regions, pointer_value) is not None),
        }


def _matches(mode: str, before: Any, after: Any, exact: Any = None) -> bool:
    if isinstance(after, float) and math.isnan(after):
        return False
    if mode in {"equal", "unchanged"}:
        return after == before
    if mode in {"not_equal", "changed"}:
        return after != before
    if mode == "increased":
        return after > before
    if mode == "decreased":
        return after < before
    if mode == "exact":
        return after == exact
    raise ValueError(f"Unsupported comparison mode: {mode}")
