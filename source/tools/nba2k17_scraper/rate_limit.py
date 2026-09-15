from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
import random
import hashlib
import os
from pathlib import Path
import threading
import time
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit

from .config import RateLimitPolicy
from .database import CrawlDatabase, HostState


_LOCK_REGISTRY_GUARD = threading.Lock()
_HOST_LOCKS: dict[tuple[str, str], _ProcessHostLock] = {}


class _ProcessHostLock:
    """Thread + OS file lock, released by the OS if a worker exits/crashes."""

    def __init__(self, database_identity: str, host: str):
        self.thread_lock = threading.Lock()
        self.path = Path(database_identity).parent / '.host-locks' / (
            hashlib.sha256((database_identity + '\n' + host).encode()).hexdigest() + '.lock')
        self.handle = None

    def acquire(self):
        self.thread_lock.acquire()
        handle = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open('a+b')
            if self.path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            if os.name == 'nt':
                import msvcrt
                while True:
                    handle.seek(0)
                    try:
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError as error:
                        if error.errno not in (13, 11, 36):
                            raise
                        time.sleep(0.1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            self.handle = handle
        except BaseException:
            if handle is not None:
                handle.close()
            self.thread_lock.release()
            raise

    def release(self):
        try:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
        finally:
            self.thread_lock.release()


def _shared_host_lock(database_identity: str, host: str) -> _ProcessHostLock:
    key = (database_identity, host)
    with _LOCK_REGISTRY_GUARD:
        lock = _HOST_LOCKS.get(key)
        if lock is None:
            lock = _ProcessHostLock(database_identity, host)
            _HOST_LOCKS[key] = lock
        return lock


def _utc_day(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def _next_utc_day(timestamp: float) -> float:
    current = datetime.fromtimestamp(timestamp, timezone.utc)
    tomorrow = current.date() + timedelta(days=1)
    return datetime.combine(tomorrow, datetime_time.min, tzinfo=timezone.utc).timestamp()


def parse_retry_after(value: str | None, now: float) -> float | None:
    """Return the exact Retry-After deadline for seconds or an HTTP date."""

    if value is None:
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.isdecimal():
        return now + int(raw)
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(now, parsed.timestamp())


def _header(headers: Mapping[str, str], name: str) -> str | None:
    target = name.casefold()
    for key, value in headers.items():
        if str(key).casefold() == target:
            return str(value)
    return None


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    blocked: bool
    retry_at: float | None = None
    reason: str | None = None
    retry_after_value: str | None = None
    consecutive_5xx: int = 0


class HostPaused(RuntimeError):
    def __init__(self, host: str, resume_at: float, reason: str):
        super().__init__(f"host {host} is paused until {resume_at:.3f}: {reason}")
        self.host = host
        self.resume_at = resume_at
        self.reason = reason


@dataclass(slots=True)
class HostPermit:
    host: str
    started_at: float
    _lock: _ProcessHostLock
    _released: bool = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class DurableRateLimiter:
    """One in-flight request per host with durable spacing and block state."""

    def __init__(
        self,
        database: CrawlDatabase,
        policy: RateLimitPolicy | None = None,
        *,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] | None = None,
    ):
        self.database = database
        self.policy = policy or RateLimitPolicy()
        self.clock = clock
        self.sleeper = sleeper
        self.jitter = jitter or random.SystemRandom().uniform

    @staticmethod
    def host_from_url(url: str) -> str:
        parts = urlsplit(url)
        host = parts.hostname
        if not host:
            raise ValueError(f"URL has no host: {url!r}")
        return host.casefold()

    def acquire(self, host_or_url: str) -> HostPermit:
        host = (
            self.host_from_url(host_or_url)
            if "://" in host_or_url
            else host_or_url.strip().casefold()
        )
        if not host:
            raise ValueError("host is required")
        lock = _shared_host_lock(self.database.identity, host)
        lock.acquire()
        try:
            while True:
                now = float(self.clock())
                state = self.database.get_host_state(host)
                if state.blocked_until is not None and state.blocked_until > now:
                    raise HostPaused(host, state.blocked_until, state.block_reason or "host pause")

                current_day = _utc_day(now)
                daily_count = (
                    state.daily_request_count if state.daily_window == current_day else 0
                )
                if (
                    self.policy.daily_request_limit is not None
                    and daily_count >= self.policy.daily_request_limit
                ):
                    resume_at = _next_utc_day(now)
                    self.database.mutate_host_state(
                        host,
                        lambda _state: {
                            "blocked_until": resume_at,
                            "block_reason": "daily request ceiling reached",
                            "daily_window": current_day,
                            "daily_request_count": daily_count,
                        },
                        now=now,
                    )
                    raise HostPaused(host, resume_at, "daily request ceiling reached")

                if state.next_request_at is not None and state.next_request_at > now:
                    self.sleeper(state.next_request_at - now)
                    continue

                jitter_seconds = float(
                    self.jitter(
                        self.policy.jitter_min_seconds,
                        self.policy.jitter_max_seconds,
                    )
                )
                jitter_seconds = max(
                    self.policy.jitter_min_seconds,
                    min(self.policy.jitter_max_seconds, jitter_seconds),
                )
                next_request = (
                    now + self.policy.minimum_spacing_seconds + jitter_seconds
                )
                self.database.mutate_host_state(
                    host,
                    lambda current: {
                        "last_request_at": now,
                        "next_request_at": next_request,
                        "daily_window": current_day,
                        "daily_request_count": (
                            current.daily_request_count + 1
                            if current.daily_window == current_day
                            else 1
                        ),
                        "blocked_until": None,
                        "block_reason": None,
                    },
                    now=now,
                )
                return HostPermit(host=host, started_at=now, _lock=lock)
        except BaseException:
            lock.release()
            raise

    def record_response(
        self,
        permit: HostPermit,
        *,
        status: int,
        headers: Mapping[str, str],
        body_sample: bytes = b"",
    ) -> RateLimitDecision:
        now = float(self.clock())
        retry_after_value = _header(headers, "Retry-After")
        retry_after_until = parse_retry_after(retry_after_value, now)
        captcha = self.looks_like_captcha(headers, body_sample)
        try:
            state = self.database.get_host_state(permit.host)
            if captcha:
                retry_at = now + self.policy.captcha_cooldown_minutes * 60.0
                self.database.mutate_host_state(
                    permit.host,
                    lambda current: {
                        "blocked_until": retry_at,
                        "block_reason": "CAPTCHA or human-verification response",
                        "blocking_events": current.blocking_events + 1,
                        "captcha_detected": True,
                    },
                    now=now,
                )
                return RateLimitDecision(
                    blocked=True,
                    retry_at=retry_at,
                    reason="CAPTCHA or human-verification response",
                    retry_after_value=retry_after_value,
                    consecutive_5xx=state.consecutive_5xx,
                )

            if status in (403, 429):
                event_count = state.blocking_events + 1
                retry_at = retry_after_until
                if retry_at is None:
                    minutes = min(
                        self.policy.backoff_max_minutes,
                        self.policy.blocking_cooldown_minutes
                        * (2 ** max(0, event_count - 1)),
                    )
                    retry_at = now + minutes * 60.0
                reason = f"HTTP {status} host block"
                self.database.mutate_host_state(
                    permit.host,
                    lambda _current: {
                        "blocked_until": retry_at,
                        "block_reason": reason,
                        "blocking_events": event_count,
                    },
                    now=now,
                )
                return RateLimitDecision(
                    blocked=True,
                    retry_at=retry_at,
                    reason=reason,
                    retry_after_value=retry_after_value,
                    consecutive_5xx=state.consecutive_5xx,
                )

            if 500 <= status <= 599:
                consecutive = state.consecutive_5xx + 1
                # An explicit server deadline is authoritative even before the
                # repeated-5xx threshold. Otherwise the third consecutive 5xx
                # pauses the host with minute-based exponential backoff.
                blocked = (
                    retry_after_until is not None
                    or consecutive >= self.policy.repeated_5xx_threshold
                )
                retry_at: float | None = retry_after_until
                reason = f"HTTP {status}"
                changes: dict[str, object] = {"consecutive_5xx": consecutive}
                if blocked:
                    if retry_at is None:
                        exponent = consecutive - self.policy.repeated_5xx_threshold
                        minutes = min(
                            self.policy.backoff_max_minutes,
                            self.policy.backoff_base_minutes * (2**exponent),
                        )
                        retry_at = now + minutes * 60.0
                        reason = f"repeated HTTP 5xx ({consecutive} consecutive)"
                    else:
                        reason = f"HTTP {status} Retry-After"
                    changes.update(
                        {
                            "blocked_until": retry_at,
                            "block_reason": reason,
                            "blocking_events": state.blocking_events + 1,
                        }
                    )
                self.database.mutate_host_state(
                    permit.host, lambda _current: changes, now=now
                )
                return RateLimitDecision(
                    blocked=blocked,
                    retry_at=retry_at,
                    reason=reason,
                    retry_after_value=retry_after_value,
                    consecutive_5xx=consecutive,
                )

            if 200 <= status <= 399:
                self.database.mutate_host_state(
                    permit.host,
                    lambda _current: {
                        "consecutive_5xx": 0,
                        "blocking_events": 0,
                        "blocked_until": None,
                        "block_reason": None,
                        "captcha_detected": False,
                    },
                    now=now,
                )
            else:
                self.database.mutate_host_state(
                    permit.host,
                    lambda _current: {"consecutive_5xx": 0},
                    now=now,
                )
            return RateLimitDecision(
                blocked=False,
                retry_after_value=retry_after_value,
                consecutive_5xx=0,
            )
        finally:
            permit.release()

    def record_transport_error(
        self,
        permit: HostPermit,
        error: BaseException | str,
    ) -> RateLimitDecision:
        now = float(self.clock())
        message = str(error)
        lower = message.casefold()
        throttle = any(
            marker in lower
            for marker in (
                "rate limit",
                "too many requests",
                "throttl",
                "captcha",
                "verify you are human",
            )
        )
        try:
            if throttle:
                state = self.database.get_host_state(permit.host)
                minutes = min(
                    self.policy.backoff_max_minutes,
                    self.policy.blocking_cooldown_minutes
                    * (2 ** state.blocking_events),
                )
                retry_at = now + minutes * 60.0
                self.database.mutate_host_state(
                    permit.host,
                    lambda current: {
                        "blocked_until": retry_at,
                        "block_reason": "transport reported throttling",
                        "blocking_events": current.blocking_events + 1,
                    },
                    now=now,
                )
                return RateLimitDecision(
                    blocked=True,
                    retry_at=retry_at,
                    reason="transport reported throttling",
                )
            retry_at = now + self.policy.backoff_base_minutes * 60.0
            return RateLimitDecision(
                blocked=False,
                retry_at=retry_at,
                reason=f"transport error: {message}",
            )
        finally:
            permit.release()

    def abort(self, permit: HostPermit) -> None:
        permit.release()

    def clear_host_pause(self, host: str) -> HostState:
        now = float(self.clock())
        return self.database.mutate_host_state(
            host,
            lambda _state: {
                "blocked_until": None,
                "block_reason": None,
                "consecutive_5xx": 0,
                "blocking_events": 0,
                "captcha_detected": False,
            },
            now=now,
        )

    @staticmethod
    def looks_like_captcha(
        headers: Mapping[str, str], body_sample: bytes | str
    ) -> bool:
        if (_header(headers, "cf-mitigated") or "").casefold() == "challenge":
            return True
        if isinstance(body_sample, bytes):
            text = body_sample[:262_144].decode("utf-8", errors="ignore")
        else:
            text = body_sample[:262_144]
        lower = text.casefold()
        return any(
            marker in lower
            for marker in (
                "captcha",
                "cf-chl-captcha",
                "verify you are human",
                "unusual traffic from your computer network",
                "human verification",
                "checking your browser",
                "/cdn-cgi/challenge-platform/",
                "attention required! | cloudflare",
            )
        )
