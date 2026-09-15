"""Pure serialization and validation for reversible executable-copy patch manifests.

This module deliberately contains no file I/O, process access, or patch-application
logic.  It describes possible future changes to a copied executable; it cannot make
those changes itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import re
from typing import Any, Mapping


PATCH_MANIFEST_SCHEMA = "nba2k17.reversible-executable-copy-patch"
PATCH_MANIFEST_SCHEMA_VERSION = 1

_SHA256_RE = re.compile(r"^[0-9A-Fa-f]{64}$")
_BYTE_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_MANIFEST_KEYS = {
    "schema",
    "schema_version",
    "created_at",
    "source_sha256",
    "patched_sha256",
    "patches",
}
_PATCH_KEYS = {
    "file_offset",
    "file_offset_hex",
    "rva",
    "rva_hex",
    "length",
    "original_bytes_hex",
    "replacement_bytes_hex",
    "reason",
    "evidence",
}


class PatchManifestValidationError(ValueError):
    """Raised when a manifest violates the versioned schema or safety invariants."""


def _normalize_sha256(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PatchManifestValidationError(f"{field_name} must be exactly 64 hexadecimal characters")
    return value.upper()


def _normalize_byte_hex(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise PatchManifestValidationError(f"{field_name} must be a hexadecimal string")
    normalized = value.strip()
    if normalized.lower().startswith("0x"):
        normalized = normalized[2:]
    if not normalized or len(normalized) % 2 or not _BYTE_HEX_RE.fullmatch(normalized):
        raise PatchManifestValidationError(
            f"{field_name} must contain a non-empty, even number of hexadecimal characters"
        )
    return normalized.upper()


def _normalize_timestamp(value: str | datetime) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise PatchManifestValidationError("created_at must be an ISO-8601 timestamp") from exc
    else:
        raise PatchManifestValidationError("created_at must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PatchManifestValidationError("created_at must include a UTC offset")
    return parsed.isoformat()


def _require_non_negative_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PatchManifestValidationError(f"{field_name} must be a non-negative integer")
    return value


def _require_exact_keys(data: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(data)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        details: list[str] = []
        if missing:
            details.append(f"missing={missing}")
        if unknown:
            details.append(f"unknown={unknown}")
        raise PatchManifestValidationError(f"{label} fields do not match schema: {', '.join(details)}")


@dataclass(frozen=True, slots=True)
class PatchRecord:
    """One same-length byte substitution at a known file offset and PE RVA."""

    file_offset: int
    rva: int
    original_bytes_hex: str
    replacement_bytes_hex: str
    reason: str
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "file_offset", _require_non_negative_int(self.file_offset, "file_offset"))
        object.__setattr__(self, "rva", _require_non_negative_int(self.rva, "rva"))
        original = _normalize_byte_hex(self.original_bytes_hex, "original_bytes_hex")
        replacement = _normalize_byte_hex(self.replacement_bytes_hex, "replacement_bytes_hex")
        if len(original) != len(replacement):
            raise PatchManifestValidationError("original and replacement byte sequences must have equal length")
        if original == replacement:
            raise PatchManifestValidationError("replacement bytes must differ from original bytes")
        object.__setattr__(self, "original_bytes_hex", original)
        object.__setattr__(self, "replacement_bytes_hex", replacement)

        if not isinstance(self.reason, str) or not self.reason.strip():
            raise PatchManifestValidationError("reason must be a non-empty string")
        object.__setattr__(self, "reason", self.reason.strip())

        if isinstance(self.evidence, str):
            raise PatchManifestValidationError("evidence must be a sequence of non-empty strings")
        try:
            evidence = tuple(self.evidence)
        except TypeError as exc:
            raise PatchManifestValidationError("evidence must be a sequence of non-empty strings") from exc
        if not evidence or any(not isinstance(item, str) or not item.strip() for item in evidence):
            raise PatchManifestValidationError("evidence must contain at least one non-empty string")
        object.__setattr__(self, "evidence", tuple(item.strip() for item in evidence))

    @property
    def length(self) -> int:
        return len(self.original_bytes_hex) // 2

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_offset": self.file_offset,
            "file_offset_hex": f"0x{self.file_offset:X}",
            "rva": self.rva,
            "rva_hex": f"0x{self.rva:X}",
            "length": self.length,
            "original_bytes_hex": self.original_bytes_hex,
            "replacement_bytes_hex": self.replacement_bytes_hex,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PatchRecord":
        if not isinstance(data, Mapping):
            raise PatchManifestValidationError("each patches entry must be an object")
        _require_exact_keys(data, _PATCH_KEYS, "patch")
        file_offset = _require_non_negative_int(data["file_offset"], "file_offset")
        rva = _require_non_negative_int(data["rva"], "rva")
        if not isinstance(data["file_offset_hex"], str):
            raise PatchManifestValidationError("file_offset_hex must be a hexadecimal string")
        if not isinstance(data["rva_hex"], str):
            raise PatchManifestValidationError("rva_hex must be a hexadecimal string")
        if data["file_offset_hex"].upper() != f"0X{file_offset:X}":
            raise PatchManifestValidationError("file_offset_hex does not match file_offset")
        if data["rva_hex"].upper() != f"0X{rva:X}":
            raise PatchManifestValidationError("rva_hex does not match rva")
        declared_length = _require_non_negative_int(data["length"], "length")
        if declared_length == 0:
            raise PatchManifestValidationError("length must be greater than zero")
        evidence = data["evidence"]
        if not isinstance(evidence, list):
            raise PatchManifestValidationError("evidence must be a JSON array")
        record = cls(
            file_offset=file_offset,
            rva=rva,
            original_bytes_hex=data["original_bytes_hex"],
            replacement_bytes_hex=data["replacement_bytes_hex"],
            reason=data["reason"],
            evidence=tuple(evidence),
        )
        if declared_length != record.length:
            raise PatchManifestValidationError("length does not match the original/replacement byte length")
        return record


@dataclass(frozen=True, slots=True)
class PatchManifest:
    """Versioned description of reversible substitutions for a copied executable."""

    source_sha256: str
    patched_sha256: str
    patches: tuple[PatchRecord, ...]
    created_at: str | datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self) -> None:
        source = _normalize_sha256(self.source_sha256, "source_sha256")
        patched = _normalize_sha256(self.patched_sha256, "patched_sha256")
        if source == patched:
            raise PatchManifestValidationError("source_sha256 and patched_sha256 must differ")
        object.__setattr__(self, "source_sha256", source)
        object.__setattr__(self, "patched_sha256", patched)
        object.__setattr__(self, "created_at", _normalize_timestamp(self.created_at))

        if isinstance(self.patches, PatchRecord):
            raise PatchManifestValidationError("patches must be a sequence of PatchRecord objects")
        try:
            patches = tuple(self.patches)
        except TypeError as exc:
            raise PatchManifestValidationError("patches must be a sequence of PatchRecord objects") from exc
        if not patches or any(not isinstance(item, PatchRecord) for item in patches):
            raise PatchManifestValidationError("patches must contain at least one PatchRecord")
        object.__setattr__(self, "patches", patches)
        self._validate_non_overlapping_ranges()

    def _validate_non_overlapping_ranges(self) -> None:
        for field_name in ("file_offset", "rva"):
            ordered = sorted(self.patches, key=lambda item: getattr(item, field_name))
            previous = ordered[0]
            for current in ordered[1:]:
                previous_end = getattr(previous, field_name) + previous.length
                if getattr(current, field_name) < previous_end:
                    raise PatchManifestValidationError(f"patch {field_name} ranges must not overlap")
                previous = current

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": PATCH_MANIFEST_SCHEMA,
            "schema_version": PATCH_MANIFEST_SCHEMA_VERSION,
            "created_at": self.created_at,
            "source_sha256": self.source_sha256,
            "patched_sha256": self.patched_sha256,
            "patches": [record.to_dict() for record in self.patches],
        }

    def to_json(self, *, indent: int | None = 2) -> str:
        """Return deterministic JSON without writing it or applying any patch."""

        return json.dumps(self.to_dict(), indent=indent, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PatchManifest":
        if not isinstance(data, Mapping):
            raise PatchManifestValidationError("manifest must be a JSON object")
        _require_exact_keys(data, _MANIFEST_KEYS, "manifest")
        if data["schema"] != PATCH_MANIFEST_SCHEMA:
            raise PatchManifestValidationError(f"schema must be {PATCH_MANIFEST_SCHEMA!r}")
        if (
            isinstance(data["schema_version"], bool)
            or not isinstance(data["schema_version"], int)
            or data["schema_version"] != PATCH_MANIFEST_SCHEMA_VERSION
        ):
            raise PatchManifestValidationError(
                f"unsupported schema_version: {data['schema_version']!r}"
            )
        raw_patches = data["patches"]
        if not isinstance(raw_patches, list):
            raise PatchManifestValidationError("patches must be a JSON array")
        return cls(
            source_sha256=data["source_sha256"],
            patched_sha256=data["patched_sha256"],
            patches=tuple(PatchRecord.from_dict(item) for item in raw_patches),
            created_at=data["created_at"],
        )

    @classmethod
    def from_json(cls, text: str) -> "PatchManifest":
        """Parse and fully validate one manifest JSON document."""

        if not isinstance(text, str):
            raise PatchManifestValidationError("manifest JSON must be text")

        def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, value in pairs:
                if key in result:
                    raise PatchManifestValidationError(f"duplicate JSON field: {key}")
                result[key] = value
            return result

        try:
            data = json.loads(text, object_pairs_hook=reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise PatchManifestValidationError(f"invalid JSON: {exc.msg}") from exc
        return cls.from_dict(data)


def validate_patch_manifest_json(text: str) -> PatchManifest:
    """Validate JSON and return its immutable, normalized representation."""

    return PatchManifest.from_json(text)
