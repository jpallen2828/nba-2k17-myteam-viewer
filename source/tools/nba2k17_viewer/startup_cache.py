"""Disposable, validated viewer projection; source catalogs remain authoritative.

Rows load immediately. Full, unchanged card dictionaries are decoded on demand.
No pickle, game handles, live addresses, or mutable lineup state is persisted.
"""
from collections.abc import MutableMapping
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import sys
from threading import RLock
import uuid
import zlib

from .archive_repository import ArchiveRepository, project_root
from .custom_import import data_root

VERSION = 1


def stamp(path):
    path = Path(path)
    try:
        s = path.stat()
        return [str(path), s.st_size, s.st_mtime_ns, s.st_ctime_ns]
    except FileNotFoundError:
        return [str(path), None]


def fingerprint(source, images=()):
    source = Path(source).resolve()
    paths = {source, Path(str(source)+'-wal'), Path(str(source)+'-journal')}
    data = data_root(source)
    if data:
        paths.update(p for p in (data/'curation').rglob('*') if p.is_file())
        signatures = data/'curation/user_signatures.json'
        if signatures.is_file():
            for evidence in json.loads(signatures.read_text('utf-8')).get('evidence', []):
                paths.add(data.parents[2]/evidence['path'])
    tools = Path(__file__).resolve().parent.parent
    if getattr(sys, 'frozen', False):
        paths.add(Path(sys.executable).resolve())
    else:
        paths.update(tools.rglob('*.py'))
    for package in ('nba2k17_viewer/assets', 'nba2k17_catalog'):
        paths.update((tools/package).rglob('*.json'))
    paths.update(Path(p) for p in images)
    return sha256(json.dumps([VERSION, str(project_root()),
        [stamp(p) for p in sorted(paths)]], separators=(',', ':')).encode()).hexdigest()


def cache_path(source):
    root = Path(os.environ.get('LOCALAPPDATA', Path.home()))/'NBA2K17MyTEAM/startup-cache'
    key = sha256(str(Path(source).resolve()).encode()).hexdigest()[:24]
    return root/(key+'.sqlite')


def encode(value):
    raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
    packed = zlib.compress(raw, 1)
    return packed, sha256(packed).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, check_same_thread=False)
        self.lock = RLock()

    def read(self, key):
        with self.lock:
            row = self.db.execute('SELECT payload, digest FROM objects WHERE key=?', (key,)).fetchone()
        if not row or sha256(row[0]).hexdigest() != row[1]:
            raise ValueError('Invalid startup cache object: '+key)
        return json.loads(zlib.decompress(row[0]))

    def __del__(self):
        if hasattr(self, 'db'):
            self.db.close()


class Cards(MutableMapping):
    def __init__(self, store, keys, source):
        self.store, self._keys, self.source = store, dict.fromkeys(keys), source
        self.loaded = {}

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        if key not in self.loaded:
            try:
                self.loaded[key] = self.store.read('card:'+key)
            except (ValueError, sqlite3.Error, zlib.error):
                # A damaged derived cache can never supply injection data.
                self.loaded.update(ArchiveRepository(self.source).cards)
        return self.loaded[key]

    def __setitem__(self, key, value):
        self._keys[key] = None
        self.loaded[key] = value

    def __delitem__(self, key):
        del self._keys[key]
        self.loaded.pop(key, None)

    def __iter__(self):
        return iter(self._keys)

    def __len__(self):
        return len(self._keys)


class CachedRepository(ArchiveRepository):
    def __getattr__(self, name):
        if name in self.__dict__.get('_lazy_attributes', ()):
            value = self._store.read('attr:'+name)
            setattr(self, name, value)
            return value
        raise AttributeError(name)


def save(repository, target, before):
    images = sorted(repository._image_dependencies)
    # Never label a projection current if inputs changed while it was loading.
    if fingerprint(repository.path) != before:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    try:
        attributes = {k: v for k, v in vars(repository).items()
                      if k not in ('path', 'cards', 'row_by_id', '_image_dependencies')}
        header = {'fingerprint': fingerprint(repository.path, images), 'images': images,
                  'keys': list(repository.cards), 'attributes': list(attributes)}
        with closing(sqlite3.connect(temporary)) as db, db:
            db.execute('CREATE TABLE objects(key TEXT PRIMARY KEY, payload BLOB, digest TEXT)')
            def put(key, value):
                payload, digest = encode(value)
                db.execute('INSERT INTO objects VALUES(?,?,?)', (key, payload, digest))
            put('header', header)
            for key, value in attributes.items():
                put('attr:'+key, value)
            for key, card in repository.cards.items():
                put('card:'+key, card)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def load_repository(source, *, refresh=False):
    source = Path(source).resolve()
    if data_root(source) is None or source.suffix.lower() != '.sqlite':
        return ArchiveRepository(source)
    target = cache_path(source)
    store = None
    if not refresh:
        try:
            store = Store(target)
            header = store.read('header')
            if header['fingerprint'] == fingerprint(source, header['images']):
                result = CachedRepository.__new__(CachedRepository)
                result.path, result._store = source, store
                result._lazy_attributes = header['attributes']
                result.rows = store.read('attr:rows')
                result.summary = store.read('attr:summary')
                result.row_by_id = {row['id']: row for row in result.rows}
                result.cards = Cards(store, header['keys'], source)
                return result
        except (OSError, ValueError, KeyError, sqlite3.Error, zlib.error):
            pass
        if store is not None:
            store.db.close()
    before = fingerprint(source)
    result = ArchiveRepository(source)
    try:
        save(result, target, before)
    except (OSError, sqlite3.Error):
        pass  # Read-only disks or another open app must not prevent startup.
    return result
