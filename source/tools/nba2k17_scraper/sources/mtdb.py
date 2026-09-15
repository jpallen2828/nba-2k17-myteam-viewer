from __future__ import annotations

import re
from urllib.parse import urlsplit

from .base import HtmlCardSourceAdapter, RawLabeledValue, RawParsedCard, SourcePage
from .html_dom import parse_html


class MTDBAdapter(HtmlCardSourceAdapter):
    """Tolerant MTDB fallback parser with the same neutral result model."""

    site = "mtdb"
    source_host = "mtdb.com"
    card_path_pattern = re.compile(
        r"(?i)(?:https?://(?:www\.)?mtdb\.com)?/"
        r"(?:(?:17|2k17|nba-2k17)/)?(?:players?|cards?)/"
        r"(?P<id>\d+)/(?P<slug>[^/?#]+)"
    )
    title_suffixes = ("MTDB", "NBA 2K17 MyTEAM Database")
    metadata_labels = {
        **HtmlCardSourceAdapter.metadata_labels,
        "tier": ("tier", "gem", "rarity", "card color"),
        "theme": ("theme", "set", "promo"),
        "collection": ("collection", "collection/set"),
        "team": ("team", "franchise"),
        "origin": ("from", "school", "country", "college"),
    }

    def _identity_from_url(self, url: str) -> tuple[str | None, str | None]:
        # Actual 2017 MTDB IDs are whole slugs, not the numeric 2KMTC IDs.
        match = re.search(r"/17/players/([a-z][a-z0-9-]*-\d+)(?:/|[?#]|$)", url, re.I)
        if match:
            return match.group(1), match.group(1)
        return super()._identity_from_url(url)

    def discover_card_urls(self, html: str, page_url: str) -> list[str]:
        from urllib.parse import urljoin
        urls = set(super().discover_card_urls(html, page_url))
        for anchor in parse_html(html).find_all("a"):
            url = urljoin(page_url, anchor.get("href") or "")
            parts = urlsplit(url)
            if (parts.hostname in ("mtdb.com", "www.mtdb.com") and not parts.query
                    and re.fullmatch(r"/17/players/[a-z][a-z0-9-]*-\d+/?", parts.path, re.I)):
                urls.add(url.rstrip("/"))
        return sorted(urls)

    def parse_page(self, page: SourcePage) -> RawParsedCard:
        result = super().parse_page(page)
        root = parse_html(page.html)
        hero = root.find_first(predicate=lambda e: e.has_class("playerHero"))
        if hero is None:
            return result  # Other observed/fixture layouts retain the generic parser.
        title = hero.find_first("h1")
        overall = hero.find_first(predicate=lambda e: e.has_class("playerONum"))
        result.name = title.text_content() if title else None
        result.overall = int(overall.text_content()) if overall and overall.text_content().isdigit() else None
        # Never infer gem tier from overall or the slug's ordinal suffix.
        collection = hero.find_first("a", predicate=lambda e: "/17/collections/" in (e.get("href") or ""))
        result.collection = collection.text_content() if collection else result.collection
        if collection:
            result.raw_metadata["mtdb_collection_path"] = collection.get("href") or ""
        position = hero.find_first("strong")
        if position:
            positions = re.findall(r"\b(?:PG|SG|SF|PF|C)\b", position.text_content())
            result.primary_position = positions[0] if positions else None
            result.secondary_position = positions[1] if len(positions) > 1 else None
        text = hero.text_content()
        for field, pattern in (("age", r"\b(\d+)\s*yrs\b"), ("weight", r"\b(\d+)\s*lbs\b")):
            match = re.search(pattern, text)
            if match:
                setattr(result, field, int(match.group(1)))
        height = re.search(r"\b\d['’]\s*\d+\"", text)
        result.height = height.group(0) if height else result.height
        result.attributes = self._list_values(root, page.url, "attributes", "statList")
        result.tendencies = self._list_values(root, page.url, "tendencies", "tendency-list")
        # Retain observed summary counts as evidence only. Three displayed zeros
        # without HOF or named records cannot establish a complete 2K17 badge set.
        result.badge_counts = {}
        for row in hero.find_all("tr"):
            label, value = row.find_first("th"), row.find_first("td")
            if label and value and label.text_content() in ("Bronze", "Silver", "Gold", "Hall of Fame"):
                if value.text_content().isdigit():
                    result.badge_counts[label.text_content()] = int(value.text_content())
        if not result.badges:
            result.warnings.append("MTDB named badges absent; summary zeros are not proof of zero badges")
        if not result.tendencies:
            result.warnings.append("MTDB tendency lists empty or unavailable; not converted to zeros")
        result.raw_metadata["mtdb_layout"] = "playerHero/statList"
        result.refresh_missing_fields()
        return result

    @staticmethod
    def _list_values(root, url: str, domain: str, css_class: str) -> list[RawLabeledValue]:
        values = []
        for group in root.find_all("ul", predicate=lambda e: e.has_class(css_class)):
            if domain == "attributes" and not any(e.get("id") == "attributes" for e in group.ancestors()):
                continue
            for row in group.find_all("li"):
                number = row.find_first(predicate=lambda e: e.has_class("statNum"))
                if number is None or not re.fullmatch(r"\d{1,3}", number.text_content()):
                    continue
                def without_number(node):
                    return " ".join(child if isinstance(child, str) else without_number(child)
                                    for child in node.children if child is not number)
                label = " ".join(without_number(row).split())
                if label:
                    values.append(RawLabeledValue(label, number.text_content(), int(number.text_content()), domain, url))
        return values

    def parse_card(
        self,
        html: str,
        source_url: str,
        *,
        attributes_html: str | None = None,
        badges_html: str | None = None,
        tendencies_html: str | None = None,
        provenance: str | None = None,
    ) -> RawParsedCard:
        pages = [SourcePage(source_url, html, provenance=provenance)]
        if attributes_html is not None:
            pages.append(
                SourcePage(
                    f"{source_url.rstrip('/')}/attributes",
                    attributes_html,
                    provenance=provenance,
                )
            )
        if badges_html is not None:
            pages.append(
                SourcePage(
                    f"{source_url.rstrip('/')}/badges",
                    badges_html,
                    provenance=provenance,
                )
            )
        if tendencies_html is not None:
            pages.append(
                SourcePage(
                    f"{source_url.rstrip('/')}/tendencies",
                    tendencies_html,
                    provenance=provenance,
                )
            )
        return self.parse_pages(pages)


Adapter = MTDBAdapter
