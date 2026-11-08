"""Scoped, build-verified Shai name presentation while the app is connected.

Roster fields remain bounded and terminated. The companion changes only the
reviewed menu getter results and jersey renderer's input pointer. It never
extends a roster buffer or changes the game's executable on disk.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .game_hook import HOOK_EXECUTABLE_SHA256, file_sha256


def _verify_process(process):
    from scanner.windows_process import ProcessReader
    from scanner.discovery.live import process_token
    with ProcessReader(process['pid']) as reader:
        if process_token(reader) != str(process['token']):
            raise ValueError('The game process changed. Refresh the connection.')
        path = Path(reader.process_info().executable_path)
        if file_sha256(path) != HOOK_EXECUTABLE_SHA256:
            raise ValueError('Name presentation does not support this game build.')


class NamePresentationController:
    """One companion per process lifetime; polling and toggles reuse it.

    Called only by the connection's serialized worker. Native callers can retain
    returned text, so its single read-only page lives until game exit and is
    reused across app restarts. All callbacks and handles detach on app exit.
    """
    def __init__(self, *, cache_path=None, attach=None, verify=None):
        self.cache_path = Path(cache_path) if cache_path else (
            Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) /
            'NBA2K17MyTEAM' / 'name-presentation.json')
        self._attach = attach
        self._verify = verify or _verify_process
        self.session = self.script = None
        self.key = None
        self.failure_key = None
        self.failure_message = None
        self._retained_key = self._retained_page = None

    def _cached_page(self, key):
        if key == self._retained_key:
            return self._retained_page
        try:
            saved = json.loads(self.cache_path.read_text(encoding='utf-8'))
            if isinstance(saved, dict) and saved.get('key') == list(key):
                return saved.get('retained_page')
        except (OSError, ValueError, TypeError):
            pass
        return None

    def _remember_page(self, key, page):
        # Preserve the address even if the settings drive becomes unwritable;
        # a manual retry in this app instance must reuse the allocation.
        self._retained_key, self._retained_page = key, page
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        pending = self.cache_path.with_suffix('.tmp')
        pending.write_text(json.dumps({'key': list(key), 'retained_page': page}), encoding='utf-8')
        pending.replace(self.cache_path)

    def update(self, process, *, enabled=True):
        if not process:
            self.close()
            self.failure_key = self.failure_message = None
            return {'state': 'waiting', 'message': 'Waiting for NBA 2K17.'}
        key = (process.get('pid'), str(process.get('token') or ''))
        if key != self.key:
            self.close()
        if not enabled:
            if self.script:
                try:
                    self.script.exports_sync.setenabled(False)
                except Exception:
                    self.close()
            self.failure_key = self.failure_message = None
            return {'state': 'disabled', 'message': 'Full-name presentation is off.'}
        if (not key[1] or not process.get('supported') or process.get('error')
                or process.get('executable_sha256') != HOOK_EXECUTABLE_SHA256):
            self.close()
            return {'state': 'unsupported', 'message': 'Full-name presentation needs the verified NBA 2K17 build.'}
        if self.failure_key == key:
            return {'state': 'error', 'message': self.failure_message}
        try:
            self._verify(process)
            if not self.script:
                if self._attach is None:
                    import frida
                    if frida.__version__ != '17.2.17':
                        raise ValueError('The bundled name companion version is unsupported.')
                    attach = frida.attach
                else:
                    attach = self._attach
                self.session = attach(key[0])
                source = Path(__file__).with_name('assets').joinpath('name-presentation.js').read_text(encoding='utf-8')
                self.script = self.session.create_script(source)
                self.script.load()
                result = self.script.exports_sync.initialize({
                    'process_token': key[1], 'retained_page': self._cached_page(key)})
                self._remember_page(key, result['retained_page'])
                self._verify(process)
                self.key = key
            self.script.exports_sync.setenabled(True)
            status = self.script.exports_sync.status()
            return {'state': 'active', 'message':
                    'Shai’s full name is active for menus and jerseys. Keep MyTEAM open; reopen an existing player view to refresh its jersey.',
                    'diagnostics': status}
        except Exception as exc:
            self.close()
            self.failure_key = key
            self.failure_message = ('Full-name presentation unavailable: ' + str(exc)
                                    + ' Toggle this option off and on to retry. Injection still uses a safely shortened native name.')
            return {'state': 'error', 'message': self.failure_message}

    def close(self):
        script, session = self.script, self.session
        self.script = self.session = self.key = None
        if script:
            try:
                script.exports_sync.stop()
            except Exception:
                pass
            try:
                script.unload()
            except Exception:
                pass
        if session:
            try:
                session.detach()
            except Exception:
                pass
