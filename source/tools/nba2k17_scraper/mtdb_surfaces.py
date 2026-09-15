"""Inspect observed MTDB links/scripts without inventing detail routes or APIs."""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit, urlunsplit

from .sources.html_dom import parse_html

RELATED = re.compile(r"badge|tenden", re.I)
HOSTS = {"mtdb.com", "www.mtdb.com", "content.mtdb.com", "cdn.mtdb.com"}


def inspect_script(body: str, url: str) -> dict:
    # Evidence inventory only: never execute JS, substitute IDs or guess API URLs.
    return {
        "url": url,
        "relevant_excerpts": [line.strip()[:2000] for line in body.splitlines() if RELATED.search(line)][:100],
        "literal_references": sorted(set(match.group(1) for match in re.finditer(
            r'''["']([^"'\s]{0,250}(?:badge|tenden)[^"'\s]{0,250})["']''', body, re.I))),
        "request_excerpts": [line.strip()[:2000] for line in body.splitlines() if re.search(
            r"\$\.(?:ajax|getJSON|get|post)\s*\(|\bfetch\s*\(|\.load\s*\(|XMLHttpRequest", line)][:100],
    }


def inspect_page(body: str, url: str) -> dict:
    root = parse_html(body)
    result = {"url": url, "in_page_tabs": [], "detail_urls": [],
              "other_related_references": [], "script_urls": [], "inline_script_evidence": []}
    original = urlsplit(url)
    for element in root.iter_elements():
        if element.tag == "script":
            src = element.get("src")
            if src:
                resolved = urljoin(url, src)
                parts = urlsplit(resolved)
                if parts.scheme in ("http", "https") and parts.hostname in HOSTS:
                    result["script_urls"].append(resolved)
            else:
                evidence = inspect_script(element.text_content(separator="\n"), url)
                if evidence["relevant_excerpts"]:
                    result["inline_script_evidence"].append(evidence)
        for attr in ("href", "data-url", "data-src", "data-remote"):
            ref = element.get(attr)
            if not ref or not RELATED.search(ref + " " + element.text_content()[:100]):
                continue
            resolved = urljoin(url, ref)
            parts = urlsplit(resolved)
            if (parts.scheme, parts.netloc, parts.path, parts.query) == (
                    original.scheme, original.netloc, original.path, original.query) and parts.fragment:
                result["in_page_tabs"].append("#" + parts.fragment)
            # Automatically eligible only when the observed URL is a child of
            # THIS exact card; different cards and unknown APIs require review.
            elif (parts.scheme in ("http", "https") and parts.hostname == original.hostname
                  and not parts.query and parts.path.startswith(original.path.rstrip("/") + "/")
                  and RELATED.search(parts.path[len(original.path.rstrip("/")) + 1:])):
                result["detail_urls"].append(urlunsplit(parts._replace(fragment="")))
            else:
                result["other_related_references"].append(resolved)
    for key in ("in_page_tabs", "detail_urls", "other_related_references", "script_urls"):
        result[key] = sorted(set(result[key]))
    return result
