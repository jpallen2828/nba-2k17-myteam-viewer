from __future__ import annotations

import csv
from datetime import datetime
import heapq
import json
import math
from pathlib import Path
import struct
from typing import Any, Callable, Iterable

from .constants import GUIDED_CLUSTER_BYTES, MAX_GUIDED_CLUSTERS
from .snapshots import SnapshotReader


ANALYSIS_FORMAT = "nba2k17-selected-source-analysis/v2"
NUMERIC_PROBES: dict[str, str] = {
    "u8": "<B",
    "u16": "<H",
    "u32": "<I",
    "u64": "<Q",
    "float32": "<f",
    "float64": "<d",
}
_NONZERO_TABLE = bytes([0] + [1] * 255)
_ZERO_TABLE = bytes([1] + [0] * 255)


def parse_named_values(text: str, *, integer_values: bool = True) -> dict[str, int | str]:
    result: dict[str, int | str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"Line {line_number} must use Name = Value")
        name, value = (part.strip() for part in line.split("=", 1))
        if not name or not value:
            raise ValueError(f"Line {line_number} must contain both a name and value")
        if integer_values:
            try:
                parsed: int | str = int(value, 0)
            except ValueError as error:
                raise ValueError(f"Line {line_number} rating must be an integer: {value}") from error
        else:
            parsed = value
        result[name] = parsed
    return result


def parse_badges(text: str) -> list[dict[str, str]]:
    valid = {"BRONZE", "SILVER", "GOLD", "HALL OF FAME"}
    parsed = parse_named_values(text, integer_values=False)
    result = []
    for name, value in parsed.items():
        level = str(value).strip().upper().replace("HOF", "HALL OF FAME")
        if level not in valid:
            raise ValueError(f"Badge {name!r} has unsupported human label {value!r}")
        result.append({"name": name, "level": level.title() if level != "HALL OF FAME" else "Hall of Fame"})
    return result


def normalize_card_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    result = {
        "player_full_name": str(metadata.get("player_full_name") or "").strip(),
        "overall": metadata.get("overall"),
        "tier": str(metadata.get("tier") or "").strip(),
        "primary_position": str(metadata.get("primary_position") or "").strip(),
        "secondary_position": str(metadata.get("secondary_position") or "").strip(),
        "team": str(metadata.get("team") or "").strip(),
        "collection": str(metadata.get("collection") or "").strip(),
        "height": str(metadata.get("height") or "").strip(),
        "weight": str(metadata.get("weight") or "").strip(),
        "age": str(metadata.get("age") or "").strip(),
        "badge_count": str(metadata.get("badge_count") or "").strip(),
        "jersey_number": str(metadata.get("jersey_number") or "").strip(),
        "attributes": dict(metadata.get("attributes") or {}),
        "badges": list(metadata.get("badges") or []),
        "observed_values": dict(metadata.get("observed_values") or {}),
        "notes": str(metadata.get("notes") or "").strip(),
    }
    if not result["player_full_name"]:
        raise ValueError("Player full name is required for Source A and Source B")
    try:
        result["overall"] = int(result["overall"])
    except (TypeError, ValueError) as error:
        raise ValueError("OVR must be an integer for Source A and Source B") from error
    if not 0 <= result["overall"] <= 255:
        raise ValueError("OVR must be between 0 and 255")
    return result


def _name_terms(full_name: str) -> dict[str, str]:
    parts = [part for part in full_name.split() if part]
    result = {"full": full_name}
    if parts:
        result["first"] = parts[0]
        result["last"] = parts[-1]
    return result


def _snapshot_identity(reader: SnapshotReader) -> tuple[str, str, int | None]:
    fingerprint = reader.metadata.get("executable_fingerprint") or {}
    process = reader.metadata.get("process") or {}
    return (
        str(reader.metadata.get("session_id") or ""),
        str(fingerprint.get("sha256") or "").upper(),
        process.get("pid"),
    )


def _validate_readers(readers: Iterable[SnapshotReader]) -> tuple[str, str, int | None]:
    identities = [_snapshot_identity(reader) for reader in readers]
    if not identities:
        raise ValueError("No snapshots were supplied")
    session_ids = {item[0] for item in identities}
    hashes = {item[1] for item in identities}
    pids = {item[2] for item in identities}
    if len(session_ids) != 1:
        raise ValueError("Selected-card analysis requires captures from one scanner session")
    if len(hashes) != 1 or not next(iter(hashes)):
        raise ValueError("Capture executable fingerprints are missing or do not match")
    if len(pids) != 1:
        raise ValueError("A/B/A captures span different game processes; restart validation is a separate experiment")
    return identities[0]


def _restored_mask(first: bytes, second: bytes, third: bytes) -> bytes:
    usable = min(len(first), len(second), len(third))
    if usable <= 0:
        return b""
    left = first[:usable]
    middle = second[:usable]
    final = third[:usable]
    different_ab = (int.from_bytes(left, "little") ^ int.from_bytes(middle, "little")).to_bytes(usable, "little")
    different_aa = (int.from_bytes(left, "little") ^ int.from_bytes(final, "little")).to_bytes(usable, "little")
    ab_mask = different_ab.translate(_NONZERO_TABLE)
    aa_equal_mask = different_aa.translate(_ZERO_TABLE)
    return (int.from_bytes(ab_mask, "little") & int.from_bytes(aa_equal_mask, "little")).to_bytes(usable, "little")


def _difference_mask(first: bytes, second: bytes) -> bytes:
    usable = min(len(first), len(second))
    if usable <= 0:
        return b""
    different = (int.from_bytes(first[:usable], "little") ^ int.from_bytes(second[:usable], "little"))
    return different.to_bytes(usable, "little").translate(_NONZERO_TABLE)


def _pack_probe(value: int | float, data_type: str) -> bytes | None:
    try:
        return struct.pack(NUMERIC_PROBES[data_type], float(value) if data_type.startswith("float") else int(value))
    except (OverflowError, struct.error, ValueError):
        return None


def _decode_probe(raw: bytes, data_type: str) -> int | float:
    return struct.unpack(NUMERIC_PROBES[data_type], raw)[0]


def _item_context(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "region_base": int(item.get("region_base") or 0),
        "allocation_base": int(item.get("allocation_base") or item.get("region_base") or 0),
        "region_category": item.get("region_category"),
        "module_name": item.get("module_name"),
    }


def _base_cluster_score(category: str | None, restored: int, window_size: int, baseline_changed: int) -> float:
    density = restored / max(1, window_size)
    score = min(24.0, math.log2(restored + 1) * 3.0)
    if category == "private_memory":
        score += 12
    elif category in {"mapped_data", "main_module"}:
        score += 7
    elif category in {"loaded_dll", "image_memory"}:
        score -= 15
    if 0.005 <= density <= 0.35:
        score += 12
    elif density > 0.8:
        score -= 20
    if baseline_changed:
        score += min(8, math.log2(baseline_changed + 1))
    return round(score, 3)


def _find_occurrences(data: bytes, needle: bytes, cap: int) -> list[int]:
    offsets: list[int] = []
    cursor = 0
    while len(offsets) < cap:
        position = data.find(needle, cursor)
        if position < 0:
            break
        offsets.append(position)
        cursor = position + 1
    return offsets


def _numeric_facts(card_a: dict[str, Any], card_b: dict[str, Any]) -> list[dict[str, Any]]:
    facts = [{"kind": "overall", "name": "OVR", "a": card_a["overall"], "b": card_b["overall"]}]
    b_attributes = {name.casefold(): (name, value) for name, value in card_b.get("attributes", {}).items()}
    for a_name, a_value in card_a.get("attributes", {}).items():
        other = b_attributes.get(a_name.casefold())
        if other is not None:
            facts.append({"kind": "attribute", "name": a_name, "a": a_value, "b": other[1]})
    return [fact for fact in facts if fact["a"] != fact["b"]]


def _confidence(score: float) -> str:
    if score >= 150:
        return "MEDIUM"
    if score >= 90:
        return "LOW"
    return "CANDIDATE"


def _hex_or_none(raw: bytes | None) -> str | None:
    return raw.hex().upper() if raw is not None else None


def _safe_number(value: int | float) -> int | float | str:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


def analyze_selected_card(
    baseline_path: Path,
    card_a_path: Path,
    card_b_path: Path,
    card_a_restored_path: Path,
    *,
    card_a: dict[str, Any],
    card_b: dict[str, Any],
    scanner_commit: str | None,
    experiment_kind: str = "selected-card-a-b-a",
    subject_label: str = "Card",
    selection_changes_identity: bool = True,
    cancel_event=None,
    progress: Callable[[int, int, int], None] | None = None,
) -> dict[str, Any]:
    """Analyze four controlled snapshots without assigning unproven game semantics."""
    card_a = normalize_card_metadata(card_a)
    card_b = normalize_card_metadata(card_b)
    paths = [baseline_path, card_a_path, card_b_path, card_a_restored_path]
    with (
        SnapshotReader(baseline_path) as baseline,
        SnapshotReader(card_a_path) as first,
        SnapshotReader(card_b_path) as second,
        SnapshotReader(card_a_restored_path) as third,
    ):
        session_id, executable_sha256, pid = _validate_readers((baseline, first, second, third))
        module_bases = {
            str(module.get("name") or "").casefold(): int(module.get("base_address") or 0)
            for module in (first.metadata.get("process") or {}).get("modules", [])
        }
        keys = sorted(set(first.chunks) & set(second.chunks) & set(third.chunks))
        baseline_keys = set(baseline.chunks)
        total_bytes = sum(key[1] for key in keys)
        if not keys:
            raise ValueError("The A/B/A snapshots have no common captured memory chunks")

        facts = _numeric_facts(card_a, card_b)
        name_terms_a = _name_terms(card_a["player_full_name"])
        name_terms_b = _name_terms(card_b["player_full_name"])
        name_queries: list[tuple[str, str, str, bytes]] = []
        for role, terms in (("A", name_terms_a), ("B", name_terms_b)):
            for kind, term in terms.items():
                for encoding, codec in (("ascii", "ascii"), ("utf8", "utf-8"), ("utf16le", "utf-16-le")):
                    try:
                        needle = term.encode(codec)
                    except UnicodeEncodeError:
                        continue
                    if needle:
                        name_queries.append((role, kind, encoding, needle))

        raw_heap: list[tuple[float, int, dict[str, Any]]] = []
        sequence = 0
        restored_byte_count = 0
        nonempty_windows = 0
        name_hits: list[dict[str, Any]] = []
        name_hit_counts: dict[tuple[str, str, str, str], int] = {}
        name_transitions: list[dict[str, Any]] = []
        transition_counts: dict[tuple[str, str], int] = {}
        numeric_hits: list[dict[str, Any]] = []
        numeric_caps: dict[tuple[str, str], int] = {}
        processed = 0

        for key in keys:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Selected-card A/B/A analysis cancelled")
            chunk_address, _chunk_size = key
            a1 = first.read_chunk(key)
            b = second.read_chunk(key)
            a2 = third.read_chunk(key)
            baseline_data = baseline.read_chunk(key) if key in baseline_keys else None
            usable = min(len(a1), len(b), len(a2))
            a1, b, a2 = a1[:usable], b[:usable], a2[:usable]
            if baseline_data is not None:
                baseline_data = baseline_data[:usable]
            baseline_mask = _difference_mask(baseline_data, a1) if baseline_data is not None else None
            item = first.chunks[key]
            context = _item_context(item)

            mask = _restored_mask(a1, b, a2)
            restored_byte_count += mask.count(1)
            for window_offset in range(0, usable, GUIDED_CLUSTER_BYTES):
                window_mask = mask[window_offset:window_offset + GUIDED_CLUSTER_BYTES]
                count = window_mask.count(1)
                if not count:
                    continue
                nonempty_windows += 1
                window_a = a1[window_offset:window_offset + GUIDED_CLUSTER_BYTES]
                window_b = b[window_offset:window_offset + GUIDED_CLUSTER_BYTES]
                window_a2 = a2[window_offset:window_offset + GUIDED_CLUSTER_BYTES]
                window_baseline = (baseline_data[window_offset:window_offset + GUIDED_CLUSTER_BYTES]
                                   if baseline_data is not None else None)
                baseline_changed = (baseline_mask[window_offset:window_offset + GUIDED_CLUSTER_BYTES].count(1)
                                    if baseline_mask is not None else 0)
                cluster = {
                    "base_address": chunk_address + window_offset,
                    "end_address": chunk_address + window_offset + len(window_a),
                    **context,
                    "restored_byte_count": count,
                    "baseline_changed_byte_count": baseline_changed,
                    "score": _base_cluster_score(context["region_category"], count, len(window_a), baseline_changed),
                    "evidence": [],
                    "anchor_addresses": [],
                    "mask": window_mask,
                    "raw_a1": window_a,
                    "raw_b": window_b,
                    "raw_a2": window_a2,
                    "raw_baseline": window_baseline,
                }
                sequence += 1
                heap_entry = (cluster["score"], sequence, cluster)
                if len(raw_heap) < MAX_GUIDED_CLUSTERS:
                    heapq.heappush(raw_heap, heap_entry)
                elif heap_entry[0] > raw_heap[0][0]:
                    heapq.heapreplace(raw_heap, heap_entry)

            state_buffers = {"A1": a1, "B": b, "A2": a2}
            for role, kind, encoding, needle in name_queries:
                states = ("A1", "A2") if role == "A" else ("B",)
                for state in states:
                    count_key = (state, role, kind, encoding)
                    remaining = 300 - name_hit_counts.get(count_key, 0)
                    if remaining <= 0:
                        continue
                    for offset in _find_occurrences(state_buffers[state], needle, remaining):
                        address = chunk_address + offset
                        radius_start = max(0, offset - 0x40)
                        radius_end = min(usable, offset + len(needle) + 0x40)
                        name_hits.append({
                            "state": state,
                            "role": role,
                            "term_kind": kind,
                            "term": needle.decode("utf-16-le" if encoding == "utf16le" else encoding, errors="replace"),
                            "encoding": encoding,
                            "address": address,
                            "address_hex": f"0x{address:X}",
                            **context,
                            "surrounding_hex": state_buffers[state][radius_start:radius_end].hex().upper(),
                        })
                        name_hit_counts[count_key] = name_hit_counts.get(count_key, 0) + 1

            for kind in sorted(set(name_terms_a) & set(name_terms_b)):
                if name_terms_a[kind] == name_terms_b[kind]:
                    continue
                for encoding, codec in (("ascii", "ascii"), ("utf8", "utf-8"), ("utf16le", "utf-16-le")):
                    try:
                        needle_a = name_terms_a[kind].encode(codec)
                        needle_b = name_terms_b[kind].encode(codec)
                    except UnicodeEncodeError:
                        continue
                    transition_key = (kind, encoding)
                    remaining = 500 - transition_counts.get(transition_key, 0)
                    if remaining <= 0:
                        continue
                    for offset in _find_occurrences(a1, needle_a, remaining):
                        if b[offset:offset + len(needle_b)] == needle_b and a2[offset:offset + len(needle_a)] == needle_a:
                            address = chunk_address + offset
                            name_transitions.append({
                                "address": address,
                                "address_hex": f"0x{address:X}",
                                "term_kind": kind,
                                "encoding": encoding,
                                "a1_text": name_terms_a[kind],
                                "b_text": name_terms_b[kind],
                                "a2_text": name_terms_a[kind],
                                "a1_raw": needle_a.hex().upper(),
                                "b_raw": needle_b.hex().upper(),
                                "a2_raw": needle_a.hex().upper(),
                                "baseline_raw": _hex_or_none(
                                    baseline_data[offset:offset + len(needle_a)] if baseline_data is not None else None
                                ),
                                **context,
                            })
                            transition_counts[transition_key] = transition_counts.get(transition_key, 0) + 1

            for fact in facts:
                for data_type in NUMERIC_PROBES:
                    packed_a = _pack_probe(fact["a"], data_type)
                    packed_b = _pack_probe(fact["b"], data_type)
                    if packed_a is None or packed_b is None:
                        continue
                    cap_key = (f"{fact['kind']}:{fact['name']}", data_type)
                    remaining = 3000 - numeric_caps.get(cap_key, 0)
                    if remaining <= 0:
                        continue
                    for offset in _find_occurrences(a1, packed_a, remaining):
                        size = len(packed_a)
                        if b[offset:offset + size] != packed_b or a2[offset:offset + size] != packed_a:
                            continue
                        address = chunk_address + offset
                        numeric_hits.append({
                            "address": address,
                            "address_hex": f"0x{address:X}",
                            "data_type": data_type,
                            "anchor": f"{fact['kind']}:{fact['name']}",
                            "a1_value": _decode_probe(packed_a, data_type),
                            "b_value": _decode_probe(packed_b, data_type),
                            "a2_value": _decode_probe(packed_a, data_type),
                            "a1_raw": packed_a.hex().upper(),
                            "b_raw": packed_b.hex().upper(),
                            "a2_raw": packed_a.hex().upper(),
                            "baseline_raw": _hex_or_none(
                                baseline_data[offset:offset + size] if baseline_data is not None else None
                            ),
                            "naturally_aligned": address % size == 0,
                            "visible_fact": fact,
                            **context,
                        })
                        numeric_caps[cap_key] = numeric_caps.get(cap_key, 0) + 1
            processed += usable
            if progress:
                progress(processed, total_bytes * 2, len(numeric_hits) + len(name_transitions))

        pointer_edges: list[dict[str, Any]] = []
        targets = {
            state: {
                int(hit["address"])
                for hit in name_hits
                if hit["state"] == state and hit["term_kind"] == "full"
            }
            for state in ("A1", "B", "A2")
        }
        target_a_list = sorted(targets["A1"])[:64]
        for key in keys:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Selected-card pointer-xref analysis cancelled")
            chunk_address, _chunk_size = key
            a1 = first.read_chunk(key)
            b = second.read_chunk(key)
            a2 = third.read_chunk(key)
            baseline_data = baseline.read_chunk(key) if key in baseline_keys else None
            usable = min(len(a1), len(b), len(a2))
            item = first.chunks[key]
            context = _item_context(item)
            seen_sources: set[int] = set()
            for target_a in target_a_list:
                needle = struct.pack("<Q", target_a)
                for offset in _find_occurrences(a1[:usable], needle, 1000):
                    if offset + 8 > usable:
                        continue
                    source = chunk_address + offset
                    if source in seen_sources:
                        continue
                    target_b = struct.unpack_from("<Q", b, offset)[0]
                    target_a2 = struct.unpack_from("<Q", a2, offset)[0]
                    if target_b not in targets["B"] or target_a2 not in targets["A2"]:
                        continue
                    if not first.contains_address(target_a) or not second.contains_address(target_b) or not third.contains_address(target_a2):
                        continue
                    target_item = first.chunk_for_address(target_a)
                    target_context = _item_context(target_item[1]) if target_item else {}
                    pointer_edges.append({
                        "source_address": source,
                        "source_address_hex": f"0x{source:X}",
                        "source_offset": None,
                        "capture_chunk_offset": offset,
                        "a1_target": target_a,
                        "a1_target_hex": f"0x{target_a:X}",
                        "b_target": target_b,
                        "b_target_hex": f"0x{target_b:X}",
                        "a2_target": target_a2,
                        "a2_target_hex": f"0x{target_a2:X}",
                        "a1_raw": needle.hex().upper(),
                        "b_raw": struct.pack("<Q", target_b).hex().upper(),
                        "a2_raw": struct.pack("<Q", target_a2).hex().upper(),
                        "baseline_raw": _hex_or_none(
                            baseline_data[offset:offset + 8] if baseline_data is not None else None
                        ),
                        "naturally_aligned": source % 8 == 0,
                        "validation_status": "readable-name-targets-in-all-three-states",
                        "selection_changes_identity": selection_changes_identity,
                        "target_region_category": target_context.get("region_category"),
                        **context,
                    })
                    seen_sources.add(source)
            processed += usable
            if progress:
                progress(processed, total_bytes * 2, len(pointer_edges))

        clusters = [entry[2] for entry in raw_heap]

        def cluster_for(address: int, context: dict[str, Any]) -> dict[str, Any]:
            for cluster in clusters:
                if cluster["base_address"] <= address < cluster["end_address"]:
                    return cluster
            located = first.chunk_for_address(address)
            if located is None:
                raise ValueError(f"Candidate address 0x{address:X} is outside the A1 snapshot")
            key, item = located
            chunk = first.read_chunk(key)
            chunk_b = second.read_chunk(key)
            chunk_a2 = third.read_chunk(key)
            chunk_baseline = baseline.read_chunk(key) if key in baseline_keys else None
            relative = address - key[0]
            start = max(0, relative - GUIDED_CLUSTER_BYTES // 2)
            end = min(len(chunk), start + GUIDED_CLUSTER_BYTES)
            start = max(0, end - GUIDED_CLUSTER_BYTES)
            window_a = chunk[start:end]
            window_b = chunk_b[start:end]
            window_a2 = chunk_a2[start:end]
            window_baseline = chunk_baseline[start:end] if chunk_baseline is not None else None
            mask = _restored_mask(window_a, window_b, window_a2)
            created = {
                "base_address": key[0] + start,
                "end_address": key[0] + end,
                **_item_context(item),
                "restored_byte_count": mask.count(1),
                "baseline_changed_byte_count": (sum(x != y for x, y in zip(window_baseline, window_a))
                                                if window_baseline is not None else 0),
                "score": _base_cluster_score(context.get("region_category"), mask.count(1), len(window_a), 0),
                "evidence": [],
                "anchor_addresses": [],
                "mask": mask,
                "raw_a1": window_a,
                "raw_b": window_b,
                "raw_a2": window_a2,
                "raw_baseline": window_baseline,
            }
            clusters.append(created)
            return created

        candidates: list[dict[str, Any]] = []
        for hit in numeric_hits:
            cluster = cluster_for(hit["address"], hit)
            boost = 58 if str(hit["anchor"]).startswith("overall:") else 38
            if hit["naturally_aligned"]:
                boost += 5
            cluster["score"] += boost
            evidence = f"direct {hit['anchor']} {hit['a1_value']} → {hit['b_value']} → {hit['a2_value']} as {hit['data_type']}"
            cluster["evidence"].append(evidence)
            cluster["anchor_addresses"].append(hit["address"])
            candidates.append({
                **hit,
                "score": boost,
                "confidence": "MEDIUM" if str(hit["anchor"]).startswith("overall:") else "LOW",
            })

        for hit in name_transitions:
            cluster = cluster_for(hit["address"], hit)
            boost = 72 if hit["term_kind"] == "full" else 42
            cluster["score"] += boost
            cluster["evidence"].append(
                f"{hit['encoding']} {hit['term_kind']} name tracks A/B/A at the same address"
            )
            cluster["anchor_addresses"].append(hit["address"])
            candidates.append({
                **hit,
                "data_type": hit["encoding"],
                "anchor": f"name_transition:{hit['term_kind']}",
                "visible_fact": {"a": hit["a1_text"], "b": hit["b_text"]},
                "score": boost,
                "confidence": "MEDIUM" if hit["term_kind"] == "full" else "LOW",
            })

        for edge in pointer_edges:
            cluster = cluster_for(edge["source_address"], edge)
            boost = 85 if selection_changes_identity else 20
            cluster["score"] += boost
            cluster["evidence"].append("validated pointer tracks readable full-name targets in A/B/A")
            cluster["anchor_addresses"].append(edge["source_address"])
            candidates.append({
                "address": edge["source_address"],
                "address_hex": edge["source_address_hex"],
                "allocation_base": edge["allocation_base"],
                "region_category": edge["region_category"],
                "module_name": edge["module_name"],
                "data_type": "pointer",
                "anchor": "pointer_to_full_name",
                "a1_raw": edge["a1_raw"],
                "b_raw": edge["b_raw"],
                "a2_raw": edge["a2_raw"],
                "baseline_raw": edge["baseline_raw"],
                "visible_fact": {"a": card_a["player_full_name"], "b": card_b["player_full_name"]},
                "score": boost,
                "confidence": "MEDIUM" if selection_changes_identity else "CANDIDATE",
            })

        clusters.sort(key=lambda item: (-float(item["score"]), item["base_address"]))
        clusters = clusters[:MAX_GUIDED_CLUSTERS]
        for rank, cluster in enumerate(clusters, 1):
            cluster["rank"] = rank
            cluster["score"] = round(float(cluster["score"]), 3)
            cluster["confidence"] = _confidence(cluster["score"])
            cluster["base_address_hex"] = f"0x{cluster['base_address']:X}"
            cluster["end_address_hex"] = f"0x{cluster['end_address']:X}"
            cluster["allocation_base_hex"] = f"0x{int(cluster['allocation_base']):X}"
            module_base = module_bases.get(str(cluster.get("module_name") or "").casefold())
            cluster["module_relative_offset"] = (
                cluster["base_address"] - module_base if module_base else None
            )
            cluster["module_relative_offset_hex"] = (
                f"0x{cluster['module_relative_offset']:X}" if cluster["module_relative_offset"] is not None else None
            )
            mask = cluster.pop("mask")
            first_offset = mask.find(b"\x01")
            cluster["first_restored_offset"] = first_offset if first_offset >= 0 else None
            cluster["first_restored_offset_hex"] = f"0x{first_offset:X}" if first_offset >= 0 else None
            view_addresses = cluster["anchor_addresses"][:8]
            if not view_addresses and first_offset >= 0:
                view_addresses = [cluster["base_address"] + first_offset]
            primitive_view = []
            for view_address in view_addresses:
                offset = view_address - cluster["base_address"]
                interpretations = {}
                for data_type, format_code in NUMERIC_PROBES.items():
                    size = struct.calcsize(format_code)
                    if offset < 0 or offset + size > len(cluster["raw_a1"]):
                        continue
                    interpretations[data_type] = {
                        "a1": _safe_number(struct.unpack_from(format_code, cluster["raw_a1"], offset)[0]),
                        "b": _safe_number(struct.unpack_from(format_code, cluster["raw_b"], offset)[0]),
                        "a2": _safe_number(struct.unpack_from(format_code, cluster["raw_a2"], offset)[0]),
                        "a1_raw": cluster["raw_a1"][offset:offset + size].hex().upper(),
                        "b_raw": cluster["raw_b"][offset:offset + size].hex().upper(),
                        "a2_raw": cluster["raw_a2"][offset:offset + size].hex().upper(),
                    }
                primitive_view.append({
                    "address": view_address,
                    "address_hex": f"0x{view_address:X}",
                    "offset": offset,
                    "offset_hex": f"0x{offset:X}",
                    "interpretations": interpretations,
                })
            cluster["primitive_view"] = primitive_view
            if rank <= 50:
                cluster["raw_neighborhood"] = {
                    "a1": cluster.pop("raw_a1").hex().upper(),
                    "b": cluster.pop("raw_b").hex().upper(),
                    "a2": cluster.pop("raw_a2").hex().upper(),
                    "baseline": _hex_or_none(cluster.pop("raw_baseline")),
                }
            else:
                cluster.pop("raw_a1")
                cluster.pop("raw_b")
                cluster.pop("raw_a2")
                cluster.pop("raw_baseline")
            cluster["anchor_addresses"] = sorted(set(cluster["anchor_addresses"]))

        for candidate in candidates:
            rank = None
            object_base = None
            for cluster in clusters:
                if cluster["base_address"] <= candidate["address"] < cluster["end_address"]:
                    rank = cluster["rank"]
                    object_base = cluster["base_address"]
                    break
            candidate["cluster_rank"] = rank
            candidate["candidate_object_base"] = object_base
            candidate["candidate_object_base_hex"] = f"0x{object_base:X}" if object_base is not None else None
            candidate["offset"] = candidate["address"] - object_base if object_base is not None else None
            candidate["offset_hex"] = f"0x{candidate['offset']:X}" if candidate["offset"] is not None else None
            module_base = module_bases.get(str(candidate.get("module_name") or "").casefold())
            candidate["module_relative_offset"] = candidate["address"] - module_base if module_base else None
            candidate["module_relative_offset_hex"] = (
                f"0x{candidate['module_relative_offset']:X}"
                if candidate["module_relative_offset"] is not None else None
            )
            candidate["provenance"] = {
                "session_id": session_id,
                "executable_sha256": executable_sha256,
                "scanner_commit": scanner_commit,
                "capture_labels": [reader.metadata.get("label") for reader in (baseline, first, second, third)],
            }
        candidates.sort(key=lambda item: (-float(item.get("score", 0)), int(item["address"])))

        strongest = clusters[0]["confidence"] if clusters else "UNKNOWN"
        analysis = {
            "format": ANALYSIS_FORMAT,
            "experiment_id": f"{session_id}-{experiment_kind}-001",
            "kind": experiment_kind,
            "subject_label": subject_label,
            "created_at": datetime.now().astimezone().isoformat(),
            "status": "analyzed-no-verified-root",
            "confidence": strongest,
            "provenance": {
                "session_id": session_id,
                "executable_sha256": executable_sha256,
                "process_id": pid,
                "scanner_commit": scanner_commit,
                "source_snapshots": [str(path.resolve()) for path in paths],
                "capture_labels": [reader.metadata.get("label") for reader in (baseline, first, second, third)],
            },
            "subjects": {"A": card_a, "B": card_b},
            "summary": {
                "common_chunk_count": len(keys),
                "common_bytes": total_bytes,
                "restored_byte_count": restored_byte_count,
                "nonempty_candidate_windows": nonempty_windows,
                "numeric_anchor_hits": len(numeric_hits),
                "name_hits": len(name_hits),
                "name_transition_hits": len(name_transitions),
                "validated_pointer_edges": len(pointer_edges),
                "ranked_cluster_count": len(clusters),
            },
            "name_hits": name_hits,
            "name_transitions": name_transitions,
            "pointer_edges": pointer_edges,
            "candidates": candidates,
            "clusters": clusters,
            "interpretation": (
                "Rankings are research triage only. A/B/A restoration and human-visible matches do not by themselves "
                f"prove a selected {subject_label.lower()} object or field semantic."
            ),
        }
        if experiment_kind == "selected-card-a-b-a":
            analysis["cards"] = analysis["subjects"]
        return analysis


def save_analysis(analysis: dict[str, Any], json_path: Path, csv_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = json_path.with_suffix(json_path.suffix + ".tmp")
    temporary.write_text(json.dumps(analysis, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(json_path)
    columns = (
        "cluster_rank", "address_hex", "region_category", "module_name", "data_type", "anchor",
        "a1_raw", "b_raw", "a2_raw", "baseline_raw", "score", "confidence", "visible_fact",
    )
    with csv_path.open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for candidate in analysis.get("candidates", []):
            writer.writerow({
                column: (json.dumps(candidate.get(column), ensure_ascii=False)
                         if column == "visible_fact" else candidate.get(column))
                for column in columns
            })


def render_report(analysis: dict[str, Any]) -> str:
    provenance = analysis["provenance"]
    summary = analysis["summary"]
    subjects = analysis.get("subjects") or analysis.get("cards")
    subject_label = str(analysis.get("subject_label") or "Source")
    kind = str(analysis.get("kind") or "selected-card-a-b-a")
    title = "Selected roster-player experiment 001" if kind == "selected-roster-player-a-b-a" else "Selected-card experiment 001"
    lines = [
        f"# {title}",
        "",
        f"Created: {analysis['created_at']}",
        f"Game SHA-256: `{provenance['executable_sha256']}`",
        f"Scanner commit: `{provenance.get('scanner_commit') or 'unavailable'}`",
        f"Session: `{provenance['session_id']}`",
        f"Process ID: `{provenance.get('process_id')}` (session-specific)",
        "",
        "## Captures and annotations",
        "",
        f"Capture labels: {', '.join(str(value) for value in provenance['capture_labels'])}",
        "",
        f"| Visible field | {subject_label} A | {subject_label} B |",
        "|---|---|---|",
        f"| Player | {subjects['A']['player_full_name']} | {subjects['B']['player_full_name']} |",
        f"| OVR | {subjects['A']['overall']} | {subjects['B']['overall']} |",
        f"| Tier | {subjects['A']['tier'] or 'not applicable/recorded'} | {subjects['B']['tier'] or 'not applicable/recorded'} |",
        f"| Primary position | {subjects['A']['primary_position'] or 'not recorded'} | {subjects['B']['primary_position'] or 'not recorded'} |",
        f"| Secondary position | {subjects['A']['secondary_position'] or 'not recorded'} | {subjects['B']['secondary_position'] or 'not recorded'} |",
        f"| Team | {subjects['A']['team'] or 'not recorded'} | {subjects['B']['team'] or 'not recorded'} |",
        f"| Collection | {subjects['A']['collection'] or 'not applicable/recorded'} | {subjects['B']['collection'] or 'not applicable/recorded'} |",
        f"| Height | {subjects['A']['height'] or 'not recorded'} | {subjects['B']['height'] or 'not recorded'} |",
        f"| Weight | {subjects['A']['weight'] or 'not recorded'} | {subjects['B']['weight'] or 'not recorded'} |",
        f"| Age | {subjects['A']['age'] or 'not recorded'} | {subjects['B']['age'] or 'not recorded'} |",
        f"| Badge count | {subjects['A']['badge_count'] or 'not recorded'} | {subjects['B']['badge_count'] or 'not recorded'} |",
        f"| Jersey number | {subjects['A']['jersey_number'] or 'not recorded'} | {subjects['B']['jersey_number'] or 'not recorded'} |",
        f"| Visible attributes | {json.dumps(subjects['A']['attributes'], ensure_ascii=False)} | {json.dumps(subjects['B']['attributes'], ensure_ascii=False)} |",
        f"| Other visible values | {json.dumps(subjects['A']['observed_values'], ensure_ascii=False)} | {json.dumps(subjects['B']['observed_values'], ensure_ascii=False)} |",
        f"| Known badges | {json.dumps(subjects['A']['badges'], ensure_ascii=False)} | {json.dumps(subjects['B']['badges'], ensure_ascii=False)} |",
        "",
        "Controlled variables were recorded as observed; differences in tier, team, position, or collection remain possible confounders.",
        "",
        "## Filtering summary",
        "",
        f"- Common captured bytes: {summary['common_bytes']:,} across {summary['common_chunk_count']:,} chunks",
        f"- Restored bytes satisfying A1 == A2 and A1 != B: {summary['restored_byte_count']:,}",
        f"- Nonempty 0x{GUIDED_CLUSTER_BYTES:X} candidate windows before ranking: {summary['nonempty_candidate_windows']:,}",
        f"- Direct OVR/attribute representation hits: {summary['numeric_anchor_hits']:,}",
        f"- Name hits: {summary['name_hits']:,}; same-address A/B/A name transitions: {summary['name_transition_hits']:,}",
        f"- Validated pointer transitions to readable full-name targets: {summary['validated_pointer_edges']:,}",
        f"- Retained ranked clusters: {summary['ranked_cluster_count']:,}",
        "",
        "## Top candidate clusters",
        "",
        "| Rank | Range | Region | Score | Confidence | Restored bytes | Anchors |",
        "|---:|---|---|---:|---|---:|---|",
    ]
    for cluster in analysis.get("clusters", [])[:20]:
        evidence = "; ".join(cluster.get("evidence", [])[:3]) or "generic restored-byte cluster"
        lines.append(
            f"| {cluster['rank']} | `{cluster['base_address_hex']}`–`{cluster['end_address_hex']}` | "
            f"{cluster.get('region_category') or 'unknown'} | {cluster['score']:.1f} | {cluster['confidence']} | "
            f"{cluster['restored_byte_count']} | {evidence} |"
        )
    lines.extend(("", "## Primitive views for strongest anchors", ""))
    viewed = 0
    for cluster in analysis.get("clusters", [])[:10]:
        for view in cluster.get("primitive_view", [])[:2]:
            interpretations = view.get("interpretations", {})
            compact = []
            for data_type in ("u8", "u16", "u32", "u64", "float32", "float64"):
                values = interpretations.get(data_type)
                if values:
                    compact.append(f"{data_type}={values['a1']}→{values['b']}→{values['a2']}")
            lines.append(
                f"- Cluster #{cluster['rank']} `{view['address_hex']}` "
                f"(offset `{view['offset_hex']}`): " + "; ".join(compact)
            )
            viewed += 1
            if viewed >= 12:
                break
        if viewed >= 12:
            break
    if not viewed:
        lines.append("- No retained cluster contained an interpretable restored offset.")
    if kind == "selected-roster-player-a-b-a":
        next_experiment = (
            "Inspect the highest-ranked cluster against a third roster player. Once the selected-player neighborhood is "
            "narrowed, keep one player selected and use the game's editor to change exactly one field and restore it. "
            "Capture only the resulting small candidate neighborhoods for attributes, tendencies, badges, animations, "
            "vitals, identity, gear, and linked appearance data. A root remains unverified until restart rediscovery."
        )
    else:
        next_experiment = (
            "Inspect the highest-ranked cluster against at least one additional card. If it continues to track selection, "
            "perform same-player/different-card captures to separate player-global from card-specific state. A root remains "
            "unverified until it can be rediscovered after restarting NBA 2K17 without reusing an absolute heap address."
        )
    lines.extend((
        "",
        "## Pointer relationships",
        "",
    ))
    if analysis.get("pointer_edges"):
        for edge in analysis["pointer_edges"][:20]:
            lines.append(
                f"- `{edge['source_address_hex']}`: `{edge['a1_target_hex']}` → "
                f"`{edge['b_target_hex']}` → `{edge['a2_target_hex']}`; "
                f"{edge['validation_status']}."
            )
    else:
        lines.append("- No pointer field was found that tracked readable full-name targets across all three selection states.")
    lines.extend((
        "",
        "## Current conclusion",
        "",
        f"No address in this report is automatically promoted to Selected{subject_label.replace(' ', '')}. The ranked results are candidates for "
        f"neighborhood review, additional {subject_label.lower()} testing, lifetime tests, and restart rediscovery.",
        "",
        "## Required next experiment",
        "",
        next_experiment,
        "",
    ))
    return "\n".join(lines)
