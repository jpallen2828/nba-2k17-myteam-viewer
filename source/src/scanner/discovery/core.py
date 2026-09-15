from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import time
import zlib


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def differences(before, after):
    if len(before) != len(after):
        raise ValueError('Region lengths changed; rebind required')
    return [{'offset': hex(i), 'before': a, 'after': b, 'xor': a ^ b}
            for i, (a, b) in enumerate(zip(before, after)) if a != b]


def signature(regions):
    return digest(json.dumps({k: digest(v) for k, v in sorted(regions.items())}, sort_keys=True).encode())


class SessionLease:
    """One local recorder/controller per session; OS releases lock after a crash."""
    def __init__(self, session):
        import msvcrt
        path = Path(session); path.mkdir(parents=True, exist_ok=True)
        self.stream = (path / '.recorder.lock').open('a+b')
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b'0'); self.stream.flush()
        self.stream.seek(0)
        try:
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.stream.close()
            raise ValueError('This session already has an active recorder/controller') from None

    def close(self):
        import msvcrt
        self.stream.seek(0)
        msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


class StableGate:
    """Stability is a measurement, never an acceptance detector."""
    def __init__(self, seconds=0.6, samples=3):
        self.seconds, self.samples = seconds, samples
        self.key, self.since, self.count = None, 0, 0

    def feed(self, key, now=None):
        now = time.monotonic() if now is None else now
        if key != self.key:
            self.key, self.since, self.count = key, now, 1
        else:
            self.count += 1
        return self.count >= self.samples and now - self.since >= self.seconds


class Journal:
    """Atomic events, content-addressed raw bytes and progress in one transaction."""
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS blobs(sha TEXT PRIMARY KEY, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, at TEXT NOT NULL,
                epoch TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')

    def get(self, key, default=None):
        row = self.db.execute('SELECT payload FROM state WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))

    def append(self, epoch, kind, regions=None, details=None, state=None):
        regions = regions or {}
        payload = dict(details or {})
        payload['regions'] = {name: {'sha256': digest(raw), 'length': len(raw)} for name, raw in regions.items()}
        with self.db:
            for raw in regions.values():
                self.db.execute('INSERT OR IGNORE INTO blobs VALUES (?,?)', (digest(raw), zlib.compress(raw)))
            cursor = self.db.execute('INSERT INTO events(at,epoch,kind,payload) VALUES (?,?,?,?)',
                                     (utc(), epoch, kind, json.dumps(payload, ensure_ascii=False)))
            for key, value in (state or {}).items():
                self.db.execute('INSERT OR REPLACE INTO state VALUES (?,?)', (key, json.dumps(value)))
        return cursor.lastrowid

    def events(self, epoch=None):
        query = 'SELECT id,at,epoch,kind,payload FROM events'
        args = ()
        if epoch:
            query += ' WHERE epoch=?'
            args = (epoch,)
        return [{**json.loads(p), 'id':i, 'at':at, 'epoch':e, 'kind':k}
                for i, at, e, k, p in self.db.execute(query + ' ORDER BY id', args)]

    def regions(self, event):
        result = {}
        for name, meta in event['regions'].items():
            row = self.db.execute('SELECT payload FROM blobs WHERE sha=?', (meta['sha256'],)).fetchone()
            if row is None:
                raise ValueError('Missing raw evidence blob')
            raw = zlib.decompress(row[0])
            if digest(raw) != meta['sha256'] or len(raw) != meta['length']:
                raise ValueError('Evidence integrity check failed')
            result[name] = raw
        return result

    def close(self):
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.close()


class Recorder:
    def __init__(self, journal, epoch, steps, decoder=lambda _: {}):
        self.journal, self.epoch, self.steps, self.decoder = journal, epoch, steps, decoder
        self.key = 'progress:' + epoch
        self.progress = journal.get(self.key, {'cursor': 0, 'baseline_event': None, 'last_event': None})
        # Events are authoritative after any interruption between convenience-state writes.
        history = journal.events(epoch)
        baselines = [e for e in history if e['kind'] == 'BASELINE']
        accepted = [e for e in history if e['kind'] == 'ACCEPTED_EDIT']
        completed = [e for e in history if e['kind'] in ('ACCEPTED_EDIT', 'SKIPPED_STEP')]
        order = {s['id']: i for i, s in enumerate(steps)}
        self.progress = {'cursor': max([order[e['step']['id']]+1 for e in completed if e.get('step', {}).get('id') in order] + [0]),
                         'baseline_event': baselines[0]['id'] if baselines else None,
                         'last_event': accepted[-1]['id'] if accepted else baselines[0]['id'] if baselines else None}
        self.gate = StableGate()
        self.editor_gate = StableGate()
        self.probe_gate = StableGate()
        self.last_signature = None
        self.last_editor = None
        self.last_probe = None
        self.pending = None
        self.previous = {}
        if self.progress['last_event']:
            event = next(x for x in journal.events(epoch) if x['id'] == self.progress['last_event'])
            self.previous = {k: v for k, v in journal.regions(event).items() if not k.startswith('editor:')}
            self.last_signature = signature({k:v for k,v in self.previous.items() if not k.startswith('probe:')})

    def request_mark(self, note='', custom_step=None):
        if not self.progress['baseline_event']:
            raise ValueError('Wait for the initial stable baseline before editing or marking')
        if self.pending is not None:
            raise ValueError('An accepted-edit capture is already pending')
        if self.progress['cursor'] >= len(self.steps):
            raise ValueError('Batch complete')
        step = custom_step or self.steps[self.progress['cursor']]
        if step.get('requires_note') and not note.strip():
            raise ValueError('Enter the actual displayed label/value in the recorder before capturing this step')
        self.pending = {'operator_marked_at': utc(), 'operator_note': note, 'step': step, 'custom_capture': custom_step is not None}
        self.journal.append(self.epoch, 'OPERATOR_MARK_PENDING', details=self.pending)
        # Require a fresh quiet interval AFTER the mark, not cached pre-accept stability.
        self.gate = StableGate()

    def sample(self, regions, metadata=None, now=None):
        authoritative = {k: v for k, v in regions.items() if not k.startswith('editor:')}
        if 'player' not in authoritative:
            raise ValueError('Canonical player region missing')
        stable_regions = {k:v for k,v in authoritative.items() if not k.startswith('probe:')}
        stable = self.gate.feed(signature(stable_regions), now)
        editors = {k: v for k, v in regions.items() if k.startswith('editor:')}
        probes = {k:v for k,v in regions.items() if k.startswith('probe:')}
        if probes and self.probe_gate.feed(signature(probes), now) and signature(probes) != self.last_probe:
            self.journal.append(self.epoch, 'UNVERIFIED_LINKED_WINDOW_OBSERVATION', probes,
                                {'acceptance': 'UNKNOWN', 'complete_block': False, 'diagnostics': metadata or {}})
            self.last_probe = signature(probes)
        if editors and self.editor_gate.feed(signature(editors), now) and signature(editors) != self.last_editor:
            self.journal.append(self.epoch, 'EDITOR_OBSERVATION', editors,
                                {'acceptance': 'UNKNOWN', 'diagnostics': metadata or {}})
            self.last_editor = signature(editors)
        if not stable:
            return 'Waiting for stable canonical regions'
        sig = signature(stable_regions)
        if not self.progress['baseline_event']:
            event = self.journal.append(self.epoch, 'BASELINE', regions,
                                        {'decoded': self.decoder(authoritative['player']), 'diagnostics': metadata or {}})
            self.progress.update(baseline_event=event, last_event=event)
            self.journal.set(self.key, self.progress)
            self.previous, self.last_signature = authoritative, sig
        if sig != self.last_signature:
            changes = {k: differences(self.previous[k], v) for k, v in authoritative.items() if k in self.previous}
            self.journal.append(self.epoch, 'STABLE_CHANGE', regions,
                                {'acceptance': 'UNKNOWN_UNTIL_OPERATOR_MARK', 'changes': changes,
                                 'decoded': self.decoder(authoritative['player']), 'diagnostics': metadata or {}})
            self.last_signature = sig
        if self.pending is not None:
            details = {**self.pending, 'acceptance': 'OPERATOR_CONFIRMED_ACCEPTED',
                       'canonical_player_unchanged_since_last_label': authoritative['player'] == self.previous.get('player'),
                       'changes_since_last_labeled_capture': {k: differences(self.previous[k], v)
                                                             for k, v in authoritative.items() if k in self.previous},
                       'decoded': self.decoder(authoritative['player']), 'diagnostics': metadata or {}}
            next_progress = {**self.progress, 'cursor': self.progress['cursor'] + (0 if self.pending.get('custom_capture') else 1)}
            # last_event is derivable from the journal after a crash; cursor and capture commit together.
            event = self.journal.append(self.epoch, 'ACCEPTED_EDIT', regions, details,
                                       {self.key: next_progress})
            next_progress['last_event'] = event
            self.journal.set(self.key, next_progress)
            self.progress = next_progress
            self.previous = authoritative
            self.pending = None
            return 'Accepted edit captured; next checklist step ready'
        return 'Stable; watching. Mark only after accepting the current checklist edit.'
