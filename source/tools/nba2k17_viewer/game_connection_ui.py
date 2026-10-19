"""Optional hook setup and serialized, read-only live roster monitoring."""
from __future__ import annotations

import os
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QSettings, QTimer, Signal, Slot, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel,
    QPushButton, QLineEdit, QCheckBox, QFileDialog, QGroupBox, QScrollArea,
    QComboBox, QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit)

from . import game_hook
from .hook_roster_sync import sync_roster


HOOK_DEPENDENCY_URL = 'https://forums.nba-live.com/downloads.php?view=detail&df_id=8772'


def _hook_unsupported(installation, process=None):
    """The roster reader and this optional DLL have separate build support."""
    return ((installation.get('executable') or {}).get('hook_supported') is False
            or (process or {}).get('hook_supported') is False)


def _hook_installed(installation):
    files = installation.get('files') or {}
    return bool(installation.get('ready') or all(
        files.get(name, {}).get('payload_match')
        for name in ('d3d11.dll', 'NBA2K_Hook.dll')))


def hook_setup_guidance(installation, process=None):
    """Describe the next setup action without claiming optional patches ran."""
    if _hook_unsupported(installation, process):
        return ('The bundled hook does not support this executable build. Installing its '
                'dependencies or restarting will not fix that mismatch. A compatible hook '
                'is required. The app checks live roster access separately.')
    if process and process.get('hook_verified'):
        return ('The verified hook DLL is loaded. Saved settings take effect at the next '
                'game launch; use the startup diagnostics to check which features applied.')
    dependencies = installation.get('dependencies') or {}
    missing = [name for name, info in dependencies.items() if not info.get('valid_x64_dll')]
    if missing:
        return ('First install the required mod loader from the download below: '
                + ', '.join(missing) + '. Place its files beside NBA2K17.exe. '
                'Then close NBA 2K17, install the bundled hook, and start the game again.')
    if installation.get('ready'):
        if process:
            return ('Hook files are installed. Exit NBA 2K17 normally and start it again '
                    'to load the hook. Refresh now checks the connection; it cannot '
                    'attach the hook to an already running game.')
        return 'Hook files are installed. Start NBA 2K17 from its game folder to load the hook.'
    if installation.get('installable'):
        return ('Close NBA 2K17, select Install bundled hook, then start the game again.'
                if process else 'Select Install bundled hook, then start NBA 2K17.')
    if installation:
        return 'Resolve the setup issues listed above before installing the bundled hook.'
    return 'Choose the game installation folder to check the hook and its required mod loader.'


def collect_snapshot(manager, game_dir, roster_file, *, synchronize=True):
    """Keep discovery separate from roster writes and feature-activation claims."""
    probe = game_hook.probe_processes()
    processes = probe['processes']
    if not game_dir and len(processes) == 1 and processes[0].get('game_dir'):
        game_dir = processes[0]['game_dir']
    matches = [p for p in processes if game_dir and p.get('game_dir')
               and Path(p['game_dir']).resolve() == Path(game_dir).resolve()]
    process = matches[0] if len(matches) == 1 else None
    inspection, inspection_error = None, None
    if game_dir:
        try:
            inspection = game_hook.inspect_installation(game_dir)
        except (OSError, ValueError) as exc:
            inspection_error = str(exc)
    error = probe.get('error')
    if not error and (len(matches) > 1 or (not game_dir and len(processes) > 1)):
        error = 'Multiple game processes found. Close the extra game instance before connecting.'
    if not error and processes and not matches:
        error = 'The running game does not match the selected game folder.'
    if process and process.get('error'):
        error = process['error']
    if error:
        # Do not use incomplete process discovery to clear or rebind a session.
        live = {'state': 'error', 'message': error}
    elif synchronize:
        live = sync_roster(manager, roster_file, process)
    else:
        live = {'state': 'paused', 'message': 'Automatic roster refresh is paused.'}
    return {'game_dir': str(game_dir or ''), 'process': process, 'probe': probe,
            'installation': inspection, 'installation_error': inspection_error, 'sync': live}


class _Signals(QObject):
    done = Signal(object)
    error = Signal(str)


class _Job(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function, self.signals = function, _Signals()

    def run(self):
        try:
            self.signals.done.emit(self.function())
        except Exception as exc:
            self.signals.error.emit(str(exc))


class GameConnectionController(QObject):
    snapshot_changed = Signal(object)
    activity_changed = Signal(bool)
    operation_finished = Signal(str, bool)
    idle = Signal()

    def __init__(self, manager, parent=None, *, settings=None, autostart=True):
        super().__init__(parent)
        self.manager = manager
        self.settings = settings or QSettings('NBA2K17MyTEAM', 'GameConnection')
        self.game_dir = str(self.settings.value('game_directory', ''))
        self.roster_file = str(self.settings.value('roster_file', ''))
        self.auto_refresh = str(self.settings.value('auto_refresh', 'true')).lower() in ('true', '1')
        self.snapshot = {}
        self.active, self.closing, self.job = False, False, None
        self._generation = 0
        self._retry_after = 0.0
        self._followup = False
        self._manual_followup = False
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self._tick)
        if autostart and os.environ.get('NBA2K17_DISABLE_GAME_MONITOR') != '1':
            self.timer.start()
            QTimer.singleShot(1500, self._tick)

    def _tick(self):
        if monotonic() >= self._retry_after:
            self.refresh()

    def set_game_directory(self, value):
        self.game_dir = str(value)
        self.settings.setValue('game_directory', self.game_dir)
        self._generation += 1
        self.snapshot = {}
        self.refresh(force=True)

    def set_roster_path(self, value):
        value = str(value or '')
        if value == self.roster_file:
            return
        self.roster_file = value
        self.settings.setValue('roster_file', value)
        self._generation += 1
        self.refresh(force=True)

    def set_auto_refresh(self, enabled):
        self.auto_refresh = bool(enabled)
        self.settings.setValue('auto_refresh', self.auto_refresh)
        self._generation += 1
        self.refresh(force=True)

    def _start(self, function, completed):
        if self.active or self.closing:
            return False
        self.active = True
        self.activity_changed.emit(True)
        self.job = _Job(function)
        self._completed = completed
        self.job.signals.done.connect(self._done, Qt.ConnectionType.QueuedConnection)
        self.job.signals.error.connect(self._error, Qt.ConnectionType.QueuedConnection)
        QThreadPool.globalInstance().start(self.job)
        return True

    def refresh(self, *, force=False, refresh_roster=False):
        if self.closing:
            return
        if self.active or self.manager.busy:
            self._followup = self._followup or force
            self._manual_followup = self._manual_followup or refresh_roster
            return
        generation = self._generation
        game_dir, roster_file = self.game_dir, self.roster_file
        synchronize = self.auto_refresh or refresh_roster or self._manual_followup
        self._followup = self._manual_followup = False

        def completed(snapshot):
            if generation != self._generation:
                self._followup = True
                return
            if not self.game_dir and snapshot['game_dir']:
                self.game_dir = snapshot['game_dir']
                self.settings.setValue('game_directory', self.game_dir)
            self.snapshot = snapshot
            state = snapshot['sync']['state']
            self._retry_after = monotonic() + (15 if state == 'error' else 0)
            self.snapshot_changed.emit(snapshot)

        self._start(lambda: collect_snapshot(self.manager, game_dir, roster_file,
                                             synchronize=synchronize), completed)

    def install(self):
        directory = self.game_dir
        return self._operation(lambda: game_hook.install_hook(directory),
                               'Hook installed. Start NBA 2K17 to load it.')

    def save_settings(self, values, expected_sha256):
        directory = self.game_dir
        return self._operation(lambda: game_hook.save_options(directory, values,
                               expected_sha256=expected_sha256),
                               'Settings saved. Restart NBA 2K17 for them to take effect.')

    def _operation(self, function, message):
        if self.manager.busy:
            return False
        def completed(result):
            self.operation_finished.emit(message, True)
            self._followup = True
        return self._start(function, completed)

    @Slot(object)
    def _done(self, value):
        try:
            if not self.closing:
                self._completed(value)
        finally:
            self._finish()

    @Slot(str)
    def _error(self, message):
        self._retry_after = monotonic() + 15
        if not self.closing:
            self.operation_finished.emit(message, False)
        self._finish()

    def _finish(self):
        self.active, self.job = False, None
        self._completed = None
        self.activity_changed.emit(False)
        self.idle.emit()
        if self._followup and not self.closing:
            self._followup = False
            QTimer.singleShot(0, lambda: self.refresh(force=True))

    def shutdown(self):
        self.closing = True
        self.timer.stop()
        if self.active:
            self.manager.cancel()
        return not self.active


def _label(value, style=None):
    label = QLabel(value)
    label.setWordWrap(True)
    if style:
        label.setObjectName(style)
    return label


class GameConnectionPage(QWidget):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller = controller
        self.dirty, self.config_hash, self.config_directory = False, None, None
        self.scan, self._shown_slots = None, None
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 16, 24, 16)
        header = QHBoxLayout()
        header.addWidget(_label('Game Connection', 'detailName'), 1)
        self.refresh_button = QPushButton('Refresh now')
        self.refresh_button.clicked.connect(lambda: controller.refresh(force=True, refresh_roster=True))
        header.addWidget(self.refresh_button)
        root.addLayout(header)
        root.addWidget(_label('Connect to NBA 2K17, manage the optional hook, and keep live roster slots up to date.', 'muted'))
        self.status = _label('Checking game connection…', 'notice')
        root.addWidget(self.status)
        self.auto = QCheckBox('Automatically refresh the selected live roster · read only')
        self.auto.setToolTip('Reads live roster records while the game is running. It does not load or attach the optional hook DLL.')
        self.auto.setChecked(controller.auto_refresh)
        self.auto.toggled.connect(controller.set_auto_refresh)
        root.addWidget(self.auto)
        self.connection_help = _label('Live roster refresh works separately from the hook. '
            'The hook loads when NBA 2K17 starts; this app does not attach it to a running game.', 'muted')
        root.addWidget(self.connection_help)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        body = QVBoxLayout(content)
        body.setContentsMargins(0, 4, 12, 4)
        body.setSpacing(16)
        scroll.setWidget(content)
        root.addWidget(scroll, 1)

        setup = QGroupBox('Hook installation')
        setup_layout = QVBoxLayout(setup)
        row = QHBoxLayout()
        self.directory = QLineEdit(controller.game_dir)
        self.directory.setReadOnly(True)
        self.directory.setPlaceholderText('Select the folder containing NBA2K17.exe')
        self.directory.setAccessibleName('NBA 2K17 game folder')
        row.addWidget(self.directory, 1)
        self.browse_game = QPushButton('Choose game folder…')
        self.browse_game.clicked.connect(self.choose_game)
        row.addWidget(self.browse_game)
        setup_layout.addLayout(row)
        self.files_status = _label('Choose the game folder or start NBA 2K17 for automatic detection.')
        setup_layout.addWidget(self.files_status)
        self.setup_guidance = _label('', 'notice')
        setup_layout.addWidget(self.setup_guidance)
        row = QHBoxLayout()
        self.install_button = QPushButton('Install bundled hook')
        self.install_button.clicked.connect(controller.install)
        row.addWidget(self.install_button)
        self.readme_button = QPushButton('Hook documentation')
        self.readme_button.clicked.connect(self.open_readme)
        row.addWidget(self.readme_button)
        self.dependencies_button = QPushButton('Get required mod loader…')
        self.dependencies_button.setToolTip('Open the dependency download linked by the hook author. Requires 2K17WAIGUADLL.dll and dinput8.dll.')
        self.dependencies_button.clicked.connect(self.open_dependencies)
        row.addWidget(self.dependencies_button)
        row.addStretch()
        setup_layout.addLayout(row)
        setup_layout.addWidget(_label('Hook by Spree#8. Requires 2K17WAIGUADLL.dll and its companion dinput8.dll in the game folder. These dependencies are not included. Existing conflicting DLLs are preserved.', 'muted'))
        body.addWidget(setup)

        settings = QGroupBox('Hook settings · next game launch')
        settings_layout = QVBoxLayout(settings)
        self.options = {}
        for key, spec in game_hook.HOOK_OPTIONS.items():
            check = QCheckBox(spec['title'])
            check.setToolTip(spec['description'])
            check.toggled.connect(self.mark_dirty)
            settings_layout.addWidget(check)
            self.options[key] = check
        settings_layout.addWidget(_label('Settings are read when the game starts. Restart the game after saving. A loaded DLL does not confirm that every optional feature applied successfully.', 'muted'))
        row = QHBoxLayout()
        self.save_button = QPushButton('Save hook settings')
        self.save_button.clicked.connect(self.save)
        row.addWidget(self.save_button)
        self.reset_button = QPushButton('Reload saved settings')
        self.reset_button.clicked.connect(self.reload_settings)
        row.addWidget(self.reset_button)
        row.addStretch()
        settings_layout.addLayout(row)
        self.operation_status = _label('', 'muted')
        settings_layout.addWidget(self.operation_status)
        body.addWidget(settings)

        roster = QGroupBox('Live roster · read only')
        roster_layout = QVBoxLayout(roster)
        roster_layout.addWidget(_label('Select the same roster file that is open in offline Roster Creator. Selecting a file here does not load it in the game. This refresh reads player records; it does not apply cards or overwrite your card database.', 'muted'))
        row = QHBoxLayout()
        self.roster_path = QLineEdit(controller.roster_file)
        self.roster_path.setReadOnly(True)
        self.roster_path.setPlaceholderText('Select the currently loaded roster file')
        row.addWidget(self.roster_path, 1)
        self.browse_roster = QPushButton('Choose loaded roster…')
        self.browse_roster.clicked.connect(self.choose_roster)
        row.addWidget(self.browse_roster)
        roster_layout.addLayout(row)
        self.roster_status = _label('Waiting for a roster selection.')
        roster_layout.addWidget(self.roster_status)
        self.teams = QComboBox()
        self.teams.setAccessibleName('Live roster team')
        self.teams.currentIndexChanged.connect(self.show_slots)
        roster_layout.addWidget(self.teams)
        self.players = QTableWidget(0, 3)
        self.players.setHorizontalHeaderLabels(['Roster slot', 'Player', 'Position'])
        self.players.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.players.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.players.setMinimumHeight(230)
        roster_layout.addWidget(self.players)
        body.addWidget(roster)

        diagnostics = QGroupBox('Hook diagnostics')
        diagnostics_layout = QVBoxLayout(diagnostics)
        diagnostics_layout.addWidget(_label('The log may contain older game sessions. Enable Write hook diagnostics and restart the game to record a fresh startup. Launch the game from its installation folder so the hook can find its configuration.', 'muted'))
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(150)
        diagnostics_layout.addWidget(self.log)
        body.addWidget(diagnostics)
        controller.snapshot_changed.connect(self.apply_snapshot)
        controller.activity_changed.connect(self.update_controls)
        controller.operation_finished.connect(self.operation_finished)
        self.apply_snapshot(controller.snapshot)

    def choose_game(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose NBA 2K17 installation', self.controller.game_dir)
        if path:
            self.dirty, self.config_hash = False, None
            self.controller.set_game_directory(path)
            self.directory.setText(path)

    def choose_roster(self):
        from .roster_injection import default_roster_directory
        path, _ = QFileDialog.getOpenFileName(self, 'Choose the roster currently loaded in Roster Creator',
                    self.controller.roster_file or str(default_roster_directory()), 'Roster files (roster*);;All files (*)')
        if path:
            self.controller.set_roster_path(path)
            self.roster_path.setText(path)

    def open_readme(self):
        payload = game_hook.hook_payload_dir()
        if payload:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(payload / 'readme.txt')))

    def open_dependencies(self):
        QDesktopServices.openUrl(QUrl(HOOK_DEPENDENCY_URL))

    def mark_dirty(self, *_):
        self.dirty = True
        self.operation_status.setText('Unsaved settings. Save to apply them at the next game launch.')
        self.update_controls()

    def save(self):
        self.controller.save_settings({key: check.isChecked() for key, check in self.options.items()}, self.config_hash)

    def reload_settings(self):
        self.dirty = False
        self.apply_snapshot(self.controller.snapshot)
        self.controller.refresh(force=True)

    def operation_finished(self, message, success):
        self.operation_status.setText(message)
        if success:
            self.dirty = False
            self.config_hash = None

    def apply_snapshot(self, snapshot):
        self.directory.setText(self.controller.game_dir)
        self.roster_path.setText(self.controller.roster_file)
        process = snapshot.get('process')
        installation = snapshot.get('installation') or {}
        live = snapshot.get('sync') or {}
        if process:
            if _hook_unsupported(installation, process):
                hook = 'Bundled hook does not support this game build'
            elif process.get('hook_verified'):
                hook = 'Verified hook DLL loaded'
            elif process.get('hook_loaded'):
                hook = 'Unrecognized hook DLL loaded'
            elif installation.get('ready'):
                hook = 'Restart NBA 2K17 to load the hook'
            elif not _hook_installed(installation):
                hook = 'Hook not installed'
            else:
                hook = 'Hook not loaded · setup needs attention'
            self.status.setText(f"NBA 2K17 running · PID {process['pid']} · {hook}")
        else:
            self.status.setText(live.get('message', 'Choose the game folder or start NBA 2K17.'))
        if installation:
            files = ('Bundled hook incompatible with this game build.' if _hook_unsupported(installation, process)
                     else 'Hook files ready on disk.' if installation.get('ready') else 'Hook setup incomplete.')
            issues = installation.get('issues') or []
            if not issues and not installation.get('ready'):
                issues = ['Install the bundled hook while NBA 2K17 is closed.']
            self.files_status.setText('\n'.join([files, *issues]))
        else:
            self.files_status.setText(snapshot.get('installation_error') or 'Choose the game installation folder to check its hook files.')
        self.setup_guidance.setText(hook_setup_guidance(installation, process))
        config = installation.get('config') or {}
        if self.config_directory != self.controller.game_dir:
            self.dirty = False
            self.config_directory = self.controller.game_dir
        if not self.dirty:
            self.config_hash = config.get('sha256')
            for key, check in self.options.items():
                check.blockSignals(True)
                check.setChecked(config.get('values', {}).get(key) is True)
                check.blockSignals(False)
        self.roster_status.setText(live.get('message', 'Select the currently loaded roster.'))
        if live.get('state') == 'ready':
            self.scan = live['scan']
            entries = [(t.get('label', str(t['key'])), t['key']) for t in self.scan.get('teams', [])]
            existing = [(self.teams.itemText(i), self.teams.itemData(i)) for i in range(self.teams.count())]
            if entries != existing:
                key = self.teams.currentData()
                self.teams.blockSignals(True)
                self.teams.clear()
                for label, value in entries:
                    self.teams.addItem(label, value)
                selected = self.teams.findData(key)
                if selected >= 0:
                    self.teams.setCurrentIndex(selected)
                self.teams.blockSignals(False)
            self.show_slots()
        elif live.get('state') not in ('busy', 'paused'):
            self.scan = None
            self._shown_slots = None
            self.teams.clear()
            self.players.setRowCount(0)
        log = installation.get('log') or {}
        self.log.setPlainText(log.get('text') or log.get('error') or 'No hook log found in the game folder. Diagnostics are off by default.')
        self.update_controls()

    def show_slots(self, *_):
        teams = (self.scan or {}).get('teams', [])
        team = next((t for t in teams if t['key'] == self.teams.currentData()), {})
        slots = team.get('slots', [])
        shown = (team.get('key'), slots)
        if self._shown_slots == shown:
            return
        row_index = self.players.currentRow()
        scroll = self.players.verticalScrollBar().value()
        self._shown_slots = shown
        self.players.setRowCount(len(slots))
        for row, slot in enumerate(slots):
            for column, value in enumerate((str(slot.get('slot', row) + 1), slot.get('name', ''), slot.get('position', ''))):
                self.players.setItem(row, column, QTableWidgetItem(str(value)))
        if 0 <= row_index < len(slots):
            self.players.selectRow(row_index)
        self.players.verticalScrollBar().setValue(scroll)

    def update_controls(self, *_):
        idle = not self.controller.active and not self.controller.manager.busy
        installation = self.controller.snapshot.get('installation') or {}
        config = installation.get('config') or {}
        valid_config = config.get('exists') and not config.get('errors')
        self.refresh_button.setEnabled(idle)
        self.browse_game.setEnabled(idle)
        self.browse_roster.setEnabled(idle)
        self.install_button.setEnabled(bool(idle and installation.get('installable') and not installation.get('ready')
                                           and not _hook_unsupported(installation, self.controller.snapshot.get('process'))
                                           and not self.controller.snapshot.get('probe', {}).get('processes')))
        self.install_button.setToolTip(hook_setup_guidance(installation, self.controller.snapshot.get('process')))
        self.save_button.setEnabled(bool(idle and valid_config and self.dirty and self.config_hash))
        self.reset_button.setEnabled(bool(idle and valid_config))
        for check in self.options.values():
            check.setEnabled(bool(idle and valid_config))
        self.readme_button.setEnabled(game_hook.hook_payload_dir() is not None)
