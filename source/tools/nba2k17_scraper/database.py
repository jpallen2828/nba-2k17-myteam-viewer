from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator


SCHEMA_VERSION = 1

FETCH_PENDING = "pending"
FETCHING = "fetching"
FETCHED = "fetched"
FETCH_RETRY = "retry"
FETCH_BLOCKED = "blocked"
FETCH_CONFIRMED_MISSING = "confirmed_missing"
FETCH_FAILED = "failed"

_FETCH_STATES = {
    FETCH_PENDING,
    FETCHING,
    FETCHED,
    FETCH_RETRY,
    FETCH_BLOCKED,
    FETCH_CONFIRMED_MISSING,
    FETCH_FAILED,
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_json(value: str | None, default: object) -> object:
    if value is None or value == "":
        return default
    return json.loads(value)


@dataclass(frozen=True, slots=True)
class CrawlUrl:
    crawl_url_id: int
    source: str
    url: str
    source_card_id: str | None
    discovery_method: str
    priority: int
    fetch_status: str
    attempt_count: int
    next_allowed_attempt: float | None
    last_http_status: int | None
    retry_after_value: str | None
    retry_after_until: float | None
    first_discovered_at: float
    last_discovered_at: float
    last_attempt_at: float | None
    last_success_at: float | None
    last_capture_at: float | None
    last_archive_at: str | None
    content_hash: str | None
    raw_cache_path: str | None
    parser_version: str | None
    parse_status: str | None
    parse_result: object | None
    canonical_card_id: str | None
    assets: tuple[object, ...]
    missing_fields: tuple[str, ...]
    manual_review_status: str
    last_error: str | None
    etag: str | None
    last_modified: str | None
    latest_snapshot_id: int | None


@dataclass(frozen=True, slots=True)
class PageSnapshot:
    snapshot_id: int
    crawl_url_id: int
    snapshot_key: str
    source_kind: str
    captured_at: float
    archive_timestamp: str | None
    content_hash: str
    raw_cache_path: str
    http_status: int
    final_url: str
    response_headers: Mapping[str, str]
    parser_version: str | None
    parse_status: str | None
    parse_result: object | None
    canonical_card_id: str | None
    missing_fields: tuple[str, ...]
    assets: tuple[object, ...]
    manual_review_status: str


@dataclass(frozen=True, slots=True)
class HostState:
    host: str
    last_request_at: float | None = None
    next_request_at: float | None = None
    daily_window: str | None = None
    daily_request_count: int = 0
    blocked_until: float | None = None
    block_reason: str | None = None
    consecutive_5xx: int = 0
    blocking_events: int = 0
    captcha_detected: bool = False
    updated_at: float | None = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_urls (
    crawl_url_id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    url TEXT NOT NULL,
    source_card_id TEXT,
    discovery_method TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 0,
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    next_allowed_attempt REAL,
    last_http_status INTEGER,
    retry_after_value TEXT,
    retry_after_until REAL,
    first_discovered_at REAL NOT NULL,
    last_discovered_at REAL NOT NULL,
    last_attempt_at REAL,
    last_success_at REAL,
    last_capture_at REAL,
    last_archive_at TEXT,
    content_hash TEXT,
    raw_cache_path TEXT,
    parser_version TEXT,
    parse_status TEXT,
    parse_result_json TEXT,
    canonical_card_id TEXT,
    assets_json TEXT NOT NULL DEFAULT '[]',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    manual_review_status TEXT NOT NULL DEFAULT 'unreviewed',
    last_error TEXT,
    etag TEXT,
    last_modified TEXT,
    latest_snapshot_id INTEGER,
    UNIQUE(source, url)
);
CREATE INDEX IF NOT EXISTS idx_crawl_urls_ready
    ON crawl_urls(fetch_status, next_allowed_attempt, priority DESC, crawl_url_id);
CREATE INDEX IF NOT EXISTS idx_crawl_urls_source_card
    ON crawl_urls(source, source_card_id);
CREATE INDEX IF NOT EXISTS idx_crawl_urls_canonical
    ON crawl_urls(canonical_card_id);

CREATE TABLE IF NOT EXISTS url_discoveries (
    discovery_id INTEGER PRIMARY KEY,
    crawl_url_id INTEGER NOT NULL REFERENCES crawl_urls(crawl_url_id) ON DELETE CASCADE,
    discovery_method TEXT NOT NULL,
    discovered_from TEXT NOT NULL DEFAULT '',
    discovered_at REAL NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(crawl_url_id, discovery_method, discovered_from)
);

CREATE TABLE IF NOT EXISTS fetch_attempts (
    attempt_id INTEGER PRIMARY KEY,
    crawl_url_id INTEGER NOT NULL REFERENCES crawl_urls(crawl_url_id) ON DELETE CASCADE,
    started_at REAL NOT NULL,
    finished_at REAL,
    outcome TEXT NOT NULL DEFAULT 'started',
    http_status INTEGER,
    retry_after_value TEXT,
    retry_after_until REAL,
    request_headers_json TEXT NOT NULL DEFAULT '{}',
    response_headers_json TEXT NOT NULL DEFAULT '{}',
    snapshot_id INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_fetch_attempts_url
    ON fetch_attempts(crawl_url_id, attempt_id);

CREATE TABLE IF NOT EXISTS page_snapshots (
    snapshot_id INTEGER PRIMARY KEY,
    crawl_url_id INTEGER NOT NULL REFERENCES crawl_urls(crawl_url_id) ON DELETE CASCADE,
    snapshot_key TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,
    captured_at REAL NOT NULL,
    archive_timestamp TEXT,
    content_hash TEXT NOT NULL,
    raw_cache_path TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    final_url TEXT NOT NULL,
    response_headers_json TEXT NOT NULL DEFAULT '{}',
    parser_version TEXT,
    parse_status TEXT,
    parse_result_json TEXT,
    canonical_card_id TEXT,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    assets_json TEXT NOT NULL DEFAULT '[]',
    manual_review_status TEXT NOT NULL DEFAULT 'unreviewed',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_page_snapshots_url_time
    ON page_snapshots(crawl_url_id, captured_at, snapshot_id);
CREATE INDEX IF NOT EXISTS idx_page_snapshots_hash
    ON page_snapshots(content_hash);

CREATE TABLE IF NOT EXISTS parse_results (
    parse_result_id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES page_snapshots(snapshot_id) ON DELETE CASCADE,
    parser_version TEXT NOT NULL,
    parsed_at REAL NOT NULL,
    parse_status TEXT NOT NULL,
    result_json TEXT,
    canonical_card_id TEXT,
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    assets_json TEXT NOT NULL DEFAULT '[]',
    manual_review_status TEXT NOT NULL DEFAULT 'unreviewed',
    error TEXT,
    UNIQUE(snapshot_id, parser_version)
);

CREATE TABLE IF NOT EXISTS assets (
    asset_id INTEGER PRIMARY KEY,
    crawl_url_id INTEGER NOT NULL REFERENCES crawl_urls(crawl_url_id) ON DELETE CASCADE,
    snapshot_id INTEGER REFERENCES page_snapshots(snapshot_id) ON DELETE SET NULL,
    asset_url TEXT NOT NULL,
    asset_kind TEXT NOT NULL DEFAULT 'unknown',
    fetch_status TEXT NOT NULL DEFAULT 'pending',
    content_hash TEXT,
    raw_cache_path TEXT,
    http_status INTEGER,
    captured_at REAL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(crawl_url_id, asset_url)
);

CREATE TABLE IF NOT EXISTS card_completeness (
    source_site TEXT NOT NULL,
    source_card_id TEXT NOT NULL,
    canonical_card_uuid TEXT,
    primary_url TEXT NOT NULL,
    parse_status TEXT NOT NULL,
    image_status TEXT NOT NULL,
    required_fields_status TEXT NOT NULL,
    roster_match_status TEXT NOT NULL,
    overall_status TEXT NOT NULL,
    needs_mtdb INTEGER NOT NULL,
    mtdb_reasons_json TEXT NOT NULL DEFAULT '[]',
    manual_review_reasons_json TEXT NOT NULL DEFAULT '[]',
    missing_domains_json TEXT NOT NULL DEFAULT '[]',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    PRIMARY KEY(source_site, primary_url)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_card_completeness_source_id
    ON card_completeness(source_site, source_card_id);
CREATE INDEX IF NOT EXISTS idx_card_completeness_mtdb
    ON card_completeness(needs_mtdb, source_card_id);
CREATE INDEX IF NOT EXISTS idx_card_completeness_status
    ON card_completeness(overall_status, image_status, parse_status);

CREATE TABLE IF NOT EXISTS host_state (
    host TEXT PRIMARY KEY,
    last_request_at REAL,
    next_request_at REAL,
    daily_window TEXT,
    daily_request_count INTEGER NOT NULL DEFAULT 0,
    blocked_until REAL,
    block_reason TEXT,
    consecutive_5xx INTEGER NOT NULL DEFAULT 0,
    blocking_events INTEGER NOT NULL DEFAULT 0,
    captcha_detected INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
"""


class CrawlDatabase:
    """Durable crawl, evidence, parse, and host-throttle state."""

    def __init__(self, path: Path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.identity = str(self.path).casefold()
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA synchronous = FULL")
        mode = str(self.connection.execute("PRAGMA journal_mode = WAL").fetchone()[0])
        if mode.casefold() != "wal":
            raise RuntimeError(f"crawl database did not enter WAL mode: {mode}")
        self._migrate()

    def __enter__(self) -> "CrawlDatabase":
        return self

    def __exit__(self, _type, _value, _traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.connection.execute("BEGIN IMMEDIATE")
            try:
                yield self.connection
            except BaseException:
                self.connection.execute("ROLLBACK")
                raise
            else:
                self.connection.execute("COMMIT")

    def _migrate(self) -> None:
        # sqlite3.executescript() owns its transaction boundary. Run the
        # idempotent DDL first, then protect the version row separately.
        with self._lock:
            self.connection.executescript(_SCHEMA)
        self._migrate_completeness_url_identity()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT version FROM schema_info WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO schema_info(singleton, version) VALUES (1, ?)",
                    (SCHEMA_VERSION,),
                )
            elif int(row["version"]) != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported crawl schema {row['version']}; expected {SCHEMA_VERSION}"
                )

    def _migrate_completeness_url_identity(self) -> None:
        """Retain URL aliases as separate evidence rows for the same card ID."""
        with self._lock:
            info = self.connection.execute("PRAGMA table_info(card_completeness)").fetchall()
        primary_key = [
            str(row["name"])
            for row in sorted((row for row in info if int(row["pk"])), key=lambda row: int(row["pk"]))
        ]
        if primary_key == ["source_site", "primary_url"]:
            return
        with self.transaction() as connection:
            connection.execute("DROP INDEX IF EXISTS idx_card_completeness_mtdb")
            connection.execute("DROP INDEX IF EXISTS idx_card_completeness_status")
            connection.execute("ALTER TABLE card_completeness RENAME TO card_completeness_legacy")
            connection.execute(
                """CREATE TABLE card_completeness (
                       source_site TEXT NOT NULL,
                       source_card_id TEXT NOT NULL,
                       canonical_card_uuid TEXT,
                       primary_url TEXT NOT NULL,
                       parse_status TEXT NOT NULL,
                       image_status TEXT NOT NULL,
                       required_fields_status TEXT NOT NULL,
                       roster_match_status TEXT NOT NULL,
                       overall_status TEXT NOT NULL,
                       needs_mtdb INTEGER NOT NULL,
                       mtdb_reasons_json TEXT NOT NULL DEFAULT '[]',
                       manual_review_reasons_json TEXT NOT NULL DEFAULT '[]',
                       missing_domains_json TEXT NOT NULL DEFAULT '[]',
                       metadata_json TEXT NOT NULL DEFAULT '{}',
                       updated_at REAL NOT NULL,
                       PRIMARY KEY(source_site, primary_url)
                   ) WITHOUT ROWID"""
            )
            connection.execute(
                """INSERT INTO card_completeness SELECT * FROM card_completeness_legacy"""
            )
            connection.execute("DROP TABLE card_completeness_legacy")
            connection.execute(
                "CREATE INDEX idx_card_completeness_source_id ON card_completeness(source_site, source_card_id)"
            )
            connection.execute(
                "CREATE INDEX idx_card_completeness_mtdb ON card_completeness(needs_mtdb, source_card_id)"
            )
            connection.execute(
                "CREATE INDEX idx_card_completeness_status ON card_completeness(overall_status, image_status, parse_status)"
            )

    @property
    def journal_mode(self) -> str:
        with self._lock:
            return str(self.connection.execute("PRAGMA journal_mode").fetchone()[0])

    def discover_url(
        self,
        *,
        source: str,
        url: str,
        discovery_method: str,
        source_card_id: str | int | None = None,
        priority: int = 0,
        discovered_from: str | None = None,
        metadata: Mapping[str, object] | None = None,
        now: float | None = None,
    ) -> CrawlUrl:
        source = source.strip()
        url = url.strip()
        discovery_method = discovery_method.strip()
        if not source or not url or not discovery_method:
            raise ValueError("source, URL, and discovery method are required")
        if type(priority) is not int:
            raise ValueError("priority must be an integer")
        timestamp = time.time() if now is None else float(now)
        card_id = None if source_card_id is None else str(source_card_id)
        origin = (discovered_from or "").strip()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT crawl_url_id, source_card_id FROM crawl_urls WHERE source=? AND url=?",
                (source, url),
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    """INSERT INTO crawl_urls(
                           source, url, source_card_id, discovery_method, priority,
                           fetch_status, first_discovered_at, last_discovered_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        source,
                        url,
                        card_id,
                        discovery_method,
                        priority,
                        FETCH_PENDING,
                        timestamp,
                        timestamp,
                    ),
                )
                crawl_url_id = int(cursor.lastrowid)
            else:
                crawl_url_id = int(existing["crawl_url_id"])
                connection.execute(
                    """UPDATE crawl_urls
                          SET source_card_id=COALESCE(source_card_id, ?),
                              priority=MAX(priority, ?),
                              last_discovered_at=?
                        WHERE crawl_url_id=?""",
                    (card_id, priority, timestamp, crawl_url_id),
                )
            connection.execute(
                """INSERT INTO url_discoveries(
                       crawl_url_id, discovery_method, discovered_from,
                       discovered_at, metadata_json
                   ) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(crawl_url_id, discovery_method, discovered_from)
                   DO UPDATE SET discovered_at=excluded.discovered_at,
                                 metadata_json=excluded.metadata_json""",
                (
                    crawl_url_id,
                    discovery_method,
                    origin,
                    timestamp,
                    _json(dict(metadata or {})),
                ),
            )
        return self.get_url(crawl_url_id)

    def get_url(self, crawl_url_id: int) -> CrawlUrl:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM crawl_urls WHERE crawl_url_id=?", (crawl_url_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown crawl URL id {crawl_url_id}")
        return self._crawl_url(row)

    def find_url(self, source: str, url: str) -> CrawlUrl | None:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM crawl_urls WHERE source=? AND url=?", (source, url)
            ).fetchone()
        return None if row is None else self._crawl_url(row)

    def list_ready(
        self,
        *,
        now: float | None = None,
        source: str | None = None,
        limit: int = 100,
    ) -> list[CrawlUrl]:
        if limit < 1:
            raise ValueError("limit must be positive")
        timestamp = time.time() if now is None else float(now)
        parameters: list[object] = [timestamp]
        # A fetching row owns a durable lease in next_allowed_attempt. If a
        # process dies mid-request, the row becomes ready again after that
        # lease instead of remaining stranded forever.
        where = "fetch_status IN ('pending','retry','blocked','fetching') AND (next_allowed_attempt IS NULL OR next_allowed_attempt<=?)"
        if source is not None:
            where += " AND source=?"
            parameters.append(source)
        parameters.append(limit)
        with self._lock:
            rows = self.connection.execute(
                f"""SELECT * FROM crawl_urls WHERE {where}
                     ORDER BY priority DESC, crawl_url_id LIMIT ?""",
                parameters,
            ).fetchall()
        return [self._crawl_url(row) for row in rows]

    def start_fetch_attempt(
        self,
        crawl_url_id: int,
        *,
        request_headers: Mapping[str, str],
        lease_seconds: float = 300.0,
        now: float | None = None,
    ) -> int:
        if lease_seconds <= 0:
            raise ValueError("fetch lease must be positive")
        timestamp = time.time() if now is None else float(now)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT fetch_status, next_allowed_attempt FROM crawl_urls WHERE crawl_url_id=?",
                (crawl_url_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown crawl URL id {crawl_url_id}")
            if (
                row["next_allowed_attempt"] is not None
                and float(row["next_allowed_attempt"]) > timestamp
            ):
                description = (
                    "an active fetch lease"
                    if row["fetch_status"] == FETCHING
                    else "a durable retry cooldown"
                )
                raise RuntimeError(f"crawl URL {crawl_url_id} already has {description}")
            # Close any attempt left open by a crashed process before creating
            # the replacement attempt. This update is idempotent.
            connection.execute(
                """UPDATE fetch_attempts
                      SET finished_at=?, outcome='interrupted',
                          error='fetch lease expired before completion'
                    WHERE crawl_url_id=? AND outcome='started'""",
                (timestamp, crawl_url_id),
            )
            connection.execute(
                """UPDATE crawl_urls
                      SET fetch_status=?, attempt_count=attempt_count+1,
                          last_attempt_at=?, next_allowed_attempt=?, last_error=NULL
                    WHERE crawl_url_id=?""",
                (FETCHING, timestamp, timestamp + float(lease_seconds), crawl_url_id),
            )
            cursor = connection.execute(
                """INSERT INTO fetch_attempts(
                       crawl_url_id, started_at, request_headers_json
                   ) VALUES (?, ?, ?)""",
                (crawl_url_id, timestamp, _json(dict(request_headers))),
            )
            return int(cursor.lastrowid)

    def record_fetch_success(
        self,
        *,
        crawl_url_id: int,
        attempt_id: int,
        source_kind: str,
        captured_at: float,
        fetched_at: float | None = None,
        archive_timestamp: str | None,
        content_hash: str,
        raw_cache_path: str,
        http_status: int,
        final_url: str,
        response_headers: Mapping[str, str],
        etag: str | None,
        last_modified: str | None,
    ) -> PageSnapshot:
        if not source_kind.strip() or not content_hash or not raw_cache_path:
            raise ValueError("source kind, content hash, and raw cache path are required")
        completed_at = captured_at if fetched_at is None else float(fetched_at)
        key_material = "\0".join(
            (
                str(crawl_url_id),
                source_kind,
                archive_timestamp or "",
                content_hash,
            )
        )
        snapshot_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        headers_json = _json(dict(response_headers))
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO page_snapshots(
                       crawl_url_id, snapshot_key, source_kind, captured_at,
                       archive_timestamp, content_hash, raw_cache_path, http_status,
                       final_url, response_headers_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(snapshot_key) DO UPDATE SET
                       captured_at=MIN(page_snapshots.captured_at, excluded.captured_at),
                       response_headers_json=excluded.response_headers_json""",
                (
                    crawl_url_id,
                    snapshot_key,
                    source_kind,
                    captured_at,
                    archive_timestamp,
                    content_hash,
                    raw_cache_path,
                    http_status,
                    final_url,
                    headers_json,
                    captured_at,
                ),
            )
            snapshot_id = int(
                connection.execute(
                    "SELECT snapshot_id FROM page_snapshots WHERE snapshot_key=?",
                    (snapshot_key,),
                ).fetchone()[0]
            )
            snapshot_state = connection.execute(
                """SELECT parser_version, parse_status, parse_result_json,
                          canonical_card_id, assets_json, missing_fields_json,
                          manual_review_status
                     FROM page_snapshots WHERE snapshot_id=?""",
                (snapshot_id,),
            ).fetchone()
            connection.execute(
                """UPDATE fetch_attempts
                      SET finished_at=?, outcome='success', http_status=?,
                          response_headers_json=?, snapshot_id=?, error=NULL
                    WHERE attempt_id=? AND crawl_url_id=?""",
                (
                    completed_at,
                    http_status,
                    headers_json,
                    snapshot_id,
                    attempt_id,
                    crawl_url_id,
                ),
            )
            connection.execute(
                """UPDATE crawl_urls
                      SET fetch_status=?, next_allowed_attempt=NULL,
                          last_http_status=?, retry_after_value=NULL,
                          retry_after_until=NULL, last_success_at=?,
                          last_capture_at=?, last_archive_at=?, content_hash=?,
                          raw_cache_path=?, latest_snapshot_id=?, etag=?,
                          last_modified=?, parser_version=?, parse_status=?,
                          parse_result_json=?, canonical_card_id=?, assets_json=?,
                          missing_fields_json=?, manual_review_status=?,
                          last_error=NULL
                    WHERE crawl_url_id=?""",
                (
                    FETCHED,
                    http_status,
                    completed_at,
                    captured_at,
                    archive_timestamp,
                    content_hash,
                    raw_cache_path,
                    snapshot_id,
                    etag,
                    last_modified,
                    snapshot_state["parser_version"],
                    snapshot_state["parse_status"],
                    snapshot_state["parse_result_json"],
                    snapshot_state["canonical_card_id"],
                    snapshot_state["assets_json"],
                    snapshot_state["missing_fields_json"],
                    snapshot_state["manual_review_status"],
                    crawl_url_id,
                ),
            )
        return self.get_snapshot(snapshot_id)

    def record_response_evidence(
        self,
        *,
        crawl_url_id: int,
        attempt_id: int,
        source_kind: str,
        captured_at: float,
        archive_timestamp: str | None,
        content_hash: str,
        raw_cache_path: str,
        http_status: int,
        final_url: str,
        response_headers: Mapping[str, str],
    ) -> PageSnapshot:
        """Attach an immutable non-success response body to a fetch attempt.

        The URL's latest successful snapshot and conditional validators are not
        changed. This preserves block, CAPTCHA, and error pages as evidence
        without allowing them to masquerade as fetched card data.
        """

        if not source_kind.strip() or not content_hash or not raw_cache_path:
            raise ValueError("source kind, content hash, and raw cache path are required")
        key_material = "\0".join(
            (
                str(crawl_url_id),
                source_kind,
                archive_timestamp or "",
                str(http_status),
                content_hash,
            )
        )
        snapshot_key = hashlib.sha256(key_material.encode("utf-8")).hexdigest()
        headers_json = _json(dict(response_headers))
        with self.transaction() as connection:
            owner = connection.execute(
                "SELECT crawl_url_id FROM fetch_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if owner is None or int(owner["crawl_url_id"]) != crawl_url_id:
                raise KeyError(f"fetch attempt {attempt_id} does not belong to URL {crawl_url_id}")
            connection.execute(
                """INSERT INTO page_snapshots(
                       crawl_url_id, snapshot_key, source_kind, captured_at,
                       archive_timestamp, content_hash, raw_cache_path, http_status,
                       final_url, response_headers_json, created_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(snapshot_key) DO UPDATE SET
                       captured_at=MIN(page_snapshots.captured_at, excluded.captured_at),
                       response_headers_json=excluded.response_headers_json""",
                (
                    crawl_url_id,
                    snapshot_key,
                    source_kind,
                    captured_at,
                    archive_timestamp,
                    content_hash,
                    raw_cache_path,
                    http_status,
                    final_url,
                    headers_json,
                    captured_at,
                ),
            )
            snapshot_id = int(
                connection.execute(
                    "SELECT snapshot_id FROM page_snapshots WHERE snapshot_key=?",
                    (snapshot_key,),
                ).fetchone()[0]
            )
            connection.execute(
                """UPDATE fetch_attempts SET snapshot_id=?, response_headers_json=?
                     WHERE attempt_id=? AND crawl_url_id=?""",
                (snapshot_id, headers_json, attempt_id, crawl_url_id),
            )
        return self.get_snapshot(snapshot_id)

    def record_not_modified(
        self,
        *,
        crawl_url_id: int,
        attempt_id: int,
        at: float,
        response_headers: Mapping[str, str],
        etag: str | None,
        last_modified: str | None,
    ) -> PageSnapshot | None:
        headers_json = _json(dict(response_headers))
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT latest_snapshot_id FROM crawl_urls WHERE crawl_url_id=?",
                (crawl_url_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown crawl URL id {crawl_url_id}")
            snapshot_id = row["latest_snapshot_id"]
            connection.execute(
                """UPDATE fetch_attempts SET finished_at=?, outcome='not_modified',
                          http_status=304, response_headers_json=?, snapshot_id=?
                    WHERE attempt_id=? AND crawl_url_id=?""",
                (at, headers_json, snapshot_id, attempt_id, crawl_url_id),
            )
            connection.execute(
                """UPDATE crawl_urls SET fetch_status=?, next_allowed_attempt=NULL,
                          last_http_status=304, last_success_at=?,
                          retry_after_value=NULL, retry_after_until=NULL,
                          etag=COALESCE(?, etag),
                          last_modified=COALESCE(?, last_modified), last_error=NULL
                    WHERE crawl_url_id=?""",
                (FETCHED, at, etag, last_modified, crawl_url_id),
            )
        return None if snapshot_id is None else self.get_snapshot(int(snapshot_id))

    def record_fetch_failure(
        self,
        *,
        crawl_url_id: int,
        attempt_id: int,
        fetch_status: str,
        finished_at: float,
        error: str,
        http_status: int | None = None,
        retry_after_value: str | None = None,
        retry_after_until: float | None = None,
        next_allowed_attempt: float | None = None,
        response_headers: Mapping[str, str] | None = None,
        manual_review_status: str | None = None,
    ) -> CrawlUrl:
        if fetch_status not in _FETCH_STATES - {FETCHING, FETCHED, FETCH_PENDING}:
            raise ValueError(f"invalid failure fetch status {fetch_status!r}")
        with self.transaction() as connection:
            connection.execute(
                """UPDATE fetch_attempts SET finished_at=?, outcome=?, http_status=?,
                          retry_after_value=?, retry_after_until=?,
                          response_headers_json=?, error=?
                    WHERE attempt_id=? AND crawl_url_id=?""",
                (
                    finished_at,
                    fetch_status,
                    http_status,
                    retry_after_value,
                    retry_after_until,
                    _json(dict(response_headers or {})),
                    error,
                    attempt_id,
                    crawl_url_id,
                ),
            )
            connection.execute(
                """UPDATE crawl_urls SET fetch_status=?, next_allowed_attempt=?,
                          last_http_status=?, retry_after_value=?,
                          retry_after_until=?, last_error=?,
                          manual_review_status=COALESCE(?, manual_review_status)
                    WHERE crawl_url_id=?""",
                (
                    fetch_status,
                    next_allowed_attempt,
                    http_status,
                    retry_after_value,
                    retry_after_until,
                    error,
                    manual_review_status,
                    crawl_url_id,
                ),
            )
        return self.get_url(crawl_url_id)

    def consecutive_retry_failures(self, crawl_url_id: int) -> int:
        """Return completed retry/block failures since the last other outcome."""

        with self._lock:
            exists = self.connection.execute(
                "SELECT 1 FROM crawl_urls WHERE crawl_url_id=?", (crawl_url_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"unknown crawl URL id {crawl_url_id}")
            rows = self.connection.execute(
                """SELECT outcome FROM fetch_attempts WHERE crawl_url_id=?
                     ORDER BY attempt_id DESC""",
                (crawl_url_id,),
            ).fetchall()
        count = 0
        for row in rows:
            outcome = str(row["outcome"])
            if outcome == "started":
                continue
            if outcome in {FETCH_RETRY, FETCH_BLOCKED}:
                count += 1
                continue
            break
        return count

    def defer_url(
        self,
        crawl_url_id: int,
        *,
        until: float,
        reason: str,
    ) -> CrawlUrl:
        with self.transaction() as connection:
            cursor = connection.execute(
                """UPDATE crawl_urls SET fetch_status=?, next_allowed_attempt=?,
                          last_error=? WHERE crawl_url_id=?""",
                (FETCH_BLOCKED, until, reason, crawl_url_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(f"unknown crawl URL id {crawl_url_id}")
        return self.get_url(crawl_url_id)

    def get_snapshot(self, snapshot_id: int) -> PageSnapshot:
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM page_snapshots WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown snapshot id {snapshot_id}")
        return self._snapshot(row)

    def snapshots_for_url(self, crawl_url_id: int) -> list[PageSnapshot]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM page_snapshots WHERE crawl_url_id=?
                     ORDER BY captured_at, snapshot_id""",
                (crawl_url_id,),
            ).fetchall()
        return [self._snapshot(row) for row in rows]

    def record_parse_result(
        self,
        *,
        snapshot_id: int,
        parser_version: str,
        parse_status: str,
        result: object | None,
        canonical_card_id: str | None = None,
        missing_fields: Sequence[str] = (),
        assets: Sequence[object] = (),
        manual_review_status: str = "unreviewed",
        error: str | None = None,
        parsed_at: float | None = None,
    ) -> int:
        if not parser_version.strip() or not parse_status.strip():
            raise ValueError("parser version and parse status are required")
        timestamp = time.time() if parsed_at is None else float(parsed_at)
        missing = tuple(str(item) for item in missing_fields)
        asset_rows = tuple(assets)
        with self.transaction() as connection:
            snapshot = connection.execute(
                "SELECT crawl_url_id FROM page_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"unknown snapshot id {snapshot_id}")
            connection.execute(
                """INSERT INTO parse_results(
                       snapshot_id, parser_version, parsed_at, parse_status,
                       result_json, canonical_card_id, missing_fields_json,
                       assets_json, manual_review_status, error
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(snapshot_id, parser_version) DO UPDATE SET
                       parsed_at=excluded.parsed_at,
                       parse_status=excluded.parse_status,
                       result_json=excluded.result_json,
                       canonical_card_id=excluded.canonical_card_id,
                       missing_fields_json=excluded.missing_fields_json,
                       assets_json=excluded.assets_json,
                       manual_review_status=excluded.manual_review_status,
                       error=excluded.error""",
                (
                    snapshot_id,
                    parser_version,
                    timestamp,
                    parse_status,
                    None if result is None else _json(result),
                    canonical_card_id,
                    _json(missing),
                    _json(asset_rows),
                    manual_review_status,
                    error,
                ),
            )
            parse_result_id = int(
                connection.execute(
                    """SELECT parse_result_id FROM parse_results
                        WHERE snapshot_id=? AND parser_version=?""",
                    (snapshot_id, parser_version),
                ).fetchone()[0]
            )
            connection.execute(
                """UPDATE page_snapshots SET parser_version=?, parse_status=?,
                          parse_result_json=?, canonical_card_id=?,
                          missing_fields_json=?, assets_json=?,
                          manual_review_status=? WHERE snapshot_id=?""",
                (
                    parser_version,
                    parse_status,
                    None if result is None else _json(result),
                    canonical_card_id,
                    _json(missing),
                    _json(asset_rows),
                    manual_review_status,
                    snapshot_id,
                ),
            )
            connection.execute(
                """UPDATE crawl_urls SET parser_version=?, parse_status=?,
                          parse_result_json=?, canonical_card_id=?,
                          missing_fields_json=?, assets_json=?,
                          manual_review_status=?, last_error=?
                    WHERE crawl_url_id=? AND latest_snapshot_id=?""",
                (
                    parser_version,
                    parse_status,
                    None if result is None else _json(result),
                    canonical_card_id,
                    _json(missing),
                    _json(asset_rows),
                    manual_review_status,
                    error,
                    int(snapshot["crawl_url_id"]),
                    snapshot_id,
                ),
            )
        return parse_result_id

    def parse_results_for_snapshot(self, snapshot_id: int) -> list[dict[str, object]]:
        with self._lock:
            rows = self.connection.execute(
                """SELECT * FROM parse_results WHERE snapshot_id=?
                     ORDER BY parse_result_id""",
                (snapshot_id,),
            ).fetchall()
        return [
            {
                **dict(row),
                "result": _load_json(row["result_json"], None),
                "missing_fields": _load_json(row["missing_fields_json"], []),
                "assets": _load_json(row["assets_json"], []),
            }
            for row in rows
        ]

    def upsert_asset(
        self,
        *,
        crawl_url_id: int,
        asset_url: str,
        asset_kind: str = "unknown",
        snapshot_id: int | None = None,
        fetch_status: str = FETCH_PENDING,
        content_hash: str | None = None,
        raw_cache_path: str | None = None,
        http_status: int | None = None,
        captured_at: float | None = None,
        metadata: Mapping[str, object] | None = None,
    ) -> int:
        if not asset_url.strip():
            raise ValueError("asset URL is required")
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO assets(
                       crawl_url_id, snapshot_id, asset_url, asset_kind,
                       fetch_status, content_hash, raw_cache_path, http_status,
                       captured_at, metadata_json
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(crawl_url_id, asset_url) DO UPDATE SET
                       snapshot_id=COALESCE(excluded.snapshot_id, assets.snapshot_id),
                       asset_kind=excluded.asset_kind,
                       fetch_status=excluded.fetch_status,
                       content_hash=COALESCE(excluded.content_hash, assets.content_hash),
                       raw_cache_path=COALESCE(excluded.raw_cache_path, assets.raw_cache_path),
                       http_status=COALESCE(excluded.http_status, assets.http_status),
                       captured_at=COALESCE(excluded.captured_at, assets.captured_at),
                       metadata_json=excluded.metadata_json""",
                (
                    crawl_url_id,
                    snapshot_id,
                    asset_url,
                    asset_kind,
                    fetch_status,
                    content_hash,
                    raw_cache_path,
                    http_status,
                    captured_at,
                    _json(dict(metadata or {})),
                ),
            )
            return int(
                connection.execute(
                    "SELECT asset_id FROM assets WHERE crawl_url_id=? AND asset_url=?",
                    (crawl_url_id, asset_url),
                ).fetchone()[0]
            )

    def upsert_card_completeness(self, row: Mapping[str, object]) -> None:
        required = {
            "source_site", "source_card_id", "primary_url", "parse_status",
            "image_status", "required_fields_status", "roster_match_status",
            "overall_status", "needs_mtdb",
        }
        missing = required - set(row)
        if missing:
            raise ValueError(f"missing completeness fields: {sorted(missing)}")
        timestamp = float(row.get("updated_at") or time.time())
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO card_completeness(
                       source_site, source_card_id, canonical_card_uuid, primary_url,
                       parse_status, image_status, required_fields_status,
                       roster_match_status, overall_status, needs_mtdb,
                       mtdb_reasons_json, manual_review_reasons_json,
                       missing_domains_json, metadata_json, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(source_site, primary_url) DO UPDATE SET
                       source_card_id=excluded.source_card_id,
                       canonical_card_uuid=excluded.canonical_card_uuid,
                       primary_url=excluded.primary_url,
                       parse_status=excluded.parse_status,
                       image_status=excluded.image_status,
                       required_fields_status=excluded.required_fields_status,
                       roster_match_status=excluded.roster_match_status,
                       overall_status=excluded.overall_status,
                       needs_mtdb=excluded.needs_mtdb,
                       mtdb_reasons_json=excluded.mtdb_reasons_json,
                       manual_review_reasons_json=excluded.manual_review_reasons_json,
                       missing_domains_json=excluded.missing_domains_json,
                       metadata_json=excluded.metadata_json,
                       updated_at=excluded.updated_at""",
                (
                    str(row["source_site"]), str(row["source_card_id"]),
                    row.get("canonical_card_uuid"), str(row["primary_url"]),
                    str(row["parse_status"]), str(row["image_status"]),
                    str(row["required_fields_status"]),
                    str(row["roster_match_status"]), str(row["overall_status"]),
                    int(bool(row["needs_mtdb"])),
                    _json(sorted(set(row.get("mtdb_reasons", [])))),
                    _json(sorted(set(row.get("manual_review_reasons", [])))),
                    _json(sorted(set(row.get("missing_domains", [])))),
                    _json(dict(row.get("metadata", {}))), timestamp,
                ),
            )

    def list_card_completeness(self, *, needs_mtdb: bool | None = None) -> list[dict[str, object]]:
        sql = "SELECT * FROM card_completeness"
        parameters: tuple[object, ...] = ()
        if needs_mtdb is not None:
            sql += " WHERE needs_mtdb=?"
            parameters = (int(needs_mtdb),)
        sql += " ORDER BY source_site, CAST(source_card_id AS INTEGER), source_card_id, primary_url"
        with self._lock:
            rows = self.connection.execute(sql, parameters).fetchall()
        return [
            {
                **dict(row),
                "needs_mtdb": bool(row["needs_mtdb"]),
                "mtdb_reasons": _load_json(row["mtdb_reasons_json"], []),
                "manual_review_reasons": _load_json(row["manual_review_reasons_json"], []),
                "missing_domains": _load_json(row["missing_domains_json"], []),
                "metadata": _load_json(row["metadata_json"], {}),
            }
            for row in rows
        ]

    def get_host_state(self, host: str) -> HostState:
        normalized = host.strip().casefold()
        if not normalized:
            raise ValueError("host is required")
        with self._lock:
            row = self.connection.execute(
                "SELECT * FROM host_state WHERE host=?", (normalized,)
            ).fetchone()
        return HostState(host=normalized) if row is None else self._host_state(row)

    def mutate_host_state(
        self,
        host: str,
        mutator: Callable[[HostState], Mapping[str, object]],
        *,
        now: float | None = None,
    ) -> HostState:
        normalized = host.strip().casefold()
        if not normalized:
            raise ValueError("host is required")
        timestamp = time.time() if now is None else float(now)
        allowed = {
            "last_request_at",
            "next_request_at",
            "daily_window",
            "daily_request_count",
            "blocked_until",
            "block_reason",
            "consecutive_5xx",
            "blocking_events",
            "captcha_detected",
        }
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM host_state WHERE host=?", (normalized,)
            ).fetchone()
            current = HostState(host=normalized) if row is None else self._host_state(row)
            changes = dict(mutator(current))
            unknown = set(changes) - allowed
            if unknown:
                raise ValueError(f"unsupported host-state fields: {sorted(unknown)}")
            values = {
                "last_request_at": current.last_request_at,
                "next_request_at": current.next_request_at,
                "daily_window": current.daily_window,
                "daily_request_count": current.daily_request_count,
                "blocked_until": current.blocked_until,
                "block_reason": current.block_reason,
                "consecutive_5xx": current.consecutive_5xx,
                "blocking_events": current.blocking_events,
                "captcha_detected": int(current.captcha_detected),
            }
            values.update(changes)
            values["captcha_detected"] = int(bool(values["captcha_detected"]))
            connection.execute(
                """INSERT INTO host_state(
                       host, last_request_at, next_request_at, daily_window,
                       daily_request_count, blocked_until, block_reason,
                       consecutive_5xx, blocking_events, captcha_detected, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host) DO UPDATE SET
                       last_request_at=excluded.last_request_at,
                       next_request_at=excluded.next_request_at,
                       daily_window=excluded.daily_window,
                       daily_request_count=excluded.daily_request_count,
                       blocked_until=excluded.blocked_until,
                       block_reason=excluded.block_reason,
                       consecutive_5xx=excluded.consecutive_5xx,
                       blocking_events=excluded.blocking_events,
                       captcha_detected=excluded.captcha_detected,
                       updated_at=excluded.updated_at""",
                (
                    normalized,
                    values["last_request_at"],
                    values["next_request_at"],
                    values["daily_window"],
                    values["daily_request_count"],
                    values["blocked_until"],
                    values["block_reason"],
                    values["consecutive_5xx"],
                    values["blocking_events"],
                    values["captcha_detected"],
                    timestamp,
                ),
            )
            result = connection.execute(
                "SELECT * FROM host_state WHERE host=?", (normalized,)
            ).fetchone()
        return self._host_state(result)

    def table_count(self, table: str) -> int:
        allowed = {
            "crawl_urls",
            "url_discoveries",
            "fetch_attempts",
            "page_snapshots",
            "parse_results",
            "assets",
            "card_completeness",
            "host_state",
        }
        if table not in allowed:
            raise ValueError("unsupported table")
        with self._lock:
            return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    @staticmethod
    def _crawl_url(row: sqlite3.Row) -> CrawlUrl:
        return CrawlUrl(
            crawl_url_id=int(row["crawl_url_id"]),
            source=str(row["source"]),
            url=str(row["url"]),
            source_card_id=row["source_card_id"],
            discovery_method=str(row["discovery_method"]),
            priority=int(row["priority"]),
            fetch_status=str(row["fetch_status"]),
            attempt_count=int(row["attempt_count"]),
            next_allowed_attempt=row["next_allowed_attempt"],
            last_http_status=row["last_http_status"],
            retry_after_value=row["retry_after_value"],
            retry_after_until=row["retry_after_until"],
            first_discovered_at=float(row["first_discovered_at"]),
            last_discovered_at=float(row["last_discovered_at"]),
            last_attempt_at=row["last_attempt_at"],
            last_success_at=row["last_success_at"],
            last_capture_at=row["last_capture_at"],
            last_archive_at=row["last_archive_at"],
            content_hash=row["content_hash"],
            raw_cache_path=row["raw_cache_path"],
            parser_version=row["parser_version"],
            parse_status=row["parse_status"],
            parse_result=_load_json(row["parse_result_json"], None),
            canonical_card_id=row["canonical_card_id"],
            assets=tuple(_load_json(row["assets_json"], [])),
            missing_fields=tuple(_load_json(row["missing_fields_json"], [])),
            manual_review_status=str(row["manual_review_status"]),
            last_error=row["last_error"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            latest_snapshot_id=row["latest_snapshot_id"],
        )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> PageSnapshot:
        return PageSnapshot(
            snapshot_id=int(row["snapshot_id"]),
            crawl_url_id=int(row["crawl_url_id"]),
            snapshot_key=str(row["snapshot_key"]),
            source_kind=str(row["source_kind"]),
            captured_at=float(row["captured_at"]),
            archive_timestamp=row["archive_timestamp"],
            content_hash=str(row["content_hash"]),
            raw_cache_path=str(row["raw_cache_path"]),
            http_status=int(row["http_status"]),
            final_url=str(row["final_url"]),
            response_headers=dict(_load_json(row["response_headers_json"], {})),
            parser_version=row["parser_version"],
            parse_status=row["parse_status"],
            parse_result=_load_json(row["parse_result_json"], None),
            canonical_card_id=row["canonical_card_id"],
            missing_fields=tuple(_load_json(row["missing_fields_json"], [])),
            assets=tuple(_load_json(row["assets_json"], [])),
            manual_review_status=str(row["manual_review_status"]),
        )

    @staticmethod
    def _host_state(row: sqlite3.Row) -> HostState:
        return HostState(
            host=str(row["host"]),
            last_request_at=row["last_request_at"],
            next_request_at=row["next_request_at"],
            daily_window=row["daily_window"],
            daily_request_count=int(row["daily_request_count"]),
            blocked_until=row["blocked_until"],
            block_reason=row["block_reason"],
            consecutive_5xx=int(row["consecutive_5xx"]),
            blocking_events=int(row["blocking_events"]),
            captcha_detected=bool(row["captcha_detected"]),
            updated_at=row["updated_at"],
        )
