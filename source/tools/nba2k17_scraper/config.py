from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_USER_AGENT = (
    "NBA2K17-MyTEAM-Preservation-Scraper/0.1 "
    "(offline preservation research; single-request crawler)"
)


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Conservative per-host request policy.

    A larger crawl priority is considered more urgent. Backoff values are in
    minutes by design: blocking responses must never produce a tight retry
    loop.
    """

    minimum_spacing_seconds: float = 15.0
    jitter_min_seconds: float = 5.0
    jitter_max_seconds: float = 15.0
    # None means there is no project-imposed daily ceiling. Server-provided
    # Retry-After, blocking, CAPTCHA, and repeated-error cooldowns still apply.
    daily_request_limit: int | None = None
    repeated_5xx_threshold: int = 3
    backoff_base_minutes: float = 5.0
    backoff_max_minutes: float = 24.0 * 60.0
    blocking_cooldown_minutes: float = 60.0
    captcha_cooldown_minutes: float = 24.0 * 60.0

    def __post_init__(self) -> None:
        if self.minimum_spacing_seconds < 0:
            raise ValueError("minimum spacing cannot be negative")
        if self.jitter_min_seconds < 0:
            raise ValueError("minimum jitter cannot be negative")
        if self.jitter_max_seconds < self.jitter_min_seconds:
            raise ValueError("maximum jitter cannot be below minimum jitter")
        if self.daily_request_limit is not None and self.daily_request_limit < 1:
            raise ValueError("daily request limit must be positive")
        if self.repeated_5xx_threshold < 1:
            raise ValueError("repeated 5xx threshold must be positive")
        for label, value in (
            ("backoff base", self.backoff_base_minutes),
            ("backoff maximum", self.backoff_max_minutes),
            ("blocking cooldown", self.blocking_cooldown_minutes),
            ("CAPTCHA cooldown", self.captcha_cooldown_minutes),
        ):
            if value <= 0:
                raise ValueError(f"{label} must be positive")
        if self.backoff_max_minutes < self.backoff_base_minutes:
            raise ValueError("maximum backoff cannot be below base backoff")


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    data_root: Path
    user_agent: str = DEFAULT_USER_AGENT
    request_timeout_seconds: float = 30.0
    rate_limit: RateLimitPolicy = field(default_factory=RateLimitPolicy)

    def __post_init__(self) -> None:
        if not self.user_agent.strip():
            raise ValueError("a transparent nonempty User-Agent is required")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request timeout must be positive")

    @property
    def database_path(self) -> Path:
        return self.data_root / "crawl.sqlite"

    @property
    def raw_cache_root(self) -> Path:
        return self.data_root / "raw"

    @property
    def normalized_root(self) -> Path:
        return self.data_root / "normalized"

    @property
    def reports_root(self) -> Path:
        return self.data_root / "reports"

    @property
    def manual_review_root(self) -> Path:
        return self.data_root / "manual_review"
