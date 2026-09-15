from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .models import CanonicalCard, DataSection, GAMEPLAY_BADGE_LEVELS, ValueStatus
from .normalizer import SOURCE_DETAIL_RECORDS_KEY, source_detail_missing


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "severity": self.severity,
            "code": self.code,
            "path": self.path,
            "message": self.message,
        }


SECTION_NAMES = (
    "identity",
    "card_art",
    "attributes",
    "tendencies",
    "badges",
    "hot_zones",
    "signatures",
    "gear",
    "gameplay_metadata",
)

ADEQUACY_IDENTITY_FIELDS = ("name", "overall", "tier")
DISTINGUISHING_IDENTITY_FIELDS = ("theme", "collection", "promotion")


def _present(section: DataSection, key: str) -> bool:
    return section.values.get(key) is not None and section.statuses.get(key) not in {
        ValueStatus.MISSING,
        ValueStatus.CONFLICT,
    }


def is_adequate(card: CanonicalCard) -> tuple[bool, list[str]]:
    missing = [key for key in ADEQUACY_IDENTITY_FIELDS if not _present(card.identity, key)]
    if not any(_present(card.identity, key) for key in DISTINGUISHING_IDENTITY_FIELDS):
        missing.append("theme_or_collection_context")
    if not card.source.get("source_card_id"):
        missing.append("source_card_id")
    if source_detail_missing(card.badges, "badges"):
        missing.append("badge_detail_records")
    if source_detail_missing(card.tendencies, "tendencies"):
        missing.append("tendency_detail_records")
    gameplay_count = sum(
        value is not None
        for section in (card.attributes, card.tendencies, card.badges)
        for key, value in section.values.items()
        if key != SOURCE_DETAIL_RECORDS_KEY
        and not key.startswith("source_count_")
        and section.statuses.get(key) not in {ValueStatus.MISSING, ValueStatus.CONFLICT}
    )
    if gameplay_count < 10:
        missing.append("at_least_10_card_specific_gameplay_values")
    return not missing, missing


def validate_card(card: CanonicalCard) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for section_name in SECTION_NAMES:
        section: DataSection = getattr(card, section_name)
        for key in sorted(section.values):
            path = f"{section_name}.{key}"
            if key not in section.statuses:
                issues.append(ValidationIssue(
                    "ERROR", "MISSING_STATUS", path,
                    "Every stored field must carry an explicit provenance status.",
                ))
                continue
            status = section.statuses[key]
            value = section.values[key]
            if status == ValueStatus.MISSING and value is not None:
                issues.append(ValidationIssue(
                    "ERROR", "MISSING_HAS_VALUE", path,
                    "A field marked MISSING must remain null; defaults cannot conceal absence.",
                ))
            if status == ValueStatus.NOT_APPLICABLE and value is not None:
                issues.append(ValidationIssue(
                    "ERROR", "NOT_APPLICABLE_HAS_VALUE", path,
                    "A field marked NOT_APPLICABLE must be null.",
                ))
            if value is not None and status not in {
                ValueStatus.MISSING,
                ValueStatus.NOT_APPLICABLE,
            } and not section.provenance and status not in {
                ValueStatus.MANUAL_VERIFIED,
                ValueStatus.CUSTOM_AUTHORED,
                ValueStatus.LIVE_ROSTER_DERIVED,
            }:
                issues.append(ValidationIssue(
                    "ERROR", "VALUE_WITHOUT_PROVENANCE", path,
                    "Web-derived values require section provenance.",
                ))

    overall = card.identity.values.get("overall")
    if overall is not None and (isinstance(overall, bool) or not isinstance(overall, int) or not 25 <= overall <= 99):
        issues.append(ValidationIssue(
            "ERROR", "OVERALL_RANGE", "identity.overall",
            "NBA 2K17 OVR must be an integer from 25 through 99.",
        ))
    for section_name in ("attributes", "tendencies"):
        section = getattr(card, section_name)
        for key, value in section.values.items():
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
                issues.append(ValidationIssue(
                    "ERROR", "RATING_RANGE", f"{section_name}.{key}",
                    "Scraped ratings and tendencies must be integers from 0 through 100.",
                ))

    allowed_badge_levels = {
        *GAMEPLAY_BADGE_LEVELS, "PERSONALITY",
        True, False, 0, 1, 2, 3, 4,
    }
    for key, value in card.badges.values.items():
        if (
            key == SOURCE_DETAIL_RECORDS_KEY
            or key.startswith("source_count_")
            or value is None
        ):
            continue
        if value not in allowed_badge_levels:
            issues.append(ValidationIssue(
                "WARNING", "UNKNOWN_BADGE_LEVEL", f"badges.{key}",
                f"Badge level {value!r} is preserved but is not in the verified display enum.",
            ))

    for domain, code, label in (
        ("badges", "MISSING_BADGE_DETAILS", "badge"),
        ("tendencies", "MISSING_TENDENCY_DETAILS", "tendency"),
    ):
        if source_detail_missing(getattr(card, domain), domain):
            issues.append(
                ValidationIssue(
                    "WARNING",
                    code,
                    f"{domain}.{SOURCE_DETAIL_RECORDS_KEY}",
                    f"Source {label} detail records are missing; manual review remains required.",
                )
            )

    adequate, missing = is_adequate(card)
    if not adequate:
        issues.append(ValidationIssue(
            "WARNING", "INADEQUATE_SOURCE_RECORD", "completeness",
            "Record is not adequate as a primary source: " + ", ".join(missing),
        ))
    return issues


def section_coverage(section: DataSection) -> dict[str, Any]:
    total = len(section.values)
    present = sum(
        value is not None
        and section.statuses.get(key) not in {
            ValueStatus.MISSING,
            ValueStatus.CONFLICT,
            ValueStatus.NOT_APPLICABLE,
        }
        for key, value in section.values.items()
    )
    missing = sorted(
        key
        for key, value in section.values.items()
        if value is None and section.statuses.get(key) == ValueStatus.MISSING
    )
    conflict = sorted(
        key for key, status in section.statuses.items() if status == ValueStatus.CONFLICT
    )
    not_applicable = sorted(
        key for key, status in section.statuses.items() if status == ValueStatus.NOT_APPLICABLE
    )
    return {
        "declared": total,
        "present": present,
        "coverage": 1.0 if total == 0 else round(present / total, 6),
        "missing_fields": missing,
        "conflict_fields": conflict,
        "not_applicable_fields": not_applicable,
    }


def update_completeness(card: CanonicalCard) -> dict[str, Any]:
    adequate, inadequate_reasons = is_adequate(card)
    issues = validate_card(card)
    card.completeness = {
        "adequate_primary_record": adequate,
        "inadequate_reasons": inadequate_reasons,
        "sections": {
            name: section_coverage(getattr(card, name)) for name in SECTION_NAMES
        },
        "error_count": sum(issue.severity == "ERROR" for issue in issues),
        "warning_count": sum(issue.severity == "WARNING" for issue in issues),
        "issues": [issue.to_dict() for issue in issues],
    }
    return card.completeness


def assert_explicit_missingness(cards: Iterable[CanonicalCard]) -> None:
    failures = []
    for card in cards:
        for section_name in SECTION_NAMES:
            section = getattr(card, section_name)
            for key, value in section.values.items():
                status = section.statuses.get(key)
                if status is None:
                    failures.append(f"{card.canonical_card_uuid}:{section_name}.{key}:no-status")
                if status in {ValueStatus.MISSING, ValueStatus.NOT_APPLICABLE} and value is not None:
                    failures.append(f"{card.canonical_card_uuid}:{section_name}.{key}:{status}-has-value")
    if failures:
        raise ValueError("silent/defaulted missingness detected: " + "; ".join(failures[:20]))
