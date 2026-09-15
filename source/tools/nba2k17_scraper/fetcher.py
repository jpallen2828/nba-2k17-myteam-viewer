from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import ScraperConfig
from .database import (
    FETCH_BLOCKED,
    FETCH_CONFIRMED_MISSING,
    FETCH_FAILED,
    FETCH_RETRY,
    CrawlDatabase,
    PageSnapshot,
)
from .rate_limit import DurableRateLimiter, HostPaused, parse_retry_after


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value)
    return None


def _archive_time(timestamp: str | None, fallback: float) -> float:
    if timestamp is None:
        return fallback
    if not re.fullmatch(r"\d{14}", timestamp):
        raise ValueError(f"invalid archive timestamp {timestamp!r}")
    return datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(
        tzinfo=timezone.utc
    ).timestamp()


@dataclass(frozen=True, slots=True)
class FetchRequest:
    url: str
    headers: Mapping[str, str]
    method: str = "GET"


@dataclass(frozen=True, slots=True)
class FetchResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    final_url: str


class Transport(Protocol):
    def send(self, request: FetchRequest, timeout: float) -> FetchResponse: ...


class _NoAutomaticRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


class UrllibTransport:
    """Plain stdlib HTTP transport with no proxy rotation or challenge bypass."""

    def __init__(self) -> None:
        # An explicit empty ProxyHandler avoids silently inheriting a rotating
        # proxy from the host environment. Automatic redirects are disabled:
        # following a redirect could contact a second host without acquiring
        # that host's one-at-a-time permit. TLS checks remain urllib defaults.
        self._opener = build_opener(ProxyHandler({}), _NoAutomaticRedirects())

    def send(self, request: FetchRequest, timeout: float) -> FetchResponse:
        wire_request = Request(
            request.url,
            headers=dict(request.headers),
            method=request.method,
        )
        try:
            with self._opener.open(wire_request, timeout=timeout) as response:
                return FetchResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=response.read(),
                    final_url=response.geturl(),
                )
        except HTTPError as error:
            # HTTP failures are still protocol responses and may contain vital
            # block/CAPTCHA evidence. Do not turn them into transport errors.
            try:
                body = error.read()
            finally:
                error.close()
            return FetchResponse(
                status=int(error.code),
                headers=dict(error.headers.items()) if error.headers else {},
                body=body,
                final_url=error.geturl(),
            )


@dataclass(frozen=True, slots=True)
class FetchResult:
    crawl_url_id: int
    request_url: str
    outcome: str
    http_status: int | None = None
    snapshot_id: int | None = None
    content_hash: str | None = None
    raw_cache_path: str | None = None
    retry_at: float | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    status: str
    result: object | None = None
    canonical_card_id: str | None = None
    missing_fields: tuple[str, ...] = ()
    assets: tuple[object, ...] = ()
    manual_review_status: str = "unreviewed"
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.status.strip():
            raise ValueError("parse status is required")


class Fetcher:
    """Resumable fetch/cache coordinator with a mandatory durable limiter."""

    def __init__(
        self,
        config: ScraperConfig,
        database: CrawlDatabase,
        *,
        rate_limiter: DurableRateLimiter | None = None,
        transport: Transport | None = None,
        clock: Callable[[], float] = time.time,
        enforce_robots: bool | None = None,
        robots_cache_ttl_seconds: float = 24.0 * 60.0 * 60.0,
    ):
        self.config = config
        self.database = database
        self.clock = clock
        self.rate_limiter = rate_limiter or DurableRateLimiter(
            database, config.rate_limit, clock=clock
        )
        self.transport = transport or UrllibTransport()
        # Real network traffic is robots-aware by default. Injected transports
        # are normally deterministic test doubles, so they opt in explicitly
        # when a test is exercising robots behavior.
        self.enforce_robots = (
            isinstance(self.transport, UrllibTransport)
            if enforce_robots is None
            else bool(enforce_robots)
        )
        if robots_cache_ttl_seconds <= 0:
            raise ValueError("robots cache TTL must be positive")
        self.robots_cache_ttl_seconds = float(robots_cache_ttl_seconds)

    def fetch(
        self,
        crawl_url_id: int,
        *,
        request_url: str | None = None,
        source_kind: str = "live",
        archive_timestamp: str | None = None,
    ) -> FetchResult:
        crawl_url = self.database.get_url(crawl_url_id)
        target = request_url or crawl_url.url
        now = float(self.clock())
        if (
            crawl_url.next_allowed_attempt is not None
            and crawl_url.next_allowed_attempt > now
        ):
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome=crawl_url.fetch_status,
                http_status=crawl_url.last_http_status,
                retry_at=crawl_url.next_allowed_attempt,
                error=crawl_url.last_error,
            )
        if self.enforce_robots and urlsplit(target).path != "/robots.txt":
            robots_result = self._robots_preflight(crawl_url_id, target)
            if robots_result is not None:
                return robots_result
        headers: dict[str, str] = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/json,application/xml;q=0.9,*/*;q=0.5",
            "Accept-Encoding": "identity",
        }
        # Validators describe the live resource. They must not be reused for a
        # distinct archived replay capture.
        if source_kind in {"live", "robots"} and archive_timestamp is None:
            if crawl_url.etag:
                headers["If-None-Match"] = crawl_url.etag
            if crawl_url.last_modified:
                headers["If-Modified-Since"] = crawl_url.last_modified

        try:
            permit = self.rate_limiter.acquire(target)
        except HostPaused as paused:
            self.database.defer_url(
                crawl_url_id,
                until=paused.resume_at,
                reason=paused.reason,
            )
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome=FETCH_BLOCKED,
                retry_at=paused.resume_at,
                error=paused.reason,
            )

        # Both a robots refresh and acquire()'s same-host spacing can wait.
        # Timestamp the actual attempt only after the permit is granted.
        now = float(self.clock())
        captured_at = _archive_time(archive_timestamp, now)

        try:
            attempt_id = self.database.start_fetch_attempt(
                crawl_url_id, request_headers=headers, now=now
            )
        except RuntimeError:
            self.rate_limiter.abort(permit)
            current = self.database.get_url(crawl_url_id)
            current_time = float(self.clock())
            if (
                current.next_allowed_attempt is not None
                and current.next_allowed_attempt > current_time
            ):
                return FetchResult(
                    crawl_url_id=crawl_url_id,
                    request_url=target,
                    outcome=current.fetch_status,
                    http_status=current.last_http_status,
                    retry_at=current.next_allowed_attempt,
                    error=current.last_error,
                )
            raise
        except BaseException:
            self.rate_limiter.abort(permit)
            raise

        try:
            response = self.transport.send(
                FetchRequest(target, headers), self.config.request_timeout_seconds
            )
        except Exception as error:
            decision = self.rate_limiter.record_transport_error(permit, error)
            retry_at = (
                decision.retry_at
                if decision.blocked and decision.retry_at is not None
                else self._attempt_backoff(crawl_url_id)
            )
            status = FETCH_BLOCKED if decision.blocked else FETCH_RETRY
            message = decision.reason or f"transport error: {error}"
            self.database.record_fetch_failure(
                crawl_url_id=crawl_url_id,
                attempt_id=attempt_id,
                fetch_status=status,
                finished_at=float(self.clock()),
                error=message,
                next_allowed_attempt=retry_at,
                manual_review_status=(
                    "required"
                    if decision.reason
                    and "captcha" in decision.reason.casefold()
                    else None
                ),
            )
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome=status,
                retry_at=retry_at,
                error=message,
            )

        response_headers = {str(key): str(value) for key, value in response.headers.items()}
        finished_at = float(self.clock())
        decision = self.rate_limiter.record_response(
            permit,
            status=response.status,
            headers=response_headers,
            body_sample=response.body[:262_144],
        )
        etag = _header(response_headers, "ETag")
        last_modified = _header(response_headers, "Last-Modified")
        retry_value = _header(response_headers, "Retry-After")
        retry_deadline = parse_retry_after(retry_value, finished_at)

        content_hash: str | None = None
        raw_cache_path: str | None = None
        if response.status != 304:
            try:
                content_hash, path = self._cache_body(
                    response.body,
                    source_kind=source_kind,
                    content_type=_header(response_headers, "Content-Type"),
                )
                raw_cache_path = str(path)
            except Exception as cache_error:
                status = FETCH_BLOCKED if decision.blocked else FETCH_RETRY
                retry_at = (
                    decision.retry_at
                    if decision.blocked and decision.retry_at is not None
                    else self._attempt_backoff(crawl_url_id)
                )
                error = f"raw evidence cache error: {type(cache_error).__name__}: {cache_error}"
                self.database.record_fetch_failure(
                    crawl_url_id=crawl_url_id,
                    attempt_id=attempt_id,
                    fetch_status=status,
                    finished_at=finished_at,
                    error=error,
                    http_status=response.status,
                    retry_after_value=retry_value,
                    retry_after_until=retry_deadline,
                    next_allowed_attempt=retry_at,
                    response_headers=response_headers,
                    manual_review_status="required",
                )
                return FetchResult(
                    crawl_url_id=crawl_url_id,
                    request_url=target,
                    outcome=status,
                    http_status=response.status,
                    retry_at=retry_at,
                    error=error,
                )

        # A CAPTCHA/challenge can arrive with HTTP 200. The limiter's decision
        # takes precedence over the status code so challenge HTML can never be
        # accepted as a successfully fetched card page.
        if decision.blocked:
            evidence_snapshot: PageSnapshot | None = None
            if content_hash is not None and raw_cache_path is not None:
                evidence_snapshot = self.database.record_response_evidence(
                    crawl_url_id=crawl_url_id,
                    attempt_id=attempt_id,
                    source_kind=source_kind,
                    captured_at=captured_at,
                    archive_timestamp=archive_timestamp,
                    content_hash=content_hash,
                    raw_cache_path=raw_cache_path,
                    http_status=response.status,
                    final_url=response.final_url,
                    response_headers=response_headers,
                )
            retry_at = decision.retry_at or retry_deadline or self._attempt_backoff(
                crawl_url_id
            )
            error = decision.reason or f"HTTP {response.status} host block"
            self.database.record_fetch_failure(
                crawl_url_id=crawl_url_id,
                attempt_id=attempt_id,
                fetch_status=FETCH_BLOCKED,
                finished_at=finished_at,
                error=error,
                http_status=response.status,
                retry_after_value=retry_value,
                retry_after_until=retry_deadline,
                next_allowed_attempt=retry_at,
                response_headers=response_headers,
                manual_review_status=(
                    "required"
                    if response.status == 403
                    or "captcha" in error.casefold()
                    or "human-verification" in error.casefold()
                    else None
                ),
            )
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome=FETCH_BLOCKED,
                http_status=response.status,
                snapshot_id=(
                    None if evidence_snapshot is None else evidence_snapshot.snapshot_id
                ),
                content_hash=content_hash,
                raw_cache_path=raw_cache_path,
                retry_at=retry_at,
                error=error,
            )

        if response.status == 304:
            if crawl_url.latest_snapshot_id is None:
                error = "HTTP 304 received without a cached snapshot"
                self.database.record_fetch_failure(
                    crawl_url_id=crawl_url_id,
                    attempt_id=attempt_id,
                    fetch_status=FETCH_FAILED,
                    finished_at=finished_at,
                    error=error,
                    http_status=304,
                    response_headers=response_headers,
                    manual_review_status="required",
                )
                return FetchResult(
                    crawl_url_id=crawl_url_id,
                    request_url=target,
                    outcome=FETCH_FAILED,
                    http_status=304,
                    error=error,
                )
            snapshot = self.database.record_not_modified(
                crawl_url_id=crawl_url_id,
                attempt_id=attempt_id,
                at=finished_at,
                response_headers=response_headers,
                etag=etag,
                last_modified=last_modified,
            )
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome="not_modified",
                http_status=304,
                snapshot_id=None if snapshot is None else snapshot.snapshot_id,
                content_hash=None if snapshot is None else snapshot.content_hash,
                raw_cache_path=None if snapshot is None else snapshot.raw_cache_path,
            )

        if 200 <= response.status <= 299:
            if content_hash is None or raw_cache_path is None:
                # Empty 2xx payloads are still evidence: SHA-256 and cache the
                # zero-length file rather than treating it as absent.
                content_hash, path = self._cache_body(
                    b"",
                    source_kind=source_kind,
                    content_type=_header(response_headers, "Content-Type"),
                )
                raw_cache_path = str(path)
            snapshot = self.database.record_fetch_success(
                crawl_url_id=crawl_url_id,
                attempt_id=attempt_id,
                source_kind=source_kind,
                captured_at=captured_at,
                fetched_at=finished_at,
                archive_timestamp=archive_timestamp,
                content_hash=content_hash,
                raw_cache_path=raw_cache_path,
                http_status=response.status,
                final_url=response.final_url,
                response_headers=response_headers,
                etag=etag,
                last_modified=last_modified,
            )
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome="fetched",
                http_status=response.status,
                snapshot_id=snapshot.snapshot_id,
                content_hash=content_hash,
                raw_cache_path=raw_cache_path,
            )

        # Cache non-success bodies as immutable evidence too. The generic
        # evidence row is linked to the failed attempt without marking it as a
        # successful crawl.
        evidence_snapshot = None
        if content_hash is not None and raw_cache_path is not None:
            evidence_snapshot = self.database.record_response_evidence(
                crawl_url_id=crawl_url_id,
                attempt_id=attempt_id,
                source_kind=source_kind,
                captured_at=captured_at,
                archive_timestamp=archive_timestamp,
                content_hash=content_hash,
                raw_cache_path=raw_cache_path,
                http_status=response.status,
                final_url=response.final_url,
                response_headers=response_headers,
            )

        if response.status in (403, 429):
            status = FETCH_BLOCKED
            retry_at = decision.retry_at or retry_deadline or self._attempt_backoff(
                crawl_url_id
            )
            error = decision.reason or f"HTTP {response.status} host block"
        elif response.status in (404, 410):
            status = FETCH_CONFIRMED_MISSING
            retry_at = None
            error = f"HTTP {response.status} confirmed missing"
        elif 500 <= response.status <= 599 or response.status in (408, 425):
            status = FETCH_RETRY
            retry_at = retry_deadline or decision.retry_at or self._attempt_backoff(
                crawl_url_id
            )
            error = f"HTTP {response.status} transient response"
        else:
            status = FETCH_FAILED
            retry_at = None
            error = f"HTTP {response.status} non-success response"

        self.database.record_fetch_failure(
            crawl_url_id=crawl_url_id,
            attempt_id=attempt_id,
            fetch_status=status,
            finished_at=finished_at,
            error=error,
            http_status=response.status,
            retry_after_value=retry_value,
            retry_after_until=retry_deadline,
            next_allowed_attempt=retry_at,
            response_headers=response_headers,
            manual_review_status="required" if status == FETCH_FAILED else None,
        )
        return FetchResult(
            crawl_url_id=crawl_url_id,
            request_url=target,
            outcome=status,
            http_status=response.status,
            snapshot_id=(
                None if evidence_snapshot is None else evidence_snapshot.snapshot_id
            ),
            content_hash=content_hash,
            raw_cache_path=raw_cache_path,
            retry_at=retry_at,
            error=error,
        )

    def fetch_ready(
        self,
        *,
        source: str | None = None,
        limit: int = 100,
    ) -> tuple[FetchResult, ...]:
        ready = self.database.list_ready(
            now=float(self.clock()), source=source, limit=limit
        )
        return tuple(self.fetch(row.crawl_url_id) for row in ready)

    def reparse_cached(
        self,
        snapshot_id: int,
        *,
        parser_version: str,
        parser: Callable[[bytes, PageSnapshot], ParseOutcome],
        force: bool = False,
    ) -> ParseOutcome:
        version = parser_version.strip()
        if not version:
            raise ValueError("parser version is required")
        if not force:
            for existing in self.database.parse_results_for_snapshot(snapshot_id):
                if existing["parser_version"] == version:
                    return ParseOutcome(
                        status=str(existing["parse_status"]),
                        result=existing["result"],
                        canonical_card_id=existing["canonical_card_id"],
                        missing_fields=tuple(existing["missing_fields"]),
                        assets=tuple(existing["assets"]),
                        manual_review_status=str(existing["manual_review_status"]),
                        error=existing["error"],
                    )

        snapshot = self.database.get_snapshot(snapshot_id)
        cache_path = Path(snapshot.raw_cache_path)
        try:
            body = cache_path.read_bytes()
            actual_hash = hashlib.sha256(body).hexdigest()
            if actual_hash != snapshot.content_hash:
                raise ValueError(
                    f"cached content hash mismatch: expected {snapshot.content_hash}, got {actual_hash}"
                )
            outcome = parser(body, snapshot)
            if not isinstance(outcome, ParseOutcome):
                raise TypeError("parser must return ParseOutcome")
        except Exception as error:
            outcome = ParseOutcome(
                status="error",
                result=None,
                missing_fields=("raw_parse_result",),
                manual_review_status="required",
                error=f"{type(error).__name__}: {error}",
            )

        self.database.record_parse_result(
            snapshot_id=snapshot_id,
            parser_version=version,
            parse_status=outcome.status,
            result=outcome.result,
            canonical_card_id=outcome.canonical_card_id,
            missing_fields=outcome.missing_fields,
            assets=outcome.assets,
            manual_review_status=outcome.manual_review_status,
            error=outcome.error,
            parsed_at=float(self.clock()),
        )
        for asset in outcome.assets:
            if not isinstance(asset, Mapping):
                continue
            asset_url = asset.get("asset_url", asset.get("url"))
            if not isinstance(asset_url, str) or not asset_url.strip():
                continue
            kind = asset.get("asset_kind", asset.get("kind", "unknown"))
            self.database.upsert_asset(
                crawl_url_id=snapshot.crawl_url_id,
                snapshot_id=snapshot_id,
                asset_url=asset_url,
                asset_kind=str(kind),
                metadata={str(key): value for key, value in asset.items()},
            )
        return outcome

    def _attempt_backoff(self, crawl_url_id: int) -> float:
        # The current attempt is still marked "started" when this is called.
        # Base the exponent on the immediately preceding retry/block streak,
        # never on the URL's lifetime attempt count.
        prior_failures = self.database.consecutive_retry_failures(crawl_url_id)
        minutes = min(
            self.config.rate_limit.backoff_max_minutes,
            self.config.rate_limit.backoff_base_minutes
            * (2 ** min(20, prior_failures)),
        )
        return float(self.clock()) + minutes * 60.0

    def _robots_preflight(
        self,
        crawl_url_id: int,
        target: str,
    ) -> FetchResult | None:
        """Return a blocking result, or ``None`` when robots permits target.

        A robots response is cached per origin for a bounded TTL. A confirmed
        404 or 410 means no robots policy is published for that TTL. Network,
        parse, redirect, and other ambiguous failures fail closed for at least
        the same TTL rather than guessing or entering a retry loop.
        """

        parsed_target = urlsplit(target)
        if (
            parsed_target.scheme.casefold() not in {"http", "https"}
            or not parsed_target.netloc
        ):
            return None
        now = float(self.clock())
        target_policy = self.database.find_url("robots_policy", target)
        if (
            target_policy is not None
            and target_policy.next_allowed_attempt is not None
            and target_policy.next_allowed_attempt > now
        ):
            return FetchResult(
                crawl_url_id=crawl_url_id,
                request_url=target,
                outcome=FETCH_BLOCKED,
                retry_at=target_policy.next_allowed_attempt,
                error=target_policy.last_error,
            )
        robot_url = urlunsplit(
            (parsed_target.scheme, parsed_target.netloc, "/robots.txt", "", "")
        )
        robot_row = self.database.discover_url(
            source="robots",
            url=robot_url,
            discovery_method="automatic_robots_preflight",
            discovered_from=target,
            now=now,
        )

        last_checked = robot_row.last_attempt_at
        policy_is_fresh = (
            last_checked is not None
            and now - last_checked < self.robots_cache_ttl_seconds
        )
        if robot_row.fetch_status == FETCH_CONFIRMED_MISSING and policy_is_fresh:
            return None
        if robot_row.latest_snapshot_id is None or not policy_is_fresh:
            robot_result = self.fetch(robot_row.crawl_url_id, source_kind="robots")
            if robot_result.outcome == FETCH_CONFIRMED_MISSING:
                return None
            if robot_result.outcome not in {"fetched", "not_modified"}:
                retry_at = max(
                    robot_result.retry_at or 0.0,
                    float(self.clock()) + self.robots_cache_ttl_seconds,
                )
                reason = (
                    "robots.txt could not be verified; crawl deferred: "
                    f"{robot_result.error or robot_result.outcome}"
                )
                # Failure to establish the origin policy applies to every
                # target on that origin, so extend the shared robots row too.
                self.database.defer_url(
                    robot_row.crawl_url_id,
                    until=retry_at,
                    reason=reason,
                )
                return self._robots_blocked_result(
                    crawl_url_id=crawl_url_id,
                    target=target,
                    robot_url=robot_url,
                    retry_at=retry_at,
                    reason=reason,
                    http_status=robot_result.http_status,
                )
            robot_row = self.database.get_url(robot_row.crawl_url_id)

        try:
            if robot_row.latest_snapshot_id is None:
                raise ValueError("successful robots response has no cached snapshot")
            snapshot = self.database.get_snapshot(robot_row.latest_snapshot_id)
            body = Path(snapshot.raw_cache_path).read_bytes()
            actual_hash = hashlib.sha256(body).hexdigest()
            if actual_hash != snapshot.content_hash:
                raise ValueError("cached robots content hash mismatch")
            media_type = (
                _header(snapshot.response_headers, "Content-Type") or ""
            ).split(";", 1)[0].strip().casefold()
            text_body = body.decode("utf-8-sig", errors="replace")
            significant_lines = [
                line.split("#", 1)[0].strip()
                for line in text_body.splitlines()
                if line.split("#", 1)[0].strip()
            ]
            if (
                media_type in {"text/html", "application/xhtml+xml"}
                or text_body.lstrip().startswith("<")
                or "\x00" in text_body
                or not any(
                    re.match(r"(?i)^user-agent\s*:", line)
                    for line in significant_lines
                )
                or any(":" not in line for line in significant_lines)
            ):
                raise ValueError("response is not a valid robots.txt policy document")
            policy = RobotFileParser()
            policy.set_url(robot_url)
            policy.parse(text_body.splitlines())
            allowed = policy.can_fetch(self.config.user_agent, target)
        except Exception as error:
            retry_at = float(self.clock()) + self.robots_cache_ttl_seconds
            reason = (
                "robots.txt could not be parsed; crawl deferred: "
                f"{type(error).__name__}: {error}"
            )
            return self._robots_blocked_result(
                crawl_url_id=crawl_url_id,
                target=target,
                robot_url=robot_url,
                retry_at=retry_at,
                reason=reason,
                http_status=snapshot.http_status if "snapshot" in locals() else None,
            )

        if allowed:
            return None
        retry_at = float(self.clock()) + self.robots_cache_ttl_seconds
        reason = f"robots.txt disallows {target} for {self.config.user_agent}"
        return self._robots_blocked_result(
            crawl_url_id=crawl_url_id,
            target=target,
            robot_url=robot_url,
            retry_at=retry_at,
            reason=reason,
            http_status=snapshot.http_status,
        )

    def _robots_blocked_result(
        self,
        *,
        crawl_url_id: int,
        target: str,
        robot_url: str,
        retry_at: float,
        reason: str,
        http_status: int | None,
    ) -> FetchResult:
        # The crawl row describes a logical source card, while target can be a
        # particular Wayback capture or CDX query. Keep the durable robots
        # cooldown on the actual request target so one archive decision cannot
        # suppress a later direct-source attempt for the same card.
        policy_row = self.database.discover_url(
            source="robots_policy",
            url=target,
            discovery_method="automatic_robots_target_decision",
            discovered_from=robot_url,
            now=float(self.clock()),
        )
        self.database.defer_url(
            policy_row.crawl_url_id,
            until=retry_at,
            reason=reason,
        )
        return FetchResult(
            crawl_url_id=crawl_url_id,
            request_url=target,
            outcome=FETCH_BLOCKED,
            http_status=http_status,
            retry_at=retry_at,
            error=reason,
        )

    def _cache_body(
        self,
        body: bytes,
        *,
        source_kind: str,
        content_type: str | None,
    ) -> tuple[str, Path]:
        digest = hashlib.sha256(body).hexdigest()
        namespace = re.sub(r"[^a-zA-Z0-9_.-]+", "_", source_kind).strip("._")
        if not namespace:
            namespace = "unknown"
        suffix = self._content_suffix(content_type)
        directory = self.config.raw_cache_root / namespace / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        destination = (directory / f"{digest}{suffix}").resolve()
        if destination.exists():
            if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
                raise OSError(f"content-addressed cache corruption at {destination}")
            return digest, destination

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{digest}.", suffix=".tmp", dir=directory
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()
        return digest, destination

    @staticmethod
    def _content_suffix(content_type: str | None) -> str:
        media_type = (content_type or "").split(";", 1)[0].strip().casefold()
        return {
            "text/html": ".html",
            "application/xhtml+xml": ".html",
            "application/json": ".json",
            "application/ld+json": ".json",
            "application/xml": ".xml",
            "text/xml": ".xml",
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "text/css": ".css",
            "text/javascript": ".js",
            "application/javascript": ".js",
        }.get(media_type, ".bin")
