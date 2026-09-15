from __future__ import annotations

from collections.abc import Iterable, Mapping
import html
import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from .models import (
    CanonicalCard,
    DataSection,
    Provenance,
    SourceSite,
    ValueStatus,
    new_canonical_card,
)


PARSER_VERSION = "nba2k17-normalizer/2"
SOURCE_DETAIL_RECORDS_KEY = "source_detail_records"

_VISIBLE_DUO_DELTA = re.compile(
    r"\s*(?:\(\s*)?(?P<delta>[+\-\N{MINUS SIGN}\N{FULLWIDTH PLUS SIGN}]\s*\d+)"
    r"(?:\s*\))?\s*$"
)
_AGGREGATE_ATTRIBUTE_KEYS = {
    "total_attribute",
    "total_attributes",
    "total_attr",
    "total_attrs",
}
_BADGE_COUNT_KEYS = {
    "source_count_bronze",
    "source_count_silver",
    "source_count_gold",
    "source_count_hall_of_fame",
}


def canonical_key(label: str) -> str:
    value = html.unescape(str(label or ""))
    value = unicodedata.normalize("NFKD", value)
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.replace("&", " and ").replace("3PT", " three ").replace("3pt", " three ")
    value = value.replace("P&R", "pick and roll").replace("Pnr", "pick and roll")
    value = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").lower()
    return re.sub(r"_+", "_", value)


ATTRIBUTE_ALIASES = {
    "open_shot_mid": "open_shot_mid_range",
    "open_shot_mid_range": "open_shot_mid_range",
    "contested_shot_mid": "contested_shot_mid_range",
    "contested_shot_mid_range": "contested_shot_mid_range",
    "off_dribble_shot_mid": "off_dribble_shot_mid_range",
    "off_dribble_shot_mid_range": "off_dribble_shot_mid_range",
    "open_shot_three": "open_shot_three",
    "open_three": "open_shot_three",
    "contested_shot_three": "contested_shot_three",
    "off_dribble_shot_three": "off_dribble_shot_three",
    "shot_close": "shot_close",
    "close_shot": "shot_close",
    "on_ball_defense_iq": "on_ball_defense_iq",
    "on_ball_defensive_iq": "on_ball_defense_iq",
    "low_post_defense_iq": "low_post_defense_iq",
    "low_post_defensive_iq": "low_post_defense_iq",
    "pick_and_roll_defense_iq": "pick_and_roll_defense_iq",
    "pick_and_roll_defensive_iq": "pick_and_roll_defense_iq",
    "pass_perception": "pass_perception",
    "help_defense_iq": "help_defense_iq",
    "help_defensive_iq": "help_defense_iq",
    "off_consistency": "offensive_consistency",
    "def_consistency": "defensive_consistency",
    "offensive_rebound": "offensive_rebound",
    "defensive_rebound": "defensive_rebound",
    "overall_durability": "overall_durability",
    "free_throw": "free_throw",
    "passing_iq": "passing_iq",
    "shot_iq": "shot_iq",
}

TENDENCY_ALIASES = {
    "shot_tendency": "shot",
    "shot_mid": "shot_mid_range",
    "shot_mid_range": "shot_mid_range",
    "shot_three_point": "shot_three",
    "pull_up_in_transition": "pull_up_in_transition",
    "transition_pullup_three": "transition_pullup_three",
    "drive_stepback": "driving_step_back",
    "drive_behind_back": "driving_behind_the_back",
    "drive_cross": "driving_crossover",
    "drive_hesitation": "driving_dribble_hesitation",
    "drive_in_and_out": "driving_in_and_out",
    "post_faceup": "post_face_up",
    "post_backdown": "post_back_down",
    "post_stepback": "post_step_back_shot",
    "play_passing_lanes": "pass_interception",
}

IDENTITY_ALIASES = {
    "player_name": "name",
    "display_name": "name",
    "ovr": "overall",
    "gem": "tier",
    "color": "tier",
    "set": "theme",
    "card_set": "theme",
    "season": "represented_season",
    "year": "card_year",
    "position": "primary_position",
    "position_primary": "primary_position",
    "position_secondary": "secondary_position",
    "from": "origin",
}

BADGE_LEVELS = {
    "bronze": "BRONZE",
    "silver": "SILVER",
    "gold": "GOLD",
    "hall_of_fame": "HALL_OF_FAME",
    "hall_of_fame_badge": "HALL_OF_FAME",
    "hof": "HALL_OF_FAME",
    "personality": "PERSONALITY",
}


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    raise TypeError(f"parsed card must be mapping-like, got {type(value).__name__}")


def _coalesce(value: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in value and value[key] is not None:
            return value[key]
    return default


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"-?\d+", str(value).replace(",", ""))
    return None if match is None else int(match.group(0))


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if canonical_key(text) in {"", "n_a", "none", "null", "unknown"} or text in {
        "-",
        "—",
    }:
        return None
    return text


def _attribute_key_and_label(label: str, duo_boost: int | None) -> tuple[str, str]:
    """Return a canonical attribute key without a rendered duo delta.

    Real 2KMTCentral markup places a visible ``+2`` after the attribute name.
    The source adapter deliberately preserves that text, so this boundary is
    where ``Ball control +2`` becomes ``ball_control``. The untouched source
    label is returned separately for provenance.
    """

    raw_label = str(label)
    base_label = raw_label
    if duo_boost is not None:
        match = _VISIBLE_DUO_DELTA.search(raw_label)
        if match is not None:
            parsed_delta = _as_int(
                match.group("delta")
                .replace("\N{MINUS SIGN}", "-")
                .replace("\N{FULLWIDTH PLUS SIGN}", "+")
            )
            if parsed_delta == duo_boost:
                base_label = raw_label[: match.start()].rstrip()
        else:
            # Also tolerate a previously canonicalized source label such as
            # ``ball_control_2``. Requiring snake case avoids stripping the 3
            # from a legitimate human label such as ``Open shot 3``.
            candidate = canonical_key(raw_label)
            suffix = f"_{abs(duo_boost)}"
            if (
                re.fullmatch(r"[a-z0-9_]+", raw_label)
                and candidate.endswith(suffix)
            ):
                candidate = candidate[: -len(suffix)]
                return ATTRIBUTE_ALIASES.get(candidate, candidate), raw_label
    candidate = canonical_key(base_label)
    return ATTRIBUTE_ALIASES.get(candidate, candidate), raw_label


def _detail_value_keys(section: DataSection) -> list[str]:
    return [
        key
        for key, value in section.values.items()
        if key != SOURCE_DETAIL_RECORDS_KEY
        and not key.startswith("source_count_")
        and value is not None
        and section.statuses.get(key) not in {
            ValueStatus.MISSING,
            ValueStatus.CONFLICT,
            ValueStatus.NOT_APPLICABLE,
        }
    ]


def source_detail_missing(section: DataSection, domain: str) -> bool:
    """Whether a badge/tendency section lacks recoverable detail records."""

    detail_count = len(_detail_value_keys(section))
    if domain == "tendencies":
        return detail_count == 0
    if domain != "badges":
        raise ValueError(f"unsupported source-detail domain: {domain}")

    counts_are_complete = all(
        isinstance(section.values.get(key), int)
        and not isinstance(section.values.get(key), bool)
        and section.values[key] >= 0
        for key in _BADGE_COUNT_KEYS
    )
    if not counts_are_complete:
        return detail_count == 0
    reported_total = sum(int(section.values[key]) for key in _BADGE_COUNT_KEYS)
    # Four explicit zero counts prove a card has no tiered gameplay badges.
    # Otherwise a count/detail mismatch means only aggregate counts were seen.
    return detail_count < reported_total


def _height_cm(value: Any) -> float | None:
    if value is None:
        return None
    text = str(value)
    metric = re.search(r"(\d+(?:\.\d+)?)\s*cm\b", text, re.IGNORECASE)
    if metric:
        return float(metric.group(1))
    imperial = re.search(r"(\d+)\s*['′]\s*(\d+)\s*(?:[\"″]|in\b)?", text)
    if imperial:
        inches = int(imperial.group(1)) * 12 + int(imperial.group(2))
        return round(inches * 2.54, 2)
    return None


def _source_status(site: SourceSite) -> ValueStatus:
    return (
        ValueStatus.WEB_PRIMARY_2KMTC
        if site == SourceSite.TWO_K_MT_CENTRAL
        else ValueStatus.WEB_FALLBACK_MTDB
    )


def _iter_labeled_values(value: Any) -> Iterable[tuple[str, Any, dict[str, Any]]]:
    if isinstance(value, Mapping):
        for label, item in value.items():
            if isinstance(item, Mapping):
                item = dict(item)
                yield str(item.get("label") or label), item.get("value"), item
            else:
                yield str(label), item, {}
        return
    if not isinstance(value, Iterable) or isinstance(value, (str, bytes)):
        return
    for item in value:
        if not isinstance(item, Mapping):
            continue
        item = dict(item)
        label = item.get("label") or item.get("name") or item.get("key")
        if label is not None:
            yield str(label), item.get("value"), item


def normalize_parsed_card(
    parsed_value: Any,
    *,
    parity_crosswalk: Path | None = None,
) -> CanonicalCard:
    parsed = _mapping(parsed_value)
    raw_site = str(_coalesce(parsed, "source_site", "site", default="")).lower()
    site_aliases = {
        "2kmtcentral": SourceSite.TWO_K_MT_CENTRAL,
        "two_k_mt_central": SourceSite.TWO_K_MT_CENTRAL,
        "2kmtc": SourceSite.TWO_K_MT_CENTRAL,
        "mtdb": SourceSite.MTDB,
    }
    if raw_site not in site_aliases:
        raise ValueError(f"unsupported website source: {raw_site!r}")
    site = site_aliases[raw_site]
    identity_raw = _mapping(parsed.get("identity") or {})
    source_card_id = _coalesce(
        parsed,
        "source_card_id",
        "card_id",
        default=_coalesce(identity_raw, "source_card_id", "card_id"),
    )
    source_url = _coalesce(parsed, "source_url", "url")
    source_slug = _coalesce(
        parsed,
        "source_card_slug",
        "slug",
        default=_coalesce(identity_raw, "source_card_slug", "slug"),
    )
    if source_card_id is None or not source_url:
        raise ValueError("parsed card requires source_card_id and source_url")

    card = new_canonical_card(
        source_site=site,
        source_card_id=str(source_card_id),
        source_url=str(source_url),
        source_card_slug=None if source_slug is None else str(source_slug),
    )
    status = _source_status(site)
    provenance = Provenance(
        source_site=site.value,
        source_url=str(source_url),
        source_card_id=str(source_card_id),
        snapshot_sha256=_coalesce(parsed, "snapshot_sha256", "content_sha256"),
        captured_at=parsed.get("captured_at"),
        archive_timestamp=parsed.get("archive_timestamp"),
        parser_version=str(parsed.get("parser_version") or PARSER_VERSION),
    ).to_dict()
    card.provenance.append(provenance)
    card.identity.provenance.append(provenance)

    for label, raw_value in identity_raw.items():
        raw_key = canonical_key(label)
        key = IDENTITY_ALIASES.get(raw_key, raw_key)
        if key in {"overall", "weight", "age", "jersey_number", "card_year"}:
            value = _as_int(raw_value)
        else:
            value = raw_value if raw_value not in ("—", "N/A", "n/a", "") else None
        field_status = ValueStatus.MISSING if value is None else status
        card.identity.set(key, value, status=field_status, raw_label=str(label))

    title_identity = {
        "name": parsed.get("name"),
        "overall": parsed.get("overall"),
        "tier": parsed.get("tier"),
        "theme": _coalesce(parsed, "theme", "set"),
        "collection": parsed.get("collection"),
        "team": parsed.get("team"),
        "primary_position": _coalesce(parsed, "primary_position", "position"),
        "secondary_position": parsed.get("secondary_position"),
        "height": parsed.get("height"),
        "height_cm": parsed.get("height_cm") or _height_cm(parsed.get("height")),
        "weight": parsed.get("weight"),
        "age": parsed.get("age"),
        "origin": parsed.get("origin"),
        "jersey_number": parsed.get("jersey_number"),
        "card_year": parsed.get("card_year"),
        "represented_season": parsed.get("represented_season"),
        "promotion": parsed.get("promotion"),
    }
    for key, raw_value in title_identity.items():
        if raw_value is None:
            continue
        value = _as_int(raw_value) if key in {"overall", "weight", "age", "jersey_number", "card_year"} else raw_value
        card.identity.set(key, value, status=status, raw_label=key)

    duo_boosts: dict[str, int] = {}
    duo_boost_raw_labels: dict[str, str] = {}

    def remember_duo_boost(label: str, boost_value: Any) -> None:
        boost = _as_int(boost_value)
        if boost in (None, 0):
            return
        key, raw_label = _attribute_key_and_label(label, boost)
        if key in _AGGREGATE_ATTRIBUTE_KEYS:
            return
        existing = duo_boosts.get(key)
        if existing is not None and existing != boost:
            card.conflicts.append(
                {
                    "kind": "DYNAMIC_DUO_BOOST_CONFLICT",
                    "field": key,
                    "first_value": existing,
                    "other_value": boost,
                    "resolution": "FIRST_SOURCE_VALUE_RETAINED",
                }
            )
            return
        duo_boosts[key] = boost
        duo_boost_raw_labels.setdefault(key, raw_label)

    for label, raw_value, metadata in _iter_labeled_values(parsed.get("attributes")):
        boost = _as_int(
            _coalesce(metadata, "dynamic_duo_boost", "duo_boost", "boost")
        )
        key, raw_label = _attribute_key_and_label(label, boost)
        if key in _AGGREGATE_ATTRIBUTE_KEYS:
            continue
        value = _as_int(raw_value)
        card.attributes.set(
            key,
            value,
            status=ValueStatus.MISSING if value is None else ValueStatus.WEB_CARD_SPECIFIC,
            raw_label=raw_label,
        )
        remember_duo_boost(label, boost)

    for label, raw_value, _metadata in _iter_labeled_values(parsed.get("tendencies")):
        key = TENDENCY_ALIASES.get(canonical_key(label), canonical_key(label))
        value = _as_int(raw_value)
        card.tendencies.set(
            key,
            value,
            status=ValueStatus.MISSING if value is None else ValueStatus.WEB_CARD_SPECIFIC,
            raw_label=label,
        )
    if source_detail_missing(card.tendencies, "tendencies"):
        card.tendencies.set(
            SOURCE_DETAIL_RECORDS_KEY,
            None,
            status=ValueStatus.MISSING,
            raw_label="tendency detail records",
        )

    for label, raw_level, metadata in _iter_labeled_values(parsed.get("badges")):
        key = canonical_key(label)
        level_source = _coalesce(metadata, "level", "tier", default=raw_level)
        level_key = canonical_key(str(level_source or ""))
        level = BADGE_LEVELS.get(level_key, str(level_source).upper() if level_source else None)
        card.badges.set(
            key,
            level,
            status=ValueStatus.MISSING if level is None else ValueStatus.WEB_CARD_SPECIFIC,
            raw_label=label,
        )
    for level, count in _mapping(parsed.get("badge_counts") or {}).items():
        key = f"source_count_{canonical_key(level)}"
        normalized_count = _as_int(count)
        card.badges.set(
            key,
            normalized_count,
            status=(
                ValueStatus.MISSING if normalized_count is None else status
            ),
            raw_label=str(level),
        )
    if source_detail_missing(card.badges, "badges"):
        card.badges.set(
            SOURCE_DETAIL_RECORDS_KEY,
            None,
            status=ValueStatus.MISSING,
            raw_label="badge detail records",
        )

    for section_name in ("hot_zones", "signatures", "gear"):
        section = getattr(card, section_name)
        for label, raw_value, _metadata in _iter_labeled_values(parsed.get(section_name)):
            key = canonical_key(label)
            value = raw_value if raw_value not in ("", "N/A", "n/a", "—") else None
            section.set(
                key,
                value,
                status=ValueStatus.MISSING if value is None else ValueStatus.WEB_CARD_SPECIFIC,
                raw_label=label,
            )

    duo = _mapping(parsed.get("dynamic_duo") or {})
    for label, raw_boost, metadata in _iter_labeled_values(duo.get("boosts")):
        remember_duo_boost(
            label,
            _coalesce(
                metadata,
                "dynamic_duo_boost",
                "duo_boost",
                "boost",
                "value",
                "raw_value",
                default=raw_boost,
            ),
        )
    for label, raw_boost, metadata in _iter_labeled_values(
        parsed.get("dynamic_duo_boosts")
    ):
        remember_duo_boost(
            label,
            _coalesce(metadata, "value", "raw_value", default=raw_boost),
        )
    if duo_boosts:
        card.gameplay_metadata.set(
            "dynamic_duo_boosts",
            dict(sorted(duo_boosts.items())),
            status=ValueStatus.WEB_CARD_SPECIFIC,
        )
        for key, raw_label in sorted(duo_boost_raw_labels.items()):
            card.gameplay_metadata.raw_labels[
                f"dynamic_duo_boosts.{key}"
            ] = raw_label

    if duo:
        partner = _optional_text(_coalesce(duo, "partner_name", "name", "partner"))
        partner_id = _optional_text(
            _coalesce(
                duo,
                "partner_source_card_id",
                "source_card_id",
                "card_id",
            )
        )
        if partner is not None:
            card.gameplay_metadata.set(
                "dynamic_duo_partner", partner, status=ValueStatus.WEB_CARD_SPECIFIC
            )
        if partner_id is not None:
            card.gameplay_metadata.set(
                "dynamic_duo_partner_source_card_id",
                str(partner_id),
                status=ValueStatus.WEB_CARD_SPECIFIC,
            )

    art = _mapping(parsed.get("card_art") or {})
    for source_key, key in (
        ("card_image_url", "card_image_url"),
        ("image_url", "card_image_url"),
        ("portrait_url", "portrait_url"),
        ("cutout_url", "cutout_url"),
        ("player_image_url", "cutout_url"),
    ):
        if art.get(source_key) or parsed.get(source_key):
            card.card_art.set(
                key,
                art.get(source_key) or parsed.get(source_key),
                status=status,
                raw_label=source_key,
            )

    for section in (
        card.card_art,
        card.attributes,
        card.tendencies,
        card.badges,
        card.hot_zones,
        card.signatures,
        card.gear,
        card.gameplay_metadata,
    ):
        if section.values:
            section.provenance.append(provenance)

    card.raw_source = parsed
    card.source["snapshots"] = [provenance]
    if parity_crosswalk is not None:
        apply_crosswalk_missingness(card, parity_crosswalk)
    return card


def apply_crosswalk_missingness(card: CanonicalCard, crosswalk_path: Path) -> None:
    crosswalk = json.loads(Path(crosswalk_path).read_text(encoding="utf-8"))
    key_aliases = {
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
    for entry in crosswalk.get("fields", []):
        domain = entry.get("domain") or entry.get("section")
        qualified_key = entry.get("nba2k17_canonical_key")
        if not domain or not qualified_key:
            continue
        section = getattr(card, str(domain), None)
        if section is None or not hasattr(section, "declare_missing"):
            continue
        tail = str(qualified_key).rsplit(".", 1)[-1]
        key = key_aliases.get(tail, tail)
        mapping_status = str(entry.get("mapping_status") or "UNKNOWN")
        if mapping_status == "NOT_APPLICABLE":
            if key not in section.values:
                section.set(key, None, status=ValueStatus.NOT_APPLICABLE)
        else:
            section.declare_missing((str(key),))
