"""Persistent custom-card availability and user-editable catalog teams/years."""
from contextlib import closing
import json
import os
import shutil
import sqlite3
from pathlib import Path
from .custom_import import data_root


def team_overrides(source):
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() == '.json':
        return {}
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_card_teams'").fetchone():
            return {}
        return dict(db.execute('SELECT source_card_id, team FROM custom_card_teams'))


def apply_team(card, overrides):
    source = card.get('source', {})
    team = overrides.get(str(source.get('source_card_id')))
    if source.get('provider') != 'user_authored' or team is None:
        return False
    identity = card.setdefault('identity', {})
    for key in ('team', 'franchise'):
        identity.setdefault('values', {})[key] = team
        identity.setdefault('statuses', {})[key] = 'CUSTOM_AUTHORED'
    if 'missing_fields' in identity:
        identity['missing_fields'] = [key for key in identity['missing_fields'] if key not in ('team', 'franchise')]
    return True


def store_teams(db, teams):
    """Use the caller's transaction, including when replacing the catalog."""
    db.execute('CREATE TABLE IF NOT EXISTS custom_card_teams (source_card_id TEXT PRIMARY KEY, team TEXT NOT NULL)')
    db.executemany('INSERT OR REPLACE INTO custom_card_teams VALUES (?, ?)', teams.items())


def set_teams(source, updates):
    """Commit team metadata and rebuild-safe overrides in one SQLite transaction."""
    known = {card['id'] for card in inventory(source)}
    if not set(updates) <= known:
        raise ValueError('Selection contains unavailable or non-custom cards')
    if any(not isinstance(team, str) or not team.strip() or len(team.strip()) > 100 for team in updates.values()):
        raise ValueError('Choose a valid team')
    updates = {cid: team.strip() for cid, team in updates.items()}
    from nba2k17_scraper.models import CanonicalCard
    from nba2k17_scraper.injection_exporter import build_injection_profile
    with closing(sqlite3.connect(source)) as db, db:
        store_teams(db, updates)
        for cid in updates:
            saved = db.execute('SELECT record_json FROM cards WHERE source_card_id=?', (cid,)).fetchone()
            if saved:
                record = json.loads(saved[0])
                if not apply_team(record['card'], updates):
                    raise ValueError('Only custom cards can change teams')
                record['card']['injection'] = build_injection_profile(CanonicalCard.from_dict(record['card']))
                db.execute('UPDATE cards SET record_json=? WHERE source_card_id=?',
                           (json.dumps(record, ensure_ascii=False), cid))


def parse_year(value):
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if type(value) not in (int, str):
        raise ValueError('Enter a four-digit year, or leave it blank to clear the year.')
    text = str(value).strip()
    if len(text) != 4 or not text.isascii() or not text.isdigit() or int(text) < 1000:
        raise ValueError('Enter a four-digit year, or leave it blank to clear the year.')
    return int(text)


def year_overrides(source):
    source = Path(source).resolve()
    if not source.is_file() or source.suffix.lower() == '.json':
        return {}
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='custom_card_years'").fetchone():
            return {}
        return dict(db.execute('SELECT source_card_id, year FROM custom_card_years'))


def apply_year(card, overrides):
    source = card.get('source', {})
    cid = str(source.get('source_card_id'))
    if source.get('provider') != 'user_authored' or cid not in overrides:
        return False
    year = overrides[cid]
    identity = card.setdefault('identity', {})
    fields = {'card_year': year, 'represented_season': str(year) if year is not None else None}
    identity.setdefault('values', {}).update(fields)
    identity.setdefault('statuses', {}).update({key: 'CUSTOM_AUTHORED' if year is not None else 'MISSING' for key in fields})
    missing = [key for key in identity.get('missing_fields', []) if key not in fields]
    identity['missing_fields'] = missing + (list(fields) if year is None else [])
    return True


def store_years(db, years):
    # NULL is an explicit cleared year, so old imports cannot restore it.
    db.execute('CREATE TABLE IF NOT EXISTS custom_card_years (source_card_id TEXT PRIMARY KEY, year INTEGER)')
    db.executemany('INSERT OR REPLACE INTO custom_card_years VALUES (?, ?)', years.items())


def set_years(source, updates):
    """Save year metadata and its import/rebuild override atomically."""
    known = {card['id'] for card in inventory(source)}
    if not set(updates) <= known:
        raise ValueError('Selection contains unavailable or non-custom cards')
    updates = {cid: parse_year(year) for cid, year in updates.items()}
    from nba2k17_scraper.models import CanonicalCard
    from nba2k17_scraper.injection_exporter import build_injection_profile
    with closing(sqlite3.connect(source)) as db, db:
        store_years(db, updates)
        for cid in updates:
            saved = db.execute('SELECT record_json FROM cards WHERE source_card_id=?', (cid,)).fetchone()
            if saved:
                record = json.loads(saved[0])
                if not apply_year(record['card'], updates):
                    raise ValueError('Only custom cards can change years')
                record['card']['injection'] = build_injection_profile(CanonicalCard.from_dict(record['card']))
                db.execute('UPDATE cards SET record_json=? WHERE source_card_id=?',
                           (json.dumps(record, ensure_ascii=False), cid))


def states(source):
    root = data_root(source)
    path = root / 'curation/custom_card_states.json' if root else None
    return json.loads(path.read_text(encoding='utf-8')) if path and path.exists() else {}


def inventory(source):
    entries = {}
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        for (body,) in db.execute('SELECT record_json FROM cards'):
            card = json.loads(body)['card']
            if card.get('source', {}).get('provider') == 'user_authored':
                entries[card['source']['source_card_id']] = card
    root = data_root(source)
    for path in (root / 'curation/studio_imports').glob('custom-*/manifest.json') if root else ():
        card = json.loads(path.read_text(encoding='utf-8'))['record']['card']
        entries[card['source']['source_card_id']] = card
    state = states(source)
    teams = team_overrides(source)
    years = year_overrides(source)
    return [dict(id=cid, name=c['identity']['values']['name'],
                 overall=c['identity']['values'].get('overall'),
                 year=years.get(cid, c['identity']['values'].get('card_year')),
                 collection=c['identity']['values'].get('collection'),
                 team=teams.get(cid, c['identity']['values'].get('team') or c['identity']['values'].get('franchise') or ''),
                 disabled=state.get(cid) == 'disabled')
            for cid, c in entries.items() if state.get(cid) != 'deleted']


def set_state(source, ids, action):
    if action not in ('enabled', 'disabled', 'deleted'):
        raise ValueError('Invalid custom-card action')
    known = {c['id'] for c in inventory(source)}
    ids = set(ids)
    if not ids <= known:
        raise ValueError('Selection contains unavailable or non-custom cards')
    root = data_root(source)
    state = states(source)
    state.update({cid: action for cid in ids})
    path = root / 'curation/custom_card_states.json'
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(state, indent=2), encoding='utf-8')
    os.replace(pending, path)
    if action == 'deleted':
        # Tombstones prevent a later catalog rebuild from resurrecting deletions.
        with sqlite3.connect(source) as db:
            db.executemany('DELETE FROM cards WHERE source_card_id=?', [(cid,) for cid in ids])
        parent = (root / 'curation/studio_imports').resolve()
        for cid in ids:
            target = (parent / cid).resolve()
            if target.parent != parent or not cid.startswith('custom-'):
                raise ValueError('Invalid custom import directory')
            if target.exists():
                shutil.rmtree(target)
