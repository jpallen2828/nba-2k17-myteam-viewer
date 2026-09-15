"""Native injection controls shared by all three team-building modes."""

from pathlib import Path

from hashlib import sha256

from copy import deepcopy

import json

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QSettings, Signal, Slot, Qt

from PySide6.QtWidgets import (QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,

    QLineEdit, QComboBox, QFileDialog, QTableWidget, QTableWidgetItem, QHeaderView, QApplication)

from .roster_injection import RosterSession, find_roster_files, default_roster_directory





class JobSignals(QObject):

    done = Signal(object)

    error = Signal(str)

    progress = Signal(str)





class InjectionJob(QRunnable):

    def __init__(self, function):

        super().__init__()

        self.function = function

        self.signals = JobSignals()



    def run(self):

        try:

            self.signals.done.emit(self.function(self.signals.progress.emit))

        except Exception as exc:

            self.signals.error.emit(str(exc))





class InjectionPanel(QFrame):

    def __init__(self, owner, lineup):

        super().__init__()

        self.owner = owner

        from .injection_session import SessionManager

        if not hasattr(owner, 'injection_manager'):

            owner.injection_manager = SessionManager()

            QApplication.instance().aboutToQuit.connect(owner.injection_manager.shutdown)

        self.manager = owner.injection_manager

        self.released = False

        self.profiles = deepcopy([owner.repository.cards[row['id']]['injection'] for _, row in lineup])

        self.busy = False

        self.prepared = False

        self.job = None

        self.selected_teams = self.manager.selected_teams

        lineup_key = sha256(json.dumps(self.profiles, sort_keys=True).encode()).hexdigest()

        self.selected_slots = self.manager.lineup_slots.setdefault(lineup_key, {})

        self.manual_slots = self.manager.manual_slots.setdefault(lineup_key, set())

        self.settings = QSettings('NBA2K17MyTEAM', 'RosterInjection')

        self.setObjectName('content')

        layout = QVBoxLayout(self)

        layout.setContentsMargins(24, 20, 24, 20)

        top = QHBoxLayout()

        title = QLabel('Inject team into NBA 2K17'); title.setObjectName('detailName')

        top.addWidget(title); top.addStretch()

        self.close_button = QPushButton('Back to lineup')

        self.close_button.clicked.connect(owner.close_overlay); top.addWidget(self.close_button)

        layout.addLayout(top)

        hint = QLabel('Choose the roster file already loaded in offline Roster Creator, scan its live slots, then select a destination team. Changing this file selection does not load a roster in the game.')

        hint.setWordWrap(True); layout.addWidget(hint)

        folder = QHBoxLayout()

        folder.addWidget(QLabel('Roster folder'))

        self.directory = QLineEdit(str(self.settings.value('directory', str(default_roster_directory()))))

        self.directory.setAccessibleName('Roster search folder'); folder.addWidget(self.directory, 1)

        self.browse = QPushButton('Browse…'); self.browse.clicked.connect(self.choose_folder); folder.addWidget(self.browse)

        self.find_button = QPushButton('Find rosters'); self.find_button.clicked.connect(self.find_files); folder.addWidget(self.find_button)

        layout.addLayout(folder)

        controls = QHBoxLayout()

        controls.addWidget(QLabel('Loaded roster'))

        self.roster = QComboBox(); self.roster.setAccessibleName('Loaded roster file'); controls.addWidget(self.roster, 1)

        self.scan_button = QPushButton('Scan live roster'); self.scan_button.clicked.connect(self.scan); controls.addWidget(self.scan_button)

        controls.addWidget(QLabel('Teams'))

        self.kind = QComboBox(); self.kind.addItem('Current NBA', 'CURRENT'); self.kind.addItem('Historic NBA', 'HISTORIC')

        self.kind.setCurrentIndex(self.kind.findData(getattr(self.manager, 'selected_kind', 'CURRENT')))

        self.kind.currentIndexChanged.connect(self.filter_teams); controls.addWidget(self.kind)

        self.team = QComboBox(); self.team.setMinimumWidth(270); self.team.setAccessibleName('Injection destination team')

        self.team.currentIndexChanged.connect(self.preview_slots); controls.addWidget(self.team, 1)

        layout.addLayout(controls)

        self.scan_note = QLabel('Scan reads the roster currently open in the game. It does not change game data.')

        self.scan_note.setWordWrap(True); layout.addWidget(self.scan_note)

        self.roster.currentIndexChanged.connect(self.invalidate)

        self.directory.textEdited.connect(self.directory_edited)

        self.table = QTableWidget(0, 4)

        self.table.setHorizontalHeaderLabels(['Incoming MyTEAM card', 'Live slot', 'Current player', 'Transfer'])

        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)

        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)

        layout.addWidget(self.table, 1)

        self.status = QLabel('Game not attached. Select the loaded roster and scan to resolve current team slots.')

        self.status.setWordWrap(True); self.status.setObjectName('notice'); layout.addWidget(self.status)

        self.transfer_note = QLabel('Transfers supported fields, shoes and accessories; unresolved player flags remain unchanged.')
        self.transfer_note.setWordWrap(True)

        self.transfer_note.setToolTip('Every unsupported field is included in the report.')

        layout.addWidget(self.transfer_note)

        actions = QHBoxLayout()

        self.prepare_button = QPushButton('Preview transfer'); self.prepare_button.clicked.connect(self.prepare); actions.addWidget(self.prepare_button)

        self.prepare_button.setToolTip('Optional preview. Inject checks the lineup automatically before writing.')

        self.cancel_button = QPushButton('Cancel scan')

        self.cancel_button.clicked.connect(self.manager.cancel)

        actions.addWidget(self.cancel_button)

        actions.addStretch()

        self.undo_button = QPushButton('Undo last injection'); self.undo_button.clicked.connect(self.undo); self.undo_button.setEnabled(False); actions.addWidget(self.undo_button)

        self.inject_button = QPushButton('Inject'); self.inject_button.setObjectName('primary'); self.inject_button.clicked.connect(self.inject); actions.addWidget(self.inject_button)

        layout.addLayout(actions)

        self.find_files()

        if self.session is not None and not self.manager.busy:

            def reuse(progress):

                self.session.progress = progress

                self.session.begin_operation()

                self.session.validate()

                self.session.refresh_mutable_records()

                return self.session

            self.start(reuse, lambda _: self.filter_teams())



    @property

    def session(self):

        return self.manager.session



    @session.setter

    def session(self, value):

        self.manager.session = value



    def release(self):

        self.released = True

        self.manager.cancel()



    def invalidate(self, *_):

        if not self.manager.busy: self.manager.discard()

        self.prepared = False; self.team.clear()

        self.undo_button.setEnabled(False)

        self.status.setText('Ready to scan. Keep the selected roster open in NBA 2K17 and click Scan live roster.')

        self.update_controls()



    def choose_folder(self):

        chosen = QFileDialog.getExistingDirectory(self, 'Choose NBA 2K17 roster folder', self.directory.text())

        if chosen:

            self.directory.setText(chosen); self.invalidate(); self.find_files()



    def directory_edited(self, *_):

        self.roster.blockSignals(True); self.roster.clear(); self.roster.blockSignals(False)

        self.invalidate()

        self.status.setText('Click Find rosters to list files in the new folder.')



    def find_files(self):

        previous = self.roster.currentData()

        try:

            files = find_roster_files(self.directory.text())

        except (OSError, ValueError) as exc:

            self.invalidate()

            self.roster.clear()

            self.status.setText(str(exc)); self.update_controls(); return

        self.roster.blockSignals(True); self.roster.clear()

        for p in files:

            self.roster.addItem(p.name, str(p)); self.roster.setItemData(self.roster.count() - 1, str(p), Qt.ItemDataRole.ToolTipRole)

        saved = previous or str(getattr(self.session, 'roster_file', '')) or self.settings.value('roster', '')

        index = self.roster.findData(saved)

        if index >= 0:

            self.roster.setCurrentIndex(index)

        self.roster.blockSignals(False)

        if (previous is not None and previous != self.roster.currentData()) or self.session is None:

            self.invalidate()

        elif getattr(self.session, 'roster_file', None) is not None and str(self.session.roster_file) != self.roster.currentData():

            self.invalidate()

        self.settings.setValue('directory', self.directory.text())

        if not files:

            self.status.setText('No roster0001-style files found in this folder.')

        if self.session is None:

            self.preview_slots()

        self.update_controls()



    def start(self, function, complete, *, writing=False):

        if self.busy or self.released or not self.manager.reserve(writing=writing):

            return

        self.busy = True; self.update_controls()

        self.job = InjectionJob(lambda progress: self.manager.run(function, progress))

        self.completion = complete

        self.job.signals.progress.connect(self.progressed, Qt.ConnectionType.QueuedConnection)

        self.job.signals.done.connect(self.finished, Qt.ConnectionType.QueuedConnection)

        self.job.signals.error.connect(self.failed, Qt.ConnectionType.QueuedConnection)

        QThreadPool.globalInstance().start(self.job)



    @Slot(str)

    def progressed(self, message):

        if not self.released:

            self.status.setText(message)

            self.update_controls()



    @Slot(object)

    def finished(self, value):

        self.busy = False

        if self.released or self.manager.closing: return

        try:

            self.completion(value)

        except Exception as exc:

            self.failed(str(exc))

        self.update_controls()



    @Slot(str)

    def failed(self, message):

        self.busy = False; self.prepared = False

        if self.released or self.manager.closing: return

        if self.session is not None and hasattr(self.session, 'save_failure'):

            try:

                self.status.setToolTip('Diagnostic report: ' + self.session.save_failure(message))

            except OSError:

                pass

        if self.session is not None and not getattr(self.session, 'valid', True):

            self.manager.discard()

            self.filter_teams()

        elif self.session is None: self.filter_teams()

        self.status.setText('Stopped: ' + message)

        self.update_controls()



    def scan(self):

        path = self.roster.currentData()

        if self.busy or self.manager.busy or not path:

            return

        self.prepared = False

        self.filter_teams()

        self.status.setText('Scanning the roster currently loaded in NBA 2K17…')

        def run(progress):

            return self.manager.replace(path, RosterSession, progress)

        def done(session):

            self.session = session

            self.settings.setValue('roster', path)

            self.filter_teams()

            message = self.status.text() if self.team.currentData() else 'The previous team is unavailable. Select a destination team.'

            self.status.setText(f"Resolved {len(session.public_scan()['teams'])} NBA teams and {len(session.records):,} player records. {message}")

        self.start(run, done)



    def filter_teams(self):

        self.manager.selected_kind = self.kind.currentData()

        self.team.blockSignals(True); self.team.clear()

        if self.session:

            teams = [t for t in self.session.teams if t['enabled'] and t['kind'] == self.kind.currentData()]

            if self.kind.currentData() == 'CURRENT':

                teams.sort(key=lambda t: (t['nickname'].casefold(), t['label'].casefold()))

            for t in teams:

                self.team.addItem(f"{t['label']} · {len(t['slots'])} slots", t['key'])

            remembered = self.selected_teams.get(self.kind.currentData())

            if remembered is not None:

                self.team.setCurrentIndex(self.team.findData(remembered))

        self.team.blockSignals(False); self.preview_slots()



    def preview_slots(self):

        self.prepared = False

        self.slot_choices = []

        team = next((t for t in self.session.teams if t['key'] == self.team.currentData()), None) if self.session else None

        if team:

            self.selected_teams[self.kind.currentData()] = team['key']

        remembered_slots = self.selected_slots.get(team['key']) if team else None

        if team and team['key'] not in self.manual_slots and hasattr(self.session, 'position_matched_slots') and len(self.profiles) <= len(team['slots']):

            remembered_slots = [s['slot'] for s in self.session.position_matched_slots(self.profiles, team)]

        self.table.setRowCount(len(self.profiles))

        for i, profile in enumerate(self.profiles):

            identity = profile['source_card_identity']

            slot = team['slots'][i] if team and i < len(team['slots']) else None

            cells = [f"{identity['name']} · {identity.get('overall', '?')} {identity.get('tier', '')}",

                     str(slot['slot'] + 1) if slot else 'Unavailable', slot['name'] if slot else '—', 'Not checked']

            for col, value in enumerate(cells):

                self.table.setItem(i, col, QTableWidgetItem(value))

            choice = QComboBox()

            choice.setAccessibleName(f'Destination slot for card {i + 1}')

            if team:

                for live in team['slots']:

                    choice.addItem(f"{live['slot'] + 1} · {live['name']}", live['slot'])

                if remembered_slots is not None and i < len(remembered_slots):

                    choice.setCurrentIndex(choice.findData(remembered_slots[i]))

                else:

                    choice.setCurrentIndex(i if i < len(team['slots']) else -1)

                slot = next((s for s in team['slots'] if s['slot'] == choice.currentData()), None)

                self.table.setItem(i, 2, QTableWidgetItem(slot['name'] if slot else 'Unavailable'))

            choice.currentIndexChanged.connect(lambda _index, row=i: self.slot_changed(row))

            self.table.setCellWidget(i, 1, choice)

            self.slot_choices.append(choice)

        if team:

            self.selected_slots[team['key']] = [c.currentData() for c in self.slot_choices]

        self.update_controls()

        self.readiness()



    def readiness(self):

        if self.session is None or self.busy or self.manager.busy or not self.team.currentData(): return

        if not hasattr(self.session, 'preflight'): return

        report = self.session.preflight(self.profiles, self.team.currentData(), slots=[c.currentData() for c in self.slot_choices], live=False)

        self.status.setText(report['message'])



    def slot_changed(self, row):

        self.prepared = False

        team = next((t for t in self.session.teams if t['key'] == self.team.currentData()), None) if self.session else None

        slot = next((s for s in team['slots'] if s['slot'] == self.slot_choices[row].currentData()), None) if team else None

        self.table.setItem(row, 2, QTableWidgetItem(slot['name'] if slot else 'Unavailable'))

        self.table.setItem(row, 3, QTableWidgetItem('Not checked'))

        if team:

            self.manual_slots.add(team['key'])

            self.selected_slots[team['key']] = [c.currentData() for c in self.slot_choices]

        self.update_controls()

        self.readiness()



    def prepare(self):

        if not self.can_inject():

            return

        session = self.session

        key = self.team.currentData()

        slots = [c.currentData() for c in self.slot_choices]

        def run(progress):

            session.progress = progress

            session.begin_operation()

            return session.prepare(self.profiles, key, slots=slots)

        def done(report):

            self.prepared = True

            self.show_transfer(report)

            self.status.setText('Ready to inject: selected lineup and destination storage checked. ' + ' '.join(report['remaining_limits']))

        self.start(run, done)



    def show_transfer(self, report):

        for i, p in enumerate(report['players']):

            self.table.setItem(i, 2, QTableWidgetItem(p['destination']['name']))

            suffix = '; mod assets required' if p['requires_mod_assets'] else ''

            origin = 'height; origin retained' if p.get('college',{}).get('resolution') == 'PRESERVED_DESTINATION' else 'height/origin'

            self.table.setItem(i, 3, QTableWidgetItem(f"{len(p['fields'])} mapped fields + {origin}; gear unsupported{suffix}"))



    def can_inject(self):

        return not self.busy and not self.manager.busy and self.session is not None and bool(self.team.currentData()) and bool(self.profiles)



    def inject(self):

        if not self.can_inject():

            return

        session = self.session

        key = self.team.currentData()

        slots = [c.currentData() for c in self.slot_choices]

        self.prepared = False

        self.status.setText('Checking the selected lineup and live destination before injection…')

        def done(value):

            report, result = value

            self.show_transfer(report)

            self.prepared = False; self.undo_button.setEnabled(True)

            seconds = result.get('performance', {}).get('elapsed_seconds')

            elapsed = f' in {seconds:.2f}s' if seconds is not None else ''

            self.status.setText(f"Injected {result['cards_written']} card(s){elapsed}; {result['field_readback_count']:,} mapped fields matched readback. Gear and unresolved flags were retained. Inspect in-game, then save a separate test roster. Report: {result['report_directory']}")

        def run(progress):

            session.progress = progress

            session.begin_operation()

            progress('Checking ownership for the selected team...')

            session.diagnose_ownership(key, self.profiles, slots=slots)

            if self.manager.cancel_event.is_set():

                raise ValueError('Injection cancelled before any game data was written.')

            report = session.prepare(self.profiles, key, slots=slots)

            self.manager.begin_write()

            progress('Injecting the verified team...')

            return report, session.write()

        self.start(run, done)



    def undo(self):

        def done(result):

            self.undo_button.setEnabled(False); self.prepared = False

            self.status.setText('The complete watched baseline was restored. Ready for another injection.')

        def run(progress):

            self.session.progress = progress

            return self.session.undo()

        self.start(run, done, writing=True)



    def update_controls(self, *_):

        busy = self.busy or self.manager.busy

        self.table.setEnabled(not busy)

        for control in (self.directory, self.browse, self.find_button, self.roster, self.kind, self.team, self.close_button):

            control.setEnabled(not busy)

        self.scan_button.setEnabled(not busy and bool(self.roster.currentData()))

        self.scan_button.setText('Scanning…' if self.busy and self.session is None else 'Scan live roster')

        if hasattr(self, 'prepare_button'):

            self.cancel_button.setEnabled(self.busy and not self.manager.writing)

            self.prepare_button.setEnabled(self.can_inject())

            self.inject_button.setEnabled(self.can_inject())

            self.undo_button.setEnabled(not busy and self.session is not None and bool(getattr(self.session, 'last_run', None)))

