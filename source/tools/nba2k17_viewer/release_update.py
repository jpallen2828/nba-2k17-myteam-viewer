"""Explicit migration from an older portable folder; never replace the new catalog."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import shutil
import sqlite3


def migrate(old_root, new_root):
    old_root, new_root = Path(old_root).resolve(), Path(new_root).resolve()
    if old_root == new_root:
        raise ValueError('Choose the older application folder, not this folder.')
    relative = Path('.project/data/nba2k17')
    old, new = old_root / relative, new_root / relative
    old_db, new_db = old / 'normalized/myteam_app.sqlite', new / 'normalized/myteam_app.sqlite'
    if not old_db.is_file() or not new_db.is_file():
        raise ValueError('Both folders must contain the MyTEAM catalog.')
    pairs = []
    for source, target in [(old / 'curation/studio_imports', new / 'curation/studio_imports'),
                           (old_root / '.project/user/teams', new_root / '.project/user/teams')]:
        for path in source.rglob('*') if source.is_dir() else ():
            if path.is_symlink():
                raise ValueError('User data contains an unsupported symbolic link.')
            if path.is_file():
                dest = target / path.relative_to(source)
                if dest.exists():
                    if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(dest.read_bytes()).digest():
                        raise ValueError(f'Conflicting existing file: {dest.name}. Use a freshly extracted release folder.')
                else:
                    pairs.append((path, dest))
    old_states = old / 'curation/custom_card_states.json'
    new_states = new / 'curation/custom_card_states.json'
    states = json.loads(new_states.read_text(encoding='utf-8')) if new_states.exists() else {}
    if old_states.exists():
        states.update(json.loads(old_states.read_text(encoding='utf-8')))
    tables = {}
    with closing(sqlite3.connect(old_db.as_uri() + '?mode=ro', uri=True)) as db:
        for table, value in [('custom_card_teams', 'team'), ('custom_card_years', 'year')]:
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                tables[table] = (value, db.execute(f'SELECT source_card_id, {value} FROM {table}').fetchall())
    # Validate the entire plan before copying. The old folder is always read-only.
    created = []
    before_states = new_states.read_bytes() if new_states.exists() else None
    try:
        with closing(sqlite3.connect(new_db)) as db, db:
            for table, (value, rows) in tables.items():
                kind = 'INTEGER' if value == 'year' else 'TEXT'
                db.execute(f'CREATE TABLE IF NOT EXISTS {table} (source_card_id TEXT PRIMARY KEY, {value} {kind})')
                db.executemany(f'INSERT OR REPLACE INTO {table} VALUES (?, ?)', rows)
            for source, target in pairs:
                target.parent.mkdir(parents=True, exist_ok=True)
                created.append(target)
                shutil.copy2(source, target)
            new_states.parent.mkdir(parents=True, exist_ok=True)
            new_states.write_text(json.dumps(states, indent=2), encoding='utf-8')
    except Exception:
        for path in reversed(created):
            path.unlink(missing_ok=True)
        if before_states is None:
            new_states.unlink(missing_ok=True)
        else:
            new_states.write_bytes(before_states)
        raise
    return {'copied_files': len(created), 'custom_states': len(states),
            'team_overrides': len(tables.get('custom_card_teams', ('', []))[1]),
            'year_overrides': len(tables.get('custom_card_years', ('', []))[1]),
            'old_folder_unchanged': True}
