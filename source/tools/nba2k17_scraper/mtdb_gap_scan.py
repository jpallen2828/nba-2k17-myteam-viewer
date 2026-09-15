"""Audited, missing-only MTDB recovery. No 2KMTC requests or app DB export.

Default commands are offline. Live execution requires the exact plan SHA256.
Card pages may be read to identify a variant; images/details require a unique
match to a currently unfilled target. Evidence and fill proposals stay separate
from primary data and the app catalog.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlsplit

from nba2k17_catalog import EXCLUDED_2KMTC_CARDS, exclusion_reason

from .archive import WaybackCdx
from .asset_validator import validate_image_bytes
from .mtdb_surfaces import inspect_page, inspect_script, RELATED
from .models import GAMEPLAY_BADGE_LEVELS
from .normalizer import normalize_parsed_card, canonical_key
from .recovery_worker import ROOT, save
from .sources import MTDBAdapter, SourcePage

DOMAINS = ("card_png", "attributes", "tendencies", "badges")
VERSION = "mtdb-missing-only/1"
DEFAULT_AUDIT = ROOT / "reports/final_audit_2026-09-07/audit_cards.json"
# New plan after the two user-directed removals. The old checkpoint-bound plan
# remains historical evidence and is rejected by verify_plan under this policy.
OUTPUT = ROOT / "reports/mtdb_gap_scan_curated"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def key(value) -> str:
    return canonical_key(str(value or "")).replace("_", "")


def known_identity(value) -> bool:
    return value is not None and key(value) not in {"", "none", "na", "unknown", "nosubcollection"}


def collection_conflicts(primary: dict, incoming: dict, raw) -> bool:
    old, new = primary.get("collection"), incoming.get("collection")
    if not known_identity(old) or not known_identity(new) or key(old) == key(new):
        return False
    path = raw.raw_metadata.get("mtdb_collection_path", "").lower()
    # Cross-site naming differences observed in the saved source evidence.
    # These do not relax the exact attribute fingerprint or uniqueness checks.
    if (path.startswith("/17/collections/current/") and key(primary.get("theme")) == "current"
            and key(primary.get("team")) == key(new) and key(new).endswith(key(old))):
        return False
    if path.startswith("/17/collections/historic/") and key(old) == key(new) + "franchise":
        return False
    return True


def readonly(db_path: Path):
    db = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def primary_fingerprint(db_path: Path) -> str:
    """Ignore MTDB activity, but fail closed if primary evidence changes."""
    with closing(readonly(db_path)) as db:
        rows = [tuple(row) for row in db.execute(
            "SELECT u.url,s.snapshot_id,s.content_hash,s.http_status FROM page_snapshots s "
            "JOIN crawl_urls u USING(crawl_url_id) WHERE u.source='2kmtcentral' ORDER BY s.snapshot_id")]
        rows += [tuple(row) for row in db.execute(
            "SELECT a.asset_id,a.asset_url,a.content_hash,a.fetch_status,a.raw_cache_path "
            "FROM assets a JOIN crawl_urls u USING(crawl_url_id) WHERE u.source='2kmtcentral' ORDER BY a.asset_id")]
    return hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()


def values(record, domain):
    return {k: v for k, v in record.get("card", {}).get(domain, {}).get("values", {}).items()
            if v is not None and not k.startswith("source_")}


def make_plan(cards: list, known: dict) -> dict:
    catalog, targets, excluded, by_name = {}, {}, [], {}
    for row in cards:
        card_id = row["source_card_id"]
        identity = row.get("card", {}).get("identity", {}).get("values", {})
        catalog[card_id] = {"identity": identity, "attributes": values(row, "attributes"),
                            "tendencies": values(row, "tendencies"), "badges": values(row, "badges")}
        # Retain an exclusion tombstone in variant matching so a removed card
        # cannot be mistaken for a different wanted version; never target it.
        if exclusion_reason('2kmtcentral', card_id):
            excluded.append(card_id)
            continue
        gaps = [d for d in DOMAINS if d in row["missing_domains"]]
        if not gaps:
            excluded.append(card_id)
            continue
        wanted = {}
        for domain in ("attributes", "tendencies"):
            if domain in gaps:
                audit = row["domain_audit"][domain]
                wanted[domain] = sorted(set(audit["missing_vs_observed_template"]) | set(audit["explicit_nulls"]))
        targets[card_id] = {"domains": gaps, "missing_fields": wanted,
            "badge_audit": row["badge_audit"], "candidate_urls": [],
            "primary_urls": [u["url"] for u in row["primary_urls"]]}
        names = {key(identity.get("name")), key(identity.get("source_card_slug"))}
        # URL slugs nominate discovery only, never establish a card match.
        names |= {key(urlsplit(u["url"]).path.rstrip("/").split("/")[-1]) for u in row["primary_urls"]}
        for name in names - {""}:
            by_name.setdefault(name, set()).add(card_id)
    probes = []
    for url, slug in sorted(known.items()):
        parts = urlsplit(url)
        if (parts.scheme not in ("http", "https") or parts.hostname not in ("mtdb.com", "www.mtdb.com")
                or parts.query or not re.fullmatch(r"/17/players/[a-z][a-z0-9-]*-\d+/?", parts.path, re.I)
                or parts.path.rstrip("/").split("/")[-1] != slug):
            raise ValueError(f"Unverified/non-card URL in MTDB index: {url}")
        ids = sorted(by_name.get(key(re.sub(r"-\d+$", "", slug)), []))
        if ids:
            probes.append({"url": url, "slug": slug, "target_ids": ids})
            for card_id in ids:
                targets[card_id]["candidate_urls"].append(url)
    return {"schema": VERSION, "created_at": time.time(), "network_started": False,
        "policy": {"primary_overwrite": False, "app_export": False, "new_cards": False,
                   "request_delay_seconds": [2, 3], "candidate_name_is_not_variant_match": True,
                   "user_excluded_primary_ids": sorted(EXCLUDED_2KMTC_CARDS),
                   "badges": list(GAMEPLAY_BADGE_LEVELS), "empty_sections_are_missing": True},
        "counts": {"primary_cards": len(cards), "target_cards": len(targets), "excluded_cards": len(excluded),
                   "user_excluded_cards": sum(str(r['source_card_id']) in EXCLUDED_2KMTC_CARDS for r in cards),
                   "candidate_page_urls": len(probes), "gap_domains": dict(Counter(d for t in targets.values() for d in t["domains"])),
                   "targets_without_name_candidate": sum(not t["candidate_urls"] for t in targets.values())},
        "targets": targets, "excluded_ids": sorted(excluded), "catalog": catalog, "probes": probes}


def match_variant(raw, catalog: dict) -> dict:
    incoming = normalize_parsed_card(raw.to_dict()).to_dict()
    identity = incoming["identity"]["values"]
    attributes = incoming["attributes"]["values"]
    matches, rejected = [], []
    if not identity.get("name") or identity.get("overall") is None:
        return {"status": "insufficient_identity", "matches": []}
    for card_id, primary in catalog.items():
        ident = primary["identity"]
        if key(ident.get("name")) != key(identity["name"]) or ident.get("overall") != identity["overall"]:
            continue
        conflicts = [field for field in ("tier", "card_year", "team", "primary_position")
                     if known_identity(ident.get(field)) and known_identity(identity.get(field))
                     and key(ident[field]) != key(identity[field])]
        if collection_conflicts(ident, identity, raw):
            conflicts.append("collection")
        common = {k for k in attributes.keys() & primary["attributes"].keys()
                  if isinstance(attributes[k], int) and isinstance(primary["attributes"][k], int)}
        differences = [k for k in sorted(common) if attributes[k] != primary["attributes"][k]]
        evidence = {"card_id": card_id, "equal_attributes": len(common) - len(differences),
                    "attribute_conflicts": differences, "identity_conflicts": conflicts}
        if not conflicts and not differences and len(common) >= 32:
            matches.append(evidence)
        else:
            rejected.append(evidence)
    # Compare to ALL variants, including excluded complete cards. Never choose
    # a missing variant just because a complete equally plausible one was hidden.
    return {"status": "unique" if len(matches) == 1 else "ambiguous" if matches else "unmatched_review",
            "matches": matches, "rejected": rejected}


def field_proposal(raw, target: dict, primary: dict) -> tuple[dict, list]:
    incoming = normalize_parsed_card(raw.to_dict()).to_dict()
    proposed, reviews = {}, []
    for domain in ("attributes", "tendencies"):
        if domain not in target["domains"]:
            continue
        allowed = set(target["missing_fields"].get(domain, []))
        patch = {k: v for k, v in incoming[domain]["values"].items()
                 if k in allowed and primary[domain].get(k) is None
                 and type(v) is int and 0 <= v <= 100}
        if patch:
            proposed[domain] = patch
    if "badges" in target["domains"]:
        audit = target["badge_audit"]
        named = {k: v for k, v in incoming["badges"]["values"].items()
                 if not k.startswith("source_") and v in GAMEPLAY_BADGE_LEVELS}
        counts = {level: sum(v == level for v in named.values()) for level in GAMEPLAY_BADGE_LEVELS}
        expected = audit["declared_main_counts"]
        raw_levels = [badge.level for badge in raw.badges]
        valid_levels = all(key(level) in {"bronze", "silver", "gold", "halloffame", "hof", "personality"} for level in raw_levels)
        levels_by_name = {}
        for badge in raw.badges:
            levels_by_name.setdefault(key(badge.name), set()).add(key(badge.level))
        valid_levels = valid_levels and all(len(levels) == 1 for levels in levels_by_name.values())
        conflicts = [k for k, v in named.items() if primary["badges"].get(k) not in (None, v)]
        if (named and valid_levels and counts == expected and not conflicts
                and audit["status"] != "INCOMPLETE_OR_CONFLICTING_BADGE_EVIDENCE"):
            patch = {k: v for k, v in named.items() if primary["badges"].get(k) is None}
            if patch:
                proposed["badges"] = patch
        elif named:
            reviews.append({"domain": "badges", "reason": "named_tiers_do_not_safely_fill_primary",
                            "observed_counts": counts, "expected_counts": expected, "conflicts": conflicts})
    return proposed, reviews


def remaining_domains(target: dict, staged: dict) -> list:
    remaining = []
    fields = staged.get("fields", {})
    for domain in target["domains"]:
        if domain == "card_png":
            asset = fields.get(domain)
            valid = False
            if asset:
                try:
                    body = Path(asset["raw_cache_path"]).read_bytes()
                    validation = validate_image_bytes(body, filename=asset["url"]).to_dict()
                    valid = hashlib.sha256(body).hexdigest() == asset["content_hash"] and validation["valid"] and validation["actual_format"] == "PNG"
                except (OSError, KeyError, ValueError):
                    pass
            if not valid:
                remaining.append(domain)
        elif domain == "badges":
            if not fields.get(domain):
                remaining.append(domain)
        elif not target["missing_fields"].get(domain) or set(target["missing_fields"][domain]) - fields.get(domain, {}).keys():
            remaining.append(domain)
    return remaining


class ArchiveEvidence:
    """Cache-first archive access. Only the gated runner enables networking."""

    def __init__(self, root: Path, live: bool = False):
        self.root, self.live = root, live
        self.db_path = root / "crawl.sqlite"

    @staticmethod
    def verified(row):
        item = dict(row)
        body = Path(item["raw_cache_path"]).read_bytes()
        if hashlib.sha256(body).hexdigest() != item["content_hash"]:
            raise ValueError(f"Cache SHA256 mismatch: {item['raw_cache_path']}")
        item["body"] = body
        return item

    def cached(self, url: str):
        with closing(readonly(self.db_path)) as db:
            rows = db.execute("SELECT s.*,u.url FROM page_snapshots s JOIN crawl_urls u USING(crawl_url_id) "
                              "WHERE u.url=? AND s.http_status BETWEEN 200 AND 299 "
                              "ORDER BY s.archive_timestamp DESC,s.snapshot_id DESC", (url,)).fetchall()
        return [self.verified(row) for row in rows]

    def documents(self, url: str, source="mtdb"):
        cached = self.cached(url)
        yield from cached
        if not self.live:
            return
        from .mtdb_worker import cdx_records, replay
        seen = {row["archive_timestamp"] for row in cached}
        slug = urlsplit(url).path.rstrip("/").split("/")[-1]
        for record in sorted(cdx_records(url), key=lambda r: r.timestamp, reverse=True):
            if record.timestamp in seen:
                continue
            page_id, snapshot, body = replay(source, url, slug, record)
            seen.add(record.timestamp)
            yield {"url": url, "crawl_url_id": page_id, "snapshot_id": snapshot.snapshot_id,
                   "archive_timestamp": snapshot.archive_timestamp, "captured_at": snapshot.captured_at,
                   "content_hash": snapshot.content_hash, "raw_cache_path": snapshot.raw_cache_path, "body": body}

    def image(self, url: str, parent: dict):
        for image in self.documents(url, source="mtdb_asset"):
            validation = validate_image_bytes(image["body"], filename=url).to_dict()
            if not validation["valid"] or validation["actual_format"] != "PNG":
                continue
            result = {k: image[k] for k in ("url", "snapshot_id", "content_hash", "raw_cache_path", "archive_timestamp")}
            result["validation"] = validation
            if self.live:
                from .database import CrawlDatabase
                with CrawlDatabase(self.db_path) as db:
                    db.upsert_asset(crawl_url_id=parent["crawl_url_id"], asset_url=url,
                        asset_kind="mtdb_card_image", snapshot_id=image["snapshot_id"], fetch_status="fetched",
                        content_hash=image["content_hash"], raw_cache_path=image["raw_cache_path"], http_status=200,
                        captured_at=image["captured_at"], metadata={"validation": validation,
                            "containing_page_snapshot_id": parent["snapshot_id"], "merge_status": "STAGED_MISSING_ONLY_NOT_IMPORTED"})
            return result
        return None


def provenance(document: dict) -> dict:
    return {k: document.get(k) for k in ("url", "snapshot_id", "archive_timestamp", "captured_at", "content_hash", "raw_cache_path")}


def run_probe(probe: dict, plan: dict, backend, staged: dict, persist=lambda *_: None) -> dict:
    from .mtdb_worker import observed_art
    active = [card_id for card_id in probe["target_ids"] if remaining_domains(plan["targets"][card_id], staged.get(card_id, {}))]
    report = {"url": probe["url"], "status": "already_filled" if not active else "no_saved_or_archived_page", "pages": []}
    if not active:
        return report
    seen_details = set()
    for document in backend.documents(probe["url"]):
        html = document["body"].decode("utf-8", errors="replace")
        raw = MTDBAdapter().parse_page(SourcePage(probe["url"], html))
        match = match_variant(raw, plan["catalog"])
        surface = inspect_page(html, probe["url"])
        page_report = {"source": provenance(document), "match": match, "surface": surface,
                       "parsed_counts": {"attributes": len(raw.attributes), "tendencies": len(raw.tendencies), "badges": len(raw.badges)}}
        report["pages"].append(page_report)
        report["status"] = match["status"]
        if match["status"] != "unique":
            # Retain evidence, but do not download art/details for uncertain cards.
            continue
        card_id = match["matches"][0]["card_id"]
        if card_id not in active:
            report["status"] = "matched_non_target_or_complete_variant"
            break
        target, primary = plan["targets"][card_id], plan["catalog"][card_id]
        entry = staged.setdefault(card_id, {"fields": {}, "evidence": [], "reviews": []})
        current = remaining_domains(target, entry)
        if not current:
            break
        narrow = {**target, "domains": current}
        patch, reviews = field_proposal(raw, narrow, primary)
        entry["reviews"].extend(reviews)
        for domain, data in patch.items():
            destination = entry["fields"].setdefault(domain, {})
            for field, value in data.items():
                if field in destination and destination[field] != value:
                    entry["reviews"].append({"domain": domain, "field": field, "reason": "conflicting_mtdb_snapshots"})
                else:
                    destination[field] = value
        entry["evidence"].append({**provenance(document), "match": match, "proposed_fields": patch})
        persist(card_id, entry)
        if "card_png" in remaining_domains(target, entry):
            for image_url in observed_art(html, probe["url"], probe["slug"]):
                image = backend.image(image_url, document)
                if image:
                    entry["fields"]["card_png"] = {**image, "containing_page": provenance(document)}
                    persist(card_id, entry)
                    break
        if {"badges", "tendencies"} & set(remaining_domains(target, entry)):
            for detail_url in surface["detail_urls"]:
                needed_domains = remaining_domains(target, entry)
                tail = urlsplit(detail_url).path[len(urlsplit(probe["url"]).path.rstrip("/")):].lower()
                if not (("badges" in needed_domains and "badge" in tail) or ("tendencies" in needed_domains and "tenden" in tail)):
                    continue
                if detail_url in seen_details:
                    continue
                seen_details.add(detail_url)
                for detail in backend.documents(detail_url):
                    detail_raw = MTDBAdapter().parse_page(SourcePage(detail_url, detail["body"].decode("utf-8", errors="replace")))
                    # A same-card URL is necessary but not sufficient: detail
                    # pages with no independent variant facts stay review-only.
                    detail_match = match_variant(detail_raw, plan["catalog"])
                    if detail_match["status"] != "unique" or detail_match["matches"][0]["card_id"] != card_id:
                        entry["reviews"].append({"reason": "detail_identity_needs_review", "source": provenance(detail)})
                        continue
                    needed = {**target, "domains": remaining_domains(target, entry)}
                    addition, review = field_proposal(detail_raw, needed, primary)
                    entry["reviews"].extend(review)
                    for domain, data in addition.items():
                        for field, value in data.items():
                            entry["fields"].setdefault(domain, {}).setdefault(field, value)
                    entry["evidence"].append({**provenance(detail), "match": detail_match, "proposed_fields": addition})
                    persist(card_id, entry)
        report.update(status="matched_evidence_checked", matched_card_id=card_id,
                      remaining_domains=remaining_domains(target, entry))
        if not report["remaining_domains"]:
            break
    return report


def surface_audit(backend: ArchiveEvidence, *, live_scripts: bool = False) -> dict:
    pages, index_refs, scripts = [], [], set()
    with closing(readonly(backend.db_path)) as db:
        rows = db.execute("SELECT s.*,u.url FROM page_snapshots s JOIN crawl_urls u USING(crawl_url_id) "
                          "WHERE u.source='mtdb' AND s.http_status BETWEEN 200 AND 299 ORDER BY s.snapshot_id").fetchall()
        indexes = db.execute("SELECT s.*,u.url FROM page_snapshots s JOIN crawl_urls u USING(crawl_url_id) "
                             "WHERE s.source_kind='wayback-cdx-prefix-index' AND u.url LIKE '%mtdb.com%' "
                             "AND s.http_status BETWEEN 200 AND 299").fetchall()
    index_count = 0
    for row in indexes:
        record = backend.verified(row)
        records = WaybackCdx().parse(record["body"])
        index_count += len(records)
        index_refs += [r.original_url for r in records if RELATED.search(urlsplit(r.original_url).path)]
    for row in rows:
        document = backend.verified(row)
        html = document["body"].decode("utf-8", errors="replace")
        surface = inspect_page(html, document["url"])
        raw = MTDBAdapter().parse_page(SourcePage(document["url"], html))
        pages.append({**provenance(document), **surface, "parsed_badges": len(raw.badges),
                      "parsed_tendencies": len(raw.tendencies), "parsed_attributes": len(raw.attributes)})
        scripts.update(surface["script_urls"])
    checked, pending, errors = [], [], []
    # Generic libraries/ads are not candidate card-data sources. Inspect the
    # actually observed MTDB application scripts; no guessed route probes.
    application_scripts = sorted(url for url in scripts if re.match(
        r"(?:common|mtdb[-.]|ui-)", urlsplit(url).path.split("/")[-1], re.I))
    for url in application_scripts:
        if live_scripts:
            print(json.dumps({"phase": "checking_observed_mtdb_script", "url": url}), flush=True)
        docs = backend.documents(url, source="mtdb_support") if live_scripts else iter(backend.cached(url))
        try:
            document = next(docs, None)
            if document:
                checked.append({**provenance(document), **inspect_script(document["body"].decode("utf-8", errors="replace"), url)})
            else:
                pending.append(url)
        except (OSError, ValueError, RuntimeError) as error:
            errors.append({"url": url, "error": str(error)})
    return {"created_at": time.time(), "saved_page_snapshots_checked": len(pages),
            "unique_page_urls_checked": len({p["url"] for p in pages}), "archive_html_index_records_checked": index_count,
            "archive_index_badge_tendency_routes": sorted(set(index_refs)), "pages": pages,
            "observed_application_scripts": application_scripts, "checked_scripts": checked,
            "unverified_script_urls": pending, "script_errors": errors,
            "limits": ["The saved /17/ prefix index is HTML-filtered; it does not rule out JavaScript/API endpoints outside /17/.",
                       "Empty archived sections are missing evidence, not proof that the historical live website lacked data.",
                       "Only page-observed same-card detail links can be fetched automatically; unfamiliar APIs require review."]}


def save_surface_audit(output: Path, report: dict):
    save(output / "badge_tendency_page_audit.json", report)
    pages = report["pages"]
    routes = report["archive_index_badge_tendency_routes"]
    details = sorted({url for page in pages for url in page["detail_urls"]})
    other = sorted({url for page in pages for url in page["other_related_references"]})
    lines = ["MTDB BADGES / TENDENCIES: SEPARATE-PAGE CHECK", "",
             f"Saved HTML snapshots checked: {len(pages)} ({report['unique_page_urls_checked']} distinct card URLs).",
             f"Saved archive HTML index records checked: {report['archive_html_index_records_checked']}.",
             f"Indexed badge/tendency routes found: {len(routes)}.",
             f"Observed separate same-card badge/tendency links: {len(details)}.",
             f"Snapshots with named badges: {sum(bool(p['parsed_badges']) for p in pages)}.",
             f"Snapshots with numeric tendencies: {sum(bool(p['parsed_tendencies']) for p in pages)}.",
             "", "Observed Badges & Tendencies links point to #badges, a section within the same HTML page.",
             "Empty sections are UNKNOWN/MISSING; the three-tier zero-count summary is not proof of zero 2K17 badges.",
             "", f"Observed application script URLs: {len(report['observed_application_scripts'])}.",
             f"Script bodies checked: {len(report['checked_scripts'])}.",
             f"Unverified script bodies: {len(report['unverified_script_urls'])}; errors: {len(report['script_errors'])}.",
             "No conclusion of site-wide absence can be drawn until the script/API investigation is completed.",
             "The live scanner checks these scripts before card recovery, respecting archive cooldowns.",
             "Unfamiliar API references are reported for review, not guessed or executed.",
             "", "LIMITATIONS", *report["limits"], "", "OBSERVED ROUTES / OTHER REFERENCES", *routes, *details, *other,
             "", "SCRIPT BODIES STILL UNVERIFIED", *report["unverified_script_urls"],
             "", "CHECKED SCRIPT REFERENCES"]
    for script in report["checked_scripts"]:
        lines.extend([script["url"], *script["literal_references"], *script["relevant_excerpts"],
                      *script.get("request_excerpts", [])])
    (output / "MTDB_BADGES_TENDENCIES_CHECK.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_plan(root: Path, audit_path: Path, output: Path) -> dict:
    known_path = root / "known_mtdb_cards.json"
    cards = json.loads(audit_path.read_text(encoding="utf-8"))
    plan = make_plan(cards, json.loads(known_path.read_text(encoding="utf-8")))
    plan["inputs"] = {"audit_path": str(audit_path.resolve()), "audit_sha256": digest(audit_path),
                      "known_path": str(known_path.resolve()), "known_sha256": digest(known_path),
                      "primary_fingerprint": primary_fingerprint(root / "crawl.sqlite")}
    save(output / "plan.json", plan)
    save_surface_audit(output, surface_audit(ArchiveEvidence(root)))
    lines = ["NBA 2K17 — MTDB MISSING-ONLY SCAN PLAN", "Live scan NOT started. App export disabled.",
             f"Plan SHA256: {digest(output / 'plan.json')}", json.dumps(plan["counts"], indent=2), "",
             "Only page-observed MTDB URLs are eligible; player names nominate metadata lookups, not card matches.",
             "A match requires name + OVR, >=32 equal shared attributes, no shared attribute/identity conflicts,",
             "and uniqueness against ALL primary variants (including complete cards). Ambiguous matches require review.",
             "Variant-identification lookups can reject complete/different variants; their PNGs/details are not downloaded.",
             "Unknown identity (including primary card 8096) may need manual matching; no identity is guessed.",
             "All card PNGs are validated. Existing primary fields are never overwritten; fills are staged separately.",
             "Partial MTDB fields do not satisfy the full observed primary template. No 2K16 field count is assumed.",
             "Named badge tiers must match the primary four-tier counts. The inconsistent badge card remains review-only.",
             "No further 2KMTC requests. Shared 2–3 second randomized pacing, robots and server cooldowns remain enforced.",
             "", "PER-CARD TARGETS"]
    for card_id, target in plan["targets"].items():
        identity = plan["catalog"][card_id]["identity"]
        lines.append(f"{card_id} | {identity.get('name')} | OVR {identity.get('overall')} | {identity.get('tier')} | {identity.get('collection')}")
        lines.append("  Missing: " + ", ".join(target["domains"]))
        for domain, fields in target["missing_fields"].items():
            lines.append(f"  {domain}: " + ", ".join(fields))
        lines.extend("  Candidate metadata: " + url for url in target["candidate_urls"])
        if not target["candidate_urls"]:
            lines.append("  No exact-name candidate in saved MTDB URL index; manual discovery required.")
    lines.extend(["", "USER-REMOVED CARDS (never recovery targets):", *[f'{k}: {v}' for k, v in EXCLUDED_2KMTC_CARDS.items()],
                  "", "EXCLUDED (complete or explicitly removed):", ", ".join(plan["excluded_ids"])])
    (output / "MTDB_MISSING_ONLY_PLAN.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return plan


@contextmanager
def worker_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another missing-only worker is running") from error
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def verify_plan(plan: dict, root: Path):
    if plan.get("schema") != VERSION:
        raise ValueError("Unsupported plan version")
    inputs = plan["inputs"]
    for label in ("audit", "known"):
        if digest(Path(inputs[label + "_path"])) != inputs[label + "_sha256"]:
            raise ValueError(f"Stale plan: {label} changed; rebuild the offline plan")
    if primary_fingerprint(root / "crawl.sqlite") != inputs["primary_fingerprint"]:
        raise ValueError("Primary evidence changed; re-audit and rebuild the missing-only plan before continuing")
    rebuilt = make_plan(json.loads(Path(inputs["audit_path"]).read_text(encoding="utf-8")),
                        json.loads(Path(inputs["known_path"]).read_text(encoding="utf-8")))
    for field in ("targets", "excluded_ids", "catalog", "probes", "counts", "policy"):
        if plan[field] != rebuilt[field]:
            raise ValueError(f"Plan {field} does not match audited missing-only scope")


def checkpoint_summary(plan: dict, staged: dict, reports: dict, live: bool) -> dict:
    remaining = {card_id: remaining_domains(target, staged.get(card_id, {})) for card_id, target in plan["targets"].items()}
    return {"pid": os.getpid(), "updated_at": time.time(), "mode": "live" if live else "offline_cache_only",
            "total_targets": len(plan["targets"]), "candidate_urls_checked": len(reports),
            "cards_with_staged_fills": sum(bool(e.get("fields")) for e in staged.values()),
            "staged_pngs": sum("card_png" in e.get("fields", {}) for e in staged.values()),
            "targets_fully_filled": sum(not gaps for gaps in remaining.values()),
            "remaining_by_domain": dict(Counter(d for gaps in remaining.values() for d in gaps)),
            "remaining_by_card": {k: v for k, v in remaining.items() if v}, "app_export": "disabled"}


def execute(plan: dict, root: Path, output: Path, *, live=False):
    backend = ArchiveEvidence(root, live)
    state_path = output / ("checkpoint.json" if live else "offline_checkpoint.json")
    plan_hash = digest(output / "plan.json")
    # Offline tests are repeatable fresh simulations, never live checkpoints.
    state = json.loads(state_path.read_text(encoding="utf-8")) if live and state_path.exists() else {}
    if state and state.get("plan_sha256") != plan_hash:
        raise ValueError("Checkpoint belongs to a different plan; use a new output directory")
    state.setdefault("plan_sha256", plan_hash)
    state.setdefault("staged", {})
    state.setdefault("reports", {})
    state.setdefault("completed_urls", [])
    staged = state["staged"]
    cached_urls = None
    if not live:
        with closing(readonly(backend.db_path)) as db:
            cached_urls = {row[0] for row in db.execute("SELECT DISTINCT u.url FROM crawl_urls u "
                "JOIN page_snapshots s USING(crawl_url_id) WHERE u.source='mtdb' AND s.http_status BETWEEN 200 AND 299")}

    def persist(card_id, entry):
        # Checkpoint first, then the readable per-card proposal. A crash can
        # regenerate proposal files from the checkpoint without re-fetching.
        save(state_path, state)
        save(output / ("staged" if live else "offline_proposals") / (card_id + ".json"), entry)

    for card_id, entry in staged.items():
        persist(card_id, entry)
    for probe in plan["probes"]:
        if (output / "STOP").exists():
            print(json.dumps({"phase": "stopped_by_user", "checkpoint": str(state_path)}), flush=True)
            break
        if live and probe["url"] in state["completed_urls"]:
            continue
        # Offline validation has no reason to iterate candidates without cache.
        if not live and probe["url"] not in cached_urls:
            continue
        if live:
            verify_primary = primary_fingerprint(root / "crawl.sqlite")
            if verify_primary != plan["inputs"]["primary_fingerprint"]:
                raise ValueError("Primary evidence changed during the pass; stopping for re-audit")
        print(json.dumps({"phase": "checking_missing_only_candidate", "url": probe["url"],
                          "checked": len(state["reports"]), "total_candidates": len(plan["probes"])}), flush=True)
        save(output / ("progress.json" if live else "offline_progress.json"),
             {**checkpoint_summary(plan, staged, state["reports"], live),
              "phase": "checking_missing_only_candidate", "current_url": probe["url"]})
        try:
            report = run_probe(probe, plan, backend, staged, persist)
            state["reports"][probe["url"]] = report
            if live:
                state["completed_urls"].append(probe["url"])
        except (OSError, ValueError, RuntimeError) as error:
            state["reports"][probe["url"]] = {"status": "retry_needed_not_completed", "error": str(error)}
        save(state_path, state)
        save(output / ("progress.json" if live else "offline_progress.json"), checkpoint_summary(plan, staged, state["reports"], live))
    summary = checkpoint_summary(plan, staged, state["reports"], live)
    summary["phase"] = "stopped_by_user" if (output / "STOP").exists() else "pass_finished"
    save(state_path, state)
    if not live:
        # Keep old diagnostic files clearly non-authoritative after re-testing
        # a stricter matcher. Never leave a stale proposal looking accepted.
        for prior in (output / "offline_proposals").glob("*.json"):
            if prior.stem not in staged:
                save(prior, {"fields": {}, "status": "NOT_PROPOSED_IN_CURRENT_DRY_RUN",
                             "authoritative_result": str(state_path)})
    save(output / ("progress.json" if live else "offline_progress.json"), summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "remaining_by_card"}), flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "dry-run", "inspect-scripts", "run"))
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--approve-plan-sha256", help="Exact reviewed plan digest; required for network access")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    if args.command == "plan":
        if (output / "checkpoint.json").exists() or (output / "offline_checkpoint.json").exists():
            raise ValueError("Do not overwrite a checkpoint-bound plan; choose a new --output directory")
        plan = write_plan(ROOT, args.audit, output)
        print(json.dumps({"plan": str(output / "plan.json"), "sha256": digest(output / "plan.json"), **plan["counts"]}), flush=True)
        return
    plan_path = output / "plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    verify_plan(plan, ROOT)
    live = args.command in ("run", "inspect-scripts")
    if live and args.approve_plan_sha256 != digest(plan_path):
        raise ValueError("Live access refused: provide --approve-plan-sha256 for the reviewed missing-only plan")
    if live:
        with worker_lock(output / "worker.lock"):
            # Shared site scripts are checked once from observed page references
            # before card recovery. Cache reuse makes subsequent runs inexpensive.
            save(output / "progress.json", {"pid": os.getpid(), "updated_at": time.time(), "phase": "checking_source_components"})
            report = surface_audit(ArchiveEvidence(ROOT, live=True), live_scripts=True)
            save_surface_audit(output, report)
            if args.command == "inspect-scripts":
                summary = {"pid": os.getpid(), "updated_at": time.time(), "phase": "source_inspection_finished",
                           "checked_scripts": len(report["checked_scripts"]),
                           "unverified_scripts": len(report["unverified_script_urls"]), "errors": report["script_errors"],
                           "card_crawl_started": False}
                save(output / "progress.json", summary)
                print(json.dumps(summary), flush=True)
            else:
                if any(script["literal_references"] for script in report["checked_scripts"]):
                    save(output / "progress.json", {"pid": os.getpid(), "updated_at": time.time(),
                        "phase": "script_endpoint_review_required", "reason": "Observed badge/tendency script references need route/parser review before the full pass"})
                    raise RuntimeError("Badge/tendency script references found: review the surface audit before card recovery")
                execute(plan, ROOT, output, live=True)
    else:
        execute(plan, ROOT, output, live=False)


if __name__ == "__main__":
    main()
