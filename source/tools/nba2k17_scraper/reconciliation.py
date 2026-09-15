from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from .live_roster_matcher import normalize_name
from .models import CanonicalCard, DataSection, SourceSite, ValueStatus
from .validator import is_adequate, update_completeness


SOURCE_ORDER = (SourceSite.TWO_K_MT_CENTRAL.value, SourceSite.MTDB.value)
SECTION_NAMES = (
    "identity", "card_art", "attributes", "tendencies", "badges",
    "hot_zones", "signatures", "gear", "gameplay_metadata",
)


@dataclass(frozen=True)
class CrossSiteMatch:
    status: str
    score: int
    explanation: str
    features: tuple[dict[str, Any], ...]
    primary_uuid: str | None
    fallback_uuid: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "explanation": self.explanation,
            "features": list(self.features),
            "primary_uuid": self.primary_uuid,
            "fallback_uuid": self.fallback_uuid,
        }


def compare_cross_site_cards(primary: CanonicalCard, fallback: CanonicalCard) -> CrossSiteMatch:
    left = primary.identity.values
    right = fallback.identity.values
    features: list[dict[str, Any]] = []
    score = 0
    left_name = normalize_name(left.get("name"))
    right_name = normalize_name(right.get("name"))
    if not left_name or left_name != right_name:
        return CrossSiteMatch(
            "REJECTED", -100, "Normalized player names differ.",
            ({"feature": "name", "matched": False, "score": -100},),
            primary.canonical_card_uuid, fallback.canonical_card_uuid,
        )
    score += 35
    features.append({"feature": "name", "matched": True, "score": 35})
    comparisons = (
        ("overall", 20, True),
        ("tier", 12, False),
        ("theme", 12, False),
        ("collection", 10, False),
        ("team", 8, False),
        ("card_year", 8, True),
        ("primary_position", 6, False),
        ("secondary_position", 4, False),
    )
    conflicting_strong_features = []
    for key, weight, numeric in comparisons:
        first, second = left.get(key), right.get(key)
        if first is None or second is None:
            continue
        if numeric:
            distance = abs(int(first) - int(second))
            matched = distance == 0
            delta = weight if matched else max(-weight, weight - distance * weight)
        else:
            matched = normalize_name(str(first)) == normalize_name(str(second))
            delta = weight if matched else -weight
        score += delta
        features.append({"feature": key, "matched": matched, "score": delta})
        if not matched and key in {"overall", "tier", "theme", "collection", "card_year"}:
            conflicting_strong_features.append(key)
    left_height, right_height = left.get("height_cm"), right.get("height_cm")
    if left_height is not None and right_height is not None:
        distance = abs(float(left_height) - float(right_height))
        delta = 5 if distance <= 2.6 else -5
        score += delta
        features.append({"feature": "height_cm", "distance": round(distance, 3), "score": delta})
    left_art = primary.card_art.values.get("card_image_sha256")
    right_art = fallback.card_art.values.get("card_image_sha256")
    if left_art and right_art:
        delta = 15 if left_art == right_art else -8
        score += delta
        features.append({"feature": "card_image_sha256", "matched": left_art == right_art, "score": delta})
    if conflicting_strong_features:
        return CrossSiteMatch(
            "AMBIGUOUS", score,
            "Strong variant features conflict: " + ", ".join(conflicting_strong_features),
            tuple(features), primary.canonical_card_uuid, fallback.canonical_card_uuid,
        )
    if score < 70:
        return CrossSiteMatch(
            "AMBIGUOUS", score,
            "Evidence is insufficient to merge distinct source identities automatically.",
            tuple(features), primary.canonical_card_uuid, fallback.canonical_card_uuid,
        )
    return CrossSiteMatch(
        "MATCHED", score,
        "Name and available variant-defining features agree.",
        tuple(features), primary.canonical_card_uuid, fallback.canonical_card_uuid,
    )


def _merge_missing_section(
    destination: DataSection,
    fallback: DataSection,
    *,
    destination_card: CanonicalCard,
    section_name: str,
) -> list[str]:
    filled: list[str] = []
    for key, fallback_value in fallback.values.items():
        current_value = destination.values.get(key)
        current_status = destination.statuses.get(key)
        fallback_status = fallback.statuses.get(key)
        if fallback_value is None:
            continue
        if current_value is None or current_status == ValueStatus.MISSING:
            destination.set(
                key,
                fallback_value,
                status=ValueStatus.WEB_FALLBACK_MTDB,
                raw_label=fallback.raw_labels.get(key),
            )
            filled.append(f"{section_name}.{key}")
            continue
        if current_value != fallback_value:
            destination_card.conflicts.append({
                "section": section_name,
                "field": key,
                "primary_value": current_value,
                "fallback_value": fallback_value,
                "primary_status": current_status,
                "fallback_status": fallback_status,
                "resolution": "PRIMARY_RETAINED",
            })
    for item in fallback.provenance:
        if item not in destination.provenance:
            destination.provenance.append(item)
    return filled


def reconcile_primary_and_fallback(
    primary: CanonicalCard | None,
    fallback: CanonicalCard | None,
    *,
    cross_site_match: CrossSiteMatch | None = None,
) -> CanonicalCard:
    if primary is None and fallback is None:
        raise ValueError("at least one source record is required")
    if primary is None:
        result = deepcopy(fallback)
        result.source["selection_reason"] = "MTDB_ONLY_NO_2KMTC_RECORD"
        update_completeness(result)
        return result
    result = deepcopy(primary)
    primary_adequate, primary_missing = is_adequate(primary)
    if fallback is None:
        result.source["selection_reason"] = (
            "ADEQUATE_2KMTC_PRIMARY" if primary_adequate else "INCOMPLETE_2KMTC_NO_MTDB_FALLBACK"
        )
        result.source["primary_inadequate_reasons"] = primary_missing
        update_completeness(result)
        return result

    match = cross_site_match or compare_cross_site_cards(primary, fallback)
    result.source["mtdb_reconciliation"] = match.to_dict()
    if match.status != "MATCHED":
        result.source["selection_reason"] = (
            "ADEQUATE_2KMTC_PRIMARY_MTDB_AMBIGUOUS"
            if primary_adequate else "INCOMPLETE_2KMTC_MTDB_AMBIGUOUS_MANUAL_REVIEW"
        )
        result.conflicts.append({
            "kind": "AMBIGUOUS_CROSS_SITE_MATCH",
            "details": match.to_dict(),
        })
        update_completeness(result)
        return result

    filled_fields: list[str] = []
    if not primary_adequate:
        result.source["primary_inadequate_reasons"] = primary_missing
    for section_name in SECTION_NAMES:
        filled_fields.extend(_merge_missing_section(
            getattr(result, section_name),
            getattr(fallback, section_name),
            destination_card=result,
            section_name=section_name,
        ))
    result.source["mtdb_gap_filled_fields"] = sorted(filled_fields)
    if primary_adequate:
        result.source["selection_reason"] = (
            "ADEQUATE_2KMTC_PRIMARY_MTDB_GAP_FILL"
            if filled_fields else "ADEQUATE_2KMTC_PRIMARY_MTDB_VALIDATION_ONLY"
        )
    else:
        result.source["selection_reason"] = "INCOMPLETE_2KMTC_ENRICHED_BY_MATCHED_MTDB_FALLBACK"
    for item in fallback.provenance:
        if item not in result.provenance:
            result.provenance.append(item)
    update_completeness(result)
    return result


def reconcile_catalogs(
    primary_cards: Iterable[CanonicalCard],
    fallback_cards: Iterable[CanonicalCard],
) -> tuple[list[CanonicalCard], list[dict[str, Any]]]:
    primary = list(primary_cards)
    fallback = list(fallback_cards)
    reports: list[dict[str, Any]] = []
    consumed_fallback: set[str] = set()
    output: list[CanonicalCard] = []
    for card in primary:
        candidates = []
        for other in fallback:
            if other.canonical_card_uuid in consumed_fallback:
                continue
            comparison = compare_cross_site_cards(card, other)
            if comparison.status != "REJECTED":
                candidates.append((comparison.score, other.canonical_card_uuid, comparison, other))
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if not candidates:
            output.append(reconcile_primary_and_fallback(card, None))
            continue
        best = candidates[0]
        ambiguous = len(candidates) > 1 and candidates[1][0] >= best[0] - 4
        if ambiguous:
            report = {
                "kind": "AMBIGUOUS_CROSS_SITE_CANDIDATES",
                "primary_uuid": card.canonical_card_uuid,
                "candidates": [item[2].to_dict() for item in candidates[:5]],
            }
            reports.append(report)
            output.append(reconcile_primary_and_fallback(card, None))
            continue
        comparison, other = best[2], best[3]
        if comparison.status == "MATCHED":
            consumed_fallback.add(other.canonical_card_uuid)
        else:
            reports.append(comparison.to_dict())
        output.append(reconcile_primary_and_fallback(card, other, cross_site_match=comparison))
    for card in fallback:
        if card.canonical_card_uuid not in consumed_fallback:
            output.append(reconcile_primary_and_fallback(None, card))
            reports.append({
                "kind": "MTDB_ONLY_CARD",
                "fallback_uuid": card.canonical_card_uuid,
                "source_card_id": card.source.get("source_card_id"),
            })
    output.sort(key=lambda card: card.canonical_card_uuid)
    return output, reports
