"""Deterministic aggregate reporting for downloaded card-art assets."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..asset_validator import (
    AssetValidationResult,
    SUPPORTED_FORMATS,
    duplicate_hash_groups,
    validate_image_bytes,
)
from ..models import stable_json_bytes


ASSET_REPORT_FORMAT = "nba2k17-card-art-validation-report/v1"


@dataclass(frozen=True)
class AssetReportRecord:
    """One downloaded asset and the source identity needed to audit it.

    Exactly one of ``data`` and ``file_path`` is required. The optional source
    fields are copied to the report as evidence; they do not influence image
    validation or duplicate detection.
    """

    asset_id: str
    data: bytes | None = None
    file_path: Path | None = None
    filename: str | None = None
    source_url: str | None = None
    source_card_id: str | None = None
    canonical_card_uuid: str | None = None

    def __post_init__(self) -> None:
        if not str(self.asset_id).strip():
            raise ValueError("asset_id is required")
        if (self.data is None) == (self.file_path is None):
            raise ValueError("exactly one of data and file_path is required")
        if self.file_path is not None:
            object.__setattr__(self, "file_path", Path(self.file_path))

    @classmethod
    def from_bytes(
        cls,
        asset_id: str,
        data: bytes,
        **source_evidence: str | None,
    ) -> "AssetReportRecord":
        return cls(asset_id=asset_id, data=bytes(data), **source_evidence)

    @classmethod
    def from_file(
        cls,
        asset_id: str,
        file_path: str | Path,
        **source_evidence: str | None,
    ) -> "AssetReportRecord":
        path = Path(file_path)
        return cls(asset_id=asset_id, file_path=path, **source_evidence)


def _coerce_record(record: AssetReportRecord | str | Path) -> AssetReportRecord:
    if isinstance(record, AssetReportRecord):
        return record
    path = Path(record)
    return AssetReportRecord.from_file(path.as_posix(), path)


def _record_filename(record: AssetReportRecord) -> str | None:
    if record.filename is not None:
        return record.filename
    if record.file_path is not None:
        return record.file_path.name
    return None


def _read_error_evidence(record: AssetReportRecord, error: OSError) -> dict[str, object]:
    return {
        "asset_id": record.asset_id,
        "canonical_card_uuid": record.canonical_card_uuid,
        "source_card_id": record.source_card_id,
        "source_url": record.source_url,
        "file_path": None if record.file_path is None else str(record.file_path),
        "filename": _record_filename(record),
        "valid": False,
        "actual_format": None,
        "width": None,
        "height": None,
        "byte_size": None,
        "sha256": None,
        "has_transparency": None,
        "duplicate_matches": [],
        "placeholder_or_error": True,
        "placeholder_reasons": ["asset_read_error"],
        "errors": [f"{type(error).__name__}: {error}"],
        "warnings": [],
    }


def _validation_evidence(
    record: AssetReportRecord,
    result: AssetValidationResult,
) -> dict[str, object]:
    evidence = result.to_dict()
    evidence.update(
        {
            "asset_id": record.asset_id,
            "canonical_card_uuid": record.canonical_card_uuid,
            "source_card_id": record.source_card_id,
            "source_url": record.source_url,
            "file_path": None if record.file_path is None else str(record.file_path),
            "filename": _record_filename(record),
        }
    )
    return evidence


def _summary(
    assets: list[dict[str, object]],
    duplicate_groups: list[dict[str, object]],
) -> dict[str, object]:
    total = len(assets)
    valid = sum(bool(asset["valid"]) for asset in assets)
    formats = Counter(str(asset["actual_format"] or "UNKNOWN") for asset in assets)
    dimension_counts = Counter(
        f"{asset['width']}x{asset['height']}"
        for asset in assets
        if asset["width"] is not None and asset["height"] is not None
    )
    transparency = Counter(
        "unknown"
        if asset["has_transparency"] is None
        else "with_transparency"
        if asset["has_transparency"]
        else "without_transparency"
        for asset in assets
    )
    hashed = [asset for asset in assets if asset["sha256"] is not None]
    unique_hashes = {str(asset["sha256"]) for asset in hashed}
    duplicate_asset_ids = {
        str(asset_id)
        for group in duplicate_groups
        for asset_id in group["asset_ids"]  # type: ignore[union-attr]
    }
    placeholder_reasons = Counter(
        str(reason)
        for asset in assets
        for reason in asset["placeholder_reasons"]  # type: ignore[union-attr]
    )
    return {
        "total_assets": total,
        "valid_assets": valid,
        "invalid_assets": total - valid,
        "placeholder_or_error_assets": sum(
            bool(asset["placeholder_or_error"]) for asset in assets
        ),
        "placeholder_reason_counts": dict(sorted(placeholder_reasons.items())),
        "duplicate_assets": len(duplicate_asset_ids),
        "duplicate_hash_groups": len(duplicate_groups),
        "formats": {
            name: formats[name] for name in (*SUPPORTED_FORMATS, "UNKNOWN")
        },
        "dimensions": {
            "known_assets": sum(asset["width"] is not None for asset in assets),
            "unknown_assets": sum(asset["width"] is None for asset in assets),
            "counts": dict(sorted(dimension_counts.items())),
        },
        "hashes": {
            "assets_with_sha256": len(hashed),
            "assets_without_sha256": total - len(hashed),
            "unique_sha256": len(unique_hashes),
            "duplicate_sha256": len(duplicate_groups),
        },
        "transparency": {
            "with_transparency": transparency["with_transparency"],
            "without_transparency": transparency["without_transparency"],
            "unknown": transparency["unknown"],
        },
    }


def build_asset_validation_report(
    records: Iterable[AssetReportRecord | str | Path],
    *,
    known_placeholder_hashes: Iterable[str] = (),
    minimum_card_art_dimension: int = 16,
    minimum_content_bytes: int = 32,
) -> dict[str, object]:
    """Validate assets and return an ordering- and timestamp-independent report.

    Plain paths are accepted for convenience and use their POSIX-style path as
    ``asset_id``. Structured records are recommended when card/source identity
    is available. Duplicate groups include valid images and repeated invalid
    downloads alike because repeated error payloads are useful crawl evidence.
    """

    record_list = [_coerce_record(record) for record in records]
    asset_ids = [record.asset_id for record in record_list]
    if len(asset_ids) != len(set(asset_ids)):
        raise ValueError("asset_id values must be unique within a report")
    record_list.sort(key=lambda record: record.asset_id)
    placeholder_hashes = tuple(known_placeholder_hashes)

    evidence_by_id: dict[str, dict[str, object]] = {}
    validation_by_id: dict[str, AssetValidationResult] = {}
    for record in record_list:
        try:
            data = record.data if record.data is not None else record.file_path.read_bytes()  # type: ignore[union-attr]
        except OSError as error:
            evidence_by_id[record.asset_id] = _read_error_evidence(record, error)
            continue
        result = validate_image_bytes(
            data,
            filename=_record_filename(record),
            known_placeholder_hashes=placeholder_hashes,
            minimum_card_art_dimension=minimum_card_art_dimension,
            minimum_content_bytes=minimum_content_bytes,
        )
        validation_by_id[record.asset_id] = result
        evidence_by_id[record.asset_id] = _validation_evidence(record, result)

    grouped = duplicate_hash_groups(validation_by_id)
    duplicate_groups = [
        {"sha256": digest, "asset_ids": list(asset_ids_for_hash)}
        for digest, asset_ids_for_hash in grouped.items()
    ]
    for group in duplicate_groups:
        group_ids = [str(asset_id) for asset_id in group["asset_ids"]]
        for asset_id in group_ids:
            evidence_by_id[asset_id]["duplicate_matches"] = [
                peer for peer in group_ids if peer != asset_id
            ]

    assets = [evidence_by_id[record.asset_id] for record in record_list]
    return {
        "format": ASSET_REPORT_FORMAT,
        "summary": _summary(assets, duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "assets": assets,
    }


def write_asset_validation_report(
    report: dict[str, object],
    *,
    output_path: str | Path,
) -> str:
    """Write stable UTF-8 JSON and return the resolved output path."""

    path = Path(output_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stable_json_bytes(report))
    return str(path)


__all__ = [
    "ASSET_REPORT_FORMAT",
    "AssetReportRecord",
    "build_asset_validation_report",
    "write_asset_validation_report",
]
