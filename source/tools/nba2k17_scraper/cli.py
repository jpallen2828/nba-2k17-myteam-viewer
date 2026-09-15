from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from .archive import CdxRecord, WaybackCdx, WaybackReplay
from .config import ScraperConfig, RateLimitPolicy
from .completeness import refresh_completeness_ledger
from .database import CrawlDatabase, PageSnapshot
from .discovery import (
    SourceDiscoveryRules,
    canonicalize_url,
    discover_index_document,
    extract_links,
)
from .exporter import export_catalog
from .catalog_curation import curate_catalog
from .fetcher import FetchResult, Fetcher, ParseOutcome
from .injection_exporter import build_injection_profile
from .live_roster_matcher import (
    apply_roster_match,
    load_native_roster_catalog,
    match_card_to_roster,
    normalize_name,
)
from .models import CanonicalCard, sha256_json
from .normalizer import PARSER_VERSION as NORMALIZER_VERSION
from .normalizer import normalize_parsed_card
from .parity import write_parity_report
from .reconciliation import reconcile_catalogs
from .reports.assets import (
    AssetReportRecord,
    build_asset_validation_report,
    write_asset_validation_report,
)
from .reports.coverage import build_pilot_report, write_pilot_report
from .sources import MTDBAdapter, SourcePage, TwoKMTCentralAdapter
from .validator import is_adequate, update_completeness


DEFAULT_DATA_ROOT = Path(".project/data/nba2k17")
SOURCE_ORDER = ("2kmtcentral", "mtdb")
ADAPTER_PARSER_VERSION = f"nba2k17-html-adapters/1+{NORMALIZER_VERSION}"
ASSET_CDX_FROM = "2016"
ASSET_CDX_TO = "2018"
_TWO_K_MTC_CARD_IMAGE = re.compile(
    r"^/img/players/17/generated/"
    r"(?P<asset_token>(?P<card_id>\d+)i(?P<revision>[1-9]\d*))\.png/"
    r"cache-(?P<cache_token>\d+)/(?P=asset_token)\.png$"
)


class CliError(RuntimeError):
    """A concise, user-correctable command error."""


def _source_rules(source: str) -> SourceDiscoveryRules:
    if source == "2kmtcentral":
        return SourceDiscoveryRules(
            source=source,
            allowed_hosts=("2kmtcentral.com",),
            card_url_patterns=(
                r"/(?:17|2k17)/players/(?P<id>\d+)(?:/[^/?#]+)?(?:/|$)",
            ),
            allow_subdomains=True,
        )
    if source == "mtdb":
        return SourceDiscoveryRules(
            source=source,
            allowed_hosts=("mtdb.com",),
            card_url_patterns=(
                r"/17/players/(?P<id>[a-z][a-z0-9-]*-\d+)(?=[/?#]|$)",
                r"/(?:(?:17|2k17|nba-2k17)/)?(?:players?|cards?)/"
                r"(?P<id>\d+)(?:/[^/?#]+)?(?:/|$)",
            ),
            allow_subdomains=True,
        )
    raise CliError(f"unsupported card source: {source}")


def _adapter(source: str):
    if source == "2kmtcentral":
        return TwoKMTCentralAdapter()
    if source == "mtdb":
        return MTDBAdapter()
    raise CliError(f"unsupported card source: {source}")


def _config(args: argparse.Namespace) -> ScraperConfig:
    delay = getattr(args, 'request_delay_seconds', None)
    policy = RateLimitPolicy() if delay is None else RateLimitPolicy(
        minimum_spacing_seconds=delay, jitter_min_seconds=0,
        jitter_max_seconds=getattr(args, 'request_jitter_seconds', 0))
    return ScraperConfig(Path(args.data_root).expanduser().resolve(), rate_limit=policy)


def _prepare_directories(config: ScraperConfig) -> None:
    for path in (
        config.data_root,
        config.raw_cache_root,
        config.normalized_root,
        config.reports_root,
        config.manual_review_root,
    ):
        path.mkdir(parents=True, exist_ok=True)


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str))


def _fetch_result(result: FetchResult) -> dict[str, Any]:
    return {key: value for key, value in asdict(result).items() if value is not None}


def _absolute_existing_file(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise CliError(f"{label} does not exist or is not a file: {path}")
    return path


def _optional_file(value: str | None, label: str) -> Path | None:
    return None if value is None else _absolute_existing_file(value, label)


def _observed_urls(args: argparse.Namespace) -> list[str]:
    urls = list(args.url or [])
    if args.url_file:
        path = _absolute_existing_file(args.url_file, "URL file")
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            value = line.strip()
            if value and not value.startswith("#"):
                urls.append(value)
    seen: set[str] = set()
    result: list[str] = []
    for raw in urls:
        normalized = canonicalize_url(raw)
        if normalized is None:
            raise CliError(f"expected an absolute HTTP(S) URL, got {raw!r}")
        if normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    if not result:
        raise CliError("at least one explicitly observed URL is required")
    return result


def _validate_card_url(source: str, url: str) -> tuple[str, str]:
    normalized = canonicalize_url(url)
    if normalized is None:
        raise CliError(f"invalid HTTP(S) card URL: {url!r}")
    rules = _source_rules(source)
    if not rules.is_card_url(normalized):
        raise CliError(
            f"URL is not an observed NBA 2K17 {source} card URL: {normalized}"
        )
    card_id = rules.source_card_id(normalized)
    assert card_id is not None
    return normalized, card_id


def _main_card_url(source: str, observed_url: str) -> tuple[str, str]:
    normalized = canonicalize_url(observed_url)
    if normalized is None:
        raise CliError(f"invalid archived URL: {observed_url!r}")
    rules = _source_rules(source)
    if not rules.accepts_host(normalized):
        raise CliError(f"archived URL is outside the {source} host boundary")
    for pattern in rules.card_url_patterns:
        match = re.search(pattern, normalized)
        if match is None:
            continue
        card_id = match.groupdict().get("id") or match.group(1)
        main = canonicalize_url(normalized[: match.end()].rstrip("/"))
        if main is not None:
            return main, str(card_id)
    raise CliError(f"archived URL is not an NBA 2K17 {source} card URL")


def _url_is_within_prefix(url: str, prefix: str) -> bool:
    """Apply the requested CDX origin/path boundary to every returned record."""

    candidate = canonicalize_url(url)
    boundary = canonicalize_url(prefix)
    if candidate is None or boundary is None:
        return False
    candidate_parts = urlsplit(candidate)
    boundary_parts = urlsplit(boundary)
    if (
        candidate_parts.scheme != boundary_parts.scheme
        or candidate_parts.netloc != boundary_parts.netloc
    ):
        return False
    boundary_path = boundary_parts.path.rstrip("/") + "/"
    return candidate_parts.path.startswith(boundary_path)


def command_init(args: argparse.Namespace) -> int:
    config = _config(args)
    _prepare_directories(config)
    with CrawlDatabase(config.database_path) as database:
        result = {
            "data_root": str(config.data_root),
            "database": str(config.database_path),
            "journal_mode": database.journal_mode,
            "schema_initialized": True,
            "network_requests": 0,
            "memory_writes": 0,
        }
    _emit(result)
    return 0


def _database_status(database: CrawlDatabase) -> dict[str, Any]:
    state_rows = database.connection.execute(
        """SELECT source, fetch_status, COUNT(*) AS count
             FROM crawl_urls GROUP BY source, fetch_status
             ORDER BY source, fetch_status"""
    ).fetchall()
    source_rows = database.connection.execute(
        """SELECT source, COUNT(*) AS count
             FROM crawl_urls GROUP BY source ORDER BY source"""
    ).fetchall()
    hosts = database.connection.execute(
        """SELECT host, daily_window, daily_request_count, blocked_until,
                  block_reason, captcha_detected
             FROM host_state ORDER BY host"""
    ).fetchall()
    return {
        "database": str(database.path),
        "journal_mode": database.journal_mode,
        "counts": {
            table: database.table_count(table)
            for table in (
                "crawl_urls",
                "url_discoveries",
                "fetch_attempts",
                "page_snapshots",
                "parse_results",
                "assets",
            )
        },
        "sources": {str(row["source"]): int(row["count"]) for row in source_rows},
        "fetch_states": {
            f"{row['source']}:{row['fetch_status']}": int(row["count"])
            for row in state_rows
        },
        "hosts": [
            {
                "host": row["host"],
                "daily_window": row["daily_window"],
                "daily_request_count": int(row["daily_request_count"]),
                "blocked_until": row["blocked_until"],
                "block_reason": row["block_reason"],
                "captcha_detected": bool(row["captcha_detected"]),
            }
            for row in hosts
        ],
    }


def command_status(args: argparse.Namespace) -> int:
    config = _config(args)
    with CrawlDatabase(config.database_path) as database:
        result = _database_status(database)
    _emit(result)
    return 0


def command_discover(args: argparse.Namespace) -> int:
    config = _config(args)
    _prepare_directories(config)
    rules = _source_rules(args.source)
    adapter = _adapter(args.source)
    discovered: dict[int, Any] = {}
    with CrawlDatabase(config.database_path) as database:
        for seed in args.seed_url or ():
            url, card_id = _validate_card_url(args.source, seed)
            row = database.discover_url(
                source=args.source,
                url=url,
                source_card_id=card_id,
                discovery_method="explicit_observed_seed",
                priority=args.priority,
                metadata={"user_supplied": True},
            )
            discovered[row.crawl_url_id] = row

        if args.index_file:
            index_path = _absolute_existing_file(args.index_file, "local index")
            body = index_path.read_bytes()
            if not args.index_url:
                raise CliError("--index-url is required with --index-file")
            index_url = canonicalize_url(args.index_url)
            if index_url is None:
                raise CliError("--index-url must be an absolute HTTP(S) source URL")
            for row in discover_index_document(
                database,
                rules,
                index_url=index_url,
                body=body,
                content_type=args.content_type,
                discovery_method="observed_local_index",
                priority=args.priority,
            ):
                discovered[row.crawl_url_id] = row

            # Archived HTML often wraps original links in a Wayback URL. The
            # source adapters unwrap only URLs actually present in the file.
            if "html" in args.content_type.casefold():
                text = body.decode("utf-8", errors="replace")
                for observed in adapter.discover_card_urls(text, index_url):
                    try:
                        url, card_id = _validate_card_url(args.source, observed)
                    except CliError:
                        # A saved page can link to an unrelated host whose path
                        # happens to resemble a card URL. It was observed, but
                        # it is outside this source's explicit host boundary.
                        continue
                    row = database.discover_url(
                        source=args.source,
                        url=url,
                        source_card_id=card_id,
                        discovery_method="observed_archived_index_link",
                        priority=args.priority,
                        discovered_from=index_url,
                        metadata={"local_index": str(index_path)},
                    )
                    discovered[row.crawl_url_id] = row

        if not discovered:
            if not args.seed_url and not args.index_file:
                raise CliError("supply --seed-url and/or --index-file")
        result = {
            "source": args.source,
            "discovered_count": len(discovered),
            "cards": [
                {
                    "crawl_url_id": row.crawl_url_id,
                    "source_card_id": row.source_card_id,
                    "url": row.url,
                }
                for row in sorted(discovered.values(), key=lambda item: item.crawl_url_id)
            ],
            "policy": "OBSERVED_LINKS_ONLY_NO_ID_ENUMERATION",
        }
    _emit(result)
    return 0


def command_discover_cdx_prefix(args: argparse.Namespace) -> int:
    """Discover observed card URLs from one path-bounded Wayback URL index."""

    config = _config(args)
    _prepare_directories(config)
    prefix = canonicalize_url(args.prefix_url)
    if prefix is None or not _source_rules(args.source).accepts_host(prefix):
        raise CliError("--prefix-url must be an absolute URL on the selected source host")
    cdx = WaybackCdx()
    query_url = cdx.prefix_query_url(
        prefix,
        from_timestamp=args.from_timestamp,
        to_timestamp=args.to_timestamp,
        limit=args.cdx_limit,
    )
    with CrawlDatabase(config.database_path) as database:
        query_row = database.discover_url(
            source="wayback_cdx",
            url=query_url,
            discovery_method="path_bounded_cdx_prefix_index",
            priority=args.priority,
            discovered_from=prefix,
            metadata={"match_type": "prefix", "numeric_id_generation": False},
        )
        snapshot = None
        if query_row.latest_snapshot_id is not None:
            candidate = database.get_snapshot(query_row.latest_snapshot_id)
            if Path(candidate.raw_cache_path).is_file():
                snapshot = candidate
        fetch_payload: dict[str, Any] = {"outcome": "cached"}
        if snapshot is None:
            result = Fetcher(config, database).fetch(
                query_row.crawl_url_id,
                source_kind="wayback-cdx-prefix-index",
            )
            fetch_payload = _fetch_result(result)
            if result.snapshot_id is None:
                raise CliError(f"CDX prefix index was not recovered: {result.error}")
            snapshot = database.get_snapshot(result.snapshot_id)
        records = cdx.parse(_verified_snapshot_body(snapshot))
        discovered: dict[str, Any] = {}
        rejected = 0
        for record in records:
            if not _url_is_within_prefix(record.original_url, prefix):
                rejected += 1
                continue
            try:
                url, card_id = _main_card_url(args.source, record.original_url)
            except CliError:
                rejected += 1
                continue
            row = database.discover_url(
                source=args.source,
                url=url,
                source_card_id=card_id,
                discovery_method="wayback_cdx_prefix_observed_url",
                priority=args.priority,
                discovered_from=query_url,
                metadata={
                    "observed_archive_timestamp": record.timestamp,
                    "observed_original_url": record.original_url,
                },
            )
            discovered[url] = row
    _emit(
        {
            "source": args.source,
            "prefix_url": prefix,
            "cdx_query_url": query_url,
            "fetch": fetch_payload,
            "cdx_record_count": len(records),
            "discovered_card_url_count": len(discovered),
            "rejected_noncard_or_subresource_count": rejected,
            "policy": "PATH_BOUNDED_ARCHIVE_INDEX_NO_NUMERIC_ID_GENERATION",
        }
    )
    return 0


def command_write_urls(args: argparse.Namespace) -> int:
    config = _config(args)
    destination = Path(args.output).expanduser().resolve()
    with CrawlDatabase(config.database_path) as database:
        rows = database.connection.execute(
            """SELECT url FROM crawl_urls
                 WHERE source=? AND source_card_id IS NOT NULL ORDER BY url""",
            (args.source,),
        ).fetchall()
    scope = getattr(args, "scope", "main")
    urls: list[str] = []
    seen: set[str] = set()
    for row in rows:
        observed = canonicalize_url(str(row["url"]))
        if observed is None:
            continue
        is_subpage = bool(
            re.search(r"/(?:badges|tendencies)/?$", urlsplit(observed).path, re.I)
        )
        try:
            main, _card_id = _main_card_url(args.source, observed)
        except CliError:
            continue
        if scope == "main":
            selected = main
        elif scope == "subpages":
            if not is_subpage:
                continue
            selected = observed
        else:
            selected = observed
        if selected not in seen:
            seen.add(selected)
            urls.append(selected)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("".join(f"{url}\n" for url in urls), encoding="utf-8", newline="\n")
    _emit(
        {
            "source": args.source,
            "url_count": len(urls),
            "scope": scope,
            "output": str(destination),
            "network_requests": 0,
        }
    )
    return 0


def _unwrap_observed_wayback_url(url: str) -> str:
    match = re.search(
        r"(?i)^https?://[^/]*web\.archive\.org/web/[^/]+/(https?://.+)$",
        url,
    )
    if match is None:
        match = re.search(r"(?i)/web/[^/]+/(https?://.+)$", url)
    if match is not None:
        return unquote(match.group(1))

    # A Wayback wrapper can itself percent-encode the embedded original URL.
    # Decode only after proving that the decoded value is still a Wayback
    # wrapper.  Decoding arbitrary observed source links could otherwise turn
    # a literal ``%2Fbadges`` slug into a generated detail-page suffix.
    decoded = unquote(url)
    match = re.search(
        r"(?i)^https?://[^/]*web\.archive\.org/web/[^/]+/(https?://.+)$",
        decoded,
    )
    if match is None:
        match = re.search(r"(?i)/web/[^/]+/(https?://.+)$", decoded)
    return match.group(1) if match is not None else url


def command_discover_cached_subpages(args: argparse.Namespace) -> int:
    """Queue only badge/tendency links observed in cached main-card HTML."""

    config = _config(args)
    _prepare_directories(config)
    kinds = tuple(args.kind or ("badges", "tendencies"))
    suffix = re.compile(rf"/(?:{'|'.join(map(re.escape, kinds))})/?$", re.I)
    discovered: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []
    scanned = 0
    with CrawlDatabase(config.database_path) as database:
        rows = database.connection.execute(
            """SELECT u.crawl_url_id, u.url, u.source_card_id,
                      s.snapshot_id, s.content_hash, s.raw_cache_path
                 FROM crawl_urls u
                 JOIN page_snapshots s ON s.crawl_url_id=u.crawl_url_id
                WHERE u.source=? AND s.http_status BETWEEN 200 AND 299
                ORDER BY u.crawl_url_id, s.captured_at, s.snapshot_id""",
            (args.source,),
        ).fetchall()
        for row in rows:
            main_url = str(row["url"])
            if re.search(r"/(?:attributes|badges|tendencies)/?$", urlsplit(main_url).path, re.I):
                continue
            scanned += 1
            try:
                body = Path(str(row["raw_cache_path"])).read_bytes()
                digest = hashlib.sha256(body).hexdigest()
                if digest != row["content_hash"]:
                    raise ValueError(
                        f"cache hash mismatch: expected {row['content_hash']}, got {digest}"
                    )
                for link in extract_links(body, main_url):
                    observed = canonicalize_url(_unwrap_observed_wayback_url(link))
                    if observed is None or not suffix.search(urlsplit(observed).path):
                        continue
                    try:
                        normalized, card_id = _validate_card_url(args.source, observed)
                    except CliError:
                        # One malformed or cross-source suffix-looking link must
                        # not suppress later valid navigation in the same page.
                        continue
                    if str(row["source_card_id"] or "") != card_id:
                        continue
                    queued = database.discover_url(
                        source=args.source,
                        url=normalized,
                        source_card_id=card_id,
                        discovery_method="observed_cached_card_navigation",
                        priority=args.priority,
                        discovered_from=main_url,
                        metadata={
                            "snapshot_id": int(row["snapshot_id"]),
                            "observed_subpage_kind": urlsplit(normalized).path.rstrip("/").rsplit("/", 1)[-1],
                        },
                    )
                    discovered[normalized] = queued
            except Exception as error:
                errors.append(
                    {
                        "crawl_url_id": int(row["crawl_url_id"]),
                        "url": main_url,
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
    _emit(
        {
            "source": args.source,
            "scanned_main_snapshot_count": scanned,
            "discovered_subpage_count": len(discovered),
            "kinds": list(kinds),
            "errors": errors,
            "network_requests": 0,
            "policy": "OBSERVED_CACHED_NAVIGATION_ONLY_NO_SUFFIX_GENERATION",
        }
    )
    return 0


def _ordered_sources(requested: str) -> tuple[str, ...]:
    return SOURCE_ORDER if requested == "all" else (requested,)


def command_fetch(args: argparse.Namespace) -> int:
    config = _config(args)
    _prepare_directories(config)
    remaining = args.limit
    results: list[FetchResult] = []
    with CrawlDatabase(config.database_path) as database:
        fetcher = Fetcher(config, database)
        for source in _ordered_sources(args.source):
            if remaining <= 0:
                break
            rows = database.list_ready(source=source, limit=remaining)
            for row in rows:
                results.append(fetcher.fetch(row.crawl_url_id))
                remaining -= 1
                if remaining <= 0:
                    break
    _emit(
        {
            "requested_limit": args.limit,
            "processed_count": len(results),
            "source_order": list(_ordered_sources(args.source)),
            "results": [_fetch_result(item) for item in results],
            "rate_limit_policy": "CONSERVATIVE_DURABLE_DEFAULTS",
        }
    )
    return 0


def _cached_archive_snapshot(
    database: CrawlDatabase, crawl_url_id: int, timestamp: str
) -> PageSnapshot | None:
    for snapshot in database.snapshots_for_url(crawl_url_id):
        if snapshot.archive_timestamp == timestamp and 200 <= snapshot.http_status <= 299:
            if Path(snapshot.raw_cache_path).is_file():
                _verified_snapshot_body(snapshot)
                return snapshot
    return None


def _fetch_replay(
    fetcher: Fetcher,
    database: CrawlDatabase,
    *,
    crawl_url_id: int,
    record: CdxRecord,
) -> dict[str, Any]:
    cached = _cached_archive_snapshot(database, crawl_url_id, record.timestamp)
    if cached is not None:
        return {
            "crawl_url_id": crawl_url_id,
            "outcome": "cached",
            "snapshot_id": cached.snapshot_id,
            "archive_timestamp": record.timestamp,
            "original_url": record.original_url,
            "raw_cache_path": cached.raw_cache_path,
        }
    replay_url = WaybackReplay().replay_url(record)
    result = fetcher.fetch(
        crawl_url_id,
        request_url=replay_url,
        source_kind="wayback-replay",
        archive_timestamp=record.timestamp,
    )
    payload = _fetch_result(result)
    payload.update(
        {
            "archive_timestamp": record.timestamp,
            "original_url": record.original_url,
            "replay_url": replay_url,
        }
    )
    return payload


def _select_cdx_records(
    records: Sequence[CdxRecord], selection: str
) -> tuple[CdxRecord, ...]:
    if not records:
        return ()
    if selection == "earliest":
        return (records[0],)
    if selection == "latest":
        return (records[-1],)
    if selection == "all":
        return tuple(records)
    raise CliError(f"unsupported CDX capture selection: {selection}")


def command_wayback(args: argparse.Namespace) -> int:
    config = _config(args)
    _prepare_directories(config)
    originals = _observed_urls(args)
    validated = [_validate_card_url(args.source, url) for url in originals]
    cdx = WaybackCdx()
    results: list[dict[str, Any]] = []
    with CrawlDatabase(config.database_path) as database:
        fetcher = Fetcher(config, database)
        for original, card_id in validated:
            card_row = database.discover_url(
                source=args.source,
                url=original,
                source_card_id=card_id,
                discovery_method="explicit_wayback_original_url",
                priority=args.priority,
                metadata={"user_supplied": True},
            )
            if args.timestamp:
                record = CdxRecord(timestamp=args.timestamp, original_url=original)
                results.append(
                    _fetch_replay(
                        fetcher,
                        database,
                        crawl_url_id=card_row.crawl_url_id,
                        record=record,
                    )
                )
                continue

            query_url = cdx.query_url(
                original,
                from_timestamp=args.from_timestamp,
                to_timestamp=args.to_timestamp,
                limit=args.cdx_limit,
                successful_only=True,
                collapse_digest=args.collapse_digest,
            )
            cdx_row = database.discover_url(
                source="wayback_cdx",
                url=query_url,
                discovery_method="exact_cdx_query_for_observed_url",
                priority=args.priority,
                discovered_from=original,
                metadata={"match_type": "exact", "original_url": original},
            )
            cdx_snapshot: PageSnapshot | None = None
            if cdx_row.latest_snapshot_id is not None:
                candidate = database.get_snapshot(cdx_row.latest_snapshot_id)
                if Path(candidate.raw_cache_path).is_file():
                    cdx_snapshot = candidate
            if cdx_snapshot is None:
                cdx_result = fetcher.fetch(
                    cdx_row.crawl_url_id,
                    source_kind="wayback-cdx-exact",
                )
                if cdx_result.snapshot_id is None or cdx_result.raw_cache_path is None:
                    results.append(
                        {
                            "original_url": original,
                            "outcome": "cdx_unavailable",
                            "cdx": _fetch_result(cdx_result),
                        }
                    )
                    continue
                cdx_snapshot = database.get_snapshot(cdx_result.snapshot_id)
            records = tuple(
                record
                for record in cdx.parse(_verified_snapshot_body(cdx_snapshot))
                if canonicalize_url(record.original_url) == original
            )
            selected = _select_cdx_records(records, args.capture)
            if not selected:
                results.append(
                    {
                        "original_url": original,
                        "outcome": "no_exact_cdx_capture",
                        "cdx_query_url": query_url,
                    }
                )
                continue
            for record in selected:
                results.append(
                    _fetch_replay(
                        fetcher,
                        database,
                        crawl_url_id=card_row.crawl_url_id,
                        record=record,
                    )
                )

    _emit(
        {
            "source": args.source,
            "mode": "timestamp" if args.timestamp else "exact_cdx",
            "original_url_count": len(validated),
            "results": results,
            "policy": "EXPLICIT_ORIGINAL_URLS_EXACT_CDX_NO_WILDCARDS",
        }
    )
    return 0


def command_wayback_manifest(args: argparse.Namespace) -> int:
    """Fetch the per-card exact captures recorded in an evidence seed manifest."""

    config = _config(args)
    _prepare_directories(config)
    manifest_path = _absolute_existing_file(args.manifest, "archive seed manifest")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    seeds = payload.get("cards")
    if not isinstance(seeds, list) or not seeds:
        raise CliError("archive seed manifest must contain a nonempty cards list")
    results: list[dict[str, Any]] = []
    with CrawlDatabase(config.database_path) as database:
        fetcher = Fetcher(config, database)
        for item in seeds:
            if not isinstance(item, dict):
                raise CliError("every archive seed must be an object")
            card_id = str(item.get("source_card_id") or "").strip()
            slug = str(item.get("slug") or "").strip()
            timestamp = str(item.get("archive_timestamp") or "").strip()
            if not card_id or not slug or not re.fullmatch(r"\d{14}", timestamp):
                raise CliError(
                    "every archive seed requires source_card_id, slug, and a 14-digit archive_timestamp"
                )
            original = f"http://2kmtcentral.com/17/players/{card_id}/{slug}"
            original, verified_id = _validate_card_url(args.source, original)
            row = database.discover_url(
                source=args.source,
                url=original,
                source_card_id=verified_id,
                discovery_method="evidence_seed_manifest",
                priority=args.priority,
                discovered_from=str(manifest_path),
                metadata={
                    "archive_timestamp": timestamp,
                    "expected_tier": item.get("tier"),
                    "expected_overall": item.get("overall"),
                },
            )
            results.append(
                _fetch_replay(
                    fetcher,
                    database,
                    crawl_url_id=row.crawl_url_id,
                    record=CdxRecord(timestamp=timestamp, original_url=original),
                )
            )
    _emit(
        {
            "source": args.source,
            "manifest": str(manifest_path),
            "seed_count": len(seeds),
            "results": results,
            "policy": "EXACT_EVIDENCE_CAPTURES_RESUMABLE_NO_ID_GENERATION",
        }
    )
    return 0


def _validate_wayback_timestamp(value: object, label: str) -> str | None:
    if value in (None, ""):
        return None
    timestamp = str(value).strip()
    if not re.fullmatch(r"\d{14}", timestamp):
        raise CliError(f"{label} must contain exactly 14 digits")
    return timestamp


def _validate_observed_asset_url(value: object) -> dict[str, str]:
    """Validate one observed 2KMTCentral card image without transforming it."""

    url = str(value or "").strip()
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise CliError(f"invalid observed card-image URL: {url!r}") from error
    if parts.scheme.casefold() not in {"http", "https"}:
        raise CliError("card-image URL must use HTTP or HTTPS")
    if (
        parts.hostname is None
        or parts.hostname.casefold() != "2kmtcentral.com"
        or port is not None
        or parts.username is not None
        or parts.password is not None
    ):
        raise CliError("card-image URL must use the exact 2kmtcentral.com source host")
    if parts.query or parts.fragment:
        raise CliError("card-image URL cannot contain a query string or fragment")
    match = _TWO_K_MTC_CARD_IMAGE.fullmatch(parts.path)
    if match is None:
        raise CliError(
            "card-image URL is outside the observed NBA 2K17 generated-card path"
        )
    return {
        "url": url,
        "source_card_id": match.group("card_id"),
        "asset_token": match.group("asset_token"),
        "revision": match.group("revision"),
        "cache_token": match.group("cache_token"),
    }


def _asset_input_specs(args: argparse.Namespace) -> list[dict[str, Any]]:
    global_timestamp = _validate_wayback_timestamp(
        args.containing_page_timestamp, "--containing-page-timestamp"
    )
    global_snapshot_id = args.containing_page_snapshot_id
    raw_items: list[dict[str, Any]] = [
        {
            "url": url,
            "containing_page_timestamp": global_timestamp,
            "containing_page_snapshot_id": global_snapshot_id,
        }
        for url in (args.url or [])
    ]
    if args.url_file:
        path = _absolute_existing_file(args.url_file, "asset URL file")
        raw_items.extend(
            {
                "url": line.strip(),
                "containing_page_timestamp": global_timestamp,
                "containing_page_snapshot_id": global_snapshot_id,
            }
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
    if args.asset_manifest:
        path = _absolute_existing_file(args.asset_manifest, "asset manifest")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get("assets")
        else:
            items = None
        if not isinstance(items, list):
            raise CliError("asset manifest must be an array or contain an assets array")
        for item in items:
            if not isinstance(item, dict):
                raise CliError("every asset manifest entry must be an object")
            raw_items.append(
                {
                    **item,
                    "url": item.get(
                        "url", item.get("asset_url", item.get("source_url"))
                    ),
                    "containing_page_timestamp": item.get(
                        "containing_page_timestamp",
                        item.get("containing_page_archive_timestamp", global_timestamp),
                    ),
                    "containing_page_snapshot_id": item.get(
                        "containing_page_snapshot_id", global_snapshot_id
                    ),
                }
            )
    if not raw_items:
        raise CliError(
            "at least one explicit --url, --url-file, or --asset-manifest is required"
        )

    by_url: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        validated = _validate_observed_asset_url(item.get("url"))
        supplied_card_id = str(item.get("source_card_id") or "").strip()
        if supplied_card_id and supplied_card_id != validated["source_card_id"]:
            raise CliError(
                f"manifest source_card_id {supplied_card_id!r} does not match "
                f"asset path {validated['source_card_id']!r}"
            )
        timestamp = _validate_wayback_timestamp(
            item.get("containing_page_timestamp"),
            "containing_page_timestamp",
        )
        snapshot_value = item.get("containing_page_snapshot_id")
        snapshot_id = None
        if snapshot_value not in (None, ""):
            try:
                snapshot_id = int(snapshot_value)
            except (TypeError, ValueError) as error:
                raise CliError("containing_page_snapshot_id must be a positive integer") from error
            if snapshot_id < 1:
                raise CliError("containing_page_snapshot_id must be a positive integer")
        spec = {
            **validated,
            "containing_page_timestamp": timestamp,
            "containing_page_snapshot_id": snapshot_id,
            "canonical_card_uuid": (
                str(item.get("canonical_card_uuid") or "").strip() or None
            ),
        }
        existing = by_url.get(validated["url"])
        if existing is not None and existing != spec:
            raise CliError(f"conflicting evidence was supplied for {validated['url']}")
        by_url[validated["url"]] = spec
    return [by_url[url] for url in sorted(by_url)]


def _same_original_asset_url(left: str, right: str) -> bool:
    try:
        left_parts = urlsplit(left)
        right_parts = urlsplit(right)
        left_port = left_parts.port
        right_port = right_parts.port
    except ValueError:
        return False

    def identity(parts: Any, port: int | None) -> tuple[object, ...] | None:
        scheme = parts.scheme.casefold()
        if scheme not in {"http", "https"} or parts.hostname is None:
            return None
        if (scheme, port) in {("http", 80), ("https", 443)}:
            port = None
        return (
            scheme,
            parts.hostname.casefold(),
            port,
            parts.path or "/",
            parts.query,
            parts.fragment,
        )

    left_identity = identity(left_parts, left_port)
    return left_identity is not None and left_identity == identity(right_parts, right_port)


def _verified_snapshot_body(snapshot: PageSnapshot) -> bytes:
    path = Path(snapshot.raw_cache_path)
    if not path.is_file():
        raise CliError(f"cached evidence file is missing: {path}")
    body = path.read_bytes()
    actual = hashlib.sha256(body).hexdigest()
    if actual != snapshot.content_hash:
        raise CliError(
            f"cached evidence hash mismatch for snapshot {snapshot.snapshot_id}: "
            f"expected {snapshot.content_hash}, got {actual}"
        )
    return body


def command_write_asset_manifest(args: argparse.Namespace) -> int:
    """Write exact card-image URLs observed in verified cached main pages."""

    config = _config(args)
    destination = Path(args.output).expanduser().resolve()
    assets_by_card: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []
    scanned = 0
    with CrawlDatabase(config.database_path) as database:
        rows = database.connection.execute(
            """SELECT u.crawl_url_id, u.url, u.source_card_id,
                      u.canonical_card_id, s.snapshot_id, s.archive_timestamp
                 FROM crawl_urls u
                 JOIN page_snapshots s ON s.crawl_url_id=u.crawl_url_id
                WHERE u.source='2kmtcentral'
                  AND u.source_card_id IS NOT NULL
                  AND s.http_status BETWEEN 200 AND 299
                ORDER BY u.source_card_id,
                         COALESCE(s.archive_timestamp, '') DESC,
                         s.snapshot_id DESC"""
        ).fetchall()
        adapter = TwoKMTCentralAdapter()
        for row in rows:
            card_id = str(row["source_card_id"])
            if card_id in assets_by_card:
                continue
            page_url = str(row["url"])
            if re.search(
                r"/(?:attributes|badges|tendencies)/?$",
                urlsplit(page_url).path,
                re.I,
            ):
                continue
            scanned += 1
            try:
                snapshot = database.get_snapshot(int(row["snapshot_id"]))
                body = _verified_snapshot_body(snapshot)
                parsed = adapter.parse_page(
                    SourcePage(page_url, body.decode("utf-8", errors="replace"))
                )
                if not parsed.card_image_url:
                    raise CliError("cached main page contains no observed card-image URL")
                validated = _validate_observed_asset_url(parsed.card_image_url)
                if validated["source_card_id"] != card_id:
                    raise CliError(
                        "observed card-image source ID does not match its cached page"
                    )
                entry: dict[str, Any] = {
                    "source_card_id": card_id,
                    "source_url": validated["url"],
                    "containing_page_snapshot_id": int(row["snapshot_id"]),
                    "containing_page_archive_timestamp": row["archive_timestamp"],
                }
                if row["canonical_card_id"]:
                    entry["canonical_card_uuid"] = str(row["canonical_card_id"])
                assets_by_card[card_id] = entry
            except (CliError, OSError, ValueError) as error:
                errors.append(
                    {
                        "source_card_id": card_id,
                        "page_url": page_url,
                        "snapshot_id": int(row["snapshot_id"]),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )

    def card_order(item: dict[str, Any]) -> tuple[int, int | str]:
        value = str(item["source_card_id"])
        return (0, int(value)) if value.isdigit() else (1, value)

    assets = sorted(assets_by_card.values(), key=card_order)
    payload = {
        "format": "nba2k17-wayback-asset-manifest/v1",
        "source_site": "2kmtcentral",
        "generation_policy": (
            "HASH_VERIFIED_CACHED_MAIN_PAGE_OBSERVATIONS_ONLY_NO_URL_SYNTHESIS"
        ),
        "assets": assets,
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    _emit(
        {
            "output": str(destination),
            "asset_count": len(assets),
            "scanned_main_snapshot_count": scanned,
            "errors": errors,
            "network_requests": 0,
            "policy": payload["generation_policy"],
        }
    )
    return 0


def _observed_asset_context(
    database: CrawlDatabase,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """Prove that the exact input URL occurs in an immutable cached card page."""

    clauses = [
        "u.source='2kmtcentral'",
        "u.source_card_id=?",
        "s.http_status BETWEEN 200 AND 299",
    ]
    parameters: list[object] = [spec["source_card_id"]]
    if spec["containing_page_snapshot_id"] is not None:
        clauses.append("s.snapshot_id=?")
        parameters.append(spec["containing_page_snapshot_id"])
    if spec["containing_page_timestamp"] is not None:
        clauses.append("s.archive_timestamp=?")
        parameters.append(spec["containing_page_timestamp"])
    rows = database.connection.execute(
        """SELECT u.crawl_url_id, u.url, u.source_card_id, u.canonical_card_id,
                  s.snapshot_id, s.archive_timestamp, s.content_hash,
                  s.raw_cache_path
             FROM crawl_urls u JOIN page_snapshots s
               ON s.crawl_url_id=u.crawl_url_id
            WHERE """
        + " AND ".join(clauses)
        + " ORDER BY COALESCE(s.archive_timestamp,'' ) DESC, s.snapshot_id DESC",
        parameters,
    ).fetchall()
    adapter = TwoKMTCentralAdapter()
    checked_snapshot_ids: list[int] = []
    for row in rows:
        page_path = urlsplit(str(row["url"])).path.rstrip("/")
        if re.search(r"/(?:attributes|badges|tendencies)$", page_path, re.I):
            continue
        checked_snapshot_ids.append(int(row["snapshot_id"]))
        snapshot = database.get_snapshot(int(row["snapshot_id"]))
        body = _verified_snapshot_body(snapshot)
        parsed = adapter.parse_page(
            SourcePage(str(row["url"]), body.decode("utf-8", errors="replace"))
        )
        if parsed.card_image_url and _same_original_asset_url(
            parsed.card_image_url, spec["url"]
        ):
            stored_uuid = row["canonical_card_id"]
            supplied_uuid = spec.get("canonical_card_uuid")
            if supplied_uuid and stored_uuid and supplied_uuid != stored_uuid:
                raise CliError(
                    f"manifest canonical_card_uuid {supplied_uuid!r} does not match "
                    f"cached page value {stored_uuid!r}"
                )
            return {
                "page_crawl_url_id": int(row["crawl_url_id"]),
                "page_url": str(row["url"]),
                "page_snapshot_id": int(row["snapshot_id"]),
                "page_archive_timestamp": row["archive_timestamp"],
                "canonical_card_uuid": supplied_uuid or stored_uuid,
            }
    constraint = (
        f" in snapshots {checked_snapshot_ids}"
        if checked_snapshot_ids
        else " in any eligible cached main-page snapshot"
    )
    raise CliError(
        f"asset URL was not observed{constraint}: {spec['url']}"
    )


def _select_asset_capture(
    records: Sequence[CdxRecord],
    *,
    original_url: str,
    containing_page_timestamp: str | None,
) -> tuple[CdxRecord | None, tuple[CdxRecord, ...]]:
    eligible = tuple(
        record
        for record in records
        if _same_original_asset_url(record.original_url, original_url)
        and record.status == 200
        and str(record.mime_type or "").casefold().startswith("image/")
    )
    if not eligible:
        return None, ()
    if containing_page_timestamp is None:
        return max(eligible, key=lambda record: record.timestamp), eligible
    target = datetime.strptime(containing_page_timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    )
    # Prefer the later capture only when two captures are equally distant.
    selected = min(
        eligible,
        key=lambda record: (
            abs(
                (
                    datetime.strptime(record.timestamp, "%Y%m%d%H%M%S").replace(
                        tzinfo=timezone.utc
                    )
                    - target
                ).total_seconds()
            ),
            -int(record.timestamp),
        ),
    )
    return selected, eligible


def _cdx_record_evidence(record: CdxRecord) -> dict[str, Any]:
    return {
        "timestamp": record.timestamp,
        "original_url": record.original_url,
        "status": record.status,
        "mime_type": record.mime_type,
        "digest": record.digest,
        "length": record.length,
    }


def _cached_latest_snapshot(database: CrawlDatabase, crawl_url_id: int) -> PageSnapshot | None:
    row = database.get_url(crawl_url_id)
    if row.latest_snapshot_id is None:
        return None
    snapshot = database.get_snapshot(row.latest_snapshot_id)
    if not 200 <= snapshot.http_status <= 299:
        return None
    _verified_snapshot_body(snapshot)
    return snapshot


def _cached_asset_fetch_result(
    database: CrawlDatabase,
    *,
    crawl_url_id: int,
    replay_url: str,
    timestamp: str,
) -> FetchResult | None:
    snapshot = _cached_archive_snapshot(database, crawl_url_id, timestamp)
    if snapshot is None:
        return None
    _verified_snapshot_body(snapshot)
    return FetchResult(
        crawl_url_id=crawl_url_id,
        request_url=replay_url,
        outcome="cached",
        http_status=snapshot.http_status,
        snapshot_id=snapshot.snapshot_id,
        content_hash=snapshot.content_hash,
        raw_cache_path=snapshot.raw_cache_path,
    )


def command_wayback_assets(args: argparse.Namespace) -> int:
    """Fetch only card-art URLs proven to occur in cached source HTML."""

    config = _config(args)
    _prepare_directories(config)
    specs = _asset_input_specs(args)
    placeholder_hashes = tuple(
        str(value).strip().casefold() for value in (args.known_placeholder_hash or [])
    )
    if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in placeholder_hashes):
        raise CliError("--known-placeholder-hash values must be 64 hexadecimal digits")
    report_path = (
        Path(args.report_output).expanduser().resolve()
        if args.report_output
        else (config.reports_root / "pilot_asset_validation.json").resolve()
    )

    results: list[dict[str, Any]] = []
    report_records: list[AssetReportRecord] = []
    operations: dict[str, dict[str, Any]] = {}
    with CrawlDatabase(config.database_path) as database:
        # Complete the evidence preflight before the first possible request.
        contexts = [
            (spec, _observed_asset_context(database, spec)) for spec in specs
        ]
        attempts_before = database.table_count("fetch_attempts")
        fetcher = Fetcher(config, database)
        cdx = WaybackCdx()
        replay = WaybackReplay()
        for spec, context in contexts:
            asset_id = (
                f"2kmtcentral:{spec['source_card_id']}:{spec['asset_token']}"
            )
            asset_row = database.discover_url(
                source="2kmtcentral_asset",
                url=spec["url"],
                source_card_id=spec["source_card_id"],
                discovery_method="observed_cached_card_image",
                priority=args.priority,
                discovered_from=context["page_url"],
                metadata={
                    "asset_token": spec["asset_token"],
                    "revision": spec["revision"],
                    "cache_token": spec["cache_token"],
                    "containing_page_snapshot_id": context["page_snapshot_id"],
                    "input_was_explicit": True,
                },
            )
            query_url = cdx.query_url(
                spec["url"],
                from_timestamp=ASSET_CDX_FROM,
                to_timestamp=ASSET_CDX_TO,
                limit=args.cdx_limit,
                successful_only=True,
                collapse_digest=True,
            )
            cdx_row = database.discover_url(
                source="wayback_cdx",
                url=query_url,
                discovery_method="exact_cdx_query_for_observed_card_image",
                priority=args.priority,
                discovered_from=spec["url"],
                metadata={
                    "match_type": "exact",
                    "from": ASSET_CDX_FROM,
                    "to": ASSET_CDX_TO,
                    "original_url": spec["url"],
                },
            )
            cdx_snapshot = _cached_latest_snapshot(database, cdx_row.crawl_url_id)
            cdx_fetch: dict[str, Any]
            if cdx_snapshot is None:
                fetched_cdx = fetcher.fetch(
                    cdx_row.crawl_url_id,
                    source_kind="wayback-cdx-asset-exact",
                )
                cdx_fetch = _fetch_result(fetched_cdx)
                if fetched_cdx.snapshot_id is None or not (
                    fetched_cdx.http_status is not None
                    and 200 <= fetched_cdx.http_status <= 299
                ):
                    outcome = {
                        "asset_id": asset_id,
                        "source_card_id": spec["source_card_id"],
                        "observed_url": spec["url"],
                        "outcome": "cdx_unavailable",
                        "cdx_query_url": query_url,
                        "cdx_fetch": cdx_fetch,
                    }
                    results.append(outcome)
                    database.upsert_asset(
                        crawl_url_id=context["page_crawl_url_id"],
                        asset_url=spec["url"],
                        asset_kind="card_image",
                        fetch_status="cdx_unavailable",
                        metadata={**outcome, **context, "asset_crawl_url_id": asset_row.crawl_url_id},
                    )
                    continue
                cdx_snapshot = database.get_snapshot(fetched_cdx.snapshot_id)
            else:
                cdx_fetch = {
                    "outcome": "cached",
                    "snapshot_id": cdx_snapshot.snapshot_id,
                    "raw_cache_path": cdx_snapshot.raw_cache_path,
                }
            cdx_records = cdx.parse(_verified_snapshot_body(cdx_snapshot))
            selected, eligible = _select_asset_capture(
                cdx_records,
                original_url=spec["url"],
                containing_page_timestamp=spec["containing_page_timestamp"],
            )
            selection_basis = (
                "nearest_containing_page_timestamp_later_tie_break"
                if spec["containing_page_timestamp"] is not None
                else "latest_exact_capture"
            )
            if selected is None:
                outcome = {
                    "asset_id": asset_id,
                    "source_card_id": spec["source_card_id"],
                    "observed_url": spec["url"],
                    "outcome": "no_exact_200_image_capture",
                    "cdx_query_url": query_url,
                    "cdx_fetch": cdx_fetch,
                    "cdx_record_count": len(cdx_records),
                    "eligible_capture_count": 0,
                    "selection_basis": selection_basis,
                }
                results.append(outcome)
                database.upsert_asset(
                    crawl_url_id=context["page_crawl_url_id"],
                    asset_url=spec["url"],
                    asset_kind="card_image",
                    fetch_status="archive_missing",
                    metadata={**outcome, **context, "asset_crawl_url_id": asset_row.crawl_url_id},
                )
                continue

            replay_url = replay.replay_url(selected)
            fetched_asset = _cached_asset_fetch_result(
                database,
                crawl_url_id=asset_row.crawl_url_id,
                replay_url=replay_url,
                timestamp=selected.timestamp,
            )
            if fetched_asset is None:
                fetched_asset = fetcher.fetch(
                    asset_row.crawl_url_id,
                    request_url=replay_url,
                    source_kind="wayback-card-art",
                    archive_timestamp=selected.timestamp,
                )
            fetch_evidence = _fetch_result(fetched_asset)
            outcome = {
                "asset_id": asset_id,
                "source_card_id": spec["source_card_id"],
                "observed_url": spec["url"],
                "outcome": fetched_asset.outcome,
                "containing_page_snapshot_id": context["page_snapshot_id"],
                "containing_page_timestamp": spec["containing_page_timestamp"],
                "cdx_query_url": query_url,
                "cdx_fetch": cdx_fetch,
                "cdx_record_count": len(cdx_records),
                "eligible_capture_count": len(eligible),
                "selection_basis": selection_basis,
                "selected_capture": _cdx_record_evidence(selected),
                "replay_url": replay_url,
                "fetch": fetch_evidence,
            }
            results.append(outcome)
            operations[asset_id] = {
                "spec": spec,
                "context": context,
                "asset_crawl_url_id": asset_row.crawl_url_id,
                "selected": selected,
                "fetch_result": fetched_asset,
                "outcome": outcome,
            }
            if fetched_asset.raw_cache_path and Path(fetched_asset.raw_cache_path).is_file():
                report_records.append(
                    AssetReportRecord.from_file(
                        asset_id,
                        fetched_asset.raw_cache_path,
                        filename=urlsplit(spec["url"]).path.rsplit("/", 1)[-1],
                        source_url=spec["url"],
                        source_card_id=spec["source_card_id"],
                        canonical_card_uuid=context["canonical_card_uuid"],
                    )
                )

        report = build_asset_validation_report(
            report_records,
            known_placeholder_hashes=placeholder_hashes,
        )
        report["requested_asset_count"] = len(specs)
        report["capture_policy"] = {
            "source": "explicit_observed_2kmtcentral_card_image_only",
            "cdx_match": "exact",
            "cdx_from": ASSET_CDX_FROM,
            "cdx_to": ASSET_CDX_TO,
            "eligible_status": 200,
            "eligible_mime_prefix": "image/",
            "replay_modifier": "id_",
            "direct_source_host_requests": 0,
        }
        report_assets = {
            str(item["asset_id"]): item for item in report["assets"]
        }
        for asset_id, operation in operations.items():
            validation = report_assets.get(asset_id)
            fetched_asset: FetchResult = operation["fetch_result"]
            if validation is not None:
                validation["fetcher_content_hash"] = fetched_asset.content_hash
                validation["fetcher_hash_matches"] = (
                    fetched_asset.content_hash is not None
                    and validation["sha256"] == fetched_asset.content_hash
                )
                validation["archive_evidence"] = {
                    "containing_page_snapshot_id": operation["context"]["page_snapshot_id"],
                    "containing_page_archive_timestamp": operation["context"]["page_archive_timestamp"],
                    "selection_basis": operation["outcome"]["selection_basis"],
                    "selected_capture": operation["outcome"]["selected_capture"],
                    "replay_url": operation["outcome"]["replay_url"],
                }
            selected: CdxRecord = operation["selected"]
            captured_at = datetime.strptime(selected.timestamp, "%Y%m%d%H%M%S").replace(
                tzinfo=timezone.utc
            ).timestamp()
            database.upsert_asset(
                crawl_url_id=operation["context"]["page_crawl_url_id"],
                snapshot_id=fetched_asset.snapshot_id,
                asset_url=operation["spec"]["url"],
                asset_kind="card_image",
                fetch_status=fetched_asset.outcome,
                content_hash=fetched_asset.content_hash,
                raw_cache_path=fetched_asset.raw_cache_path,
                http_status=fetched_asset.http_status,
                captured_at=captured_at,
                metadata={
                    **operation["outcome"],
                    **operation["context"],
                    "asset_crawl_url_id": operation["asset_crawl_url_id"],
                    "validation": validation,
                },
            )
        attempts_after = database.table_count("fetch_attempts")

    written_report = write_asset_validation_report(report, output_path=report_path)
    _emit(
        {
            "requested_asset_count": len(specs),
            "validated_evidence_count": report["summary"]["total_assets"],
            "network_requests": attempts_after - attempts_before,
            "report": written_report,
            "report_summary": report["summary"],
            "results": results,
            "policy": "OBSERVED_ASSETS_EXACT_CDX_ARCHIVE_REPLAY_ONLY_NO_SOURCE_REWRITE",
        }
    )
    return 0


def _iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


def _parse_snapshot_body(
    body: bytes,
    snapshot: PageSnapshot,
    *,
    source: str,
    original_url: str,
    crosswalk: Path | None,
) -> ParseOutcome:
    raw = _adapter(source).parse_page(
        SourcePage(
            original_url,
            body.decode("utf-8", errors="replace"),
            captured_at=_iso_time(snapshot.captured_at),
            content_hash=snapshot.content_hash,
            provenance=(
                f"WAYBACK_CAPTURE_{snapshot.archive_timestamp}"
                if snapshot.archive_timestamp
                else "SAVED_SOURCE_SNAPSHOT"
            ),
        )
    )
    parsed = raw.to_dict()
    parsed.update(
        {
            "snapshot_sha256": snapshot.content_hash,
            "captured_at": _iso_time(snapshot.captured_at),
            "archive_timestamp": snapshot.archive_timestamp,
            "parser_version": ADAPTER_PARSER_VERSION,
        }
    )
    card = normalize_parsed_card(parsed, parity_crosswalk=crosswalk)
    adequate, inadequate = is_adequate(card)
    update_completeness(card)
    assets = []
    for kind, url in (
        ("card_image", card.card_art.values.get("card_image_url")),
        ("portrait", card.card_art.values.get("portrait_url")),
        ("cutout", card.card_art.values.get("cutout_url")),
    ):
        if isinstance(url, str) and url:
            assets.append({"url": url, "kind": kind})
    return ParseOutcome(
        status="complete" if adequate else "partial",
        result=card.to_dict(),
        canonical_card_id=card.canonical_card_uuid,
        missing_fields=tuple(sorted(set(raw.missing_fields + inadequate))),
        assets=tuple(assets),
        manual_review_status="unreviewed" if adequate else "required",
    )


def _snapshot_rows(
    database: CrawlDatabase,
    *,
    source: str | None,
    snapshot_ids: Sequence[int],
) -> list[Any]:
    clauses = ["u.source IN ('2kmtcentral','mtdb')", "s.http_status BETWEEN 200 AND 299"]
    parameters: list[Any] = []
    if source:
        clauses.append("u.source=?")
        parameters.append(source)
    if snapshot_ids:
        clauses.append("s.snapshot_id IN (%s)" % ",".join("?" for _ in snapshot_ids))
        parameters.extend(snapshot_ids)
    query = (
        "SELECT s.snapshot_id, u.source, u.url FROM page_snapshots s "
        "JOIN crawl_urls u ON u.crawl_url_id=s.crawl_url_id WHERE "
        + " AND ".join(clauses)
        + " ORDER BY s.snapshot_id"
    )
    return list(database.connection.execute(query, parameters).fetchall())


def command_reparse(args: argparse.Namespace) -> int:
    config = _config(args)
    crosswalk = _optional_file(args.crosswalk, "schema crosswalk")
    results: list[dict[str, Any]] = []
    with CrawlDatabase(config.database_path) as database:
        fetcher = Fetcher(config, database)
        rows = _snapshot_rows(
            database,
            source=None if args.source == "all" else args.source,
            snapshot_ids=args.snapshot_id or (),
        )
        for row in rows:
            source = str(row["source"])
            original_url = str(row["url"])

            def parser(body: bytes, snapshot: PageSnapshot, *, _source=source, _url=original_url):
                return _parse_snapshot_body(
                    body,
                    snapshot,
                    source=_source,
                    original_url=_url,
                    crosswalk=crosswalk,
                )

            outcome = fetcher.reparse_cached(
                int(row["snapshot_id"]),
                parser_version=ADAPTER_PARSER_VERSION,
                parser=parser,
                force=args.force,
            )
            results.append(
                {
                    "snapshot_id": int(row["snapshot_id"]),
                    "source": source,
                    "status": outcome.status,
                    "canonical_card_id": outcome.canonical_card_id,
                    "missing_fields": list(outcome.missing_fields),
                    "error": outcome.error,
                }
            )
    _emit(
        {
            "snapshot_count": len(results),
            "parser_version": ADAPTER_PARSER_VERSION,
            "network_requests": 0,
            "results": results,
        }
    )
    return 0


def _page_order(url: str) -> tuple[int, str]:
    path = urlsplit(url).path.rstrip("/").casefold()
    for rank, suffix in enumerate(("/attributes", "/badges", "/tendencies"), start=1):
        if path.endswith(suffix):
            return rank, url
    return 0, url


def _latest_cached_rows(database: CrawlDatabase, sources: Sequence[str]) -> list[Any]:
    placeholders = ",".join("?" for _ in sources)
    return list(
        database.connection.execute(
            f"""SELECT u.source, u.source_card_id, u.url, s.snapshot_id,
                       s.captured_at, s.archive_timestamp, s.content_hash,
                       s.raw_cache_path, s.source_kind
                  FROM crawl_urls u
                  JOIN page_snapshots s ON s.snapshot_id=u.latest_snapshot_id
                 WHERE u.source IN ({placeholders})
                   AND s.http_status BETWEEN 200 AND 299
                 ORDER BY u.source, u.source_card_id, u.url""",
            tuple(sources),
        ).fetchall()
    )


def _load_cached_cards(
    database: CrawlDatabase,
    *,
    sources: Sequence[str],
    crosswalk: Path | None,
) -> tuple[list[CanonicalCard], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[tuple[SourcePage, Any]]] = {}
    errors: list[dict[str, Any]] = []
    for row in _latest_cached_rows(database, sources):
        # Prefix-discovery index evidence is intentionally cached too, but it
        # is not a card page and must never be presented to the card adapter.
        if row["source"] == "2kmtcentral" and not str(row["source_card_id"] or "").isdigit():
            continue
        path = Path(str(row["raw_cache_path"]))
        try:
            body = path.read_bytes()
            digest = hashlib.sha256(body).hexdigest()
            if digest != row["content_hash"]:
                raise ValueError(
                    f"cache hash mismatch: expected {row['content_hash']}, got {digest}"
                )
            page = SourcePage(
                str(row["url"]),
                body.decode("utf-8", errors="replace"),
                captured_at=_iso_time(float(row["captured_at"])),
                content_hash=str(row["content_hash"]),
                provenance=(
                    f"WAYBACK_CAPTURE_{row['archive_timestamp']}"
                    if row["archive_timestamp"]
                    else str(row["source_kind"])
                ),
            )
            key = (str(row["source"]), str(row["source_card_id"] or row["url"]))
            grouped.setdefault(key, []).append((page, row))
        except Exception as error:
            errors.append(
                {
                    "snapshot_id": int(row["snapshot_id"]),
                    "source": row["source"],
                    "source_card_id": row["source_card_id"],
                    "url": row["url"],
                    "error": f"{type(error).__name__}: {error}",
                }
            )

    cards: list[CanonicalCard] = []
    for (source, source_card_id), page_rows in sorted(grouped.items()):
        page_rows.sort(key=lambda item: _page_order(item[0].url))
        try:
            raw = _adapter(source).parse_pages(item[0] for item in page_rows)
            parsed = raw.to_dict()
            first_row = page_rows[0][1]
            parsed.update(
                {
                    "snapshot_sha256": first_row["content_hash"],
                    "captured_at": _iso_time(float(first_row["captured_at"])),
                    "archive_timestamp": first_row["archive_timestamp"],
                    "parser_version": ADAPTER_PARSER_VERSION,
                }
            )
            card = normalize_parsed_card(parsed, parity_crosswalk=crosswalk)
            update_completeness(card)
            cards.append(card)
        except Exception as error:
            errors.append(
                {
                    "source": source,
                    "source_card_id": source_card_id,
                    "snapshot_ids": [int(item[1]["snapshot_id"]) for item in page_rows],
                    "error": f"{type(error).__name__}: {error}",
                }
            )
    cards.sort(key=lambda card: card.canonical_card_uuid)
    return cards, errors


def _force_crosswalk_blocker(card: CanonicalCard) -> None:
    profile = card.injection
    blockers = sorted(set(profile.get("blockers", []) + ["crosswalk_not_supplied"]))
    profile["blockers"] = blockers
    profile["unresolved_mappings"] = sorted(
        set(profile.get("unresolved_mappings", []) + ["nba2k17_schema_crosswalk"])
    )
    profile["safe_to_inject"] = False
    profile["write_behavior"] = "DATA_ONLY_NO_GAME_MEMORY_WRITE"
    profile.pop("profile_sha256", None)
    profile["profile_sha256"] = sha256_json(profile)


def _build_export(
    args: argparse.Namespace,
    *,
    pilot: bool,
) -> dict[str, Any]:
    config = _config(args)
    _prepare_directories(config)
    crosswalk = _optional_file(args.crosswalk, "schema crosswalk")
    roster_path = _optional_file(args.roster_catalog, "native roster catalog")
    seed_manifest_path = _optional_file(
        getattr(args, "seed_manifest", None), "pilot seed manifest"
    )
    output_root = (
        Path(args.output).expanduser().resolve()
        if args.output
        else config.normalized_root
    )
    report_root = (
        Path(args.report_output).expanduser().resolve()
        if args.report_output
        else config.reports_root
    )
    with CrawlDatabase(config.database_path) as database:
        primary, primary_errors = _load_cached_cards(
            database, sources=("2kmtcentral",), crosswalk=crosswalk
        )
        if pilot:
            if seed_manifest_path is not None:
                seed_payload = json.loads(seed_manifest_path.read_text(encoding="utf-8"))
                seed_ids = [
                    str(item["source_card_id"])
                    for item in seed_payload.get("cards", [])
                    if isinstance(item, dict) and item.get("source_card_id") is not None
                ]
                seed_id_set = set(seed_ids)
                # A bounded pilot reports only failures belonging to its
                # explicit seed set.  Cached index pages and unrelated cards
                # remain durable evidence but are not pilot parser errors.
                primary_errors = [
                    error
                    for error in primary_errors
                    if str(error.get("source_card_id") or "") in seed_id_set
                ]
                by_source_id = {
                    str(card.source.get("source_card_id")): card for card in primary
                }
                missing_seed_ids = [item for item in seed_ids if item not in by_source_id]
                primary = [by_source_id[item] for item in seed_ids if item in by_source_id]
                if missing_seed_ids:
                    primary_errors.append(
                        {
                            "kind": "PILOT_SEEDS_WITHOUT_CACHED_CARD",
                            "source_card_ids": missing_seed_ids,
                        }
                    )
                expectations = {
                    str(item["source_card_id"]): item
                    for item in seed_payload.get("cards", [])
                    if isinstance(item, dict) and item.get("source_card_id") is not None
                }
                for card in primary:
                    source_id = str(card.source.get("source_card_id"))
                    expected = expectations[source_id]
                    actual = card.identity.values
                    mismatches = {}
                    for field in ("overall", "tier"):
                        expected_value = expected.get(field)
                        actual_value = actual.get(field)
                        if expected_value is not None and str(actual_value).casefold() != str(expected_value).casefold():
                            mismatches[field] = {
                                "expected": expected_value,
                                "actual": actual_value,
                            }
                    if expected.get("name") and normalize_name(str(actual.get("name") or "")) != normalize_name(str(expected["name"])):
                        mismatches["name"] = {
                            "expected": expected["name"],
                            "actual": actual.get("name"),
                        }
                    if mismatches:
                        primary_errors.append(
                            {
                                "kind": "PILOT_SEED_EXPECTATION_MISMATCH",
                                "source_card_id": source_id,
                                "mismatches": mismatches,
                            }
                        )
            primary = primary[: args.limit]
        fallback: list[CanonicalCard] = []
        fallback_errors: list[dict[str, Any]] = []
        if args.include_mtdb_fallback:
            fallback, fallback_errors = _load_cached_cards(
                database, sources=("mtdb",), crosswalk=crosswalk
            )
            if pilot:
                selected_names = {
                    normalize_name(str(card.identity.values.get("name") or ""))
                    for card in primary
                }
                fallback = [
                    card
                    for card in fallback
                    if normalize_name(str(card.identity.values.get("name") or ""))
                    in selected_names
                ]
        cards, reconciliation_reports = reconcile_catalogs(primary, fallback)
        cards = curate_catalog(cards)
        crawl_stats = _database_status(database)
        rate_limit_events = [
            {
                "attempt_id": int(row["attempt_id"]),
                "url": str(row["url"]),
                "outcome": str(row["outcome"]),
                "http_status": row["http_status"],
                "retry_after": row["retry_after_value"],
                "error": row["error"],
            }
            for row in database.connection.execute(
                """SELECT a.attempt_id, u.url, a.outcome, a.http_status,
                          a.retry_after_value, a.error
                     FROM fetch_attempts a
                     JOIN crawl_urls u ON u.crawl_url_id=a.crawl_url_id
                    WHERE a.outcome NOT IN ('success','not_modified','started')
                    ORDER BY a.attempt_id"""
            ).fetchall()
        ]

    roster_players = []
    if roster_path is not None:
        roster_players = load_native_roster_catalog(roster_path)
        for card in cards:
            apply_roster_match(card, match_card_to_roster(card, roster_players))
    else:
        for card in cards:
            apply_roster_match(card, match_card_to_roster(card, ()))

    for card in cards:
        build_injection_profile(card, crosswalk_path=crosswalk)
        if crosswalk is None:
            _force_crosswalk_blocker(card)
        update_completeness(card)

    # The permanent ledger is refreshed from the same normalized objects that
    # feed the desktop catalog.  Validated image paths are linked here before
    # cards.json/cards.sqlite are written.
    with CrawlDatabase(config.database_path) as database:
        ledger = refresh_completeness_ledger(
            database,
            cards,
            report_root=report_root,
        )

    manifest = export_catalog(
        cards,
        output_root=output_root,
        roster_players=roster_players,
        completeness_rows=ledger["rows"],
    )
    parser_errors = primary_errors + fallback_errors
    parity_path = None
    if crosswalk is not None:
        parity_path = report_root / "parity_report.json"
        write_parity_report(
            cards,
            crosswalk_path=crosswalk,
            output_path=parity_path,
        )
    report = build_pilot_report(
        cards,
        crawl_stats=crawl_stats,
        parser_errors=parser_errors,
        rate_limit_events=rate_limit_events,
        discovered_count=(
            len(primary)
            if pilot and seed_manifest_path is not None
            else crawl_stats["sources"].get("2kmtcentral", 0)
        ),
        estimated_full_card_count=args.estimated_full_card_count,
    )
    report_paths = write_pilot_report(report, output_directory=report_root)
    return {
        "mode": "pilot" if pilot else "full_export",
        "output_root": str(output_root),
        "report_paths": report_paths,
        "parity_report": None if parity_path is None else str(parity_path),
        "manifest": manifest,
        "primary_cached_card_count": len(primary),
        "fallback_cached_card_count": len(fallback),
        "parser_error_count": len(parser_errors),
        "reconciliation_report_count": len(reconciliation_reports),
        "recovery_counts": ledger["counts"],
        "network_requests": 0,
        "memory_writes": 0,
        "write_behavior": "OFFLINE_DATA_EXPORT_ONLY",
    }


def command_build_pilot(args: argparse.Namespace) -> int:
    _emit(_build_export(args, pilot=True))
    return 0


def command_export(args: argparse.Namespace) -> int:
    _emit(_build_export(args, pilot=False))
    return 0


def command_ledger(args: argparse.Namespace) -> int:
    config = _config(args)
    _prepare_directories(config)
    cards_path = (
        Path(args.cards).expanduser().resolve()
        if args.cards else config.normalized_root / "cards.json"
    )
    cards: list[CanonicalCard] = []
    if cards_path.is_file():
        payload = json.loads(cards_path.read_text(encoding="utf-8"))
        cards = [CanonicalCard.from_dict(item) for item in payload.get("cards", [])]
    report_root = (
        Path(args.report_output).expanduser().resolve()
        if args.report_output else config.reports_root
    )
    with CrawlDatabase(config.database_path) as database:
        result = refresh_completeness_ledger(database, cards, report_root=report_root)
    _emit({
        "mode": "ledger_refresh",
        "cards_input": str(cards_path),
        "report_root": str(report_root),
        "counts": result["counts"],
        "network_requests": 0,
    })
    return 0


def _fallback_needed(row: Any, primary_cards: Sequence[CanonicalCard]) -> bool:
    slug = unquote(urlsplit(str(row.url)).path.rstrip("/").rsplit("/", 1)[-1])
    slug_name = normalize_name(re.sub(r"[-_]+", " ", slug))
    same_name = [
        card
        for card in primary_cards
        if normalize_name(str(card.identity.values.get("name") or "")) == slug_name
    ]
    if not same_name:
        return True
    return any(not is_adequate(card)[0] for card in same_name)


def _unresolved_primary_count(database: CrawlDatabase) -> int:
    row = database.connection.execute(
        """SELECT COUNT(*) AS count FROM crawl_urls
             WHERE source='2kmtcentral'
               AND fetch_status IN ('pending','retry','blocked','fetching')"""
    ).fetchone()
    return int(row["count"])


def command_full_crawl(args: argparse.Namespace) -> int:
    """Resume queued URLs without ever scheduling MTDB ahead of 2KMTCentral."""

    config = _config(args)
    _prepare_directories(config)
    remaining = args.max_requests
    primary_results: list[FetchResult] = []
    fallback_results: list[FetchResult] = []
    skipped_fallback: list[dict[str, Any]] = []
    primary_errors: list[dict[str, Any]] = []
    with CrawlDatabase(config.database_path) as database:
        fetcher = Fetcher(config, database)
        for row in database.list_ready(source="2kmtcentral", limit=remaining):
            primary_results.append(fetcher.fetch(row.crawl_url_id))
            remaining -= 1
            if remaining <= 0:
                break

        unresolved_primary = _unresolved_primary_count(database)
        primary_cards: list[CanonicalCard] = []
        if remaining > 0 and unresolved_primary == 0:
            primary_cards, primary_errors = _load_cached_cards(
                database, sources=("2kmtcentral",), crosswalk=None
            )
            fallback_scan_limit = max(1, database.table_count("crawl_urls"))
            for row in database.list_ready(source="mtdb", limit=fallback_scan_limit):
                if remaining <= 0:
                    break
                if not _fallback_needed(row, primary_cards):
                    skipped_fallback.append(
                        {
                            "crawl_url_id": row.crawl_url_id,
                            "url": row.url,
                            "reason": "adequate_2kmtcentral_primary_exists",
                        }
                    )
                    continue
                fallback_results.append(fetcher.fetch(row.crawl_url_id))
                remaining -= 1

    _emit(
        {
            "requested_limit": args.max_requests,
            "processed_count": len(primary_results) + len(fallback_results),
            "source_order": list(SOURCE_ORDER),
            "primary_results": [_fetch_result(item) for item in primary_results],
            "fallback_results": [_fetch_result(item) for item in fallback_results],
            "fallback_skipped": skipped_fallback,
            "unresolved_primary_count": unresolved_primary,
            "primary_parse_error_count": len(primary_errors),
            "fallback_gate": "MTDB_ONLY_WHEN_2KMTC_PRIMARY_INADEQUATE_OR_MISSING",
            "rate_limit_policy": "CONSERVATIVE_DURABLE_DEFAULTS",
        }
    )
    return 0


def _add_fetch_parser(subparsers: Any, name: str, *, aliases: Sequence[str] = ()) -> None:
    parser = subparsers.add_parser(
        name,
        aliases=list(aliases),
        help="resume queued URLs with the durable conservative rate limiter",
    )
    parser.add_argument(
        "--source",
        choices=("2kmtcentral", "mtdb", "all"),
        default="2kmtcentral",
    )
    parser.add_argument("--limit", type=_positive_int, default=100)
    parser.set_defaults(handler=command_fetch)


def _add_export_arguments(parser: argparse.ArgumentParser, *, pilot: bool) -> None:
    parser.add_argument("--output", help="normalized export directory")
    parser.add_argument("--report-output", help="pilot report directory")
    parser.add_argument("--roster-catalog", help="optional NBA 2K17 native roster JSON")
    parser.add_argument("--crosswalk", help="optional NBA 2K17 schema crosswalk JSON")
    parser.add_argument(
        "--include-mtdb-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="use cached MTDB only through conservative reconciliation",
    )
    parser.add_argument("--estimated-full-card-count", type=_positive_int)
    if pilot:
        parser.add_argument("--limit", type=_positive_int, default=40)
        parser.add_argument(
            "--seed-manifest",
            help="optional evidence-backed pilot seed manifest selecting source IDs",
        )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be a positive integer")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m nba2k17_scraper",
        description=(
            "Resumable NBA 2K17 MyTEAM card preservation pipeline. "
            "This tool only crawls observed URLs and produces offline data; "
            "it never writes game memory."
        ),
    )
    parser.add_argument(
        "--data-root",
        default=str(DEFAULT_DATA_ROOT),
        help="durable crawl/cache root (default: .project/data/nba2k17)",
    )
    parser.add_argument('--request-delay-seconds', type=float, default=None,
                        help='minimum request spacing; retains server cooldowns and single-request operation')
    parser.add_argument('--request-jitter-seconds', type=float, default=0,
                        help='random additional spacing from zero to this many seconds')
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="initialize the durable data root")
    init.set_defaults(handler=command_init)

    status = subparsers.add_parser("status", help="show durable crawl state")
    status.set_defaults(handler=command_status)

    ledger = subparsers.add_parser(
        "ledger",
        aliases=("progress",),
        help="refresh the permanent completeness and MTDB fallback ledger",
    )
    ledger.add_argument("--cards", help="normalized cards.json (default: data root export)")
    ledger.add_argument("--report-output", help="ledger report directory")
    ledger.set_defaults(handler=command_ledger)

    discover = subparsers.add_parser(
        "discover", help="queue explicit card URLs or links observed in a local index"
    )
    discover.add_argument("--source", choices=SOURCE_ORDER, required=True)
    discover.add_argument("--seed-url", action="append", help="explicitly observed card URL")
    discover.add_argument("--index-file", help="saved HTML/XML/JSON/text index")
    discover.add_argument("--index-url", help="original URL of the saved index")
    discover.add_argument("--content-type", default="text/html")
    discover.add_argument("--priority", type=int, default=0)
    discover.set_defaults(handler=command_discover)

    prefix = subparsers.add_parser(
        "discover-cdx-prefix",
        help="discover observed card URLs from one path-bounded Wayback CDX index",
    )
    prefix.add_argument("--source", choices=SOURCE_ORDER, required=True)
    prefix.add_argument("--prefix-url", required=True)
    prefix.add_argument("--from-timestamp")
    prefix.add_argument("--to-timestamp")
    prefix.add_argument("--cdx-limit", type=_positive_int)
    prefix.add_argument("--priority", type=int, default=50)
    prefix.set_defaults(handler=command_discover_cdx_prefix)

    url_list = subparsers.add_parser(
        "write-url-list", help="write deduplicated observed card URLs for resumable archive crawling"
    )
    url_list.add_argument("--source", choices=SOURCE_ORDER, required=True)
    url_list.add_argument("--output", required=True)
    url_list.add_argument(
        "--scope",
        choices=("main", "subpages", "all"),
        default="main",
        help="select main cards, observed detail subpages, or all queued card URLs",
    )
    url_list.set_defaults(handler=command_write_urls)

    subpages = subparsers.add_parser(
        "discover-cached-subpages",
        help="queue badge/tendency links observed in cached main-card HTML",
    )
    subpages.add_argument("--source", choices=SOURCE_ORDER, required=True)
    subpages.add_argument(
        "--kind",
        action="append",
        choices=("badges", "tendencies"),
        help="detail kind to queue (repeatable; defaults to both)",
    )
    subpages.add_argument("--priority", type=int, default=40)
    subpages.set_defaults(handler=command_discover_cached_subpages)

    _add_fetch_parser(subparsers, "fetch", aliases=("resume",))

    wayback = subparsers.add_parser(
        "wayback",
        aliases=("fetch-wayback",),
        help="fetch explicit original card URLs from an exact Wayback capture",
    )
    wayback.add_argument("--source", choices=SOURCE_ORDER, default="2kmtcentral")
    wayback.add_argument("--url", action="append", help="known original card URL")
    wayback.add_argument("--url-file", help="UTF-8 file containing known original URLs")
    capture_mode = wayback.add_mutually_exclusive_group(required=True)
    capture_mode.add_argument("--timestamp", help="exact 14-digit Wayback timestamp")
    capture_mode.add_argument(
        "--cdx", action="store_true", help="query CDX with matchType=exact"
    )
    wayback.add_argument("--capture", choices=("latest", "earliest", "all"), default="latest")
    wayback.add_argument("--from-timestamp")
    wayback.add_argument("--to-timestamp")
    wayback.add_argument("--cdx-limit", type=_positive_int, default=20)
    wayback.add_argument("--collapse-digest", action="store_true")
    wayback.add_argument("--priority", type=int, default=10)
    wayback.set_defaults(handler=command_wayback)

    manifest_fetch = subparsers.add_parser(
        "wayback-manifest",
        help="fetch exact per-card Wayback captures from an evidence seed manifest",
    )
    manifest_fetch.add_argument(
        "--source", choices=("2kmtcentral",), default="2kmtcentral"
    )
    manifest_fetch.add_argument("--manifest", required=True)
    manifest_fetch.add_argument("--priority", type=int, default=90)
    manifest_fetch.set_defaults(handler=command_wayback_manifest)

    asset_manifest = subparsers.add_parser(
        "write-asset-manifest",
        help="write image URLs observed in hash-verified cached 2K17 card pages",
    )
    asset_manifest.add_argument("--output", required=True)
    asset_manifest.set_defaults(handler=command_write_asset_manifest)

    assets = subparsers.add_parser(
        "wayback-assets",
        help="fetch explicitly observed 2K17 card images through exact Wayback captures",
    )
    assets.add_argument("--url", action="append", help="observed original card-image URL")
    assets.add_argument("--url-file", help="UTF-8 file of observed card-image URLs")
    assets.add_argument(
        "--asset-manifest",
        help=(
            "JSON assets array with source_url/source_card_id and optional "
            "containing_page_archive_timestamp/canonical_card_uuid"
        ),
    )
    assets.add_argument(
        "--containing-page-timestamp",
        help="14-digit page capture timestamp applied to direct URL inputs",
    )
    assets.add_argument(
        "--containing-page-snapshot-id",
        type=_positive_int,
        help="cached page snapshot proving direct URL inputs were observed",
    )
    assets.add_argument("--cdx-limit", type=_positive_int, default=50)
    assets.add_argument("--priority", type=int, default=80)
    assets.add_argument(
        "--known-placeholder-hash",
        action="append",
        help="independently verified placeholder SHA-256 (repeatable)",
    )
    assets.add_argument(
        "--report-output",
        help="asset-validation JSON path (default: reports/pilot_asset_validation.json)",
    )
    assets.set_defaults(handler=command_wayback_assets)

    reparse = subparsers.add_parser(
        "reparse", help="parse immutable cached snapshots without network access"
    )
    reparse.add_argument("--source", choices=("2kmtcentral", "mtdb", "all"), default="all")
    reparse.add_argument("--snapshot-id", action="append", type=_positive_int)
    reparse.add_argument("--crosswalk", help="optional NBA 2K17 schema crosswalk JSON")
    reparse.add_argument("--force", action="store_true")
    reparse.set_defaults(handler=command_reparse)

    pilot = subparsers.add_parser(
        "build-pilot", help="build a bounded offline export from cached snapshots"
    )
    _add_export_arguments(pilot, pilot=True)
    pilot.set_defaults(handler=command_build_pilot)

    export = subparsers.add_parser(
        "export", help="build the full offline export from cached snapshots"
    )
    _add_export_arguments(export, pilot=False)
    export.set_defaults(handler=command_export)

    full = subparsers.add_parser(
        "full-crawl",
        aliases=("full-resume",),
        help="resume 2KMTCentral first, then gate MTDB fallback",
    )
    full.add_argument("--max-requests", type=_positive_int, default=100)
    full.set_defaults(handler=command_full_crawl)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "timestamp", None) is not None and not re.fullmatch(
        r"\d{14}", args.timestamp
    ):
        parser.error("--timestamp must contain exactly 14 digits")
    try:
        return int(args.handler(args))
    except (CliError, OSError, ValueError, json.JSONDecodeError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
