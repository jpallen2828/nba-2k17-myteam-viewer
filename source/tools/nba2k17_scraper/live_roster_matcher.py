from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import re
import unicodedata
from typing import Any, Iterable, Mapping

from .models import CanonicalCard, MatchStatus, ValueStatus, sha256_json


MATCHER_VERSION = "nba2k17-roster-matcher/1"
INHERITABLE_DOMAINS = ("signatures", "hot_zones", "gear", "appearance", "gameplay_metadata")


def normalize_name(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    normalized = "".join(character for character in normalized if not unicodedata.combining(character))
    normalized = normalized.casefold().replace("’", "'")
    normalized = re.sub(r"\b(jr|sr|ii|iii|iv)\.?\b", "", normalized)
    return re.sub(r"[^a-z0-9]+", " ", normalized).strip()


def _position_label(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("label")
    if value in (None, "", "NONE"):
        return None
    return str(value).upper()


@dataclass(frozen=True)
class RosterPlayer:
    source_roster_identifier: str
    source_team: str | None
    source_slot: int | None
    source_roster_overall: int | None
    source_player_id: str | None
    full_name: str
    first_name: str | None = None
    last_name: str | None = None
    primary_position: str | None = None
    secondary_position: str | None = None
    height_cm: float | None = None
    weight: int | None = None
    era: str | None = None
    sticker_family: str | None = None
    face_id: int | None = None
    graphic_id: int | None = None
    portrait_id: int | None = None
    appearance_id: int | None = None
    row_sha256: str = ""
    domain_values: dict[str, dict[str, Any]] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_roster_identifier": self.source_roster_identifier,
            "source_team": self.source_team,
            "source_slot": self.source_slot,
            "source_roster_overall": self.source_roster_overall,
            "source_player_id": self.source_player_id,
            "full_name": self.full_name,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "primary_position": self.primary_position,
            "secondary_position": self.secondary_position,
            "height_cm": self.height_cm,
            "weight": self.weight,
            "era": self.era,
            "sticker_family": self.sticker_family,
            "face_id": self.face_id,
            "graphic_id": self.graphic_id,
            "portrait_id": self.portrait_id,
            "appearance_id": self.appearance_id,
            "row_sha256": self.row_sha256,
            "available_domains": sorted(self.domain_values),
            "domain_values": self.domain_values,
        }


@dataclass(frozen=True)
class RosterMatch:
    status: str
    score: int
    explanation: str
    matching_features: tuple[dict[str, Any], ...]
    source: RosterPlayer | None
    alternatives: tuple[dict[str, Any], ...] = ()
    matcher_version: str = MATCHER_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "explanation": self.explanation,
            "matching_features": list(self.matching_features),
            "source": None if self.source is None else self.source.to_dict(),
            "alternatives": list(self.alternatives),
            "matcher_version": self.matcher_version,
        }


def roster_player_from_catalog_row(row: Mapping[str, Any]) -> RosterPlayer:
    raw = dict(row)
    roster_id = str(row.get("roster_id"))
    appearance = row.get("appearance_value_0x200")
    sticker = row.get("sticker_family_0x178") or {}
    canonical_row = {
        "master_record_index": row.get("master_record_index"),
        "roster_id": row.get("roster_id"),
        "first_name": row.get("first_name"),
        "last_name": row.get("last_name"),
        "full_name": row.get("full_name"),
        "native_overall": row.get("native_overall"),
        "primary_position": row.get("primary_position"),
        "secondary_position": row.get("secondary_position"),
        "height_cm": row.get("height_cm"),
        "appearance_value_0x200": appearance,
        "sticker_family_0x178": sticker,
    }
    return RosterPlayer(
        source_roster_identifier=roster_id,
        source_team=row.get("team"),
        source_slot=row.get("master_record_index"),
        source_roster_overall=row.get("native_overall"),
        source_player_id=str(row.get("underlying_player_value_0xd6")) if row.get("underlying_player_value_0xd6") is not None else None,
        full_name=str(row.get("full_name") or ""),
        first_name=row.get("first_name"),
        last_name=row.get("last_name"),
        primary_position=_position_label(row.get("primary_position")),
        secondary_position=_position_label(row.get("secondary_position")),
        height_cm=float(row["height_cm"]) if row.get("height_cm") is not None else None,
        weight=row.get("weight"),
        era=row.get("era"),
        sticker_family=sticker.get("crc32_preimage") if isinstance(sticker, Mapping) else None,
        face_id=row.get("face_id"),
        graphic_id=row.get("graphic_id"),
        portrait_id=row.get("portrait_id"),
        appearance_id=appearance,
        row_sha256=sha256_json(canonical_row),
        domain_values=dict(row.get("domain_values") or {}),
        raw=raw,
    )


def load_native_roster_catalog(path: Path) -> list[RosterPlayer]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("native roster catalog does not contain a rows list")
    result = [roster_player_from_catalog_row(row) for row in rows]
    identifiers = [player.source_roster_identifier for player in result]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("native roster identifiers are not unique")
    return result


def _card_context(card: CanonicalCard) -> dict[str, Any]:
    identity = card.identity.values
    context = " ".join(
        str(identity.get(key) or "")
        for key in ("theme", "collection", "promotion", "represented_season")
    ).casefold()
    if any(token in context for token in ("historic", "throwback", "classic", "all-time", "all time")):
        era = "HISTORIC"
    elif any(token in context for token in ("moment", "current", "league")):
        era = "CURRENT"
    else:
        era = None
    return {
        "name": identity.get("name"),
        "team": identity.get("team") or identity.get("franchise"),
        "overall": identity.get("overall"),
        "primary_position": _position_label(identity.get("primary_position")),
        "secondary_position": _position_label(identity.get("secondary_position")),
        "height_cm": identity.get("height_cm"),
        "era": era,
    }


def _score_candidate(
    context: Mapping[str, Any],
    player: RosterPlayer,
    aliases: Mapping[str, str],
) -> tuple[int, list[dict[str, Any]]]:
    card_name = normalize_name(str(context.get("name") or ""))
    roster_name = normalize_name(player.full_name)
    canonical_card_name = aliases.get(card_name, card_name)
    canonical_roster_name = aliases.get(roster_name, roster_name)
    features: list[dict[str, Any]] = []
    if not canonical_card_name or canonical_card_name != canonical_roster_name:
        return -1000, [{"feature": "name", "matched": False, "score": -1000}]
    alias_used = card_name != roster_name
    score = 55 if alias_used else 60
    features.append({"feature": "name", "matched": True, "alias_used": alias_used, "score": score})

    card_team = normalize_name(str(context.get("team") or ""))
    roster_team = normalize_name(player.source_team or "")
    if card_team and roster_team:
        delta = 15 if card_team == roster_team else -5
        score += delta
        features.append({"feature": "team", "matched": card_team == roster_team, "score": delta})

    card_positions = {position for position in (
        context.get("primary_position"), context.get("secondary_position")
    ) if position}
    roster_positions = {position for position in (
        player.primary_position, player.secondary_position
    ) if position}
    if card_positions and roster_positions:
        overlap = bool(card_positions & roster_positions)
        delta = 10 if overlap else -8
        score += delta
        features.append({"feature": "position", "matched": overlap, "score": delta})

    card_height = context.get("height_cm")
    if card_height is not None and player.height_cm is not None:
        distance = abs(float(card_height) - player.height_cm)
        delta = 8 if distance <= 2.6 else 4 if distance <= 5.1 else -5
        score += delta
        features.append({"feature": "height_cm", "distance": round(distance, 3), "score": delta})

    card_era = context.get("era")
    roster_era = (player.era or player.sticker_family or "").upper()
    if card_era and roster_era:
        matched = card_era in roster_era
        delta = 10 if matched else -6
        score += delta
        features.append({"feature": "era", "matched": matched, "score": delta})

    card_overall = context.get("overall")
    if isinstance(card_overall, int) and isinstance(player.source_roster_overall, int):
        distance = abs(card_overall - player.source_roster_overall)
        delta = 3 if distance <= 2 else 1 if distance <= 5 else 0
        score += delta
        features.append({
            "feature": "overall_weak_support", "distance": distance, "score": delta,
        })
    return score, features


def match_card_to_roster(
    card: CanonicalCard,
    players: Iterable[RosterPlayer],
    *,
    aliases: Mapping[str, str] | None = None,
    minimum_score: int = 70,
    ambiguity_margin: int = 4,
    manual_roster_id: str | None = None,
) -> RosterMatch:
    normalized_aliases = {
        normalize_name(key): normalize_name(value) for key, value in (aliases or {}).items()
    }
    context = _card_context(card)
    ranked = []
    for player in players:
        score, features = _score_candidate(context, player, normalized_aliases)
        if score > -1000:
            ranked.append((score, player.source_roster_identifier, player, features))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    if manual_roster_id is not None:
        selected = next((item for item in ranked if item[1] == str(manual_roster_id)), None)
        if selected is None:
            return RosterMatch(
                status=MatchStatus.UNMATCHED,
                score=0,
                explanation=f"Manual roster identifier {manual_roster_id} is not a name-compatible source.",
                matching_features=(),
                source=None,
            )
        score, _identifier, player, features = selected
        return RosterMatch(
            status=MatchStatus.MANUAL_VERIFIED,
            score=score,
            explanation="Human-selected NBA 2K17 roster source; domain provenance remains separate.",
            matching_features=tuple(features),
            source=player,
        )
    if not ranked or ranked[0][0] < minimum_score:
        return RosterMatch(
            status=MatchStatus.UNMATCHED,
            score=0 if not ranked else ranked[0][0],
            explanation="No NBA 2K17 roster row met the automatic confidence threshold.",
            matching_features=() if not ranked else tuple(ranked[0][3]),
            source=None,
            alternatives=tuple(
                {"roster_id": item[1], "name": item[2].full_name, "score": item[0]}
                for item in ranked[:5]
            ),
        )
    best = ranked[0]
    alternatives = tuple(
        {"roster_id": item[1], "name": item[2].full_name, "score": item[0]}
        for item in ranked[1:6]
    )
    if len(ranked) > 1 and ranked[1][0] >= best[0] - ambiguity_margin:
        return RosterMatch(
            status=MatchStatus.AMBIGUOUS,
            score=best[0],
            explanation=(
                f"Top NBA 2K17 roster candidates are within {ambiguity_margin} points; "
                "automatic selection is rejected."
            ),
            matching_features=tuple(best[3]),
            source=None,
            alternatives=tuple(
                {"roster_id": item[1], "name": item[2].full_name, "score": item[0]}
                for item in ranked[:6]
            ),
        )
    status = (
        MatchStatus.EXACT_VERIFIED
        if best[0] >= 96 and len(ranked) == 1
        else MatchStatus.HIGH_CONFIDENCE
    )
    return RosterMatch(
        status=status,
        score=best[0],
        explanation=(
            "Unique NBA 2K17 roster candidate selected from name, context, positions, height, "
            "era, and weak OVR support. Each inherited domain must still exist in the source row."
        ),
        matching_features=tuple(best[3]),
        source=best[2],
        alternatives=alternatives,
    )


def apply_roster_match(card: CanonicalCard, match: RosterMatch) -> None:
    payload = match.to_dict()
    card.roster_matches["overall"] = payload
    if match.source is None:
        team = str(card.identity.values.get("team") or "").strip().casefold()
        if match.status == MatchStatus.UNMATCHED and team == "2kmtcentral":
            classification = "MYTEAM_EXCLUSIVE_NO_NATIVE_SOURCE"
        elif match.status == MatchStatus.AMBIGUOUS:
            classification = "NO_INHERITANCE_AMBIGUOUS_MATCH"
        else:
            classification = "NO_NATIVE_SOURCE_UNMATCHED"
        card.roster_matches["classification"] = classification
        card.roster_matches["inheritance_policy"] = "NO_LIVE_ROSTER_FIELDS_INHERITED"
        for domain in INHERITABLE_DOMAINS:
            card.roster_matches[f"{domain}_source_player"] = None
        return
    card.roster_matches["classification"] = "NATIVE_ROSTER_TEMPLATE_ASSIGNED"
    card.roster_matches["inheritance_policy"] = "FILL_MISSING_FIELDS_ONLY"
    for domain in INHERITABLE_DOMAINS:
        card.roster_matches[f"{domain}_source_player"] = {
            "roster_id": match.source.source_roster_identifier,
            "row_sha256": match.source.row_sha256,
            "domain_available": domain in match.source.domain_values,
        }
    for domain, values in match.source.domain_values.items():
        if domain not in {"signatures", "hot_zones", "gear", "gameplay_metadata"}:
            continue
        section = getattr(card, domain)
        for key, value in values.items():
            if section.values.get(key) is None or section.statuses.get(key) == ValueStatus.MISSING:
                section.set(key, value, status=ValueStatus.LIVE_ROSTER_DERIVED)
        if values:
            section.provenance.append({
                "source_site": "nba2k17_roster",
                "source_roster_identifier": match.source.source_roster_identifier,
                "source_row_sha256": match.source.row_sha256,
                "match_status": match.status,
                "domain": domain,
            })
