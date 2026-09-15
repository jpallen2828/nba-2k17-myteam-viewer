from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import json
from typing import Any, Iterable
import uuid


CARD_SCHEMA = "nba2k17.myteam-card/v1"
INJECTION_SCHEMA = "nba2k17.injection-profile/v1"
CARD_UUID_NAMESPACE = uuid.UUID("41465d85-8d79-5e30-a0bb-915881478df2")

# Verified NBA 2K17 gameplay-badge display order. This is deliberately not a
# count or closed list of badge names: 2K17 may expose more badges than 2K16.
GAMEPLAY_BADGE_LEVELS = ("BRONZE", "SILVER", "GOLD", "HALL_OF_FAME")
GAMEPLAY_BADGE_LEVEL_RANK = {
    level: rank for rank, level in enumerate(GAMEPLAY_BADGE_LEVELS, start=1)
}


class SourceSite(StrEnum):
    TWO_K_MT_CENTRAL = "2kmtcentral"
    MTDB = "mtdb"
    NBA2K17_ROSTER = "nba2k17_roster"
    MANUAL = "manual"


class ValueStatus(StrEnum):
    WEB_PRIMARY_2KMTC = "WEB_PRIMARY_2KMTC"
    WEB_FALLBACK_MTDB = "WEB_FALLBACK_MTDB"
    WEB_CARD_SPECIFIC = "WEB_CARD_SPECIFIC"
    LIVE_ROSTER_DERIVED = "LIVE_ROSTER_DERIVED"
    MANUAL_VERIFIED = "MANUAL_VERIFIED"
    CUSTOM_AUTHORED = "CUSTOM_AUTHORED"
    CONFLICT = "CONFLICT"
    MISSING = "MISSING"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class MatchStatus(StrEnum):
    EXACT_VERIFIED = "EXACT_VERIFIED"
    HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
    MANUAL_VERIFIED = "MANUAL_VERIFIED"
    AMBIGUOUS = "AMBIGUOUS"
    UNMATCHED = "UNMATCHED"


def stable_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(stable_json_bytes(value)).hexdigest()


def canonical_card_uuid(source_site: str | SourceSite, source_card_id: str | int) -> str:
    site = SourceSite(str(source_site)).value
    card_id = str(source_card_id).strip()
    if not card_id:
        raise ValueError("source_card_id is required for canonical identity")
    return str(uuid.uuid5(CARD_UUID_NAMESPACE, f"{site}:{card_id}"))


@dataclass(frozen=True)
class Provenance:
    source_site: str
    source_url: str | None
    source_card_id: str | None
    snapshot_sha256: str | None = None
    captured_at: str | None = None
    archive_timestamp: str | None = None
    parser_version: str | None = None
    source_label: str | None = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass
class DataSection:
    values: dict[str, Any] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    raw_labels: dict[str, str] = field(default_factory=dict)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)

    def set(
        self,
        key: str,
        value: Any,
        *,
        status: str | ValueStatus,
        raw_label: str | None = None,
    ) -> None:
        normalized_status = ValueStatus(str(status)).value
        self.values[key] = value
        self.statuses[key] = normalized_status
        if raw_label:
            self.raw_labels[key] = raw_label
        if value is None and normalized_status == ValueStatus.MISSING:
            if key not in self.missing_fields:
                self.missing_fields.append(key)
        elif key in self.missing_fields:
            self.missing_fields.remove(key)

    def declare_missing(self, keys: Iterable[str]) -> None:
        for key in keys:
            if key not in self.values:
                self.set(key, None, status=ValueStatus.MISSING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "values": dict(sorted(self.values.items())),
            "statuses": dict(sorted(self.statuses.items())),
            "raw_labels": dict(sorted(self.raw_labels.items())),
            "provenance": self.provenance,
            "missing_fields": sorted(set(self.missing_fields)),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "DataSection":
        value = value or {}
        return cls(
            values=dict(value.get("values") or {}),
            statuses=dict(value.get("statuses") or {}),
            raw_labels=dict(value.get("raw_labels") or {}),
            provenance=list(value.get("provenance") or []),
            missing_fields=list(value.get("missing_fields") or []),
        )


IDENTITY_FIELDS = (
    "name",
    "first_name",
    "last_name",
    "overall",
    "tier",
    "team",
    "franchise",
    "theme",
    "collection",
    "promotion",
    "represented_season",
    "card_year",
    "primary_position",
    "secondary_position",
    "height",
    "height_cm",
    "weight",
    "age",
    "college",
    "country",
    "origin",
    "jersey_number",
    "source_card_id",
    "source_card_slug",
)

CARD_ART_FIELDS = (
    "card_image_url",
    "card_image_path",
    "card_image_sha256",
    "portrait_url",
    "portrait_path",
    "portrait_sha256",
    "cutout_url",
    "cutout_path",
    "cutout_sha256",
)

GAMEPLAY_METADATA_FIELDS = (
    "dominant_hand",
    "dominant_dunk_hand",
    "play_initiator",
    "play_type_1",
    "play_type_2",
    "play_type_3",
    "play_type_4",
    "dynamic_duo_partner",
    "dynamic_duo_partner_source_card_id",
    "dynamic_duo_boosts",
    "wingspan",
    "injury_type_1",
    "injury_duration_days_1",
    "injury_type_2",
    "injury_duration_days_2",
    "face_id",
    "portrait_id",
    "graphic_id",
    "appearance_id",
)


@dataclass
class CanonicalCard:
    canonical_card_uuid: str
    identity: DataSection
    source: dict[str, Any]
    card_art: DataSection = field(default_factory=DataSection)
    attributes: DataSection = field(default_factory=DataSection)
    tendencies: DataSection = field(default_factory=DataSection)
    badges: DataSection = field(default_factory=DataSection)
    hot_zones: DataSection = field(default_factory=DataSection)
    signatures: DataSection = field(default_factory=DataSection)
    gear: DataSection = field(default_factory=DataSection)
    gameplay_metadata: DataSection = field(default_factory=DataSection)
    roster_matches: dict[str, Any] = field(default_factory=dict)
    injection: dict[str, Any] = field(default_factory=dict)
    provenance: list[dict[str, Any]] = field(default_factory=list)
    completeness: dict[str, Any] = field(default_factory=dict)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    raw_source: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CARD_SCHEMA,
            "canonical_card_uuid": self.canonical_card_uuid,
            "identity": self.identity.to_dict(),
            "source": self.source,
            "card_art": self.card_art.to_dict(),
            "attributes": self.attributes.to_dict(),
            "tendencies": self.tendencies.to_dict(),
            "badges": self.badges.to_dict(),
            "hot_zones": self.hot_zones.to_dict(),
            "signatures": self.signatures.to_dict(),
            "gear": self.gear.to_dict(),
            "gameplay_metadata": self.gameplay_metadata.to_dict(),
            "roster_matches": self.roster_matches,
            "injection": self.injection,
            "provenance": self.provenance,
            "completeness": self.completeness,
            "conflicts": self.conflicts,
            "raw_source": self.raw_source,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CanonicalCard":
        if value.get("schema") != CARD_SCHEMA:
            raise ValueError(f"unsupported card schema: {value.get('schema')!r}")
        return cls(
            canonical_card_uuid=value["canonical_card_uuid"],
            identity=DataSection.from_dict(value.get("identity")),
            source=dict(value.get("source") or {}),
            card_art=DataSection.from_dict(value.get("card_art")),
            attributes=DataSection.from_dict(value.get("attributes")),
            tendencies=DataSection.from_dict(value.get("tendencies")),
            badges=DataSection.from_dict(value.get("badges")),
            hot_zones=DataSection.from_dict(value.get("hot_zones")),
            signatures=DataSection.from_dict(value.get("signatures")),
            gear=DataSection.from_dict(value.get("gear")),
            gameplay_metadata=DataSection.from_dict(value.get("gameplay_metadata")),
            roster_matches=dict(value.get("roster_matches") or {}),
            injection=dict(value.get("injection") or {}),
            provenance=list(value.get("provenance") or []),
            completeness=dict(value.get("completeness") or {}),
            conflicts=list(value.get("conflicts") or []),
            raw_source=dict(value.get("raw_source") or {}),
        )


def new_canonical_card(
    *,
    source_site: str | SourceSite,
    source_card_id: str | int,
    source_url: str,
    source_card_slug: str | None = None,
) -> CanonicalCard:
    site = SourceSite(str(source_site)).value
    card_id = str(source_card_id)
    identity = DataSection()
    identity.declare_missing(IDENTITY_FIELDS)
    identity.set(
        "source_card_id",
        card_id,
        status=(
            ValueStatus.WEB_PRIMARY_2KMTC
            if site == SourceSite.TWO_K_MT_CENTRAL
            else ValueStatus.WEB_FALLBACK_MTDB
        ),
    )
    identity.set(
        "source_card_slug",
        source_card_slug,
        status=(
            ValueStatus.MISSING if source_card_slug is None else
            ValueStatus.WEB_PRIMARY_2KMTC if site == SourceSite.TWO_K_MT_CENTRAL else
            ValueStatus.WEB_FALLBACK_MTDB
        ),
    )
    card = CanonicalCard(
        canonical_card_uuid=canonical_card_uuid(site, card_id),
        identity=identity,
        source={
            "primary_site": site,
            "source_card_id": card_id,
            "source_url": source_url,
            "source_card_slug": source_card_slug,
            "snapshots": [],
        },
    )
    card.card_art.declare_missing(CARD_ART_FIELDS)
    card.gameplay_metadata.declare_missing(GAMEPLAY_METADATA_FIELDS)
    return card
