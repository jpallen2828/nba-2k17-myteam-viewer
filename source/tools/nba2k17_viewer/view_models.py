from __future__ import annotations

from typing import Any, Mapping


BADGE_ORDER = ("HALL_OF_FAME", "GOLD", "SILVER", "BRONZE", "PERSONALITY")
BADGE_LABELS = {
    "HALL_OF_FAME": "Hall of Fame (purple)",
    "GOLD": "Gold", "SILVER": "Silver", "BRONZE": "Bronze",
    "PERSONALITY": "Personality badges",
}


def present_values(section: Mapping[str, Any]) -> list[tuple[str, Any, str]]:
    values = section.get("values") or {}
    statuses = section.get("statuses") or {}
    return [
        (key, value, ("Reconstructed" if "estimat" in str(statuses.get(key)).lower() else str(statuses.get(key) or "UNKNOWN")))
        for key, value in sorted(values.items())
        if value is not None and not key.startswith("source_")
    ]


def grouped_badges(card: Mapping[str, Any]) -> dict[str, list[str]]:
    groups = {level: [] for level in BADGE_ORDER}
    for name, level in (card.get("badges", {}).get("values") or {}).items():
        if name.startswith("source_count_") or name == "source_detail_records":
            continue
        normalized = str(level).upper()
        if normalized in groups:
            groups[normalized].append(name)
    return {level: sorted(names) for level, names in groups.items()}


def duo_summary(card: Mapping[str, Any]) -> dict[str, Any]:
    values = card.get("gameplay_metadata", {}).get("values") or {}
    return {
        "partner": values.get("dynamic_duo_partner"),
        "partner_source_card_id": values.get("dynamic_duo_partner_source_card_id"),
        "boosts": values.get("dynamic_duo_boosts"),
    }


def source_assignments(card: Mapping[str, Any]) -> list[tuple[str, str]]:
    matches = card.get("roster_matches") or {}
    fields = (
        "signatures_source_player", "hot_zones_source_player", "gear_source_player",
        "appearance_source_player", "gameplay_metadata_source_player",
    )
    return [(field, str(matches.get(field) or "No native source assigned")) for field in fields]
