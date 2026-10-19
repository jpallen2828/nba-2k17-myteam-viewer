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
from PySide6.QtWidgets import QApplication

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
    assert 'does not attach' in page.connection_help.text()
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
