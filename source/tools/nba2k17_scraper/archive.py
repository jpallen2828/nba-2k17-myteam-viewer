from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import quote, urlencode, urlsplit


_WAYBACK_TIMESTAMP = re.compile(r"^\d{14}$")


def _validate_public_url(url: str) -> str:
    value = url.strip()
    parts = urlsplit(value)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"expected an absolute HTTP(S) URL, got {url!r}")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class CdxRecord:
    """One immutable capture described by the Internet Archive CDX index."""

    timestamp: str
    original_url: str
    status: int | None = None
    mime_type: str | None = None
    digest: str | None = None
    length: int | None = None

    def __post_init__(self) -> None:
        if not _WAYBACK_TIMESTAMP.fullmatch(self.timestamp):
            raise ValueError(f"invalid Wayback timestamp {self.timestamp!r}")
        _validate_public_url(self.original_url)


class WaybackCdx:
    """Build and parse CDX index requests; this class never fetches replays."""

    DEFAULT_ENDPOINT = "https://web.archive.org/cdx/search/cdx"
    FIELDS = ("timestamp", "original", "statuscode", "mimetype", "digest", "length")

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = _validate_public_url(endpoint)

    def query_url(
        self,
        target_url: str,
        *,
        from_timestamp: str | None = None,
        to_timestamp: str | None = None,
        limit: int | None = None,
        successful_only: bool = True,
        collapse_digest: bool = False,
    ) -> str:
        """Return an exact-URL CDX query.

        Callers must supply a URL learned from a real index, sitemap, page, or
        prior evidence. This helper intentionally does not manufacture numeric
        card URLs or switch to a domain-wide prefix search.
        """

        target = _validate_public_url(target_url)
        if limit is not None and limit < 1:
            raise ValueError("CDX limit must be positive")
        for label, value in (("from", from_timestamp), ("to", to_timestamp)):
            if value is not None and not re.fullmatch(r"\d{4}(?:\d{2}){0,5}", value):
                raise ValueError(f"invalid CDX {label} timestamp {value!r}")

        parameters: list[tuple[str, str]] = [
            ("url", target),
            ("matchType", "exact"),
            ("output", "json"),
            ("fl", ",".join(self.FIELDS)),
        ]
        if successful_only:
            parameters.append(("filter", "statuscode:200"))
        if collapse_digest:
            parameters.append(("collapse", "digest"))
        if from_timestamp is not None:
            parameters.append(("from", from_timestamp))
        if to_timestamp is not None:
            parameters.append(("to", to_timestamp))
        if limit is not None:
            parameters.append(("limit", str(limit)))
        separator = "&" if "?" in self.endpoint else "?"
        return self.endpoint + separator + urlencode(parameters)

    def prefix_query_url(
        self,
        target_prefix: str,
        *,
        from_timestamp: str | None = None,
        to_timestamp: str | None = None,
        limit: int | None = None,
    ) -> str:
        """Build a URL-index query for one explicitly supplied source prefix.

        This is for discovery from an archival index, not numeric-ID guessing.
        Results are collapsed by URL key and must still be filtered through a
        source adapter before any card URL is queued.
        """

        # `matchType=prefix` already supplies the wildcard semantics. Keep the
        # URL as the literal path prefix; combining an explicit `/*` with
        # matchType produced empty results on the deployed IA CDX endpoint.
        target = _validate_public_url(target_prefix).rstrip("/") + "/"
        if limit is not None and limit < 1:
            raise ValueError("CDX limit must be positive")
        for label, value in (("from", from_timestamp), ("to", to_timestamp)):
            if value is not None and not re.fullmatch(r"\d{4}(?:\d{2}){0,5}", value):
                raise ValueError(f"invalid CDX {label} timestamp {value!r}")
        parameters: list[tuple[str, str]] = [
            ("url", target),
            ("matchType", "prefix"),
            ("output", "json"),
            ("fl", ",".join(self.FIELDS)),
            ("filter", "statuscode:200"),
            ("filter", "mimetype:text/html"),
            ("collapse", "urlkey"),
        ]
        if from_timestamp is not None:
            parameters.append(("from", from_timestamp))
        if to_timestamp is not None:
            parameters.append(("to", to_timestamp))
        if limit is not None:
            parameters.append(("limit", str(limit)))
        separator = "&" if "?" in self.endpoint else "?"
        return self.endpoint + separator + urlencode(parameters)

    def parse(self, document: bytes | str) -> tuple[CdxRecord, ...]:
        """Parse the JSON representation returned by the CDX endpoint."""

        if isinstance(document, bytes):
            text = document.decode("utf-8-sig", errors="strict")
        else:
            text = document.lstrip("\ufeff")
        try:
            payload: Any = json.loads(text)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid CDX JSON: {error}") from error
        if not isinstance(payload, list):
            raise ValueError("CDX JSON root must be an array")
        if not payload:
            return ()

        rows: list[dict[str, object]] = []
        if isinstance(payload[0], list):
            header = [str(item).casefold() for item in payload[0]]
            if "timestamp" not in header or "original" not in header:
                raise ValueError("CDX JSON is missing timestamp/original columns")
            for values in payload[1:]:
                if not isinstance(values, list):
                    continue
                rows.append(
                    {
                        name: values[index] if index < len(values) else None
                        for index, name in enumerate(header)
                    }
                )
        else:
            rows = [dict(item) for item in payload if isinstance(item, dict)]

        records: list[CdxRecord] = []
        seen: set[tuple[str, str, str | None]] = set()
        for row in rows:
            timestamp = str(row.get("timestamp", "")).strip()
            original = str(
                row.get("original", row.get("url", row.get("original_url", "")))
            ).strip()
            if not _WAYBACK_TIMESTAMP.fullmatch(timestamp):
                continue
            try:
                original = _validate_public_url(original)
            except ValueError:
                continue
            digest_value = row.get("digest")
            digest = None if digest_value in (None, "", "-") else str(digest_value)
            key = (timestamp, original, digest)
            if key in seen:
                continue
            seen.add(key)
            mime_value = row.get("mimetype", row.get("mime"))
            records.append(
                CdxRecord(
                    timestamp=timestamp,
                    original_url=original,
                    status=_optional_int(row.get("statuscode", row.get("status"))),
                    mime_type=(
                        None if mime_value in (None, "", "-") else str(mime_value)
                    ),
                    digest=digest,
                    length=_optional_int(row.get("length")),
                )
            )
        records.sort(key=lambda record: (record.timestamp, record.original_url))
        return tuple(records)


class WaybackReplay:
    """Construct raw Wayback replay URLs from already discovered CDX records."""

    DEFAULT_ENDPOINT = "https://web.archive.org/web"

    def __init__(self, endpoint: str = DEFAULT_ENDPOINT):
        self.endpoint = _validate_public_url(endpoint).rstrip("/")

    def replay_url(
        self,
        record_or_timestamp: CdxRecord | str,
        original_url: str | None = None,
    ) -> str:
        if isinstance(record_or_timestamp, CdxRecord):
            if original_url is not None:
                raise ValueError("do not supply original_url with a CdxRecord")
            timestamp = record_or_timestamp.timestamp
            original = record_or_timestamp.original_url
        else:
            timestamp = str(record_or_timestamp)
            if original_url is None:
                raise ValueError("original_url is required with a timestamp")
            original = _validate_public_url(original_url)
        if not _WAYBACK_TIMESTAMP.fullmatch(timestamp):
            raise ValueError(f"invalid Wayback timestamp {timestamp!r}")
        # id_ requests the archived payload without toolbar rewriting. Keep URL
        # delimiters readable but encode fragments/spaces that could change the
        # replay request itself.
        encoded_original = quote(original, safe=":/?&=;%@+$,~!*'()[]")
        return f"{self.endpoint}/{timestamp}id_/{encoded_original}"
