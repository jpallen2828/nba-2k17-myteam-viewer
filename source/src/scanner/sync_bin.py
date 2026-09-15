from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re
import struct
from typing import Callable


SIGNATURES = {
    b"PK\x03\x04": "ZIP local header",
    b"\x1F\x8B": "gzip stream",
    b"\x78\x01": "zlib stream (low compression candidate)",
    b"\x78\x9C": "zlib stream (default compression candidate)",
    b"\x78\xDA": "zlib stream (high compression candidate)",
    b"SQLite format 3\x00": "SQLite database",
    b"\x89PNG\r\n\x1A\n": "PNG image",
    b"DDS ": "DDS texture",
    b"RIFF": "RIFF container",
    b"OggS": "Ogg stream",
}

DEFAULT_SEARCH_TERMS = (
    "myteam",
    "my team",
    "collection",
    "badge",
    "hall of fame",
    "pink diamond",
    "onyx",
    "player card",
    "playercard",
)


def shannon_entropy(data: bytes) -> float:
    if not data:
        return 0.0
    counts = Counter(data)
    total = len(data)
    return -sum((count / total) * math.log2(count / total) for count in counts.values())


def readable_strings(data: bytes, minimum: int = 6, limit: int = 200) -> list[str]:
    pattern = rb"[\x20-\x7E]{" + str(minimum).encode("ascii") + rb",}"
    return [match.group(0).decode("ascii", errors="replace")[:512]
            for match in list(re.finditer(pattern, data))[:limit]]


def inspect_sync_bin(
    path: Path,
    *,
    sample_size: int = 1024 * 1024,
    cancel_event=None,
    progress: Callable[[int, int], None] | None = None,
) -> dict:
    resolved = path.resolve()
    stat = resolved.stat()
    sha256 = hashlib.sha256()
    sha1 = hashlib.sha1()
    signatures: list[dict] = []
    sample_offsets = sorted(set((
        0,
        max(0, stat.st_size // 4),
        max(0, stat.st_size // 2),
        max(0, stat.st_size * 3 // 4),
        max(0, stat.st_size - sample_size),
    )))
    samples: list[dict] = []
    term_needles = {
        (term, encoding): term.encode(encoding).lower()
        for term in DEFAULT_SEARCH_TERMS
        for encoding in ("ascii", "utf-16-le")
    }
    term_offsets: dict[tuple[str, str], list[int]] = {key: [] for key in term_needles}
    longest_needle = max(len(value) for value in term_needles.values())
    with resolved.open("rb") as source:
        processed = 0
        tail = b""
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("SYNC.BIN inspection cancelled")
            sha256.update(block)
            sha1.update(block)
            combined = tail + block
            lowered = combined.lower()
            combined_start = processed - len(tail)
            for key, needle in term_needles.items():
                cursor = 0
                offsets = term_offsets[key]
                while len(offsets) < 100:
                    position = lowered.find(needle, cursor)
                    if position < 0:
                        break
                    absolute = combined_start + position
                    if absolute >= 0 and absolute not in offsets:
                        offsets.append(absolute)
                    cursor = position + 1
            processed += len(block)
            tail = combined[-(longest_needle - 1):] if longest_needle > 1 else b""
            if progress:
                progress(processed, stat.st_size)
        for offset in sample_offsets:
            source.seek(offset)
            data = source.read(min(sample_size, stat.st_size - offset))
            found = []
            for magic, label in SIGNATURES.items():
                cursor = 0
                while True:
                    position = data.find(magic, cursor)
                    if position < 0:
                        break
                    found.append({"offset": offset + position, "offset_hex": f"0x{offset + position:X}", "signature": label})
                    cursor = position + 1
                    if len(found) >= 200:
                        break
            signatures.extend(found)
            samples.append({
                "offset": offset,
                "offset_hex": f"0x{offset:X}",
                "size": len(data),
                "entropy": round(shannon_entropy(data), 6),
                "prefix_hex": data[:64].hex().upper(),
                "readable_strings": readable_strings(data),
            })
    with resolved.open("rb") as source:
        header = source.read(64)
    header_u32 = {
        f"unknown_{offset:02X}": struct.unpack_from("<I", header, offset)[0]
        for offset in range(0, min(len(header), 24), 4)
    }
    advertised_size = header_u32.get("unknown_00")
    return {
        "path": str(resolved),
        "size": stat.st_size,
        "modified": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
        "sha256": sha256.hexdigest().upper(),
        "sha1": sha1.hexdigest().upper(),
        "sample_strategy": "five bounded 1 MiB samples unless file is smaller",
        "header": {
            "prefix_hex": header.hex().upper(),
            "u32_little_endian": header_u32,
            "unknown_00_equals_file_size": advertised_size == stat.st_size,
        },
        "samples": samples,
        "embedded_signature_candidates": signatures[:1000],
        "terminology_search": [
            {
                "term": term,
                "encoding": encoding,
                "hit_count_capped": len(term_offsets[(term, encoding)]),
                "offsets": term_offsets[(term, encoding)],
                "offsets_hex": [f"0x{offset:X}" for offset in term_offsets[(term, encoding)]],
            }
            for term in DEFAULT_SEARCH_TERMS
            for encoding in ("ascii", "utf-16-le")
        ],
        "claims": {
            "compression": "unknown",
            "myteam_card_database": "not yet proven",
            "attributes": "not yet proven",
            "badges": "not yet proven",
            "artwork": "not yet proven"
        }
    }
