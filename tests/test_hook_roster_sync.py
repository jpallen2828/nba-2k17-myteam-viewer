from pathlib import Path
from threading import Event, Thread

import pytest

from nba2k17_viewer.hook_roster_sync import sync_roster
from nba2k17_viewer.injection_session import SessionManager


PROCESS = {'pid': 52, 'token': '100002', 'supported': True}


class FakeSession:
    def __init__(self, path, *, pid=52, token='100002', changes=0):
        self.roster_file = Path(path).resolve()
        self.pid, self.process_start = pid, token
        self.valid = True
        self.cancel_event = None
        self.calls = []
        self.last_run = ('undo-report', object())
        self.changes = changes
        self.scan = {'teams': [{'slots': [{'name': 'Old player'}]}]}
        self.validation_error = None
        self.refresh_error = None

    def begin_operation(self):
        self.calls.append('begin')

    def validate(self):
        self.calls.append('validate')
        if self.validation_error:
            raise self.validation_error
        return True

    def refresh_mutable_records(self):
        self.calls.append('refresh')
        if self.refresh_error:
            raise self.refresh_error
        if self.changes:
            self.scan['teams'][0]['slots'][0]['name'] = 'Custom player'
        return self.changes

    def public_scan(self):
        self.calls.append('scan')
        return self.scan

    def close(self):
        self.calls.append('close')
        self.valid = False

    def write(self, *args, **kwargs):
        pytest.fail('Automatic roster synchronization must not write game memory')

    undo = diagnose_ownership = write


@pytest.fixture
def roster(tmp_path):
    path = tmp_path / 'roster0005'
    path.write_bytes(b'roster')
    return path


def factory_log(*, token='100002', error=None, callback=None):
    calls = []

    def factory(path, *, pid, progress, cancel_event=None):
        calls.append((path, pid, cancel_event))
        if callback:
            callback()
        if error:
            raise error
        return FakeSession(path, pid=pid, token=token)

    return factory, calls


def test_initial_attachment_uses_only_confirmed_selection_and_explicit_process(roster):
    manager = SessionManager()
    factory, calls = factory_log()
    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result['state'] == 'ready'
    assert result['rebound'] and result['changed']
    assert calls == [(roster.resolve(), 52, manager.cancel_event)]
    assert manager.session.calls == ['validate', 'scan']
    assert not manager.busy and not manager.writing
    assert manager.session.cancel_event is None


@pytest.mark.parametrize('changes', [0, 3])
def test_stable_binding_reads_current_names_without_losing_undo(roster, changes):
    manager = SessionManager()
    original = manager.session = FakeSession(roster, changes=changes)
    undo = original.last_run
    factory, calls = factory_log()
    result = sync_roster(manager, str(roster), PROCESS, session_factory=factory)
    assert result['state'] == 'ready' and not result['rebound']
    assert result['changed'] is bool(changes)
    assert manager.session is original and original.last_run is undo
    assert original.calls == ['begin', 'validate', 'refresh', 'validate', 'scan']
    assert not calls and not manager.busy
    expected = 'Custom player' if changes else 'Old player'
    assert result['scan']['teams'][0]['slots'][0]['name'] == expected
    result['scan']['teams'][0]['slots'][0]['name'] = 'UI-local copy'
    assert original.scan['teams'][0]['slots'][0]['name'] == expected


@pytest.mark.parametrize('change', ['pid', 'token', 'path', 'invalid', 'slot', 'file', 'structure'])
def test_changed_binding_is_closed_and_fully_resolved_again(roster, tmp_path, change):
    manager = SessionManager()
    original = manager.session = FakeSession(roster)
    if change == 'pid':
        original.pid = 33
    elif change == 'token':
        original.process_start = 'older-start'
    elif change == 'path':
        original.roster_file = tmp_path / 'roster0007'
    elif change == 'invalid':
        original.valid = False
    elif change == 'slot':
        original.validation_error = ValueError('Live team references changed.')
    elif change == 'file':
        original.validation_error = OSError('The roster file changed.')
    else:
        original.refresh_error = ValueError('Structural, unknown or unstable change.')
    factory, calls = factory_log()
    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result['state'] == 'ready' and result['rebound'] and result['changed']
    assert original.calls[-1] == 'close' and not original.valid
    assert manager.session is not original
    assert len(calls) == 1


@pytest.mark.parametrize('path_kind,process,state', [
    ('present', None, 'stopped'),
    ('present', {**PROCESS, 'supported': False}, 'unsupported'),
    ('present', {**PROCESS, 'supported': None}, 'unsupported'),
    ('none', PROCESS, 'waiting'),
    ('empty', PROCESS, 'waiting'),
    ('missing', PROCESS, 'waiting'),
])
def test_no_game_or_no_confirmed_roster_releases_stale_session(roster, path_kind, process, state):
    manager = SessionManager()
    original = manager.session = FakeSession(roster)
    path = {'present': roster, 'none': None, 'empty': '', 'missing': roster.with_name('missing')}[path_kind]
    factory, calls = factory_log()
    result = sync_roster(manager, path, process, session_factory=factory)
    assert result['state'] == state
    assert manager.session is None and original.calls == ['close']
    assert not calls and not manager.busy


@pytest.mark.parametrize('change', [{'pid': 0}, {'pid': True}, {'token': None}, {'token': ''}])
def test_unverified_process_identity_never_attaches(roster, change):
    manager = SessionManager()
    factory, calls = factory_log()
    result = sync_roster(manager, roster, {**PROCESS, **change}, session_factory=factory)
    assert result['state'] == 'error' and not calls and manager.session is None


@pytest.mark.parametrize('writing', [False, True])
def test_reserved_operations_are_not_interrupted_even_when_game_stops(roster, writing):
    manager = SessionManager()
    original = manager.session = FakeSession(roster)
    assert manager.reserve(writing=writing)
    factory, calls = factory_log()
    result = sync_roster(manager, roster, None, session_factory=factory)
    assert result['state'] == 'busy'
    assert manager.busy and manager.writing is writing
    assert manager.session is original and not original.calls and not calls
    manager.run(lambda progress: None, lambda message: None)


def test_shutdown_prevents_new_scan(roster):
    manager = SessionManager()
    assert manager.shutdown()
    factory, calls = factory_log()
    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result['state'] == 'busy' and not calls
    assert not manager.busy


def test_shutdown_during_scan_cancels_result_and_closes_handle(roster):
    manager = SessionManager()
    created = []

    def factory(path, *, pid, progress, cancel_event):
        session = FakeSession(path, pid=pid)
        created.append(session)
        assert manager.shutdown() is False
        assert cancel_event.is_set()
        return session

    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result['state'] == 'error'
    assert manager.session is None and created[0].calls == ['close']
    assert not manager.busy


def test_restart_between_process_probe_and_attachment_is_rejected(roster):
    manager = SessionManager()
    created = []

    def factory(path, *, pid, progress):
        session = FakeSession(path, pid=pid, token='new-start')
        created.append(session)
        return session

    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result['state'] == 'error' and 'restarted' in result['message']
    assert manager.session is None and created[0].calls == ['close']
    assert not manager.busy


def test_failed_scan_drops_stale_binding_and_releases_operation(roster):
    manager = SessionManager()
    original = manager.session = FakeSession(roster)
    original.validation_error = ValueError('Changed team pointer')
    factory, calls = factory_log(error=ValueError('Invalid team directory'))
    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result == {'state': 'error', 'message': 'Invalid team directory'}
    assert manager.session is None and original.calls[-1] == 'close'
    assert not manager.busy and len(calls) == 1


def test_unexpected_refresh_failure_does_not_rebind_or_reuse_partial_state(roster):
    manager = SessionManager()
    original = manager.session = FakeSession(roster)
    original.refresh_error = RuntimeError('Decode failure')
    factory, calls = factory_log()
    result = sync_roster(manager, roster, PROCESS, session_factory=factory)
    assert result == {'state': 'error', 'message': 'Decode failure'}
    assert manager.session is None and original.calls[-1] == 'close'
    assert not manager.busy and not calls


def test_transaction_cannot_reserve_during_background_refresh(roster):
    manager = SessionManager()
    entered, release = Event(), Event()
    results = []

    def hold_scan():
        entered.set()
        assert release.wait(3)

    factory, calls = factory_log(callback=hold_scan)
    worker = Thread(target=lambda: results.append(sync_roster(manager, roster, PROCESS, session_factory=factory)))
    worker.start()
    try:
        assert entered.wait(3)
        assert not manager.reserve(writing=True)
        assert sync_roster(manager, roster, PROCESS, session_factory=factory)['state'] == 'busy'
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive()
    assert results[0]['state'] == 'ready' and len(calls) == 1
    assert manager.reserve(writing=True)
    manager.run(lambda progress: None, lambda message: None)
