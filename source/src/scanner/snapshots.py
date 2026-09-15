from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
import struct
from bisect import bisect_right
from typing import Any, Callable, Iterator
import zipfile

from .constants import CAPTURE_CHUNK_BYTES, CAPTURE_HARD_LIMIT_BYTES, MAX_COMPARE_RESULTS
from .models import MemoryRegion
from .search import NUMERIC_TYPES, SearchHit, SearchResults, decode_value
from .windows_process import ProcessReader


SNAPSHOT_FORMAT = "nba2k17-myteam-memory-snapshot/v2"
SUPPORTED_SNAPSHOT_FORMATS = {
    "nba2k17-myteam-memory-snapshot/v1",
    SNAPSHOT_FORMAT,
}


@dataclass(slots=True)
class SnapshotChunk:
    address: int
    size: int
    entry: str
    sha256: str
    region_base: int
    allocation_base: int
    region_size: int
    region_protect: int
    region_type: int
    region_category: str
    module_name: str | None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["address_hex"] = f"0x{self.address:X}"
        data["region_base_hex"] = f"0x{self.region_base:X}"
        data["allocation_base_hex"] = f"0x{self.allocation_base:X}"
        return data


def estimate_capture_bytes(regions: list[MemoryRegion]) -> int:
    return sum(region.region_size for region in regions)


def capture_snapshot(
    process: ProcessReader,
    path: Path,
    *,
    label: str,
    observations: dict[str, Any],
    executable_fingerprint: dict[str, Any],
    session_id: str,
    regions: list[MemoryRegion] | None = None,
    hard_limit: int = CAPTURE_HARD_LIMIT_BYTES,
    cancel_event=None,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    selected = regions or process.readable_regions()
    estimated = estimate_capture_bytes(selected)
    if estimated > hard_limit:
        raise ValueError(
            f"Requested capture covers {estimated:,} readable bytes, above the {hard_limit:,}-byte hard limit. "
            "Use exact-value scans or narrow the region set."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    chunks: list[SnapshotChunk] = []
    skipped: list[dict[str, Any]] = []
    processed = 0
    process_info = process.process_info()
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            for region_index, region in enumerate(selected, 1):
                offset = 0
                while offset < region.region_size:
                    if cancel_event is not None and cancel_event.is_set():
                        raise InterruptedError("Memory capture cancelled")
                    size = min(CAPTURE_CHUNK_BYTES, region.region_size - offset)
                    address = region.base_address + offset
                    data = process.read(address, size, allow_partial=True)
                    if not data:
                        skipped.append({
                            "address": address,
                            "address_hex": f"0x{address:X}",
                            "requested_size": size,
                            "reason": "ReadProcessMemory failed or page changed",
                        })
                    else:
                        entry = f"regions/{address:016X}-{len(data):08X}.bin"
                        archive.writestr(entry, data)
                        chunks.append(SnapshotChunk(
                            address=address,
                            size=len(data),
                            entry=entry,
                            sha256=hashlib.sha256(data).hexdigest().upper(),
                            region_base=region.base_address,
                            allocation_base=region.allocation_base,
                            region_size=region.region_size,
                            region_protect=region.protect,
                            region_type=region.type,
                            region_category=region.category,
                            module_name=region.module_name,
                        ))
                    processed += size
                    if progress:
                        progress(processed, estimated, len(chunks))
                    offset += size
            metadata = {
                "format": SNAPSHOT_FORMAT,
                "created_at": datetime.now().astimezone().isoformat(),
                "session_id": session_id,
                "label": label,
                "observations": observations,
                "process": process_info.to_dict(),
                "executable_fingerprint": executable_fingerprint,
                "estimated_readable_bytes": estimated,
                "captured_bytes": sum(chunk.size for chunk in chunks),
                "region_count": len(selected),
                "chunk_count": len(chunks),
                "chunks": [chunk.to_dict() for chunk in chunks],
                "skipped_reads": skipped,
            }
            archive.writestr("metadata.json", json.dumps(metadata, indent=2) + "\n")
        temporary.replace(path)
        return metadata
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


class SnapshotReader:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self._archive = zipfile.ZipFile(self.path, "r")
        self.metadata = json.loads(self._archive.read("metadata.json").decode("utf-8"))
        if self.metadata.get("format") not in SUPPORTED_SNAPSHOT_FORMATS:
            self.close()
            raise ValueError(f"Unsupported snapshot format: {self.metadata.get('format')}")
        self.chunks = {
            (int(item["address"]), int(item["size"])): item
            for item in self.metadata.get("chunks", [])
        }
        self._chunk_starts = sorted(key[0] for key in self.chunks)
        self._chunk_by_start = {key[0]: key for key in self.chunks}

    def close(self) -> None:
        self._archive.close()

    def __enter__(self) -> "SnapshotReader":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def read_chunk(self, key: tuple[int, int]) -> bytes:
        return self._archive.read(self.chunks[key]["entry"])

    def contains_address(self, address: int, size: int = 1) -> bool:
        return self.chunk_for_address(address, size) is not None

    def chunk_for_address(self, address: int, size: int = 1) -> tuple[tuple[int, int], dict[str, Any]] | None:
        index = bisect_right(self._chunk_starts, address) - 1
        if index >= 0:
            start = self._chunk_starts[index]
            key = self._chunk_by_start[start]
            if start <= address and address + size <= start + key[1]:
                return key, self.chunks[key]
        return None

    def read_at(self, address: int, size: int) -> bytes | None:
        located = self.chunk_for_address(address, size)
        if located is None:
            return None
        key, _item = located
        data = self.read_chunk(key)
        return data[address - key[0]:address - key[0] + size]


def _compare_values(mode: str, first, second, third=None) -> bool:
    if mode == "changed":
        return first != second
    if mode == "unchanged":
        return first == second
    if mode == "increased":
        return second > first
    if mode == "decreased":
        return second < first
    if mode == "restored":
        return third is not None and first != second and first == third
    raise ValueError(f"Unsupported snapshot comparison: {mode}")


def compare_snapshots(
    first_path: Path,
    second_path: Path,
    data_type: str,
    *,
    mode: str = "changed",
    third_path: Path | None = None,
    alignment: int | None = None,
    max_results: int = MAX_COMPARE_RESULTS,
    cancel_event=None,
    progress: Callable[[int, int, int], None] | None = None,
) -> SearchResults:
    if data_type not in NUMERIC_TYPES:
        raise ValueError("Snapshot comparisons support numeric and pointer types")
    format_code = NUMERIC_TYPES[data_type]
    value_size = struct.calcsize(format_code)
    alignment = value_size if alignment is None else max(1, alignment)
    hits: list[SearchHit] = []
    scanned = 0
    truncated = False
    with SnapshotReader(first_path) as first, SnapshotReader(second_path) as second:
        third = SnapshotReader(third_path) if third_path else None
        try:
            keys = sorted(set(first.chunks) & set(second.chunks))
            if third is not None:
                keys = [key for key in keys if key in third.chunks]
            total = sum(key[1] for key in keys)
            for key in keys:
                if cancel_event is not None and cancel_event.is_set():
                    truncated = True
                    break
                address, _size = key
                left = first.read_chunk(key)
                right = second.read_chunk(key)
                final = third.read_chunk(key) if third is not None else None
                usable = min(len(left), len(right), len(final) if final is not None else len(left))
                start = (-address) % alignment
                for offset in range(start, usable - value_size + 1, alignment):
                    before = struct.unpack_from(format_code, left, offset)[0]
                    after = struct.unpack_from(format_code, right, offset)[0]
                    restored = struct.unpack_from(format_code, final, offset)[0] if final is not None else None
                    if _compare_values(mode, before, after, restored):
                        raw = right[offset:offset + value_size]
                        item = first.chunks[key]
                        hits.append(SearchHit(
                            address=address + offset,
                            data_type=data_type,
                            value=after,
                            raw_hex=raw.hex().upper(),
                            region_base=int(item["region_base"]),
                            region_category=item.get("region_category"),
                            module_name=item.get("module_name"),
                        ))
                        if len(hits) >= max_results:
                            truncated = True
                            break
                scanned += usable
                if progress:
                    progress(scanned, total, len(hits))
                if truncated:
                    break
        finally:
            if third is not None:
                third.close()
    return SearchResults(
        data_type=data_type,
        query=f"snapshot:{mode}",
        hits=hits,
        scanned_bytes=scanned,
        scanned_regions=len(keys),
        read_failures=0,
        truncated=truncated,
    )


def save_comparison(results: SearchResults, path: Path, sources: list[Path]) -> None:
    payload = results.to_dict()
    payload["created_at"] = datetime.now().astimezone().isoformat()
    payload["source_snapshots"] = [str(source.resolve()) for source in sources]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
