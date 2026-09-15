"""Resumable, evidence-preserving NBA 2K17 MyTEAM scraping core."""

from .archive import CdxRecord, WaybackCdx, WaybackReplay
from .config import DEFAULT_USER_AGENT, RateLimitPolicy, ScraperConfig
from .database import CrawlDatabase, CrawlUrl, HostState, PageSnapshot
from .discovery import SourceDiscoveryRules, discover_index_document, extract_links
from .fetcher import (
    FetchRequest,
    FetchResponse,
    FetchResult,
    Fetcher,
    ParseOutcome,
    UrllibTransport,
)
from .rate_limit import DurableRateLimiter, HostPaused, RateLimitDecision

__all__ = [
    "CdxRecord",
    "CrawlDatabase",
    "CrawlUrl",
    "DEFAULT_USER_AGENT",
    "DurableRateLimiter",
    "FetchRequest",
    "FetchResponse",
    "FetchResult",
    "Fetcher",
    "HostPaused",
    "HostState",
    "PageSnapshot",
    "ParseOutcome",
    "RateLimitDecision",
    "RateLimitPolicy",
    "ScraperConfig",
    "SourceDiscoveryRules",
    "UrllibTransport",
    "WaybackCdx",
    "WaybackReplay",
    "discover_index_document",
    "extract_links",
]

__version__ = "0.1.0"
