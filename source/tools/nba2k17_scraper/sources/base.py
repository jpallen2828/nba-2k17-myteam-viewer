from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
import re
from typing import Any, Iterable, Mapping
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .html_dom import Element, child_text, class_or_id_contains, parse_html


@dataclass(frozen=True, slots=True)
class SourcePage:
    """One saved source document supplied to an adapter."""

    url: str
    html: str
    captured_at: str | None = None
    content_hash: str | None = None
    provenance: str | None = None


@dataclass(frozen=True, slots=True)
class RawLabeledValue:
    """A source label and value, retained before canonical normalization."""

    label: str
    raw_value: str
    value: int | float | str | None
    section: str
    source_url: str
    dynamic_duo_boost: int | None = None


@dataclass(frozen=True, slots=True)
class RawBadge:
    name: str
    level: str | None
    raw_level: str | None
    source_url: str


@dataclass(frozen=True, slots=True)
class RawDuo:
    partner_name: str | None
    partner_source_card_id: str | None
    partner_url: str | None
    boosts: tuple[RawLabeledValue, ...] = ()
    raw_label: str | None = None

    @property
    def partner_source_id(self) -> str | None:
        """Backward-friendly alias for consumers using the shorter name."""

        return self.partner_source_card_id


@dataclass(slots=True)
class RawParsedCard:
    """Neutral source-layer result; deliberately not a canonical 2K schema."""

    source_site: str
    source_url: str | None = None
    source_urls: list[str] = field(default_factory=list)
    source_card_id: str | None = None
    slug: str | None = None
    name: str | None = None
    overall: int | None = None
    tier: str | None = None
    theme: str | None = None
    collection: str | None = None
    represented_season: str | None = None
    card_year: int | None = None
    team: str | None = None
    primary_position: str | None = None
    secondary_position: str | None = None
    height: str | None = None
    weight: int | None = None
    age: int | None = None
    origin: str | None = None
    jersey_number: str | None = None
    card_image_url: str | None = None
    player_image_url: str | None = None
    attributes: list[RawLabeledValue] = field(default_factory=list)
    tendencies: list[RawLabeledValue] = field(default_factory=list)
    badges: list[RawBadge] = field(default_factory=list)
    badge_counts: dict[str, int] = field(default_factory=dict)
    hot_zones: list[RawLabeledValue] = field(default_factory=list)
    dynamic_duo: RawDuo | None = None
    raw_metadata: dict[str, str] = field(default_factory=dict)
    missing_fields: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    page_provenance: list[dict[str, str | None]] = field(default_factory=list)

    def identity_inputs(self) -> dict[str, str | int | None]:
        """Inputs useful for reconciliation, not a claim of canonical identity."""

        return {
            "source_site": self.source_site,
            "source_card_id": self.source_card_id,
            "slug": self.slug,
            "name": self.name,
            "overall": self.overall,
            "tier": self.tier,
            "theme": self.theme,
            "collection": self.collection,
            "represented_season": self.represented_season,
            "card_year": self.card_year,
            "team": self.team,
            "primary_position": self.primary_position,
            "secondary_position": self.secondary_position,
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_dict(self) -> dict[str, Any]:
        """Mapping form expected by the canonical normalizer boundary."""

        return self.as_dict()

    def merge(self, other: "RawParsedCard") -> "RawParsedCard":
        if self.source_site != other.source_site:
            raise ValueError("cannot merge cards parsed by different source adapters")
        if (
            self.source_card_id
            and other.source_card_id
            and self.source_card_id != other.source_card_id
        ):
            raise ValueError("cannot merge pages belonging to different source card IDs")

        scalar_fields = (
            "source_card_id",
            "slug",
            "name",
            "overall",
            "tier",
            "theme",
            "collection",
            "represented_season",
            "card_year",
            "team",
            "primary_position",
            "secondary_position",
            "height",
            "weight",
            "age",
            "origin",
            "jersey_number",
            "card_image_url",
            "player_image_url",
        )
        for name in scalar_fields:
            current = getattr(self, name)
            incoming = getattr(other, name)
            if current is None and incoming is not None:
                setattr(self, name, incoming)
            elif current is not None and incoming is not None and current != incoming:
                warning = f"conflicting {name}: {current!r} versus {incoming!r}"
                if warning not in self.warnings:
                    self.warnings.append(warning)

        if self.source_url is None:
            self.source_url = other.source_url
        self.source_urls = _ordered_unique([*self.source_urls, *other.source_urls])
        self.attributes = _unique_dataclasses([*self.attributes, *other.attributes])
        self.tendencies = _unique_dataclasses([*self.tendencies, *other.tendencies])
        self.badges = _unique_dataclasses([*self.badges, *other.badges])
        self.hot_zones = _unique_dataclasses([*self.hot_zones, *other.hot_zones])
        for level, count in other.badge_counts.items():
            if level not in self.badge_counts:
                self.badge_counts[level] = count
            elif self.badge_counts[level] != count:
                warning = f"conflicting {level} badge count"
                if warning not in self.warnings:
                    self.warnings.append(warning)
        if self.dynamic_duo is None:
            self.dynamic_duo = other.dynamic_duo
        elif other.dynamic_duo is not None and self.dynamic_duo != other.dynamic_duo:
            warning = "conflicting dynamic duo data across source pages"
            if warning not in self.warnings:
                self.warnings.append(warning)
        for label, value in other.raw_metadata.items():
            if label not in self.raw_metadata:
                self.raw_metadata[label] = value
            elif self.raw_metadata[label] != value:
                warning = f"conflicting raw metadata label {label!r}"
                if warning not in self.warnings:
                    self.warnings.append(warning)
        self.warnings = _ordered_unique([*self.warnings, *other.warnings])
        self.page_provenance = _unique_mappings(
            [*self.page_provenance, *other.page_provenance]
        )
        self.refresh_missing_fields()
        return self

    def refresh_missing_fields(self) -> None:
        required = (
            "source_card_id",
            "slug",
            "name",
            "overall",
            "tier",
            "theme",
            "collection",
            "team",
            "primary_position",
            "height",
            "weight",
            "card_image_url",
        )
        self.missing_fields = [name for name in required if getattr(self, name) is None]


class SourceAdapter(ABC):
    site: str

    @abstractmethod
    def discover_card_urls(self, html: str, page_url: str) -> list[str]:
        raise NotImplementedError

    @abstractmethod
    def parse_page(self, page: SourcePage) -> RawParsedCard:
        raise NotImplementedError

    def parse_pages(self, pages: Iterable[SourcePage]) -> RawParsedCard:
        parsed: RawParsedCard | None = None
        for page in pages:
            current = self.parse_page(page)
            parsed = current if parsed is None else parsed.merge(current)
        if parsed is None:
            raise ValueError("at least one source page is required")
        parsed.refresh_missing_fields()
        return parsed


class HtmlCardSourceAdapter(SourceAdapter):
    """Tolerant HTML extraction shared by archived card-database sources."""

    card_path_pattern: re.Pattern[str]
    source_host: str
    title_suffixes: tuple[str, ...] = ()
    metadata_labels: Mapping[str, tuple[str, ...]] = {
        "overall": ("overall", "ovr"),
        "tier": ("tier", "gem", "rarity", "color"),
        "theme": ("theme", "set"),
        "collection": ("collection",),
        "team": ("team", "franchise"),
        "position": ("position", "positions", "pos"),
        "height": ("height",),
        "weight": ("weight",),
        "age": ("age",),
        "origin": ("from", "college", "country", "origin"),
        "jersey_number": ("jersey", "jersey number", "number"),
    }

    def discover_card_urls(self, html: str, page_url: str) -> list[str]:
        root = parse_html(html)
        found: list[str] = []
        seen: set[str] = set()
        for anchor in root.find_all("a"):
            href = (anchor.get("href") or "").strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            resolved = _resolve_source_link(href, page_url, self.source_host)
            match = self.card_path_pattern.search(unquote(resolved))
            if not match:
                continue
            original = _unwrapped_source_url(resolved, self.source_host)
            original = _strip_card_subpage(original)
            dedupe_key = _source_url_dedupe_key(original)
            if dedupe_key not in seen:
                seen.add(dedupe_key)
                found.append(original)
        return found

    def parse_page(self, page: SourcePage) -> RawParsedCard:
        root = parse_html(page.html)
        card_id, slug = self._identity_from_url(page.url)
        raw_metadata = _metadata_pairs(root)
        page_hint = _page_section_hint(page.url, root)
        result = RawParsedCard(
            source_site=self.site,
            source_url=page.url,
            source_urls=[page.url],
            source_card_id=card_id,
            slug=slug,
            raw_metadata=raw_metadata,
        )

        result.name = self._extract_name(root)
        result.overall = _extract_overall(root, raw_metadata, self.metadata_labels)
        result.tier = _extract_scalar(root, raw_metadata, self.metadata_labels, "tier")
        result.theme = _extract_scalar(root, raw_metadata, self.metadata_labels, "theme")
        result.collection = _extract_scalar(
            root, raw_metadata, self.metadata_labels, "collection"
        )
        result.team = _extract_scalar(root, raw_metadata, self.metadata_labels, "team")
        position = _extract_scalar(root, raw_metadata, self.metadata_labels, "position")
        result.primary_position, result.secondary_position = _split_positions(position)
        result.height = _extract_scalar(root, raw_metadata, self.metadata_labels, "height")
        result.weight = parse_int(
            _extract_scalar(root, raw_metadata, self.metadata_labels, "weight")
        )
        result.age = parse_int(
            _extract_scalar(root, raw_metadata, self.metadata_labels, "age")
        )
        result.origin = _extract_scalar(root, raw_metadata, self.metadata_labels, "origin")
        result.jersey_number = _extract_scalar(
            root, raw_metadata, self.metadata_labels, "jersey_number"
        )
        result.card_image_url, result.player_image_url = _extract_images(root, page.url)
        result.attributes = _extract_stats(root, page.url, "attributes", page_hint)
        result.tendencies = _extract_stats(root, page.url, "tendencies", page_hint)
        result.badges = _extract_badges(root, page.url, page_hint)
        result.badge_counts = _extract_badge_counts(root)
        result.hot_zones = _extract_hot_zones(root, page.url)
        result.dynamic_duo = self._extract_duo(root, page.url, raw_metadata)

        title_name, title_overall, title_tier = _title_card_facts(root)
        if title_name and (not result.name or re.fullmatch(r"['’]\d{2}", result.name)):
            result.name = title_name
        result.name, result.represented_season, result.card_year = _split_era_prefix(
            result.name
        )
        # The title describes the selected card. Generic ``overall``/color
        # nodes can appear earlier for edition previews of the same player.
        result.overall = title_overall if title_overall is not None else result.overall
        result.tier = title_tier or result.tier
        result.collection = result.collection or _collection_breadcrumb(root)

        fixture_provenance = _fixture_provenance(root)
        result.page_provenance.append(
            {
                "url": page.url,
                "captured_at": page.captured_at,
                "content_hash": page.content_hash,
                "provenance": page.provenance or fixture_provenance,
            }
        )
        if not card_id:
            result.warnings.append("source card ID was not recoverable from the URL")
        result.refresh_missing_fields()
        return result

    def _identity_from_url(self, url: str) -> tuple[str | None, str | None]:
        match = self.card_path_pattern.search(unquote(url))
        if not match:
            return None, None
        return match.group("id"), match.group("slug").strip("/") or None

    def _extract_name(self, root: Element) -> str | None:
        for attr in ("data-player-name", "data-card-name", "data-name"):
            candidate = root.find_first(predicate=lambda item, key=attr: bool(item.get(key)))
            if candidate:
                return (candidate.get(attr) or "").strip() or None
        for fragment in ("player-name", "card-name", "player_name", "card_name"):
            candidate = root.find_first(
                predicate=lambda item, token=fragment: class_or_id_contains(item, token)
            )
            if candidate and candidate.text_content():
                return candidate.text_content()
        heading = root.find_first("h1")
        if heading:
            strong = heading.find_first("strong")
            strong_text = strong.text_content() if strong is not None else ""
            direct_text = _direct_text(heading)
            if strong_text and re.fullmatch(r"['’]\d{2}", direct_text or ""):
                heading_text = f"{direct_text} {strong_text}"
            else:
                heading_text = direct_text or strong_text or heading.text_content()
            if heading_text:
                return heading_text
        title = root.find_first("title")
        text = title.text_content() if title else ""
        for suffix in self.title_suffixes:
            if text.casefold().endswith(suffix.casefold()):
                text = text[: -len(suffix)].rstrip(" -|")
        return text or None

    def _extract_duo(
        self, root: Element, page_url: str, metadata: Mapping[str, str]
    ) -> RawDuo | None:
        return _extract_dynamic_duo(
            root,
            page_url,
            metadata,
            self.card_path_pattern,
            self.source_host,
        )


def parse_int(raw: str | None, *, signed: bool = False) -> int | None:
    if raw is None:
        return None
    text = raw.strip().replace(",", "")
    if signed:
        import re

        match = re.search(r"[-+]?\d+", text)
    else:
        import re

        match = re.search(r"\d+", text)
    return int(match.group(0)) if match else None


def _visible_row_text(element: Element, *, excluded: Element | None = None) -> str:
    """Return desktop-visible label text while excluding a numeric child."""

    pieces: list[str] = []

    def walk(node: Element) -> None:
        for child in node.children:
            if child is excluded:
                continue
            if isinstance(child, str):
                text = " ".join(child.split())
                if text:
                    pieces.append(text)
                continue
            classes = child.classes
            if any(name.startswith("visible-xs") for name in classes):
                continue
            walk(child)

    walk(element)
    return " ".join(pieces).strip()


def _title_card_facts(root: Element) -> tuple[str | None, int | None, str | None]:
    title = root.find_first("title")
    text = title.text_content() if title is not None else ""
    match = re.search(
        r"^\s*(.+?)\s*\((\d{2})\)\s*-\s*MyTEAM\s+(.+?)\s+Card\b",
        text,
        re.IGNORECASE,
    )
    if match is None:
        return None, None, None
    return _clean_text(match.group(1)), int(match.group(2)), _clean_text(match.group(3))


def _split_era_prefix(name: str | None) -> tuple[str | None, str | None, int | None]:
    if not name:
        return name, None, None
    match = re.match(r"^['’](\d{2})\s+(.+)$", name.strip())
    if match is None:
        return name, None, None
    short_year = int(match.group(1))
    year = 2000 + short_year if short_year <= 17 else 1900 + short_year
    return _clean_text(match.group(2)), str(year), year


def _collection_breadcrumb(root: Element) -> str | None:
    for header in root.find_all("header"):
        candidates: list[str] = []
        for anchor in header.find_all("a"):
            href = anchor.get("href") or ""
            if re.search(r"/17/collections/\d+(?:/|$)", href):
                text = _clean_text(anchor.text_content())
                if text and "{{" not in text:
                    candidates.append(text)
        if candidates:
            return candidates[-1]
    return None


def _extract_badge_counts(root: Element) -> dict[str, int]:
    counts: dict[str, int] = {}
    labels = {
        "bronze": "Bronze",
        "silver": "Silver",
        "gold": "Gold",
        "hof": "Hall of Fame",
    }
    for container in root.find_all(
        predicate=lambda item: class_or_id_contains(item, "skill-badge-count")
    ):
        image = container.find_first("img")
        source = "" if image is None else (image.get("src") or "")
        match = re.search(r"badge_(bronze|silver|gold|hof)_count", source, re.IGNORECASE)
        number = child_text(container, "number")
        count = parse_int(number)
        if match is not None and count is not None:
            counts[labels[match.group(1).casefold()]] = count
    return counts


def _extract_hot_zones(root: Element, page_url: str) -> list[RawLabeledValue]:
    candidates: list[list[int]] = []
    for script in root.find_all("script"):
        for match in re.finditer(
            r"hotZonesValues\s*=\s*\[([\d,\s-]+)\]",
            script.text_content(separator=" "),
            re.IGNORECASE,
        ):
            values = [int(value) for value in re.findall(r"-?\d+", match.group(1))]
            if values:
                candidates.append(values)
    if not candidates:
        return []
    values = max(candidates, key=len)
    return [
        RawLabeledValue(
            label=f"Hot zone {index:02d}",
            raw_value=str(value),
            value=value,
            section="hot_zones",
            source_url=page_url,
        )
        for index, value in enumerate(values, start=1)
    ]


def normalized_badge_level(raw: str | None) -> str | None:
    """Normalize spelling only; retain the untouched value in ``raw_level``."""

    if raw is None:
        return None
    collapsed = " ".join(raw.replace("_", " ").replace("-", " ").split())
    folded = collapsed.casefold()
    aliases = {
        "hof": "Hall of Fame",
        "hall of fame": "Hall of Fame",
        "halloffame": "Hall of Fame",
        "bronze": "Bronze",
        "silver": "Silver",
        "gold": "Gold",
    }
    return aliases.get(folded, collapsed or None)


def first_present(mapping: Mapping[str, str], labels: Iterable[str]) -> str | None:
    folded = {key.casefold().strip().rstrip(":"): value for key, value in mapping.items()}
    for label in labels:
        value = folded.get(label.casefold().strip().rstrip(":"))
        if value is not None and value.strip():
            return value.strip()
    return None


def _metadata_pairs(root: Element) -> dict[str, str]:
    pairs: dict[str, str] = {}

    def remember(label: str | None, value: str | None) -> None:
        clean_label = " ".join((label or "").split()).strip().rstrip(":")
        clean_value = " ".join((value or "").split()).strip()
        if "{{" in clean_label or "{{" in clean_value:
            return
        if clean_label and clean_value and clean_label not in pairs:
            pairs[clean_label] = clean_value

    for element in root.iter_elements(include_self=True):
        label_attr = (
            element.get("data-label")
            or element.get("data-field")
            or element.get("data-key")
        )
        value_attr = element.get("data-value")
        if label_attr and value_attr:
            remember(label_attr, value_attr)

    for row in root.find_all("tr"):
        cells = [
            item
            for item in row.element_children()
            if item.tag in {"th", "td"}
        ]
        if len(cells) >= 2:
            remember(cells[0].text_content(), cells[1].text_content())

    for term in root.find_all("dt"):
        parent = term.parent
        if parent is None:
            continue
        siblings = parent.element_children()
        try:
            index = siblings.index(term)
        except ValueError:
            continue
        if index + 1 < len(siblings) and siblings[index + 1].tag == "dd":
            remember(term.text_content(), siblings[index + 1].text_content())

    # Real 2017 2KMTCentral pages use sibling h6/span pairs for general info.
    for term in root.find_all("h6"):
        parent = term.parent
        if parent is None:
            continue
        value = parent.find_first("span")
        if value is not None:
            remember(term.text_content(), value.text_content())

    for row in root.find_all(
        predicate=lambda item: class_or_id_contains(
            item, "meta-row", "info-row", "detail-row", "vital-row"
        )
    ):
        label = child_text(row, "label", "name", "key")
        value = child_text(row, "value", "data")
        if label and value:
            remember(label, value)
    return pairs


def _extract_scalar(
    root: Element,
    metadata: Mapping[str, str],
    aliases: Mapping[str, tuple[str, ...]],
    field_name: str,
) -> str | None:
    data_names = {
        "tier": ("data-tier", "data-gem", "data-rarity"),
        "theme": ("data-theme", "data-set"),
        "collection": ("data-collection",),
        "team": ("data-team", "data-franchise"),
        "position": ("data-position", "data-positions"),
        "height": ("data-height",),
        "weight": ("data-weight",),
        "age": ("data-age",),
        "origin": ("data-origin", "data-from", "data-college", "data-country"),
        "jersey_number": ("data-jersey", "data-jersey-number"),
    }.get(field_name, ())
    for attr in data_names:
        element = root.find_first(predicate=lambda item, key=attr: bool(item.get(key)))
        if element:
            return (element.get(attr) or "").strip() or None
    class_fragments = {
        "tier": ("card-tier", "gem-tier", "rarity"),
        "theme": ("card-theme", "theme-name", "set-name"),
        "collection": ("card-collection", "collection-name"),
        "team": ("card-team", "team-name", "franchise-name"),
        "position": ("card-position", "positions"),
        "height": ("player-height",),
        "weight": ("player-weight",),
        "age": ("player-age",),
        "origin": ("player-origin", "player-from"),
        "jersey_number": ("jersey-number",),
    }.get(field_name, ())
    for fragment in class_fragments:
        element = root.find_first(
            predicate=lambda item, token=fragment: class_or_id_contains(item, token)
        )
        if element and element.text_content():
            return element.text_content()
    return first_present(metadata, aliases.get(field_name, (field_name,)))


def _extract_overall(
    root: Element,
    metadata: Mapping[str, str],
    aliases: Mapping[str, tuple[str, ...]],
) -> int | None:
    for attr in ("data-overall", "data-ovr"):
        element = root.find_first(predicate=lambda item, key=attr: bool(item.get(key)))
        if element:
            value = parse_int(element.get(attr))
            if value is not None:
                return value
    element = root.find_first(
        predicate=lambda item: class_or_id_contains(
            item, "card-overall", "player-overall", "overall-rating", "ovr"
        ) or item.has_class("overall")
    )
    if element:
        value = parse_int(element.text_content())
        if value is not None:
            return value
    return parse_int(first_present(metadata, aliases.get("overall", ("overall", "ovr"))))


def _split_positions(raw: str | None) -> tuple[str | None, str | None]:
    if not raw:
        return None, None
    tokens = re.findall(r"(?<![A-Z])(?:PG|SG|SF|PF|C)(?![A-Z])", raw.upper())
    if tokens:
        return tokens[0], tokens[1] if len(tokens) > 1 else None
    pieces = [piece.strip() for piece in re.split(r"[/,|]", raw) if piece.strip()]
    return (pieces[0] if pieces else None, pieces[1] if len(pieces) > 1 else None)


def _extract_stats(
    root: Element,
    page_url: str,
    kind: str,
    page_hint: str | None,
) -> list[RawLabeledValue]:
    values: list[RawLabeledValue] = []
    seen: set[tuple[str, str]] = set()

    for element in root.iter_elements(include_self=True):
        # The live 2KMTCentral card markup reuses ``attribute`` classes in the
        # roster-update history table.  Those rows are revisions of the card,
        # not additional gameplay attributes.
        if _inside_roster_update_history(element):
            continue
        section = _element_section(element, page_hint)
        explicit_label = None
        if kind == "attributes":
            explicit_label = element.get("data-attribute") or element.get("data-stat-label")
        elif kind == "tendencies":
            explicit_label = element.get("data-tendency")
        if section != kind and not explicit_label:
            continue
        if element.tag not in {"tr", "li", "div", "article"} and not explicit_label:
            continue
        label, raw = _row_label_value(element, explicit_label)
        if not label or not raw:
            continue
        folded_label = label.casefold().strip().rstrip(":")
        if kind == "attributes" and re.match(
            r"^total(?:\s+(?:attributes?|attr\.?))?(?:\s*[+-]\d+)?$",
            folded_label,
        ):
            continue
        if folded_label in {
            "overall",
            "ovr",
            "tier",
            "team",
            "position",
            "height",
            "weight",
            "age",
        }:
            continue
        key = (folded_label, raw)
        if key in seen:
            continue
        seen.add(key)
        numeric = parse_int(raw, signed=True)
        rating_element = element.find_first(
            predicate=lambda item: bool(item.get("data-original-value"))
        )
        duo_boost = None
        if rating_element is not None:
            duo_boost = parse_int(
                rating_element.get("data-dynamic-duo-boost"), signed=True
            )
        values.append(
            RawLabeledValue(
                label=label,
                raw_value=raw,
                value=numeric if numeric is not None else raw,
                section=kind,
                source_url=page_url,
                dynamic_duo_boost=duo_boost,
            )
        )
    return values


def _row_label_value(
    element: Element, explicit_label: str | None = None
) -> tuple[str | None, str | None]:
    label = explicit_label or element.get("data-label") or element.get("data-stat-name")
    raw = element.get("data-value") or element.get("data-rating")
    if label and raw:
        return _clean_text(label), _clean_text(raw)
    children = element.element_children()
    cells = [child for child in children if child.tag in {"th", "td"}]
    if len(cells) >= 2:
        return _clean_text(cells[0].text_content()), _clean_text(cells[1].text_content())
    label = label or child_text(element, "stat-name", "attribute-name", "tendency-name", "label")
    raw = raw or child_text(element, "stat-value", "rating", "value")
    if label and raw:
        return _clean_text(label), _clean_text(raw)
    # Actual NBA 2K17 pages put the numeric box first and the source label in
    # the remaining visible content of each list item. Attribute pages expose
    # ``data-original-value``; tendency pages use only an ``attribute-box``
    # class, so support both without guessing a default value.
    if element.tag == "li":
        rating = element.find_first(
            predicate=lambda item: bool(item.get("data-original-value"))
            or class_or_id_contains(item, "attribute-box", "stat-value")
        )
        if rating is not None:
            raw = rating.get("data-original-value") or rating.text_content()
            label = _visible_row_text(element, excluded=rating)
            if label and raw:
                # Duo pages render a second visible ``+N`` span next to the
                # label. The boost belongs in metadata, not in the stable
                # attribute name used by the normalizer.
                if parse_int(rating.get("data-dynamic-duo-boost"), signed=True):
                    label = re.sub(r"\s*[+-]\s*\d+\s*$", "", label)
                return _clean_text(label), _clean_text(raw)
    if element.tag == "li":
        text = element.text_content()
        match = re.match(r"(.+?)\s*(?::|[-–—])\s*([-+]?\d+(?:\.\d+)?)\s*$", text)
        if match:
            return _clean_text(match.group(1)), _clean_text(match.group(2))
    return None, None


def _extract_badges(
    root: Element, page_url: str, page_hint: str | None
) -> list[RawBadge]:
    # Real 2KMTCentral badge pages do not label their rows with semantic
    # classes or level text.  The source of truth is the badge icon filename:
    # levels 1..4 are Bronze..Hall of Fame, while the personality category is
    # an explicitly separate, non-tiered badge domain.
    source_rows = _extract_2kmt_badge_sections(root, page_url)
    if source_rows:
        return source_rows

    badges: list[RawBadge] = []
    seen: set[tuple[str, str | None]] = set()
    for element in root.iter_elements(include_self=True):
        explicit_name = element.get("data-badge-name") or element.get("data-badge")
        section = _element_section(element, page_hint)
        looks_like_row = class_or_id_contains(
            element, "badge-row", "badge-item", "player-badge"
        ) or (element.tag == "tr" and section == "badges")
        if section != "badges" and not explicit_name:
            continue
        if not explicit_name and not looks_like_row:
            continue
        name = explicit_name or child_text(element, "badge-name", "name", "label")
        raw_level = element.get("data-level") or element.get("data-badge-level")
        raw_level = raw_level or child_text(element, "badge-level", "level", "tier")
        cells = [child for child in element.element_children() if child.tag in {"th", "td"}]
        if len(cells) >= 2:
            name = name or cells[0].text_content()
            raw_level = raw_level or cells[1].text_content()
        class_text = (element.get("class") or "").replace("_", " ").replace("-", " ")
        if not raw_level:
            for candidate in ("Hall of Fame", "HOF", "Gold", "Silver", "Bronze"):
                if candidate.casefold() in class_text.casefold():
                    raw_level = candidate
                    break
        text = element.text_content()
        if not raw_level:
            match = re.search(
                r"\b(Hall\s+of\s+Fame|HOF|Gold|Silver|Bronze)\b", text, re.IGNORECASE
            )
            if match:
                raw_level = match.group(1)
        if not name and text:
            name = re.sub(
                r"\s*(?:[-–—:]\s*)?(?:Hall\s+of\s+Fame|HOF|Gold|Silver|Bronze)\s*$",
                "",
                text,
                flags=re.IGNORECASE,
            )
        name = _clean_text(name)
        raw_level = _clean_text(raw_level)
        if not name:
            continue
        key = (name.casefold(), raw_level.casefold() if raw_level else None)
        if key in seen:
            continue
        seen.add(key)
        badges.append(
            RawBadge(
                name=name,
                level=normalized_badge_level(raw_level),
                raw_level=raw_level,
                source_url=page_url,
            )
        )
    return badges


def _extract_2kmt_badge_sections(root: Element, page_url: str) -> list[RawBadge]:
    containers = root.find_all(
        predicate=lambda item: "badges-container" in item.classes
    )
    if not containers:
        return []

    levels = {
        1: "Bronze",
        2: "Silver",
        3: "Gold",
        4: "Hall of Fame",
    }
    badges: list[RawBadge] = []
    seen: set[str] = set()
    for container in containers:
        for section in container.find_all("section"):
            image = section.find_first("img")
            source = "" if image is None else (image.get("src") or "")
            icon = re.search(
                r"icon_badge_(?P<category>[a-z0-9_]+)_(?P<level>[1-4])\.png",
                source,
                re.IGNORECASE,
            )
            if icon is None:
                continue
            anchor = section.find_first(
                "a",
                predicate=lambda item: bool(
                    re.search(r"/players/badges/\d+[a-z]?(?:$|[/?#])", item.get("href") or "")
                ),
            )
            if anchor is None:
                continue
            strong = anchor.find_first("strong")
            name = _clean_text(
                strong.text_content() if strong is not None else anchor.text_content()
            )
            if not name or name.casefold() in seen:
                continue
            seen.add(name.casefold())
            category = icon.group("category").casefold()
            raw_level = (
                "Personality"
                if category == "personality"
                else levels[int(icon.group("level"))]
            )
            badges.append(
                RawBadge(
                    name=name,
                    level=normalized_badge_level(raw_level),
                    raw_level=raw_level,
                    source_url=page_url,
                )
            )
    return badges


def _extract_dynamic_duo(
    root: Element,
    page_url: str,
    metadata: Mapping[str, str],
    card_path_pattern: re.Pattern[str],
    source_host: str,
) -> RawDuo | None:
    # ``dynamic-duo-overlay``, ``dynamic-duo-checkbox`` and boost spans occur
    # before the actual relationship section. Match the exact section class so
    # an image cannot become an empty/ghost duo record.
    containers = root.find_all(
        predicate=lambda item: item.tag in {"section", "div", "article", "aside"}
        and (
            "dynamic-duo" in item.classes
            or "dynamic_duo" in item.classes
            or (item.get("id") or "").casefold() in {"dynamic-duo", "dynamic_duo"}
        )
    )
    container = next(
        (
            candidate
            for candidate in containers
            if candidate.find_first(
                "a",
                predicate=lambda item: bool(
                    card_path_pattern.search(unquote(item.get("href") or ""))
                ),
            )
            is not None
        ),
        containers[0] if containers else None,
    )
    if container is None:
        heading = root.find_first(
            predicate=lambda item: item.tag in {"h2", "h3", "h4", "legend"}
            and "dynamic duo" in item.text_content().casefold()
        )
        if heading is not None:
            container = heading.parent
    raw_label = first_present(metadata, ("dynamic duo", "duo", "duo partner"))
    if container is None and not raw_label:
        return None

    partner_name: str | None = None
    partner_url: str | None = None
    partner_source_id: str | None = None
    if container is not None:
        partner = container.find_first(
            predicate=lambda item: bool(item.get("data-duo-partner"))
            or class_or_id_contains(item, "duo-partner", "partner-name")
        )
        if partner is not None:
            partner_name = (
                partner.get("data-duo-partner") or partner.text_content()
            ).strip() or None
        for anchor in container.find_all("a"):
            href = anchor.get("href") or ""
            resolved = _resolve_source_link(href, page_url, source_host)
            match = card_path_pattern.search(unquote(resolved))
            if match:
                partner_url = _strip_card_subpage(
                    _unwrapped_source_url(resolved, source_host)
                )
                partner_source_id = match.group("id")
                partner_name = partner_name or anchor.text_content() or None
                break
    partner_name = partner_name or raw_label

    boosts: list[RawLabeledValue] = []
    if container is not None:
        seen: set[tuple[str, str]] = set()
        # Real pages place boosted attribute rows in the main attribute grid,
        # outside the compact duo partner section.
        for element in root.find_all("li"):
            rating = element.find_first(
                predicate=lambda item: item.get("data-dynamic-duo-boost") is not None
            )
            if rating is None:
                continue
            boost = parse_int(rating.get("data-dynamic-duo-boost"), signed=True)
            if not boost:
                continue
            label, _raw_value = _row_label_value(element)
            if not label or re.match(
                r"^total(?:\s+(?:attributes?|attr\.?))?", label, re.IGNORECASE
            ):
                continue
            raw_boost = f"{boost:+d}"
            key = (label.casefold(), raw_boost)
            if key in seen:
                continue
            seen.add(key)
            boosts.append(
                RawLabeledValue(
                    label=label,
                    raw_value=raw_boost,
                    value=boost,
                    section="dynamic_duo_boosts",
                    source_url=page_url,
                )
            )
        for element in container.iter_elements(include_self=True):
            label, raw = _row_label_value(element)
            if not label or not raw:
                text = element.text_content() if element.tag == "li" else ""
                match = re.match(r"\s*([-+]\d+)\s+(.+?)\s*$", text)
                if match:
                    raw, label = match.group(1), match.group(2)
            if not label or not raw or parse_int(raw, signed=True) is None:
                continue
            key = (label.casefold(), raw)
            if key in seen:
                continue
            seen.add(key)
            boosts.append(
                RawLabeledValue(
                    label=label,
                    raw_value=raw,
                    value=parse_int(raw, signed=True),
                    section="dynamic_duo_boosts",
                    source_url=page_url,
                )
            )
    return RawDuo(
        partner_name=partner_name,
        partner_source_card_id=partner_source_id,
        partner_url=partner_url,
        boosts=tuple(boosts),
        raw_label=raw_label,
    )


def _extract_images(root: Element, page_url: str) -> tuple[str | None, str | None]:
    card: str | None = None
    player: str | None = None
    for meta in root.find_all("meta"):
        key = (meta.get("property") or meta.get("name") or "").casefold()
        if key in {"og:image", "twitter:image", "twitter:image:src"}:
            card = _resolve_image_url(meta.get("content") or "", page_url)
            if card:
                break
    for image in root.find_all("img"):
        raw = (
            image.get("data-src")
            or image.get("data-original")
            or image.get("src")
            or ""
        )
        if not raw:
            continue
        resolved = _resolve_image_url(raw, page_url)
        marker = f"{image.get('class') or ''} {image.get('id') or ''}".casefold()
        if any(
            token in marker
            for token in ("player-cutout", "player-image", "portrait", "cutout")
        ):
            player = player or resolved
        elif any(
            token in marker
            for token in ("card-image", "card-art", "generated-card", "player-card")
        ):
            card = card or resolved
    return card, player


def _page_section_hint(url: str, root: Element) -> str | None:
    path = urlsplit(_unwrapped_source_url(url, "")).path.casefold().rstrip("/")
    for section in ("attributes", "tendencies", "badges"):
        if path.endswith(f"/{section}"):
            return section
    body = root.find_first("body")
    marker = "" if body is None else f"{body.get('class') or ''} {body.get('id') or ''}"
    for section in ("attributes", "tendencies", "badges"):
        if section in marker.casefold():
            return section
    return None


def _element_section(element: Element, page_hint: str | None) -> str | None:
    for current in (element, *element.ancestors()):
        marker = f"{current.get('class') or ''} {current.get('id') or ''}".casefold()
        if "tendenc" in marker:
            return "tendencies"
        if "attribute" in marker or "rating" in marker:
            # 2KMTCentral's tendency page intentionally reuses the same
            # ``attribute-list`` and ``attribute-box`` CSS as ratings.
            if page_hint == "tendencies":
                return "tendencies"
            return "attributes"
        if "badge" in marker:
            return "badges"
    return page_hint


def _inside_roster_update_history(element: Element) -> bool:
    for current in (element, *element.ancestors()):
        if "updates" in current.classes and (
            "players-list" in current.classes or current.tag in {"div", "section"}
        ):
            return True
    return False


def _fixture_provenance(root: Element) -> str | None:
    element = root.find_first(
        predicate=lambda item: bool(item.get("data-fixture-provenance"))
    )
    if element:
        return element.get("data-fixture-provenance")
    for meta in root.find_all("meta"):
        if (meta.get("name") or "").casefold() == "fixture-provenance":
            return (meta.get("content") or "").strip() or None
    return None


def _resolve_source_link(href: str, page_url: str, source_host: str) -> str:
    href = href.strip()
    if href.startswith("//"):
        return f"https:{href}"
    if (
        href.startswith("/")
        and source_host
        and source_host not in urlsplit(page_url).netloc.casefold()
    ):
        return f"https://{source_host}{href}"
    return urljoin(page_url, href)


def _unwrapped_source_url(url: str, source_host: str) -> str:
    decoded = unquote(url)
    match = re.search(r"https?://[^/]*web\.archive\.org/web/[^/]+/(https?://.+)$", decoded)
    if match:
        return match.group(1)
    match = re.search(r"/web/[^/]+/(https?://.+)$", decoded)
    if match:
        return match.group(1)
    if source_host and source_host in decoded.casefold():
        position = decoded.casefold().find("http", decoded.casefold().find(source_host) - 10)
        if position >= 0:
            return decoded[position:]
    return decoded


def _strip_card_subpage(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/(?:attributes|tendencies|badges)/?$", "", parts.path, flags=re.I)
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def _source_url_dedupe_key(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.netloc.casefold()}{parts.path.rstrip('/').casefold()}"


def _resolve_image_url(raw: str, page_url: str) -> str | None:
    raw = raw.strip()
    if not raw or raw.startswith("data:"):
        return None
    if raw.startswith("//"):
        return f"https:{raw}"
    return urljoin(page_url, raw)


def _clean_text(value: str | None) -> str | None:
    text = " ".join((value or "").split()).strip().rstrip(":")
    return text or None


def _direct_text(element: Element) -> str | None:
    pieces = [child for child in element.children if isinstance(child, str)]
    return _clean_text(" ".join(pieces))


def _ordered_unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _unique_dataclasses(values: Iterable[Any]) -> list[Any]:
    result: list[Any] = []
    seen: set[tuple[Any, ...]] = set()
    for value in values:
        key = tuple(asdict(value).items())
        # Nested dataclasses (the duo type) are not passed through this helper.
        try:
            unseen = key not in seen
        except TypeError:
            unseen = value not in result
        if unseen:
            try:
                seen.add(key)
            except TypeError:
                pass
            result.append(value)
    return result


def _unique_mappings(values: Iterable[dict[str, str | None]]) -> list[dict[str, str | None]]:
    result: list[dict[str, str | None]] = []
    seen: set[tuple[tuple[str, str | None], ...]] = set()
    for value in values:
        key = tuple(sorted(value.items()))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result
