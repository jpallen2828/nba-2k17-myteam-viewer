from __future__ import annotations

from collections import Counter
import math
from pathlib import Path
from typing import Any, Iterable

from ..models import CanonicalCard, MatchStatus, SourceSite, ValueStatus, stable_json_bytes
from ..validator import update_completeness


REPORT_FORMAT = "nba2k17-scraper-pilot-report/v1"


def _present_count(
    card: CanonicalCard,
    section_name: str,
    *,
    key_filter=lambda _key: True,
) -> tuple[int, int]:
    section = getattr(card, section_name)
    declared = sum(key_filter(key) for key in section.values)
    present = sum(
        value is not None
        and key_filter(key)
        and section.statuses.get(key) not in {
            ValueStatus.MISSING,
            ValueStatus.CONFLICT,
            ValueStatus.NOT_APPLICABLE,
        }
        for key, value in section.values.items()
    )
    return present, declared


def _coverage(
    cards: list[CanonicalCard],
    section_name: str,
    *,
    key_filter=lambda _key: True,
) -> dict[str, Any]:
    pairs = [
        _present_count(card, section_name, key_filter=key_filter) for card in cards
    ]
    present, declared = sum(item[0] for item in pairs), sum(item[1] for item in pairs)
    cards_with_any = sum(item[0] > 0 for item in pairs)
    return {
        "present_fields": present,
        "declared_fields": declared,
        "field_coverage": 1.0 if declared == 0 else round(present / declared, 6),
        "cards_with_any": cards_with_any,
        "card_coverage": 1.0 if not cards else round(cards_with_any / len(cards), 6),
    }


def build_pilot_report(
    cards: Iterable[CanonicalCard],
    *,
    crawl_stats: dict[str, Any] | None = None,
    parser_errors: list[dict[str, Any]] | None = None,
    rate_limit_events: list[dict[str, Any]] | None = None,
    discovered_count: int | None = None,
    estimated_full_card_count: int | None = None,
    requests_per_card_estimate: float = 8.0,
    effective_interval_seconds: float = 25.0,
    daily_request_ceiling: int = 1_500,
) -> dict[str, Any]:
    card_list = sorted(list(cards), key=lambda card: card.canonical_card_uuid)
    for card in card_list:
        update_completeness(card)
    sites = Counter(str(card.source.get("primary_site") or "") for card in card_list)
    archive_primary = sum(
        any(item.get("archive_timestamp") for item in card.provenance)
        and card.source.get("primary_site") == SourceSite.TWO_K_MT_CENTRAL
        for card in card_list
    )
    match_status = Counter(
        str((card.roster_matches.get("overall") or {}).get("status") or MatchStatus.UNMATCHED)
        for card in card_list
    )
    tiers = Counter(str(card.identity.values.get("tier") or "MISSING") for card in card_list)
    source_reasons = Counter(str(card.source.get("selection_reason") or "UNSPECIFIED") for card in card_list)
    missing_fields = Counter()
    for card in card_list:
        for section_name in (
            "identity", "card_art", "attributes", "tendencies", "badges",
            "hot_zones", "signatures", "gear", "gameplay_metadata",
        ):
            section = getattr(card, section_name)
            for key, value in section.values.items():
                if value is None and section.statuses.get(key) == ValueStatus.MISSING:
                    missing_fields[f"{section_name}.{key}"] += 1
    image_count = sum(card.card_art.values.get("card_image_url") is not None for card in card_list)
    full_cards = estimated_full_card_count or len(card_list)
    request_estimate = int(round(full_cards * requests_per_card_estimate))
    duration_seconds = request_estimate * effective_interval_seconds
    report = {
        "format": REPORT_FORMAT,
        "pilot_card_count": len(card_list),
        "discovered_count": len(card_list) if discovered_count is None else discovered_count,
        "source_counts": {
            "2kmtcentral_records": sites[SourceSite.TWO_K_MT_CENTRAL],
            "2kmtcentral_archived_records": archive_primary,
            "mtdb_fallback_or_only_records": sites[SourceSite.MTDB],
            "selection_reasons": dict(sorted(source_reasons.items())),
        },
        "tier_counts": dict(sorted(tiers.items())),
        "coverage": {
            "attributes": _coverage(card_list, "attributes"),
            "tendencies": _coverage(card_list, "tendencies"),
            "badges": _coverage(
                card_list,
                "badges",
                key_filter=lambda key: not key.startswith("source_count_"),
            ),
            "badge_counts": _coverage(
                card_list,
                "badges",
                key_filter=lambda key: key.startswith("source_count_"),
            ),
            "hot_zones": _coverage(card_list, "hot_zones"),
            "signatures": _coverage(card_list, "signatures"),
            "gear": _coverage(card_list, "gear"),
            "images": {
                "cards_with_image_url": image_count,
                "card_coverage": 1.0 if not card_list else round(image_count / len(card_list), 6),
            },
            "dynamic_duos": {
                "cards_with_any": sum(
                    card.gameplay_metadata.values.get("dynamic_duo_partner") is not None
                    for card in card_list
                ),
                "card_coverage": (
                    1.0
                    if not card_list
                    else round(
                        sum(
                            card.gameplay_metadata.values.get("dynamic_duo_partner")
                            is not None
                            for card in card_list
                        )
                        / len(card_list),
                        6,
                    )
                ),
            },
        },
        "roster_match_counts": {
            status.value: match_status[status.value] for status in MatchStatus
        },
        "injection_ready_count": sum(
            bool(card.injection.get("safe_to_inject")) for card in card_list
        ),
        "manual_review_count": sum(
            not card.injection.get("safe_to_inject", False)
            or not card.completeness.get("adequate_primary_record", False)
            or bool(card.conflicts)
            for card in card_list
        ),
        "missing_fields": [
            {"field": field, "card_count": count}
            for field, count in sorted(missing_fields.items(), key=lambda item: (-item[1], item[0]))
        ],
        "parser_errors": parser_errors or [],
        "rate_limit_events": rate_limit_events or [],
        "crawl_stats": crawl_stats or {},
        "full_crawl_estimate": {
            "estimated_card_count": full_cards,
            "requests_per_card_estimate": requests_per_card_estimate,
            "estimated_requests": request_estimate,
            "effective_interval_seconds": effective_interval_seconds,
            "estimated_duration_seconds": duration_seconds,
            "estimated_duration_hours": round(duration_seconds / 3600, 2),
            "daily_request_ceiling": daily_request_ceiling,
            "minimum_calendar_days_at_daily_ceiling": math.ceil(
                request_estimate / daily_request_ceiling
            ),
            "request_model": (
                "main, badge, tendency, and card-art CDX+replay pairs"
            ),
            "excludes_cooldowns_and_retries": True,
        },
    }
    return report


def _markdown(report: dict[str, Any]) -> str:
    source = report["source_counts"]
    coverage = report["coverage"]
    matches = report["roster_match_counts"]
    lines = [
        "# NBA 2K17 MyTEAM scraper pilot",
        "",
        f"- Pilot cards: {report['pilot_card_count']}",
        f"- URLs discovered: {report['discovered_count']}",
        f"- 2KMTCentral records: {source['2kmtcentral_records']}",
        f"- Archived 2KMTCentral recoveries: {source['2kmtcentral_archived_records']}",
        f"- MTDB fallback/only records: {source['mtdb_fallback_or_only_records']}",
        f"- Injection-ready: {report['injection_ready_count']}",
        f"- Manual review: {report['manual_review_count']}",
        "",
        "## Coverage",
        "",
        "| Domain | Cards with any | Card coverage |",
        "|---|---:|---:|",
    ]
    for name in (
        "attributes", "tendencies", "badges", "badge_counts", "images",
        "hot_zones", "dynamic_duos", "signatures", "gear",
    ):
        item = coverage[name]
        count = item.get("cards_with_any", item.get("cards_with_image_url", 0))
        lines.append(f"| {name.replace('_', ' ').title()} | {count} | {item['card_coverage']:.1%} |")
    lines.extend(["", "## Roster matching", ""])
    for status in MatchStatus:
        lines.append(f"- {status.value}: {matches.get(status.value, 0)}")
    estimate = report["full_crawl_estimate"]
    lines.extend([
        "", "## Full-crawl estimate", "",
        f"- Estimated requests: {estimate['estimated_requests']}",
        f"- Baseline duration: {estimate['estimated_duration_hours']} hours",
        f"- Daily-ceiling minimum: {estimate['minimum_calendar_days_at_daily_ceiling']} days",
        f"- Model: {estimate['request_model']}",
        "- This excludes server-requested cooldowns, blocking events, and retries.",
        "", "## Evidence boundary", "",
        "A parsed value is not called official merely because it passes type/range checks. Missing fields remain null, and unresolved 2K17 option mappings block injection readiness.",
        "",
    ])
    return "\n".join(lines)


def write_pilot_report(report: dict[str, Any], *, output_directory: Path) -> dict[str, str]:
    directory = Path(output_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    json_path = directory / "pilot_report.json"
    markdown_path = directory / "pilot_report.md"
    json_path.write_bytes(stable_json_bytes(report))
    markdown_path.write_text(_markdown(report), encoding="utf-8", newline="\n")
    return {"json": str(json_path), "markdown": str(markdown_path)}
