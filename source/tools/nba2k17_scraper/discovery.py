from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from .database import CrawlDatabase, CrawlUrl


_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "referrer",
}


def canonicalize_url(url: str, base_url: str | None = None) -> str | None:
    """Normalize an observed HTTP URL without inventing another URL."""

    absolute = urljoin(base_url, url) if base_url is not None else url
    try:
        parts = urlsplit(absolute.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.casefold()
    if scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    host = parts.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    path = parts.path or "/"
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(query_items, doseq=True), ""))


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        target = "href" if tag.casefold() in {"a", "link"} else None
        if tag.casefold() in {"img", "script", "source"}:
            target = "src"
        if target is None:
            return
        for name, value in attrs:
            if name.casefold() == target and value:
                self.links.append(value)


def extract_links(html: bytes | str, base_url: str) -> tuple[str, ...]:
    text = html.decode("utf-8", errors="replace") if isinstance(html, bytes) else html
    parser = _LinkParser()
    parser.feed(text)
    result: list[str] = []
    seen: set[str] = set()
    for raw in parser.links:
        normalized = canonicalize_url(raw, base_url)
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def extract_sitemap_urls(xml: bytes | str) -> tuple[str, ...]:
    raw = xml.encode("utf-8") if isinstance(xml, str) else xml
    root = ET.fromstring(raw)
    result: list[str] = []
    seen: set[str] = set()
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1].casefold() != "loc" or not element.text:
            continue
        normalized = canonicalize_url(element.text.strip())
        if normalized is not None and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)


def _json_urls(value: object) -> Iterable[str]:
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "/")):
            yield value
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _json_urls(child)
    elif isinstance(value, list):
        for child in value:
            yield from _json_urls(child)


@dataclass(frozen=True, slots=True)
class SourceDiscoveryRules:
    source: str
    allowed_hosts: tuple[str, ...]
    card_url_patterns: tuple[str, ...]
    allow_subdomains: bool = False

    def __post_init__(self) -> None:
        if not self.source.strip():
            raise ValueError("source is required")
        if not self.allowed_hosts:
            raise ValueError("at least one allowed host is required")
        for pattern in self.card_url_patterns:
            re.compile(pattern)

    def accepts_host(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        for allowed in self.allowed_hosts:
            normalized = allowed.casefold()
            if host == normalized or (
                self.allow_subdomains and host.endswith("." + normalized)
            ):
                return True
        return False

    def source_card_id(self, url: str) -> str | None:
        for pattern in self.card_url_patterns:
            match = re.search(pattern, url)
            if match is None:
                continue
            if "id" in match.groupdict():
                return match.group("id")
            if match.groups():
                return match.group(1)
        return None

    def is_card_url(self, url: str) -> bool:
        return self.accepts_host(url) and self.source_card_id(url) is not None


def links_from_index_document(
    body: bytes | str,
    *,
    index_url: str,
    content_type: str = "",
) -> tuple[str, ...]:
    raw = body.encode("utf-8") if isinstance(body, str) else body
    stripped = raw.lstrip()
    media_type = content_type.split(";", 1)[0].strip().casefold()
    if media_type in {"application/xml", "text/xml", "application/rss+xml"} or stripped.startswith(b"<?xml"):
        return extract_sitemap_urls(raw)
    if media_type == "application/json" or stripped.startswith((b"{", b"[")):
        document = json.loads(raw.decode("utf-8"))
        seen: set[str] = set()
        result: list[str] = []
        for candidate in _json_urls(document):
            normalized = canonicalize_url(candidate, index_url)
            if normalized is not None and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
        return tuple(result)
    if media_type == "text/plain":
        result = []
        seen: set[str] = set()
        for line in raw.decode("utf-8", errors="replace").splitlines():
            candidate = canonicalize_url(line.strip(), index_url)
            if candidate is not None and candidate not in seen:
                seen.add(candidate)
                result.append(candidate)
        return tuple(result)
    return extract_links(raw, index_url)


def discover_index_document(
    database: CrawlDatabase,
    rules: SourceDiscoveryRules,
    *,
    index_url: str,
    body: bytes | str,
    content_type: str = "text/html",
    discovery_method: str = "site_index_link",
    priority: int = 0,
    now: float | None = None,
) -> list[CrawlUrl]:
    """Persist only card URLs actually present in an index document."""

    observed = links_from_index_document(
        body, index_url=index_url, content_type=content_type
    )
    discovered: list[CrawlUrl] = []
    for url in observed:
        if not rules.is_card_url(url):
            continue
        discovered.append(
            database.discover_url(
                source=rules.source,
                url=url,
                source_card_id=rules.source_card_id(url),
                discovery_method=discovery_method,
                priority=priority,
                discovered_from=index_url,
                metadata={"content_type": content_type},
                now=now,
            )
        )
    return discovered


def discover_page_links(
    database: CrawlDatabase,
    rules: SourceDiscoveryRules,
    *,
    page_url: str,
    html: bytes | str,
    discovery_method: str = "page_link",
    priority: int = 0,
    include: Callable[[str], bool] | None = None,
    now: float | None = None,
) -> list[CrawlUrl]:
    discovered: list[CrawlUrl] = []
    for url in extract_links(html, page_url):
        if not rules.is_card_url(url) or (include is not None and not include(url)):
            continue
        discovered.append(
            database.discover_url(
                source=rules.source,
                url=url,
                source_card_id=rules.source_card_id(url),
                discovery_method=discovery_method,
                priority=priority,
                discovered_from=page_url,
                now=now,
            )
        )
    return discovered
