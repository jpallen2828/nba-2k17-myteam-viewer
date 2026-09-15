from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .constants import BADGE_LEVEL_NAMES, CONFIDENCE_LEVELS, TIER_NAMES


class Confidence(str, Enum):
    UNKNOWN = "UNKNOWN"
    CANDIDATE = "CANDIDATE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERIFIED = "VERIFIED"


class BadgeLevel(str, Enum):
    NONE = "NONE"
    BRONZE = "BRONZE"
    SILVER = "SILVER"
    GOLD = "GOLD"
    HALL_OF_FAME = "HALL_OF_FAME"
    UNKNOWN = "UNKNOWN"


def unknown_enum(prefix: str, raw: Any) -> str:
    value = str(raw).strip()
    safe = "".join(char if char.isalnum() else "_" for char in value).strip("_")
    return f"UNKNOWN_{prefix}_{safe or 'EMPTY'}".upper()


def tier_from_raw(raw: Any, verified_mapping: dict[str, str] | None = None) -> str:
    key = str(raw)
    mapped = (verified_mapping or {}).get(key)
    if mapped and mapped.upper() in TIER_NAMES:
        return mapped.upper()
    return unknown_enum("TIER", raw)


def badge_level_from_raw(raw: Any, verified_mapping: dict[str, str] | None = None) -> str:
    key = str(raw)
    mapped = (verified_mapping or {}).get(key)
    if mapped and mapped.upper() in BADGE_LEVEL_NAMES:
        return mapped.upper()
    return unknown_enum("BADGE_LEVEL", raw)


@dataclass(slots=True)
class MemoryRegion:
    base_address: int
    region_size: int
    allocation_base: int
    allocation_protect: int
    state: int
    protect: int
    type: int
    category: str = "unknown"
    module_name: str | None = None

    @property
    def end_address(self) -> int:
        return self.base_address + self.region_size

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["base_address_hex"] = f"0x{self.base_address:X}"
        data["end_address_hex"] = f"0x{self.end_address:X}"
        data["allocation_base_hex"] = f"0x{self.allocation_base:X}"
        return data


@dataclass(slots=True)
class ModuleInfo:
    name: str
    path: str
    base_address: int
    size: int

    @property
    def end_address(self) -> int:
        return self.base_address + self.size

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["base_address_hex"] = f"0x{self.base_address:X}"
        data["end_address_hex"] = f"0x{self.end_address:X}"
        return data


@dataclass(slots=True)
class ProcessInfo:
    pid: int
    name: str
    executable_path: str
    architecture: str
    modules: list[ModuleInfo] = field(default_factory=list)

    @property
    def main_module(self) -> ModuleInfo:
        for module in self.modules:
            if module.name.casefold() == self.name.casefold():
                return module
        if self.modules:
            return self.modules[0]
        raise RuntimeError("Process has no enumerated modules")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["modules"] = [module.to_dict() for module in self.modules]
        return data


@dataclass(slots=True)
class FileFingerprint:
    path: str
    size: int
    modified: str
    sha256: str
    sha1: str
    file_version: str | None = None
    pe_machine: str | None = None
    pe_timestamp: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PlayerDefinition:
    native_player_id: int | str | None = None
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BadgeDefinition:
    badge_id: int | str | None
    badge_name: str | None
    badge_level_raw: int | str | None
    badge_level: str
    confidence: Confidence = Confidence.UNKNOWN


@dataclass(slots=True)
class CardDefinition:
    native_card_id: int | str | None = None
    native_player_id: int | str | None = None
    display_name: str | None = None
    overall: int | None = None
    tier_raw: int | str | None = None
    tier: str | None = None
    collection_id: int | str | None = None
    collection_name: str | None = None
    theme_id: int | str | None = None
    theme_name: str | None = None
    team_id: int | str | None = None
    team_name: str | None = None
    primary_position: str | None = None
    secondary_position: str | None = None
    jersey_number: int | str | None = None
    height: float | None = None
    weight: float | None = None
    wingspan: float | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    badges: list[BadgeDefinition] = field(default_factory=list)
    unknown_fields: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["badges"] = [asdict(badge) for badge in self.badges]
        return data


@dataclass(slots=True)
class FieldEvidence:
    structure: str
    offset: int
    data_type: str
    proposed_name: str
    confidence: Confidence
    evidence: list[str]
    executable_sha256: str
    recorded_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())

    def __post_init__(self) -> None:
        if self.confidence.value not in CONFIDENCE_LEVELS:
            raise ValueError(f"Unknown confidence: {self.confidence}")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["offset_hex"] = f"0x{self.offset:X}"
        return data
