"""Connection decisions, asynchronous lifecycle, and settings UI regressions."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ['NBA2K17_DISABLE_GAME_MONITOR'] = '1'

from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from PySide6.QtCore import QSettings
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QWidget

from nba2k17_viewer import game_connection_ui as ui
from nba2k17_viewer.injection_session import SessionManager


@pytest.fixture(scope='module')
def app():
    result = QApplication.instance() or QApplication([])
    for name in ('segoeui.ttf', 'segoeuib.ttf', 'seguisb.ttf'):
        path = Path('C:/Windows/Fonts') / name
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))
    return result


@pytest.fixture
def controller(tmp_path, app):
    result = ui.GameConnectionController(SessionManager(), settings=QSettings(
        str(tmp_path / 'settings.ini'), QSettings.Format.IniFormat), autostart=False)
    yield result
    result.shutdown()
    wait_idle(app, result)
    result.manager.shutdown()


def process(folder, **kwargs):
    return {'pid': 10, 'token': 'birth', 'game_dir': str(folder), 'supported': True,
            'hook_loaded': True, 'hook_verified': True, 'error': None, **kwargs}


def setup_probe(monkeypatch, processes, error=None):
    monkeypatch.setattr(ui.game_hook, 'probe_processes', lambda: {'processes': processes, 'error': error})
    monkeypatch.setattr(ui.game_hook, 'inspect_installation', lambda folder: {'game_dir': str(folder)})


def test_unique_process_detects_directory_and_syncs(monkeypatch, tmp_path):
    p = process(tmp_path)
    setup_probe(monkeypatch, [p])
    called = []
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: called.append(args) or {'state': 'ready'})
    manager = object()
    result = ui.collect_snapshot(manager, '', 'roster0005')
    assert result['game_dir'] == str(tmp_path)
    assert called == [(manager, 'roster0005', p)]


@pytest.mark.parametrize('monitor_active',[False,True])
def test_window_close_never_abandons_an_active_write_or_reader_recovery(monitor_active, app):
    from nba2k17_viewer.desktop import ViewerWindow
    events=[]
    owner=QWidget()
    owner.injection_manager=SessionManager()
    owner.injection_manager.reserve(writing=True)
    owner.game_connection=SimpleNamespace(active=monitor_active, timer=ui.QTimer())
    event=SimpleNamespace(ignore=lambda:events.append('ignored'),accept=lambda:pytest.fail('Write owner abandoned'))
    ViewerWindow.closeEvent(owner,event)
    assert events==['ignored']
    assert owner._close_retry_timer.isActive()
    assert not owner.injection_manager.cancel_event.is_set()
    owner._close_retry_timer.stop()
    owner.injection_manager.run(lambda _: None, lambda _: None)


@pytest.mark.parametrize('directory', ['', 'selected'])
def test_background_process_does_not_hide_loaded_game(monkeypatch, tmp_path, directory):
    background, loaded = process(tmp_path, pid=10), process(tmp_path, pid=20)
    loaded['custom_face_reader'] = {'state': 'inactive', 'message': 'Reader inactive'}
    setup_probe(monkeypatch, [background, loaded])
    probes, calls = [], []
    registry = {'teams': ['test-only-registry']}
    monkeypatch.setattr(ui, 'runtime_definition', lambda: registry)
    monkeypatch.setattr(ui, 'select_roster_process',
                        lambda candidates, definition: probes.append((candidates, definition)) or 20)
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: calls.append(args) or {'state': 'ready'})
    manager = object()
    result = ui.collect_snapshot(manager, str(tmp_path) if directory else '', 'roster0005')
    assert result['game_dir'] == str(tmp_path)
    assert result['process'] is loaded
    assert result['process']['custom_face_reader']['state'] == 'inactive'
    assert probes == [([(10, 'NBA2K17.exe'), (20, 'NBA2K17.exe')], registry)]
    assert calls == [(manager, 'roster0005', loaded)]


def test_two_loaded_games_remain_ambiguous(monkeypatch, tmp_path):
    setup_probe(monkeypatch, [process(tmp_path, pid=10), process(tmp_path, pid=20)])
    def ambiguous(*args):
        raise ValueError('Multiple NBA 2K17 games have loaded rosters')
    monkeypatch.setattr(ui, 'select_roster_process', ambiguous)
    monkeypatch.setattr(ui, 'runtime_definition', lambda: {})
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: pytest.fail('Ambiguous process'))
    result = ui.collect_snapshot(object(), '', 'roster0005')
    assert result['process'] is None
    assert result['sync']['state'] == 'error'
    assert 'Multiple NBA 2K17 games' in result['sync']['message']


@pytest.mark.parametrize('problem', [{'supported': False}, {'error': 'Access denied'}, {'pid': 0}, {'token': None}])
def test_uncertain_duplicate_process_is_not_probed(monkeypatch, tmp_path, problem):
    setup_probe(monkeypatch, [process(tmp_path, pid=10), process(tmp_path, **{'pid': 20, **problem})])
    monkeypatch.setattr(ui, 'select_roster_process', lambda *args: pytest.fail('Unverified candidate'))
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: pytest.fail('Unverified candidate'))
    assert ui.collect_snapshot(object(), '', 'roster0005')['sync']['state'] == 'error'


def test_multiple_installations_require_directory_selection(monkeypatch, tmp_path):
    setup_probe(monkeypatch, [process(tmp_path, pid=10), process(tmp_path / 'other', pid=20)])
    monkeypatch.setattr(ui, 'select_roster_process', lambda *args: pytest.fail('No selected installation'))
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: pytest.fail('No selected installation'))
    result = ui.collect_snapshot(object(), '', 'roster0005')
    assert result['game_dir'] == ''
    assert result['sync']['state'] == 'error'
    assert 'Select the game folder' in result['sync']['message']


@pytest.mark.parametrize('scenario', ['wrong_folder', 'two_instances', 'probe_failure', 'process_failure'])
def test_uncertain_process_never_rebinds(monkeypatch, tmp_path, scenario):
    processes, error = [process(tmp_path)], None
    folder = str(tmp_path)
    if scenario == 'wrong_folder': folder = str(tmp_path / 'other')
    if scenario == 'two_instances': processes *= 2
    if scenario == 'probe_failure': error = 'Access denied'
    if scenario == 'process_failure': processes[0]['error'] = 'Process restarted'
    setup_probe(monkeypatch, processes, error)
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: pytest.fail('Must not rebind uncertain process'))
    assert ui.collect_snapshot(object(), folder, 'roster0005')['sync']['state'] == 'error'


def test_pause_skips_roster_but_reports_hook(monkeypatch, tmp_path):
    setup_probe(monkeypatch, [process(tmp_path)])
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: pytest.fail('Paused'))
    result = ui.collect_snapshot(object(), str(tmp_path), 'roster0005', synchronize=False)
    assert result['sync']['state'] == 'paused'
    assert result['process']['hook_loaded']


def test_stopped_game_flows_through_serialized_disposal(monkeypatch, tmp_path):
    setup_probe(monkeypatch, [])
    called = []
    monkeypatch.setattr(ui, 'sync_roster', lambda *args: called.append(args) or {'state': 'stopped'})
    ui.collect_snapshot(object(), str(tmp_path), 'roster0005')
    assert called[0][-1] is None


def wait_idle(app, controller):
    deadline = time.monotonic() + 5
    while controller.active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(.005)
    assert not controller.active


def test_busy_injection_does_not_start_worker(monkeypatch, controller):
    controller.manager.reserve(writing=True)
    monkeypatch.setattr(ui, 'collect_snapshot', lambda *a, **kw: pytest.fail('Overlapped injection'))
    controller.refresh(force=True)
    assert not controller.active
    controller.manager.run(lambda _: None, lambda _: None)


def test_manual_refresh_works_with_automatic_refresh_off(monkeypatch, controller, app):
    calls = []
    def collect(*args, **kwargs):
        calls.append(kwargs['synchronize'])
        return {'game_dir': '', 'sync': {'state': 'waiting'}}
    monkeypatch.setattr(ui, 'collect_snapshot', collect)
    controller.auto_refresh = False
    controller.refresh(refresh_roster=True)
    wait_idle(app, controller)
    assert calls == [True]
    controller.refresh()
    wait_idle(app, controller)
    assert calls == [True, False]


def test_worker_shutdown_discards_late_result(monkeypatch, controller, app):
    import threading
    release = threading.Event()
    def collect(*args, **kwargs):
        release.wait(2)
        return {'game_dir': '', 'sync': {'state': 'waiting'}}
    monkeypatch.setattr(ui, 'collect_snapshot', collect)
    controller.refresh()
    assert controller.active
    assert not controller.shutdown()
    release.set()
    wait_idle(app, controller)
    assert controller.snapshot == {}
    assert controller.closing


def test_hook_cleanup_runs_off_ui_thread(monkeypatch, controller, app):
    import threading
    entered, release = threading.Event(), threading.Event()
    def cleanup():
        entered.set()
        release.wait(3)
    monkeypatch.setattr(controller.name_companion, 'close', cleanup)
    try:
        assert not controller.shutdown()
        assert entered.wait(1)
        ticks=[]
        ui.QTimer.singleShot(0, lambda: ticks.append(True))
        app.processEvents()
        assert ticks and controller.active
    finally:
        release.set()
        wait_idle(app, controller)
    assert controller.shutdown()


def test_close_cancels_stalled_frida_attach(monkeypatch, controller, app):
    import frida
    import threading
    entered=threading.Event()
    def stalled_attach(*args, **kwargs):
        cancelled=threading.Event()
        token=frida.Cancellable.get_current()
        handler=token.connect(cancelled.set)
        entered.set()
        try:
            assert cancelled.wait(3), 'Close did not cancel the blocked hook operation'
            token.raise_if_cancelled()
        finally:
            token.disconnect(handler)
    monkeypatch.setattr(ui, 'collect_snapshot', stalled_attach)
    controller.refresh()
    assert entered.wait(1)
    assert not controller.shutdown()
    wait_idle(app, controller)
    assert controller.shutdown()
    assert controller.snapshot == {}


def test_connection_job_deadline_cancels_frida_wait(app):
    import frida
    import threading
    def stalled():
        cancelled=threading.Event()
        token=frida.Cancellable.get_current()
        handler=token.connect(cancelled.set)
        try:
            assert cancelled.wait(2)
            token.raise_if_cancelled()
        finally:
            token.disconnect(handler)
    job=ui._Job(stalled, timeout=.05)
    errors=[]
    job.signals.error.connect(errors.append)
    job.run()
    assert len(errors)==1


@pytest.mark.parametrize('writing', [False, True])
def test_one_close_request_finishes_after_busy_work(app, writing):
    from nba2k17_viewer.desktop import ViewerWindow
    class Window(QWidget):
        closeEvent = ViewerWindow.closeEvent
    window=Window()
    window.injection_manager=SessionManager()
    window.injection_manager.reserve(writing=writing)
    window.game_connection=None
    window.overlay_history=[]
    window.studio=None
    window.show()
    assert not window.close()
    assert window.isVisible()
    assert window.injection_manager.cancel_event.is_set() == (not writing)
    try:
        window.injection_manager.run(lambda _: None, lambda _: None)
    except ValueError:
        assert not writing
    deadline=time.monotonic()+2
    while window.isVisible() and time.monotonic()<deadline:
        app.processEvents()
        time.sleep(.01)
    assert not window.isVisible()
    assert not window._close_retry_timer.isActive()


def snapshot(folder, *, loaded=False):
    return {'installation': {'ready': True, 'installable': True, 'issues': [],
        'config': {'exists': True, 'sha256': 'current-hash', 'errors': [],
                   'values': {key: False for key in ui.game_hook.HOOK_OPTIONS}},
        'log': {'text': 'Old log: all features installed'}},
        'game_dir': str(folder), 'process': process(folder) if loaded else None,
        'probe': {'processes': [process(folder)] if loaded else []},
        'sync': {'state': 'stopped', 'message': 'Game stopped'}}


def test_dirty_choices_survive_poll_and_use_original_hash(controller, tmp_path, app, monkeypatch):
    controller.game_dir = str(tmp_path)
    controller.snapshot = snapshot(tmp_path)
    page = ui.GameConnectionPage(controller)
    page.options['forcedisplayphotos'].setChecked(True)
    controller.snapshot['installation']['config']['sha256'] = 'external-change'
    page.apply_snapshot(controller.snapshot)
    assert page.options['forcedisplayphotos'].isChecked()
    assert page.config_hash == 'current-hash'
    calls = []
    monkeypatch.setattr(controller, 'save_settings', lambda values, digest: calls.append((values, digest)))
    page.save()
    assert calls[0][1] == 'current-hash'
    assert calls[0][0]['forcedisplayphotos'] is True
    page.close()


def test_old_log_cannot_claim_active_hook(controller, tmp_path):
    controller.game_dir = str(tmp_path)
    controller.snapshot = snapshot(tmp_path)
    page = ui.GameConnectionPage(controller)
    assert page.status.text() == 'Game stopped'
    assert 'Old log' in page.log.toPlainText()
    assert 'next game launch' in next(g.title() for g in page.findChildren(ui.QGroupBox) if 'settings' in g.title())
    page.close()


def test_loaded_hook_is_separate_from_saved_settings(controller, tmp_path):
    controller.game_dir = str(tmp_path)
    controller.snapshot = snapshot(tmp_path, loaded=True)
    page = ui.GameConnectionPage(controller)
    assert 'Verified hook DLL loaded' in page.status.text()
    assert not page.install_button.isEnabled()
    assert not any(check.isChecked() for check in page.options.values())
    page.close()


@pytest.mark.parametrize('state,label',[('inactive','inactive'),('broken','needs repair'),('unverified','unverified')])
def test_custom_reader_problem_is_visible_even_with_verified_hook(controller,tmp_path,state,label):
    controller.game_dir=str(tmp_path)
    controller.snapshot=snapshot(tmp_path,loaded=True)
    controller.snapshot['process']['custom_face_reader']={'state':state,'message':'Reader-specific explanation.'}
    page=ui.GameConnectionPage(controller)
    assert 'custom-face reader '+label in page.status.text()
    assert page.setup_guidance.text()=='Reader-specific explanation.'
    page.close()


def test_missing_loader_guides_setup_without_claiming_running_attachment(controller, tmp_path):
    controller.game_dir = str(tmp_path)
    state = snapshot(tmp_path, loaded=True)
    state['process'].update(hook_loaded=False, hook_verified=False)
    state['installation'].update(ready=False, installable=False, dependencies={
        '2K17WAIGUADLL.dll': {'valid_x64_dll': False},
        'dinput8.dll': {'valid_x64_dll': False}})
    controller.snapshot = state
    page = ui.GameConnectionPage(controller)
    assert 'Hook not installed' in page.status.text()
    assert '2K17WAIGUADLL.dll' in page.setup_guidance.text()
    assert 'dinput8.dll' in page.setup_guidance.text()
    assert 'close NBA 2K17' in page.setup_guidance.text()
    assert not page.install_button.isEnabled()
    assert 'read only' in page.auto.text()
    assert 'mod-file hook loads when NBA 2K17 starts' in page.connection_help.text()
    assert 'Full-name presentation below connects while MyTEAM is open' in page.connection_help.text()
    page.close()


def test_installed_hook_requires_restart_when_game_is_running(controller, tmp_path):
    controller.game_dir = str(tmp_path)
    controller.snapshot = snapshot(tmp_path, loaded=True)
    controller.snapshot['process'].update(hook_loaded=False, hook_verified=False)
    page = ui.GameConnectionPage(controller)
    assert 'Restart NBA 2K17 to load the hook' in page.status.text()
    assert 'cannot attach' in page.setup_guidance.text()
    assert not page.install_button.isEnabled()
    page.close()


def test_hook_incompatible_build_never_uses_roster_reader_support_as_proof(controller, tmp_path):
    controller.game_dir = str(tmp_path)
    controller.snapshot = snapshot(tmp_path, loaded=True)
    controller.snapshot['installation'].update(ready=False, installable=True,
        executable={'supported': True, 'hook_supported': False})
    # Even a loaded, known DLL is not proof that its build guard accepted the game.
    page = ui.GameConnectionPage(controller)
    assert 'does not support this game build' in page.status.text()
    assert 'Verified hook DLL loaded' not in page.status.text()
    assert 'restarting will not fix' in page.setup_guidance.text()
    assert 'live roster access separately' in page.setup_guidance.text()
    assert not page.install_button.isEnabled()
    controller.snapshot['process'] = None
    controller.snapshot['probe']['processes'] = []
    page.apply_snapshot(controller.snapshot)
    assert not page.install_button.isEnabled()
    page.close()


def test_dependency_button_opens_author_link_without_installing(controller, tmp_path, monkeypatch):
    controller.snapshot = snapshot(tmp_path)
    opened = []
    monkeypatch.setattr(ui.QDesktopServices, 'openUrl', lambda url: opened.append(url.toString()))
    monkeypatch.setattr(ui.game_hook, 'install_hook', lambda *args: pytest.fail('Link must not install'))
    page = ui.GameConnectionPage(controller)
    page.dependencies_button.click()
    assert opened == ['https://forums.nba-live.com/downloads.php?view=detail&df_id=8772']
    page.close()


def test_live_table_preserves_team_and_slot_gaps(controller, tmp_path):
    controller.snapshot = snapshot(tmp_path)
    controller.snapshot['sync'] = {'state': 'ready', 'message': 'Live roster synchronized.',
        'scan': {'teams': [{'key': 'hawks', 'label': 'Historic Hawks',
                           'slots': [{'slot': 3, 'name': 'Custom Player', 'position': 'PF'}]}]}}
    page = ui.GameConnectionPage(controller)
    assert page.players.item(0, 0).text() == '4'
    assert page.players.item(0, 1).text() == 'Custom Player'
    page.apply_snapshot(controller.snapshot)
    assert page.teams.currentData() == 'hawks'
    item = page.players.item(0, 1)
    page.apply_snapshot(controller.snapshot)
    assert page.players.item(0, 1) is item
    controller.snapshot['sync'] = {'state': 'paused', 'message': 'Automatic roster refresh is paused.'}
    page.apply_snapshot(controller.snapshot)
    assert page.players.item(0, 1) is item
    assert 'paused' in page.roster_status.text()
    page.close()


def test_layout_has_no_horizontal_overflow(controller, tmp_path, app):
    from nba2k17_viewer.desktop import STYLE
    app.setStyleSheet(STYLE)
    controller.snapshot = snapshot(tmp_path)
    page = ui.GameConnectionPage(controller)
    page.resize(1030, 510)
    page.show()
    app.processEvents()
    scroll = page.findChild(ui.QScrollArea)
    assert scroll.horizontalScrollBar().maximum() == 0
    assert scroll.verticalScrollBar().maximum() > 0
    for check in page.options.values():
        assert check.width() >= check.sizeHint().width()
    page.close()


def test_main_navigation_fits_minimum_window(monkeypatch, tmp_path, app):
    from nba2k17_viewer.desktop import ViewerWindow
    monkeypatch.setattr(ViewerWindow, 'load_archive', lambda self: None)
    monkeypatch.setattr(ui, 'collect_snapshot', lambda *args, **kwargs: {
        'game_dir': '', 'sync': {'state': 'stopped', 'message': 'NBA 2K17 is not running.'}})
    window = ViewerWindow(tmp_path / 'not-opened.sqlite')
    window.resize(1030, 690)
    window.show()
    window.show_game_connection()
    wait_idle(app, window.game_connection)
    app.processEvents()
    assert window.width() == 1030
    assert window.pages.currentWidget() is window.connection_page
    assert window.connection_button.geometry().right() < window.width()
    assert window.connection_button.width() >= window.connection_button.sizeHint().width()
    window.close()


def test_injection_view_pauses_stale_roster_refresh(controller, monkeypatch):
    calls=[]
    monkeypatch.setattr(ui, 'collect_snapshot', lambda *a, **k: calls.append(True))
    controller.manager.reserve()
    controller.active=True
    controller.pause_for_injection()
    assert controller.manager.cancel_event.is_set()
    try:
        controller.manager.run(lambda _: None, lambda _: None)
    except ValueError:
        pass
    controller.active=False
    controller.refresh(force=True)
    assert not calls and not controller.active
    resumed=[]
    monkeypatch.setattr(controller, 'refresh', lambda **k: resumed.append(k))
    controller.timer.start()
    controller.resume_after_injection()
    controller.timer.stop()
    assert resumed == [{'force': True}]


def test_injection_pause_never_cancels_a_write(controller):
    controller.manager.reserve(writing=True)
    controller.active=True
    controller.pause_for_injection()
    assert not controller.manager.cancel_event.is_set()
    controller.manager.run(lambda _: None, lambda _: None)
    controller.active=False
