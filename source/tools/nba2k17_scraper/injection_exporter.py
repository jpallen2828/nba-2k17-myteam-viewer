from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from .models import (
    CanonicalCard,
    INJECTION_SCHEMA,
    MatchStatus,
    ValueStatus,
    sha256_json,
)


WEB_OVERRIDE_STATUSES = {
    ValueStatus.WEB_PRIMARY_2KMTC,
    ValueStatus.WEB_FALLBACK_MTDB,
    ValueStatus.WEB_CARD_SPECIFIC,
    ValueStatus.MANUAL_VERIFIED,
    ValueStatus.CUSTOM_AUTHORED,
}
OVERRIDE_SECTIONS = ("identity", "attributes", "tendencies", "badges", "hot_zones", "signatures", "gear", "gameplay_metadata")
# These remain separately labelled evidence, not newly verified observations.
DERIVED_OVERRIDE_STATUSES = {
    "tendencies": {"LIVE_ROSTER_DERIVED", "ESTIMATED_ATTRIBUTES_BADGES", "REPORTED_ARTICLE_OBSERVATION", "USER_REQUESTED_ESTIMATE"},
    "identity": {"USER_CORRECTED", "USER_CONFIRMED", "USER_VERIFIED", "VISUALLY_VERIFIED_CARD_ART"},
    "badges": {"USER_REQUESTED_ESTIMATE", "ESTIMATED_ATTRIBUTES_BADGES"},
    "gameplay_metadata": {"CONFIRMED_PLAYER_PROFILE"},
    # User-authorized reconstruction values remain card edits when a native
    # reusable source becomes available later (George Johnson, for example).
    "signatures": {"TRANSLATED_REFERENCE", "ESTIMATED"},
}
APP_IDENTITY_KEYS = {"team", "franchise", "tier", "theme", "collection", "promotion", "card_year",
                     "represented_season", "source_card_id", "source_card_slug"}
CALCULATED_KEYS = {"overall", "offense", "defense", "offensive_overall", "defensive_overall"}
IGNORED_AGE_KEYS = {"age", "birth_year", "birth_month", "birth_day", "birth_date", "date_of_birth"}


def field_role(section: str, key: str) -> str:
    if section == 'identity' and key in IGNORED_AGE_KEYS:
        return 'ignored_by_user_policy'
    if key.startswith("source_") or (section == "identity" and key in APP_IDENTITY_KEYS) or key.startswith("dynamic_duo_"):
        return "app_metadata"
    if key in CALCULATED_KEYS:
        return "calculated_outputs"
    return "player_data"


def _exportable(section, key, value, status):
    return (value is not None and field_role(section, key) == "player_data"
            and (status in (WEB_OVERRIDE_STATUSES | DERIVED_OVERRIDE_STATUSES.get(section, set()))
                 or (section == 'gameplay_metadata' and key == 'face_id' and status == 'USER_SUPPLIED_FACE_ID')
                 or (section == 'gameplay_metadata' and key == 'portrait_id' and status == 'USER_CONFIRMED_FACE_PORTRAIT_RULE')
                 or (section == 'gameplay_metadata' and key in ('face_id','portrait_id') and status == ValueStatus.LIVE_ROSTER_DERIVED)))
CUSTOM_AUDIT_FIELDS = (
    "author", "date", "reason", "reference_player_or_rationale",
    "validation_result", "notes",
)

_CROSSWALK_SECTION_ALIASES = {
    "gameplay_badges": "badges",
    "personality_badges": "badges",
    "card_metadata": "identity",
    "appearance": "identity",
    "player_template_metadata": "gameplay_metadata",
}

_CROSSWALK_KEY_ALIASES = {
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


def _crosswalk_fields(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return []
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = payload.get("fields")
    if not isinstance(fields, list):
        raise ValueError("NBA 2K17 crosswalk must contain a fields list")
    return fields


def _template_match(card: CanonicalCard) -> dict[str, Any] | None:
    overall = card.roster_matches.get("overall") or {}
    if overall.get("status") not in {
        MatchStatus.EXACT_VERIFIED,
        MatchStatus.HIGH_CONFIDENCE,
        MatchStatus.MANUAL_VERIFIED,
    }:
        return None
    source = overall.get("source")
    return source if isinstance(source, dict) else None


def _section_overrides(card: CanonicalCard) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section_name in ("identity", "attributes", "tendencies", "badges", "hot_zones", "signatures", "gear", "gameplay_metadata"):
        section = getattr(card, section_name)
        values = {
            key: value
            for key, value in section.values.items()
            if _exportable(section_name, key, value, section.statuses.get(key))
        }
        if values:
            result[section_name] = dict(sorted(values.items()))
    return result


def resolve_player_values(profile: Mapping[str, Any], template_defaults: Mapping[str, Mapping[str, Any]],
                          *, preserve_durability: bool = False) -> dict[str, Any]:
    """Resolve values only. This does not establish offsets or authorize writes.

    The caller supplies decoded, verified player defaults, never destination
    data or source addresses. Null means missing; 0, False and 'NONE' are values.
    """
    resolved, origins, excluded, ignored = {}, {}, [], set()
    for layer, sections in (("template", template_defaults), ("card", profile["card_specific_overrides"])):
        for section, fields in sections.items():
            if section not in OVERRIDE_SECTIONS:
                raise ValueError(f"Not a player data section: {section}")
            for key, value in fields.items():
                if field_role(section, key) == 'ignored_by_user_policy':
                    ignored.add(f'{section}.{key}')
                if value is None or field_role(section, key) != "player_data":
                    continue
                field = f"{section}.{key}"
                if preserve_durability and "durability" in key:
                    excluded.append(field)
                    continue
                resolved.setdefault(section, {})[key] = deepcopy(value)
                origins[field] = {"layer": layer}
                if layer == "card":
                    origins[field].update(deepcopy(profile["override_evidence"][section][key]))
    metadata = resolved.get('gameplay_metadata', {})
    if metadata.get('dominant_hand') is not None:
        metadata['dominant_dunk_hand'] = metadata['dominant_hand']
        origins['gameplay_metadata.dominant_dunk_hand'] = {'layer': 'user_policy', 'matches': 'gameplay_metadata.dominant_hand'}
    return {"values": resolved, "origins": origins, "preserved_destination_fields": sorted(set(excluded)|ignored),
            "ignored_by_user_policy": sorted(ignored),
            "memory_write_authorized": False}


def _inherited_fields(card: CanonicalCard) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for section_name in ("hot_zones", "signatures", "gear", "gameplay_metadata"):
        section = getattr(card, section_name)
        values = {
            key: value
            for key, value in section.values.items()
            if value is not None and section.statuses.get(key) == ValueStatus.LIVE_ROSTER_DERIVED
        }
        if values:
            result[section_name] = dict(sorted(values.items()))
    return result


def _custom_audit_issues(card: CanonicalCard) -> list[str]:
    issues = []
    custom = card.raw_source.get("custom_authoring") or {}
    custom_fields = []
    for section_name in ("identity", "attributes", "tendencies", "badges", "hot_zones", "signatures", "gear", "gameplay_metadata"):
        section = getattr(card, section_name)
        custom_fields.extend(
            f"{section_name}.{key}"
            for key, status in section.statuses.items()
            if status == ValueStatus.CUSTOM_AUTHORED
        )
    if not custom_fields:
        return issues
    for key in CUSTOM_AUDIT_FIELDS:
        if not custom.get(key):
            issues.append(f"custom_authoring.{key}")
    if any(field.startswith("signatures.") for field in custom_fields) and not custom.get("exact_nba2k17_animation_ids"):
        issues.append("custom_authoring.exact_nba2k17_animation_ids")
    return issues


def _validate_signature_options(
    card: CanonicalCard,
    verified_options: Mapping[str, set[int] | list[int] | tuple[int, ...]] | None,
) -> list[str]:
    signature_values = {
        key: value for key, value in card.signatures.values.items() if value is not None
    }
    if not signature_values:
        return ["signatures:missing"]
    if not verified_options:
        return ["signatures:verified_nba2k17_option_table_missing"]
    issues = []
    for key, value in signature_values.items():
        allowed = verified_options.get(key)
        if allowed is None:
            issues.append(f"signatures.{key}:no_verified_2k17_option_domain")
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value not in set(allowed):
            issues.append(f"signatures.{key}:invalid_2k17_option_id:{value!r}")
    return issues


def build_injection_profile(
    card: CanonicalCard,
    *,
    crosswalk_path: Path | None = None,
    verified_signature_options: Mapping[str, set[int] | list[int] | tuple[int, ...]] | None = None,
) -> dict[str, Any]:
    template = _template_match(card)
    missing: list[str] = []
    unresolved_mappings: list[str] = []
    fields = _crosswalk_fields(crosswalk_path)
    for entry in fields:
        if not entry.get("required_for_injection"):
            continue
        raw_section_name = entry.get("domain") or entry.get("section")
        qualified_key = entry.get("nba2k17_canonical_key")
        section_name = _CROSSWALK_SECTION_ALIASES.get(
            str(raw_section_name), str(raw_section_name)
        )
        tail = str(qualified_key or "").rsplit(".", 1)[-1]
        key = _CROSSWALK_KEY_ALIASES.get(tail, tail)
        if field_role(section_name, key) == 'ignored_by_user_policy':
            continue
        if not raw_section_name or not qualified_key or not hasattr(card, section_name):
            unresolved_mappings.append(str(qualified_key or entry.get("nba2k16_canonical_key") or "unknown"))
            continue
        section = getattr(card, section_name)
        status = section.statuses.get(key)
        value = section.values.get(key)
        if status in {ValueStatus.MISSING, ValueStatus.CONFLICT, None} or value is None:
            missing.append(f"{section_name}.{key}")
        if str(entry.get("mapping_status") or "UNKNOWN") not in {"VERIFIED", "NOT_APPLICABLE"}:
            unresolved_mappings.append(f"{section_name}.{key}")
        destination = entry.get("nba2k17_injection_destination")
        if destination is None:
            destination = entry.get("nba2k17_roster_injection_destination")
        destination_verified = bool(destination)
        if isinstance(destination, Mapping):
            destination_verified = (
                str(destination.get("status") or "").upper() == "VERIFIED"
                and bool(destination.get("target") or destination.get("offset_hex"))
            )
        if (
            str(entry.get("mapping_status") or "UNKNOWN") == "VERIFIED"
            and not destination_verified
        ):
            unresolved_mappings.append(f"{section_name}.{key}:injection_destination")

    if template is None:
        missing.append("roster_template_match")
    signature_issues = _validate_signature_options(card, verified_signature_options)
    reusable_template=card.raw_source.get('live_roster_template') or {}
    signature_summary=reusable_template.get('signature_mapping_summary',{})
    if (signature_issues==['signatures:missing'] and signature_summary.get('signature_fields')==44
            and len(reusable_template.get('defaults',{}).get('signatures',{}))==44):
        signature_issues=['signatures:inferred_layout_visual_validation_pending']
    custom_audit_issues = _custom_audit_issues(card)
    conflicts = [
        conflict for conflict in card.conflicts
        if conflict.get("resolution") not in {"PRIMARY_RETAINED_VALIDATION_CONFLICT"}
    ]
    source_hashes = sorted({
        item.get("snapshot_sha256")
        for item in card.provenance
        if item.get("snapshot_sha256")
    })
    if template and template.get("row_sha256"):
        source_hashes.append(template["row_sha256"])
        source_hashes = sorted(set(source_hashes))
    blockers = sorted(set(
        missing
        + [f"unresolved_mapping:{item}" for item in unresolved_mappings]
        + signature_issues
        + [f"custom_audit:{item}" for item in custom_audit_issues]
        + (["unresolved_conflicts"] if conflicts else [])
    ))
    overrides = _section_overrides(card)
    evidence = {section: {key: {"status": str(getattr(card, section).statuses[key]),
                               "raw_label": getattr(card, section).raw_labels.get(key),
                               "provenance": deepcopy(getattr(card, section).provenance)}
                          for key in fields} for section, fields in overrides.items()}
    excluded = {f"{section}.{key}": {"value": value, "status": str(getattr(card, section).statuses.get(key)),
                                    "role": field_role(section, key)}
                for section in OVERRIDE_SECTIONS for key, value in getattr(card, section).values.items()
                if key not in overrides.get(section, {})}
    profile = {
        "schema": INJECTION_SCHEMA,
        "canonical_card_uuid": card.canonical_card_uuid,
        "source_card_identity": {
            "source_site": card.source.get("primary_site"),
            "source_card_id": card.source.get("source_card_id"),
            "source_card_slug": card.source.get("source_card_slug"),
            "name": card.identity.values.get("name"),
            "overall": card.identity.values.get("overall"),
            "tier": card.identity.values.get("tier"),
        },
        "template_source": template,
        "reusable_roster_template": deepcopy(card.raw_source.get('live_roster_template')),
        "signature_mapping_summary": deepcopy(signature_summary),
        "gear_mapping_summary": deepcopy(reusable_template.get('gear_mapping_summary',{})),
        "card_specific_overrides": overrides,
        "override_evidence": evidence,
        "excluded_card_values": excluded,
        "card_provenance": deepcopy(card.provenance),
        "precedence": ["preserve_destination_structure", "verified_player_template_defaults", "explicit_card_overrides"],
        "age_policy": "User instruction: disregard age during injection; retain destination birth-date fields and keep card age as archived data only.",
        "missing_value_policy": "Explicit zero, false and NONE override template defaults. The app treats unknown and unlisted badges as NONE by user policy. Other missing fields retain their existing resolution rules.",
        "inherited_fields": _inherited_fields(card),
        "custom_fields": sorted(
            f"{section_name}.{key}"
            for section_name in ("identity", "attributes", "tendencies", "badges", "hot_zones", "signatures", "gear", "gameplay_metadata")
            for key, status in getattr(card, section_name).statuses.items()
            if status == ValueStatus.CUSTOM_AUTHORED
        ),
        "source_hashes": source_hashes,
        "required_fields_resolved": not missing,
        "verified_nba2k17_signature_ids": not signature_issues,
        "unresolved_mappings": sorted(set(unresolved_mappings)),
        "missing_required_fields": sorted(set(missing)),
        "blockers": blockers,
        "safe_to_inject": not blockers,
        "write_behavior": "DATA_ONLY_NO_GAME_MEMORY_WRITE",
    }
    profile["profile_sha256"] = sha256_json(profile)
    card.injection = profile
    return profile
