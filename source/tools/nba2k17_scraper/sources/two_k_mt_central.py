from __future__ import annotations

import re

from .base import HtmlCardSourceAdapter, RawParsedCard, SourcePage


class TwoKMTCentralAdapter(HtmlCardSourceAdapter):
    """Parser for direct or archived NBA 2K17 2KMTCentral HTML."""

    site = "2kmtcentral"
    source_host = "2kmtcentral.com"
    card_path_pattern = re.compile(
        r"(?i)(?:https?://(?:www\.)?2kmtcentral\.com)?/"
        r"(?:17|2k17)/players/(?P<id>\d+)/(?P<slug>[^/?#]+)"
    )
    title_suffixes = (
        "2KMTCentral",
        "NBA 2K17 MyTEAM Player",
        "NBA 2K17 Player",
    )
    metadata_labels = {
        **HtmlCardSourceAdapter.metadata_labels,
        "tier": ("tier", "gem", "color"),
        "theme": ("theme", "set", "card theme"),
        "collection": ("collection", "collection name"),
        "team": ("team", "franchise", "nba team"),
        "origin": ("from", "college", "country"),
    }

    def parse_card(
        self,
        main_html: str,
        source_url: str,
        *,
        attributes_html: str | None = None,
        badges_html: str | None = None,
        tendencies_html: str | None = None,
    ) -> RawParsedCard:
        pages = [SourcePage(source_url, main_html)]
        if attributes_html is not None:
            pages.append(SourcePage(f"{source_url.rstrip('/')}/attributes", attributes_html))
        if badges_html is not None:
            pages.append(SourcePage(f"{source_url.rstrip('/')}/badges", badges_html))
        if tendencies_html is not None:
            pages.append(SourcePage(f"{source_url.rstrip('/')}/tendencies", tendencies_html))
        return self.parse_pages(pages)


Adapter = TwoKMTCentralAdapter
