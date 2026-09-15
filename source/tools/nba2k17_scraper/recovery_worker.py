"""Resumable sequential recovery of outstanding pages, art and detail pages."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time

from .cli import main, _verified_snapshot_body, _fetch_replay
from .archive import WaybackCdx
from .database import CrawlDatabase
from .config import ScraperConfig, RateLimitPolicy
from .fetcher import Fetcher
from .discovery import canonicalize_url

ROOT = Path('.project/data/nba2k17').resolve()
STATE = ROOT / 'reports/recovery_worker.json'
CHECKPOINT = ROOT / 'recovery_checkpoint.json'
REQUEST_DELAY_SECONDS = 2.0
REQUEST_JITTER_SECONDS = 1.0
CHECKPOINT_RETRY_DELAYS = (0.1, 0.2, 0.4, 0.8, 1.6, 2.0, 2.0, 2.0, 2.0, 2.0)


def recovery_config():
    return ScraperConfig(ROOT, rate_limit=RateLimitPolicy(
        minimum_spacing_seconds=REQUEST_DELAY_SECONDS,
        jitter_min_seconds=0, jitter_max_seconds=REQUEST_JITTER_SECONDS))


def cached_captures():
    """Use only successful, hash-verified index evidence, with exact URL keys."""
    result = {}
    with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
        rows = db.connection.execute(
            "SELECT latest_snapshot_id FROM crawl_urls WHERE source='wayback_cdx' "
            "AND latest_snapshot_id IS NOT NULL").fetchall()
        for row in rows:
            snapshot = db.get_snapshot(row[0])
            if not 200 <= snapshot.http_status < 300:
                continue
            try:
                records = WaybackCdx().parse(_verified_snapshot_body(snapshot))
            except (OSError, ValueError):
                continue
            for record in records:
                if record.status != 200 or not '2016' <= record.timestamp[:4] <= '2018':
                    continue
                url = canonicalize_url(record.original_url)
                if url not in result or record.timestamp > result[url].timestamp:
                    result[url] = record
    return result


def recover_detail(url, captures):
    record = captures.get(canonicalize_url(url))
    if record is not None:
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            row = db.discover_url(source='2kmtcentral', url=url,
                source_card_id=url.split('/players/')[1].split('/')[0],
                discovery_method='verified_cached_cdx_capture',
                metadata={'capture_selection': 'latest_known_cached_not_guaranteed_latest'})
            result = _fetch_replay(Fetcher(recovery_config(), db), db,
                                  crawl_url_id=row.crawl_url_id, record=record)
        if result.get('outcome') in ('cached', 'fetched', 'blocked', 'retry'):
            return result
        cooldown()
    return command('wayback', '--source', '2kmtcentral', '--url', url, '--cdx',
                   '--capture', 'latest', '--collapse-digest', '--from-timestamp', '2016', '--to-timestamp', '2018')


def save(path, payload):
    """Publish a complete JSON file, tolerating brief Windows reader/AV locks.

    Never truncate the last good checkpoint or delete it before replacement.
    A failed write leaves its uniquely named pending file for diagnosis/recovery.
    Persistent permission errors still fail closed after bounded retries.
    """
    path = Path(path)
    serialized = json.dumps(payload, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
            prefix='.' + path.name + '.', suffix='.tmp', delete=False) as handle:
        temp = Path(handle.name)
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(len(CHECKPOINT_RETRY_DELAYS) + 1):
        try:
            os.replace(temp, path)
            return
        except OSError as error:
            # WinError 5/32/33 can occur while another process is reading the
            # destination without FILE_SHARE_DELETE. Other I/O failures must
            # surface immediately rather than being mistaken for network errors.
            retryable = (isinstance(error, PermissionError)
                         or getattr(error, 'winerror', None) in (5, 32, 33))
            if not retryable or attempt == len(CHECKPOINT_RETRY_DELAYS):
                error.add_note(f'Last good checkpoint retained at {path}; pending JSON at {temp}')
                raise
            delay = CHECKPOINT_RETRY_DELAYS[attempt]
            print(json.dumps({'phase': 'checkpoint_write_retry', 'path': str(path),
                              'attempt': attempt + 1, 'retry_in_seconds': delay,
                              'error': str(error)}), file=sys.stderr, flush=True)
            time.sleep(delay)


def progress(phase, **details):
    value = dict(pid=os.getpid(), phase=phase, updated_at=time.time(), **details)
    save(STATE, value)
    print(json.dumps(value), flush=True)


def cooldown():
    while True:
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            state = db.get_host_state('web.archive.org')
        delay = (state.blocked_until or 0) - time.time()
        if delay <= 0:
            return
        progress('archive_cooldown', reason=state.block_reason, resume_at=state.blocked_until)
        time.sleep(min(delay + 1, 60))


def command(*args):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        result = main(['--data-root', str(ROOT), '--request-delay-seconds',
                       str(REQUEST_DELAY_SECONDS), '--request-jitter-seconds',
                       str(REQUEST_JITTER_SECONDS), *map(str, args)])
    if result:
        raise RuntimeError(f'Command failed ({result}): {args[0]}')
    return json.loads(output.getvalue())


def export():
    progress('exporting_database')
    result = command('export', '--roster-catalog', '.project/research/cardgame/NATIVE_PLAYER_VARIANT_CATALOG.json',
                     '--crosswalk', '.project/data/schema/nba2k17_schema_crosswalk.json')
    progress('database_updated', manifest=result['manifest'])


def resolve_pages():
    originals = set((ROOT / 'known_2kmtcentral_urls.txt').read_text().split())
    report = []
    with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
        rows = [dict(r) for r in db.connection.execute(
            "SELECT * FROM crawl_urls WHERE source='2kmtcentral' AND fetch_status!='fetched'") if r['url'] in originals]
    for row in rows:
        progress('resolving_main_url', url=row['url'])
        cooldown()
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            captures = []
            for r in db.connection.execute(
                "SELECT u.latest_snapshot_id FROM url_discoveries d JOIN crawl_urls u USING(crawl_url_id) "
                "WHERE d.discovered_from=? AND u.source='wayback_cdx' AND u.latest_snapshot_id IS NOT NULL", (row['url'],)):
                try:
                    captures.extend(WaybackCdx().parse(_verified_snapshot_body(db.get_snapshot(r[0]))))
                except ValueError:
                    continue
            captures = sorted({(r.timestamp, r.original_url):r for r in captures
                               if canonicalize_url(r.original_url)==row['url']}.values(), key=lambda r:r.timestamp, reverse=True)
        attempts = []
        # Try the most recent observed alternatives; do not invent captures or redirect targets.
        for record in captures[:4]:
            cooldown()
            with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
                outcome = _fetch_replay(Fetcher(recovery_config(), db), db,
                                        crawl_url_id=row['crawl_url_id'], record=record)
                attempts.append(outcome)
                if db.get_url(row['crawl_url_id']).fetch_status == 'fetched':
                    break
        with CrawlDatabase(ROOT / 'crawl.sqlite') as db:
            final = db.get_url(row['crawl_url_id'])
        report.append(dict(url=row['url'], status=final.fetch_status, observed_captures=len(captures), attempts=attempts))
        save(ROOT / 'reports/main_url_recovery.json', report)


def run():
    stop_file = ROOT / 'recovery_stop.json'
    if stop_file.exists():
        progress('cancelled_by_user', reason=json.loads(stop_file.read_text())['reason'])
        return
    checkpoint = json.loads(CHECKPOINT.read_text()) if CHECKPOINT.exists() else {'done': []}
    done = set(checkpoint['done'])
    if not checkpoint.get('resolved'):
        resolve_pages()
        checkpoint['resolved'] = True
        save(CHECKPOINT, checkpoint)
    if not checkpoint.get('prepared'):
        progress('building_png_manifest')
        result = command('write-asset-manifest', '--output', ROOT / 'known_2kmtcentral_asset_manifest.json')
        save(ROOT / 'reports/asset_manifest_preflight.json', result)
        progress('discovering_detail_urls')
        command('discover-cached-subpages', '--source', '2kmtcentral')
        command('write-url-list', '--source', '2kmtcentral', '--scope', 'subpages',
                '--output', ROOT / 'known_2kmtcentral_subpage_urls.txt')
        checkpoint['prepared'] = True
        save(CHECKPOINT, checkpoint)
    assets = json.loads((ROOT / 'known_2kmtcentral_asset_manifest.json').read_text())['assets']
    subpages = (ROOT / 'known_2kmtcentral_subpage_urls.txt').read_text().split()
    by_id = {}
    for url in subpages:
        by_id.setdefault(url.split('/players/')[1].split('/')[0], []).append(url)
    jobs = []
    for asset in assets:
        jobs.append(('png', asset['source_url'], asset))
        jobs.extend(('detail', url, None) for url in by_id.pop(str(asset['source_card_id']), []))
    jobs.extend(('detail', url, None) for urls in by_id.values() for url in urls)
    captures = cached_captures()
    progress('recovery_ready', cached_capture_urls=len(captures),
             request_delay_seconds=REQUEST_DELAY_SECONDS,
             request_jitter_seconds=REQUEST_JITTER_SECONDS,
             app_export='deferred_until_user_requests')
    for kind, url, asset in jobs:
        key = kind + ':' + url
        if key in done:
            continue
        cooldown()
        progress('recovering_' + kind, url=url, completed=len(done), total=len(jobs))
        try:
            if kind == 'png':
                single = ROOT / 'active_asset.json'
                save(single, {'assets':[asset]})
                result = command('wayback-assets', '--asset-manifest', single,
                                 '--report-output', ROOT / 'reports/assets' / (str(asset['source_card_id']) + '.json'))
            else:
                result = recover_detail(url, captures)
            save(ROOT / 'reports/recovery_last_result.json', result)
            # Leave transient or blocked operations retryable on the next run.
            text = json.dumps(result)
            transient = any(token in text for token in ('"blocked"', '"retry"', '"cdx_unavailable"'))
            if not transient:
                done.add(key)
                checkpoint['done'] = sorted(done)
                save(CHECKPOINT, checkpoint)
        except Exception as error:
            progress('operation_error', url=url, error=str(error))
    # A finished pass can still leave unavailable data requiring another source.
    # Keep raw data/checkpoints durable; rebuild the app only after gap review.
    progress('pass_finished', completed=len(done), total=len(jobs), remaining=len(jobs)-len(done))


if __name__ == '__main__':
    run()
