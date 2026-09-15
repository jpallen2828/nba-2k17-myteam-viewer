from __future__ import annotations

from collections import Counter
import json
from pathlib import Path
from typing import Any, Iterable

from .models import CanonicalCard, ValueStatus, stable_json_bytes


PARITY_STATES = (
    "PRESENT",
    "EXPLICITLY_MISSING",
    "NOT_APPLICABLE",
    "AWAITING_MANUAL_REVIEW",
)

_KEY_ALIASES = {
    "full_name": "name",
    "card_year": "card_year",
    "card_overall": "overall",
    "card_tier": "tier",
    "theme_name": "theme",
    "collection_name": "collection",
    "card_team": "team",
    "image": "card_image_url",
    "stored_art": "card_image_path",
}


def _sections_for_domain(card: CanonicalCard, domain: str) -> list[Any]:
    direct = {
        "attributes": (card.attributes,),
        "tendencies": (card.tendencies,),
        "gameplay_badges": (card.badges,),
        "personality_badges": (card.badges,),
        "signatures": (card.signatures,),
        "hot_zones": (card.hot_zones,),
        "gear": (card.gear,),
        "card_art": (card.card_art,),
    }
    if domain in direct:
        return list(direct[domain])
    if domain in {
        "identity",
        "card_metadata",
        "appearance",
        "player_template_metadata",
        "package_metadata",
    }:
        return [card.identity, card.gameplay_metadata, card.card_art]
    return []


def _candidate_keys(entry: dict[str, Any]) -> list[str]:
    canonical = str(entry.get("nba2k17_canonical_key") or "")
    tail = canonical.rsplit(".", 1)[-1]
    candidates = [tail, _KEY_ALIASES.get(tail, tail)]
    parity_tail = str(entry.get("parity_id") or "").rsplit(".", 1)[-1]
    candidates.extend((parity_tail, _KEY_ALIASES.get(parity_tail, parity_tail)))
    return list(dict.fromkeys(key for key in candidates if key))


def evaluate_card_parity(
    card: CanonicalCard,
    crosswalk: dict[str, Any] | Path,
) -> dict[str, Any]:
    payload = (
        json.loads(Path(crosswalk).read_text(encoding="utf-8"))
        if isinstance(crosswalk, Path)
        else crosswalk
    )
    results: list[dict[str, Any]] = []
    for entry in payload.get("fields", []):
        state = "AWAITING_MANUAL_REVIEW"
        located_path = None
        status = None
        if entry.get("mapping_status") == "NOT_APPLICABLE":
            state = "NOT_APPLICABLE"
        else:
            for section in _sections_for_domain(card, str(entry.get("domain") or "")):
                for key in _candidate_keys(entry):
                    if key not in section.values:
                        continue
                    located_path = key
                    status = section.statuses.get(key)
                    value = section.values.get(key)
                    if status == ValueStatus.NOT_APPLICABLE:
                        state = "NOT_APPLICABLE"
                    elif value is None and status == ValueStatus.MISSING:
                        state = "EXPLICITLY_MISSING"
                    elif value is not None and status not in {
                        None,
                        ValueStatus.MISSING,
                        ValueStatus.CONFLICT,
                        ValueStatus.NOT_APPLICABLE,
                    }:
                        state = "PRESENT"
                    break
                if located_path is not None:
                    break
        results.append(
            {
                "parity_id": entry.get("parity_id"),
                "state": state,
                "located_key": located_path,
                "value_status": status,
            }
        )
    counts = Counter(item["state"] for item in results)
    if any(item["state"] not in PARITY_STATES for item in results):
        raise ValueError("parity evaluation produced an untracked state")
    return {
        "canonical_card_uuid": card.canonical_card_uuid,
        "field_count": len(results),
        "state_counts": {state: counts[state] for state in PARITY_STATES},
        "fields": results,
        "silent_or_defaulted_count": 0,
    }


def write_parity_report(
    cards: Iterable[CanonicalCard],
    *,
    crosswalk_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    crosswalk = json.loads(Path(crosswalk_path).read_text(encoding="utf-8"))
    evaluations = [
        evaluate_card_parity(card, crosswalk)
        for card in sorted(cards, key=lambda item: item.canonical_card_uuid)
    ]
    totals = Counter(
        state
        for evaluation in evaluations
        for state, count in evaluation["state_counts"].items()
        for _ in range(count)
    )
    report = {
        "format": "nba2k17-parity-coverage/v1",
        "card_count": len(evaluations),
        "crosswalk_field_count": len(crosswalk.get("fields", [])),
        "state_totals": {state: totals[state] for state in PARITY_STATES},
        "silent_or_defaulted_count": 0,
        "cards": evaluations,
    }
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(stable_json_bytes(report))
    return report
