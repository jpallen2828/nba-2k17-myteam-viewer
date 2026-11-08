"""Name companion lifecycle without attaching to or writing a game process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / '.project/tools'), str(ROOT / '.project/src')]
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['NBA2K17_DISABLE_GAME_MONITOR'] = '1'

from nba2k17_viewer import name_presentation as names


def process(**changes):
    return {'pid': 410, 'token': '410:creation-time-A', 'supported': True,
            'executable_sha256': names.HOOK_EXECUTABLE_SHA256, 'error': None,
            **changes}


class FakeRuntime:
    """Exercise ownership and failure paths while making any real attach impossible."""
    def __init__(self):
        self.events = []
        self.sessions = []
        self.initializations = []
        self.allocations = 0
        self.failure = None
        self.teardown_failures = set()

    def event(self, action, value=None):
        self.events.append((action, value))
        if self.failure == action or action in self.teardown_failures:
            raise RuntimeError('simulated ' + action + ' failure')

    def verify(self, p):
        self.event('verify', (p['pid'], p['token']))

    def attach(self, pid):
        self.event('attach', pid)
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def count(self, action):
        return sum(kind == action for kind, _ in self.events)


class FakeSession:
    def __init__(self, runtime):
        self.runtime = runtime
        self.script = None

    def create_script(self, source):
        self.runtime.event('create', source)
        self.script = FakeScript(self.runtime)
        return self.script

    def detach(self):
        self.runtime.event('detach')


class FakeScript:
    def __init__(self, runtime):
        self.runtime = runtime
        self.exports_sync = self
        self.enabled = False

    def load(self):
        self.runtime.event('load')

    def initialize(self, options):
        self.runtime.event('initialize', options)
        self.runtime.initializations.append(options.copy())
        page = options.get('retained_page')
        if not page:
            self.runtime.allocations += 1
            page = hex(0x30000000 + 0x1000 * self.runtime.allocations)
        return {'retained_page': page}

    def setenabled(self, enabled):
        self.runtime.event('enabled', enabled)
        self.enabled = enabled

    def status(self):
        self.runtime.event('status')
        return {'enabled': self.enabled, 'menu_calls': 2, 'jersey_calls': 3}

    def stop(self):
        self.runtime.event('stop')

    def unload(self):
        self.runtime.event('unload')


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    # Supply only a dummy script to the fake session, never the production agent.
    package = tmp_path / 'package'
    (package / 'assets').mkdir(parents=True)
    (package / 'assets/name-presentation.js').write_text('// test script', encoding='utf-8')
    monkeypatch.setattr(names, '__file__', str(package / 'name_presentation.py'))
    return FakeRuntime()


def controller(tmp_path, runtime):
    return names.NamePresentationController(cache_path=tmp_path / 'retained.json',
        attach=runtime.attach, verify=runtime.verify)


def test_repeated_refresh_keeps_one_session_and_one_initialization(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    try:
        for _ in range(40):
            result = companion.update(process())
            assert result['state'] == 'active'
            assert result['diagnostics']['enabled'] is True
        assert runtime.count('attach') == runtime.count('load') == runtime.count('initialize') == 1
        assert runtime.allocations == 1
        assert runtime.count('detach') == runtime.count('stop') == runtime.count('unload') == 0
    finally:
        companion.close()
    assert runtime.count('detach') == runtime.count('stop') == runtime.count('unload') == 1


def test_off_on_reuses_native_page_and_session(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    try:
        assert companion.update(process(), enabled=False)['state'] == 'disabled'
        assert runtime.count('attach') == 0
        companion.update(process())
        session = companion.session
        for _ in range(6):
            assert companion.update(process(), enabled=False)['state'] == 'disabled'
            assert companion.session is session
            assert not companion.script.enabled
            assert companion.update(process())['state'] == 'active'
        assert runtime.count('attach') == runtime.count('initialize') == runtime.allocations == 1
    finally:
        companion.close()


def test_game_exit_closes_all_handles_and_pid_reuse_gets_fresh_page(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    companion.update(process())
    assert companion.update(None)['state'] == 'waiting'
    assert companion.session is companion.script is companion.key is None
    assert [event for event, _ in runtime.events][-3:] == ['stop', 'unload', 'detach']
    assert companion.update(process(token='410:creation-time-B'))['state'] == 'active'
    assert runtime.initializations[-1]['retained_page'] is None
    assert runtime.allocations == 2
    companion.close()


def test_direct_process_replacement_disposes_old_session_before_attach(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    companion.update(process())
    start = len(runtime.events)
    assert companion.update(process(pid=411, token='411:new-time'))['state'] == 'active'
    events = [event for event, _ in runtime.events[start:]]
    assert events[:3] == ['stop', 'unload', 'detach']
    assert events.index('detach') < events.index('attach')
    assert runtime.initializations[-1]['retained_page'] is None
    companion.close()


@pytest.mark.parametrize('failure', ['verify', 'attach', 'create', 'load', 'initialize', 'enabled', 'status'])
def test_failed_process_is_not_retried_by_polling_but_toggle_can_retry(tmp_path, runtime, failure):
    companion = controller(tmp_path, runtime)
    runtime.failure = failure
    first = companion.update(process())
    assert first['state'] == 'error'
    assert failure in first['message']
    event_count = len(runtime.events)
    for _ in range(10):
        assert companion.update(process()) == first
    assert len(runtime.events) == event_count
    assert companion.session is companion.script is companion.key is None
    assert runtime.count('detach') == len(runtime.sessions)
    assert runtime.count('unload') == sum(session.script is not None for session in runtime.sessions)
    runtime.failure = None
    assert companion.update(process(), enabled=False)['state'] == 'disabled'
    assert companion.update(process())['state'] == 'active'
    companion.close()


def test_second_process_verification_failure_cleans_up_loaded_agent(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    calls = 0
    def changed_after_attach(p):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError('process replaced during attachment')
    companion._verify = changed_after_attach
    assert companion.update(process())['state'] == 'error'
    assert runtime.count('initialize') == 1
    assert [event for event, _ in runtime.events][-3:] == ['stop', 'unload', 'detach']
    assert companion.script is companion.session is None


@pytest.mark.parametrize('changes', [
    {'supported': False}, {'executable_sha256': 'wrong-build'},
    {'token': None}, {'error': 'access denied'},
])
def test_unsupported_process_never_imports_or_attaches_frida(tmp_path, monkeypatch, changes):
    import builtins
    original_import = builtins.__import__
    def forbid_frida(name, *args, **kwargs):
        assert name != 'frida', 'Unsupported build tried to import Frida'
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', forbid_frida)
    companion = names.NamePresentationController(cache_path=tmp_path / 'retained.json',
        verify=lambda _: pytest.fail('Unsupported build reached process attachment'))
    assert companion.update(process(**changes))['state'] == 'unsupported'
    assert companion.session is companion.script is None


def test_cache_is_reused_only_for_exact_pid_and_creation_token(tmp_path, runtime):
    first = controller(tmp_path, runtime)
    first.update(process())
    first.close()
    second = controller(tmp_path, runtime)
    assert second.update(process())['state'] == 'active'
    saved = json.loads((tmp_path / 'retained.json').read_text(encoding='utf-8'))
    assert runtime.initializations[-1]['retained_page'] == saved['retained_page']
    assert runtime.initializations[-1]['process_token'] == process()['token']
    assert runtime.allocations == 1
    second.close()
    third = controller(tmp_path, runtime)
    third.update(process(pid=999))
    assert runtime.initializations[-1]['retained_page'] is None
    third.close()


@pytest.mark.parametrize('contents', ['not json', 'null', '[]', '{"key": [410,"stale"]}'])
def test_invalid_cache_does_not_block_new_attachment(tmp_path, runtime, contents):
    (tmp_path / 'retained.json').write_text(contents, encoding='utf-8')
    companion = controller(tmp_path, runtime)
    try:
        assert companion.update(process())['state'] == 'active'
        assert runtime.initializations[-1]['retained_page'] is None
    finally:
        companion.close()


def test_teardown_attempts_every_handle_even_if_each_step_raises(tmp_path, runtime):
    companion = controller(tmp_path, runtime)
    companion.update(process())
    runtime.teardown_failures = {'stop', 'unload', 'detach'}
    companion.close()
    assert [event for event, _ in runtime.events][-3:] == ['stop', 'unload', 'detach']
    assert companion.script is companion.session is companion.key is None
    previous = len(runtime.events)
    companion.close()
    assert len(runtime.events) == previous


def test_unwritable_page_cache_reuses_retained_page_on_manual_retry(tmp_path, runtime, monkeypatch):
    companion = controller(tmp_path, runtime)
    original_replace = Path.replace
    def reject_cache_replace(path, target):
        if Path(target) == companion.cache_path:
            raise PermissionError('Settings drive is read only')
        return original_replace(path, target)
    monkeypatch.setattr(Path, 'replace', reject_cache_replace)
    try:
        for _ in range(4):
            companion.update(process(), enabled=False)
            companion.update(process())
        assert runtime.allocations == 1
        assert runtime.initializations[0]['retained_page'] is None
        assert all(item['retained_page'] == '0x30001000' for item in runtime.initializations[1:])
    finally:
        companion.close()


@pytest.mark.parametrize('invalid', ['token', 'build', None])
def test_process_verification_closes_reader_even_on_mismatch(monkeypatch, tmp_path, invalid):
    import scanner.windows_process as windows
    import scanner.discovery.live as live
    calls = []
    class Reader:
        def __init__(self, pid):
            calls.append(('open', pid))
        def __enter__(self):
            return self
        def __exit__(self, *_):
            calls.append(('close', None))
        def process_info(self):
            return SimpleNamespace(executable_path=str(tmp_path / 'NBA2K17.exe'))
    monkeypatch.setattr(windows, 'ProcessReader', Reader)
    monkeypatch.setattr(live, 'process_token', lambda _: 'reused-pid' if invalid == 'token' else process()['token'])
    def digest(path):
        calls.append(('hash', str(path)))
        return 'unknown-build' if invalid == 'build' else names.HOOK_EXECUTABLE_SHA256
    monkeypatch.setattr(names, 'file_sha256', digest)
    if invalid:
        with pytest.raises(ValueError, match='process changed|does not support'):
            names._verify_process(process())
    else:
        names._verify_process(process())
    assert calls[0] == ('open', process()['pid'])
    assert calls[-1] == ('close', None)
    if invalid == 'token':
        assert not any(action == 'hash' for action, _ in calls)


def test_ui_shutdown_waits_for_active_worker_before_detaching(tmp_path, runtime, monkeypatch):
    from PySide6.QtCore import QSettings
    from PySide6.QtWidgets import QApplication
    from nba2k17_viewer import game_connection_ui as ui
    from nba2k17_viewer.injection_session import SessionManager

    app = QApplication.instance() or QApplication([])
    manager = SessionManager()
    connection = ui.GameConnectionController(manager, autostart=False, settings=QSettings(
        str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat))
    connection.name_companion = controller(tmp_path, runtime)
    entered, release = threading.Event(), threading.Event()
    def collect(*args, **kwargs):
        kwargs['name_companion'].update(process())
        entered.set()
        assert release.wait(4), 'Test did not release connection worker'
        return {'game_dir': '', 'sync': {'state': 'waiting'}}
    monkeypatch.setattr(ui, 'collect_snapshot', collect)
    try:
        connection.refresh()
        assert entered.wait(3)
        assert not connection.shutdown()
        assert runtime.count('detach') == 0
        release.set()
        deadline = time.monotonic() + 4
        while connection.active and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert not connection.active
        assert connection.snapshot == {}
        assert runtime.count('stop') == runtime.count('unload') == runtime.count('detach') == 1
    finally:
        release.set()
        connection.shutdown()
        manager.shutdown()
