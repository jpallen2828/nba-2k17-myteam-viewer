"""Archive helpers retained for the missing-only scanner; broad entry retired."""
from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlsplit

from .archive import WaybackCdx
from .asset_validator import validate_image_bytes
from .cli import _fetch_replay, _verified_snapshot_body
from .database import CrawlDatabase
from .discovery import canonicalize_url, extract_links
from .fetcher import Fetcher
from .recovery_worker import ROOT, recovery_config, save

STATE = ROOT / 'reports/mtdb_worker.json'
CHECKPOINT = ROOT / 'mtdb_checkpoint.json'
SCOPE_PAUSE = ROOT / 'mtdb_scope_pause.json'
CARD_PATH = re.compile(r'^/17/players/([a-z][a-z0-9-]*-\d+)/?$', re.I)


def progress(phase, **details):
    payload = dict(pid=os.getpid(), phase=phase, updated_at=time.time(), **details)
    save(STATE, payload)
    print(json.dumps(payload), flush=True)


def cooldown():
    while True:
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            host = db.get_host_state('web.archive.org')
        delay = (host.blocked_until or 0) - time.time()
        if delay <= 0:
            return
        progress('archive_cooldown', reason=host.block_reason, resume_at=host.blocked_until)
        time.sleep(min(delay + 1, 60))


def cdx_records(original, *, prefix=False):
    cdx = WaybackCdx()
    query = (cdx.prefix_query_url(original, from_timestamp='2016', to_timestamp='2018')
             if prefix else cdx.query_url(original, from_timestamp='2016',
                                         to_timestamp='2018', collapse_digest=True))
    with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
        row = db.discover_url(source='wayback_cdx', url=query,
                              discovery_method='mtdb_observed_archive_lookup', discovered_from=original)
        snapshot = db.get_snapshot(row.latest_snapshot_id) if row.latest_snapshot_id else None
        if snapshot is None or not 200 <= snapshot.http_status < 300:
            cooldown()
            fetched = Fetcher(recovery_config(), db).fetch(row.crawl_url_id,
                source_kind='wayback-cdx-prefix-index' if prefix else 'wayback-cdx-exact')
            if fetched.http_status != 200 or fetched.snapshot_id is None:
                raise RuntimeError(f'CDX retry needed: {fetched.outcome}: {fetched.error}')
            snapshot = db.get_snapshot(fetched.snapshot_id)
        records = cdx.parse(_verified_snapshot_body(snapshot))
    return [r for r in records if r.status == 200 and
            (prefix or canonicalize_url(r.original_url) == canonicalize_url(original))]


def indexed_cards(records):
    cards = {}
    for record in records:
        url = canonicalize_url(record.original_url)
        if not url:
            continue
        parts = urlsplit(url)
        match = CARD_PATH.fullmatch(parts.path)
        if parts.hostname not in ('mtdb.com', 'www.mtdb.com') or parts.query or not match:
            continue
        cards[url] = match.group(1)
    return cards


def observed_art(body, page_url, slug):
    # Only page-observed, same-card CDN paths; never manufacture an artwork URL.
    result = []
    for link in extract_links(body, page_url):
        parts = urlsplit(link)
        if (parts.hostname in ('cdn.mtdb.com', 'content.mtdb.com', 'mtdb.com', 'www.mtdb.com')
                and parts.path.lower() in (f'/nba2k17/{slug}.png'.lower(),
                                          f'/www/nba2k17/{slug}.png'.lower())):
            result.append(link)
    return sorted(set(result))


def replay(source, url, slug, record):
    cooldown()
    with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
        row = db.discover_url(source=source, url=url, source_card_id=slug,
                              discovery_method='observed_mtdb_archive_capture')
        result = _fetch_replay(Fetcher(recovery_config(), db), db,
                               crawl_url_id=row.crawl_url_id, record=record)
        snapshot = db.get_snapshot(result['snapshot_id']) if result.get('snapshot_id') else None
        if result.get('outcome') not in ('fetched', 'cached', 'not_modified') or snapshot is None:
            raise RuntimeError(f'Replay retry needed: {result}')
        body = _verified_snapshot_body(snapshot)
        return row.crawl_url_id, snapshot, body


def recover_card(url, slug):
    records = cdx_records(url)
    if not records:
        return {'status': 'no_archived_page', 'url': url}
    # Prefer the latest exact capture; retain older evidence separately, never overwrite primary data.
    page_id, snapshot, body = replay('mtdb', url, slug, max(records, key=lambda r:r.timestamp))
    images = observed_art(body, url, slug)
    result = {'status': 'collected', 'url': url, 'page_snapshot_id': snapshot.snapshot_id,
              'archive_timestamp': snapshot.archive_timestamp, 'images': [],
              'merge_status': 'UNMATCHED_SOURCE_EVIDENCE_NOT_IMPORTED'}
    recover_images(page_id, snapshot, images, slug, result)
    # MTDB moved from cdn.mtdb.com to content.mtdb.com. If the newer artwork
    # wasn't archived, older page captures can supply an actually observed
    # alternate URL. Do not infer either hostname/path from the other.
    if not any(item['status'] == 'fetched' for item in result['images']):
        seen = set(images)
        # Check the oldest distinct capture first for the historical CDN link.
        for record in sorted(records, key=lambda r:r.timestamp):
            if record.timestamp == snapshot.archive_timestamp:
                continue
            _, older, older_body = replay('mtdb', url, slug, record)
            extra = [u for u in observed_art(older_body, url, slug) if u not in seen]
            seen.update(extra)
            recover_images(page_id, older, extra, slug, result)
            if any(item['status'] == 'fetched' for item in result['images']):
                break
    if not result['images']:
        result['status'] = 'page_saved_no_observed_art'
    return result


def recover_images(page_id, snapshot, images, slug, result):
    for image_url in images:
        image_records = cdx_records(image_url)
        if not image_records:
            with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
                db.upsert_asset(crawl_url_id=page_id, asset_url=image_url,
                    asset_kind='mtdb_card_image', fetch_status='archive_missing',
                    metadata={'containing_page_snapshot_id':snapshot.snapshot_id})
            result['images'].append({'url':image_url, 'status':'archive_missing'})
            continue
        _, image_snapshot, image_body = replay('mtdb_asset', image_url, slug,
                                               max(image_records, key=lambda r:r.timestamp))
        validation = validate_image_bytes(image_body, filename=image_url.split('?')[0]).to_dict()
        status = 'fetched' if validation['valid'] and validation['actual_format'] == 'PNG' else 'invalid'
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            db.upsert_asset(crawl_url_id=page_id, asset_url=image_url,
                asset_kind='mtdb_card_image', snapshot_id=image_snapshot.snapshot_id,
                fetch_status=status, content_hash=image_snapshot.content_hash,
                raw_cache_path=image_snapshot.raw_cache_path, http_status=image_snapshot.http_status,
                captured_at=image_snapshot.captured_at,
                metadata={'validation':validation, 'containing_page_snapshot_id':snapshot.snapshot_id,
                          'merge_status':'UNMATCHED_SOURCE_EVIDENCE_NOT_IMPORTED'})
        result['images'].append({'url':image_url, 'status':status, 'validation':validation})


def run():
    # Removing the old pause file must NEVER resurrect the broad 1,630-card
    # crawl. The replacement verifies a frozen, field-specific missing plan.
    progress('retired_broad_mtdb_worker', reason='Use nba2k17_scraper.mtdb_gap_scan with an audited missing-only plan. No network requests made.')


if __name__ == '__main__':
    run()
