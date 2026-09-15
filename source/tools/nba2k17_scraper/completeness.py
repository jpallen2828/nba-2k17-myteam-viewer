from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
import re
import time
from typing import Any, Iterable, Mapping

from .database import CrawlDatabase
from .models import CanonicalCard, ValueStatus, stable_json_bytes


LEDGER_FORMAT = "nba2k17-card-completeness/v1"
FALLBACK_REASONS = (
    "TWO_KMTC_PAGE_MISSING",
    "TWO_KMTC_PAGE_PARSE_FAILED",
    "MISSING_CARD_PNG",
    "INVALID_CARD_PNG",
    "PLACEHOLDER_CARD_PNG",
    "MISSING_ATTRIBUTES",
    "MISSING_TENDENCIES",
    "MISSING_BADGES",
    "MISSING_VITALS",
    "MISSING_THEME_OR_COLLECTION",
    "MISSING_DYNAMIC_DUO_DATA_WHERE_EXPECTED",
    "OTHER_REQUIRED_DATA_MISSING",
)

_MAIN_CARD_PATH = re.compile(r"/17/players/[^/]+/[^/?#]+/?$", re.IGNORECASE)
_COUNT_KEYS = {
    "source_count_bronze", "source_count_silver", "source_count_gold",
    "source_count_hall_of_fame", "source_count_personality",
}


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not key.endswith("_json") and key != "updated_at"
    }


def _numeric_count(values: Mapping[str, Any]) -> int:
    return sum(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for key, value in values.items()
        if not key.startswith("source_")
    )


def _badge_detail_known(card: CanonicalCard) -> bool:
    values = card.badges.values
    if any(key in values for key in _COUNT_KEYS):
        return True
    return any(
        value in {"BRONZE", "SILVER", "GOLD", "HALL_OF_FAME", "PERSONALITY"}
        for key, value in values.items()
        if key not in _COUNT_KEYS
    )


def _is_myteam_exclusive(card: CanonicalCard) -> bool:
    identity = card.identity.values
    return str(identity.get("team") or "").strip().casefold() == "2kmtcentral"


def _roster_status(card: CanonicalCard | None) -> tuple[str, list[str]]:
    if card is None:
        return "UNKNOWN", []
    overall = card.roster_matches.get("overall") or {}
    status = str(overall.get("status") or "UNMATCHED").upper()
    if status in {"EXACT_VERIFIED", "HIGH_CONFIDENCE", "MANUAL_VERIFIED"}:
        return status, []
    if status == "UNMATCHED" and _is_myteam_exclusive(card):
        return "MYTEAM_EXCLUSIVE_NO_NATIVE_SOURCE", []
    if status == "AMBIGUOUS":
        return status, ["AMBIGUOUS_NATIVE_ROSTER_MATCH"]
    return status, ["UNMATCHED_NATIVE_ROSTER_SOURCE"]


def _verified_asset(asset: Mapping[str, Any]) -> tuple[str, dict[str, Any] | None]:
    try:
        metadata = json.loads(str(asset.get("metadata_json") or "{}"))
    except json.JSONDecodeError:
        return "INVALID", None
    validation = metadata.get("validation") or {}
    if validation.get("placeholder_or_error"):
        return "PLACEHOLDER", None
    path_value = asset.get("raw_cache_path")
    expected = str(asset.get("content_hash") or "")
    if not path_value or not expected:
        return "MISSING", None
    path = Path(str(path_value))
    if not path.is_file():
        return "INVALID", None
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected or validation.get("valid") is not True:
        return "INVALID", None
    if validation.get("sha256") and validation["sha256"] != actual:
        return "INVALID", None
    return "VALID", {
        "path": str(path.resolve()),
        "sha256": actual,
        "source_url": asset.get("asset_url"),
        "width": validation.get("width"),
        "height": validation.get("height"),
    }


def _asset_status(assets: Iterable[Mapping[str, Any]]) -> tuple[str, dict[str, Any] | None]:
    statuses: list[str] = []
    for asset in assets:
        status, evidence = _verified_asset(asset)
        if status == "VALID":
            return status, evidence
        statuses.append(status)
    if "PLACEHOLDER" in statuses:
        return "PLACEHOLDER", None
    if "INVALID" in statuses:
        return "INVALID", None
    return "MISSING", None


def _link_validated_art(card: CanonicalCard, evidence: Mapping[str, Any]) -> None:
    card.card_art.set(
        "card_image_path", str(evidence["path"]),
        status=ValueStatus.WEB_PRIMARY_2KMTC, raw_label="validated card PNG",
    )
    card.card_art.set(
        "card_image_sha256", str(evidence["sha256"]),
        status=ValueStatus.WEB_PRIMARY_2KMTC, raw_label="validated card PNG SHA-256",
    )


def _exact_cdx_is_empty(database: CrawlDatabase, original_url: str) -> bool:
    row = database.connection.execute(
        """SELECT u.raw_cache_path, u.content_hash
             FROM url_discoveries d
             JOIN crawl_urls u ON u.crawl_url_id=d.crawl_url_id
            WHERE u.source='wayback_cdx'
              AND d.discovery_method='exact_cdx_query_for_observed_url'
              AND d.discovered_from=?
              AND u.fetch_status='fetched'
            ORDER BY u.last_success_at DESC LIMIT 1""",
        (original_url,),
    ).fetchone()
    if row is None or not row["raw_cache_path"] or not row["content_hash"]:
        return False
    path = Path(str(row["raw_cache_path"]))
    if not path.is_file():
        return False
    body = path.read_bytes()
    if hashlib.sha256(body).hexdigest() != str(row["content_hash"]):
        return False
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return False
    return isinstance(payload, list) and len(payload) <= 1


def _main_card_rows(database: CrawlDatabase) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    rows = database.connection.execute(
        """SELECT * FROM crawl_urls
             WHERE source='2kmtcentral' AND source_card_id IS NOT NULL
             ORDER BY crawl_url_id"""
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        if not _MAIN_CARD_PATH.search(str(row["url"])):
            continue
        selected.append(row)
    return sorted(
        selected,
        key=lambda row: (
            int(row["source_card_id"]) if str(row["source_card_id"]).isdigit() else 10**20,
            str(row["source_card_id"]), str(row["url"]),
        ),
    )


def refresh_completeness_ledger(
    database: CrawlDatabase,
    cards: Iterable[CanonicalCard],
    *,
    report_root: Path,
    now: float | None = None,
) -> dict[str, Any]:
    """Refresh all discovered card rows without removing prior ledger identities."""

    timestamp = time.time() if now is None else float(now)
    cards_by_id = {
        str(card.source.get("source_card_id")): card
        for card in cards
        if card.source.get("primary_site") == "2kmtcentral"
    }
    for crawl in _main_card_rows(database):
        card_id = str(crawl["source_card_id"])
        card = cards_by_id.get(card_id)
        assets = database.connection.execute(
            """SELECT a.* FROM assets a
                 JOIN crawl_urls u ON u.crawl_url_id=a.crawl_url_id
                 WHERE u.source='2kmtcentral' AND u.source_card_id=?
                   AND a.asset_kind='card_image'
                 ORDER BY asset_id""",
            (card_id,),
        ).fetchall()
        image_status, image_evidence = _asset_status(dict(item) for item in assets)
        if card is not None and image_evidence is not None:
            _link_validated_art(card, image_evidence)

        reasons: list[str] = []
        missing_domains: list[str] = []
        manual: list[str] = []
        if image_status != "VALID":
            reasons.append("MISSING_CARD_PNG")
            missing_domains.append("card_png")
            if image_status == "INVALID":
                reasons.append("INVALID_CARD_PNG")
            elif image_status == "PLACEHOLDER":
                reasons.append("PLACEHOLDER_CARD_PNG")

        if card is None:
            if _exact_cdx_is_empty(database, str(crawl["url"])):
                parse_status = "CONFIRMED_MISSING"
                reasons.append("TWO_KMTC_PAGE_MISSING")
                missing_domains.append("primary_page")
            elif crawl["latest_snapshot_id"] is not None:
                parse_status = "PARSE_FAILED"
                reasons.append("TWO_KMTC_PAGE_PARSE_FAILED")
                missing_domains.append("parsed_card")
            else:
                parse_status = "PENDING"
        else:
            parse_status = "PARSED"
            if _numeric_count(card.attributes.values) < 48:
                reasons.append("MISSING_ATTRIBUTES")
                missing_domains.append("attributes")
            if _numeric_count(card.tendencies.values) < 68:
                reasons.append("MISSING_TENDENCIES")
                missing_domains.append("tendencies")
            if not _badge_detail_known(card):
                reasons.append("MISSING_BADGES")
                missing_domains.append("badges")
            identity = card.identity.values
            if not (identity.get("height") or identity.get("height_cm")) or identity.get("weight") is None:
                reasons.append("MISSING_VITALS")
                missing_domains.append("vitals")
            if not (identity.get("theme") or identity.get("collection") or identity.get("promotion")):
                reasons.append("MISSING_THEME_OR_COLLECTION")
                missing_domains.append("theme_or_collection")

        roster_status, roster_manual = _roster_status(card)
        manual.extend(roster_manual)
        reasons = sorted(set(reasons))
        missing_domains = sorted(set(missing_domains))
        manual = sorted(set(manual))
        field_reasons = [reason for reason in reasons if reason not in {
            "MISSING_CARD_PNG", "INVALID_CARD_PNG", "PLACEHOLDER_CARD_PNG",
            "TWO_KMTC_PAGE_MISSING", "TWO_KMTC_PAGE_PARSE_FAILED",
        }]
        fields_status = "UNKNOWN" if card is None else "PARTIAL" if field_reasons else "COMPLETE"
        needs_mtdb = bool(reasons)
        overall = "NEEDS_MTDB" if needs_mtdb else "MANUAL_REVIEW" if manual else "COMPLETE"
        row = {
            "source_site": "2kmtcentral",
            "source_card_id": card_id,
            "canonical_card_uuid": None if card is None else card.canonical_card_uuid,
            "primary_url": str(crawl["url"]),
            "parse_status": parse_status,
            "image_status": image_status,
            "required_fields_status": fields_status,
            "roster_match_status": roster_status,
            "overall_status": overall,
            "needs_mtdb": needs_mtdb,
            "mtdb_reasons": reasons,
            "manual_review_reasons": manual,
            "missing_domains": missing_domains,
            "metadata": {
                "crawl_url_id": int(crawl["crawl_url_id"]),
                "fetch_status": str(crawl["fetch_status"]),
                "name": None if card is None else card.identity.values.get("name"),
                "overall": None if card is None else card.identity.values.get("overall"),
                "tier": None if card is None else card.identity.values.get("tier"),
                "valid_image": image_evidence,
            },
            "updated_at": timestamp,
        }
        database.upsert_card_completeness(row)

    rows = [_public_row(row) for row in database.list_card_completeness()]
    queue = [row for row in rows if row["needs_mtdb"]]
    missing_png = sum("MISSING_CARD_PNG" in row["mtdb_reasons"] for row in rows)
    counts = {
        "discovered_cards": len(rows),
        "parsed_cards": sum(row["parse_status"] == "PARSED" for row in rows),
        "pending_primary_pages": sum(row["parse_status"] == "PENDING" for row in rows),
        "confirmed_missing_primary_pages": sum(row["parse_status"] == "CONFIRMED_MISSING" for row in rows),
        "valid_card_pngs": sum(row["image_status"] == "VALID" for row in rows),
        "missing_card_pngs_queued_for_mtdb": missing_png,
        "mtdb_queue_cards": len(queue),
        "manual_review_cards": sum(bool(row["manual_review_reasons"]) for row in rows),
        "complete_cards": sum(row["overall_status"] == "COMPLETE" for row in rows),
    }
    report_root = Path(report_root).resolve()
    _write(report_root / "mtdb_fallback_queue.json", stable_json_bytes({
        "format": LEDGER_FORMAT, "counts": counts, "cards": queue,
    }))
    fields = (
        "source_site", "source_card_id", "canonical_card_uuid", "primary_url",
        "parse_status", "image_status", "required_fields_status",
        "roster_match_status", "overall_status", "mtdb_reasons",
        "manual_review_reasons", "missing_domains",
    )
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in queue:
        out = dict(row)
        for key in ("mtdb_reasons", "manual_review_reasons", "missing_domains"):
            out[key] = "|".join(row[key])
        writer.writerow(out)
    _write(report_root / "mtdb_fallback_queue.csv", stream.getvalue().encode("utf-8"))
    _write(report_root / "recovery_status.json", stable_json_bytes({
        "format": LEDGER_FORMAT, "counts": counts,
        "fallback_reason_counts": {
            reason: sum(reason in row["mtdb_reasons"] for row in rows)
            for reason in FALLBACK_REASONS
        },
    }))
    markdown = (
        "# NBA 2K17 MyTEAM recovery status\n\n"
        f"- DISCOVERED CARDS: {counts['discovered_cards']}\n"
        f"- PARSED CARDS: {counts['parsed_cards']}\n"
        f"- VALIDATED CARD PNGS: {counts['valid_card_pngs']}\n"
        f"- CARDS MISSING PNG / QUEUED FOR MTDB: {missing_png}\n"
        f"- TOTAL CARDS QUEUED FOR MTDB: {counts['mtdb_queue_cards']}\n"
        f"- MANUAL REVIEW CARDS: {counts['manual_review_cards']}\n"
        f"- COMPLETE CARDS: {counts['complete_cards']}\n"
    )
    _write(report_root / "recovery_status.md", markdown.encode("utf-8"))
    return {"format": LEDGER_FORMAT, "counts": counts, "rows": rows, "queue": queue}
