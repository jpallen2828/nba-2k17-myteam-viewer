"""Native team workspaces embedded in the archive window."""
from datetime import datetime
from pathlib import Path
from PySide6.QtCore import Qt, QRect, QSize
from PySide6.QtGui import QPixmap, QPainter, QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (QWidget, QFrame, QLabel, QPushButton, QVBoxLayout,
    QHBoxLayout, QGridLayout, QComboBox, QLineEdit, QCheckBox, QScrollArea,
    QFileDialog, QMessageBox, QSizePolicy, QButtonGroup)
from .teams import TeamSession, POSITIONS, DRAFT_KEYS, POSITION_KEYS, draft_odds, bonus_odds, bonus_round_name, percentage_text
from .archive_repository import project_root
from .presentation import TIER_COLORS, BADGE_COLORS, section_values
from nba2k17_catalog import tier_rank

TITLES = {'custom': 'Create a Team', 'random': 'Random Team', 'draft': 'Draft a Team', 'blacktop': 'Blacktop'}

def slot_label(key):
    if key == 'bonus':
        return '13th player · Bonus round'
    if '|' in key:
        role, position = key.split('|')
        return f'{"Deep bench" if role == "bench" else role.title()} {position}'
    return key if key in POSITIONS else f'Player {key}'

def clear(layout):
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().hide()
            item.widget().deleteLater()

class DraftArt(QWidget):
    """Scale the full card into the available column without cropping."""
    def __init__(self, path):
        super().__init__()
        self.pixmap = QPixmap(path or '')
        self.setMinimumSize(110, 160)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def sizeHint(self):
        return QSize(250, 365)

    def paintEvent(self, event):
        if not self.pixmap.isNull():
            art = self.pixmap.scaled(self.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter = QPainter(self)
            painter.drawPixmap((self.width() - art.width()) // 2, (self.height() - art.height()) // 2, art)

class DraftRosterTile(QPushButton):
    """A clickable card with a fitted border and only its lineup role above it."""
    def __init__(self, row, title, primary, action):
        super().__init__()
        self.row, self.title, self.primary = row, title, primary
        self.pixmap = QPixmap(row.get('image_path') or '') if row else QPixmap()
        self.action_button = self
        self.setMinimumSize(90, 100)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.clicked.connect(action)
        description = f"{title} · {row['name']} · {row['tier']} · {row['overall']} OVR" if row else title + ' · ' + primary
        self.setAccessibleName(description)
        self.setToolTip(description + ('\n' + str(row.get('year') or '') + ' ' + (row.get('team') or '') + '\nClick to view card details' if row else ''))

    def sizeHint(self):
        return QSize(200, 315)

    def art_rect(self):
        size = QSize(650, 950).scaled(max(1, self.width() - 10), max(1, self.height() - 26), Qt.AspectRatioMode.KeepAspectRatio)
        return QRect((self.width() - size.width()) // 2, 22, size.width(), size.height())

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = self.art_rect()
        frame = QRect(rect.x() - 4, 0, rect.width() + 8, rect.height() + 26)
        painter.fillRect(frame, QColor('#0d151f'))
        painter.setPen(QColor('#60dce5' if self.hasFocus() or (self.underMouse() and self.isEnabled()) else '#243544'))
        painter.drawRect(frame.adjusted(0, 0, -1, -1))
        painter.setFont(QFont('Segoe UI', 9, QFont.Weight.Bold))
        painter.setPen(QColor('#60dce5' if self.isEnabled() else '#62768a'))
        painter.drawText(QRect(frame.x() + 3, 1, frame.width() - 6, 20), Qt.AlignmentFlag.AlignCenter, self.title)
        if not self.pixmap.isNull():
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(rect, self.pixmap)
        else:
            painter.setFont(QFont('Segoe UI', 10))
            painter.drawText(rect.adjusted(5, 5, -5, -5), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.primary)

class LineupPreview(QWidget):
    def __init__(self, lineup):
        super().__init__()
        self.rows = list(lineup)
        self.pixmaps = [QPixmap(row.get('image_path') or '') for _, row in self.rows]
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumHeight(32)
        self.setAccessibleName('Your lineup before the final pick: ' + ', '.join(row['name'] for _, row in self.rows))

    def heightForWidth(self, width):
        card_width = max(1, width // max(1, len(self.rows)) - 6)
        return max((round(card_width * p.height() / p.width()) + 4
                    for p in self.pixmaps if not p.isNull()), default=32)

    def sizeHint(self):
        return QSize(1200, self.heightForWidth(1200))

    def minimumSizeHint(self):
        return QSize(0, 32)

    def paintEvent(self, event):
        painter = QPainter(self)
        step = self.width() // max(1, len(self.rows))
        start = (self.width() - step * len(self.rows)) // 2
        for index, pixmap in enumerate(self.pixmaps):
            if pixmap.isNull():
                continue
            art = pixmap.scaled(max(1, step - 6), max(1, self.height() - 4), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(start + index * step + (step - art.width()) // 2, (self.height() - art.height()) // 2, art)

class CustomRosterTile(QFrame):
    def __init__(self, row, title, remove, inspect):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(3)
        self.card = DraftRosterTile(row, title, 'Empty slot', inspect or (lambda: None))
        self.card.setEnabled(row is not None)
        layout.addWidget(self.card, 1)
        self.action_button = QPushButton('Remove' if row else 'Empty')
        self.action_button.setStyleSheet('padding: 3px; font-size: 11px;')
        self.action_button.clicked.connect(remove)
        self.action_button.setEnabled(row is not None)
        layout.addWidget(self.action_button)

class DraftStatLabel(QLabel):
    """Keep draft statistics on one line, fitting even narrow choice panels."""
    def __init__(self, text, color, alignment=Qt.AlignmentFlag.AlignLeft):
        super().__init__(text)
        self.color = color
        self.pixel_size = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(alignment | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(34)
        self.fit_text()

    def fit_text(self):
        font = QFont(self.font())
        font.setBold(True)
        size = 26
        while size > 8:
            font.setPixelSize(size)
            metrics = QFontMetrics(font)
            if (metrics.horizontalAdvance(self.text()) <= self.contentsRect().width() - 4
                    and metrics.height() <= self.contentsRect().height() - 2):
                break
            size -= 1
        if size != self.pixel_size:
            self.pixel_size = size
            self.setStyleSheet(f'color: {self.color}; font-size: {size}px; font-weight: 700;')

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fit_text()


class DraftChoice(QFrame):
    def __init__(self, workspace, row, compact=False):
        super().__init__()
        self.setObjectName('group')
        self.setMinimumWidth(160)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(11, 11, 11, 11)
        if compact:
            layout.setSpacing(4)
        view = QPushButton('View badges && attributes')
        view.setStyleSheet('font-size: 11px; padding: 9px 3px;')
        view.clicked.connect(lambda: workspace.inspect(row))
        layout.addWidget(view)
        card = workspace.owner.repository.cards[row['id']]
        badges = section_values(card, 'badges')
        self.badge_counts = {}
        badge_grid = QGridLayout()
        badge_grid.setHorizontalSpacing(12)
        badge_grid.setVerticalSpacing(0)
        for index, (level, title) in enumerate((('HALL_OF_FAME', 'HOF'), ('GOLD', 'Gold'),
                                                ('SILVER', 'Silver'), ('BRONZE', 'Bronze'))):
            count = sum(value == level for value in badges.values())
            alignment = Qt.AlignmentFlag.AlignRight if index % 2 else Qt.AlignmentFlag.AlignLeft
            indicator = DraftStatLabel(f'{title}: {count}', BADGE_COLORS[level], alignment)
            self.badge_counts[level] = indicator
            badge_grid.addWidget(indicator, index // 2, index % 2)
        badge_grid.setColumnStretch(0, 1)
        badge_grid.setColumnStretch(1, 1)
        layout.addLayout(badge_grid)
        self.art = DraftArt(row.get('image_path'))
        # The artwork takes remaining space; a fixed minimum can force Qt to
        # overlap the statistics at short window heights.
        self.art.setMinimumHeight(0)
        layout.addWidget(self.art, 1)
        three = section_values(card, 'attributes').get('open_shot_three')
        self.open_three = DraftStatLabel(f'Open 3PT: {three if three is not None else "—"}',
                                        '#60dce5', Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self.open_three)
        name = QLabel(row['name']); name.setWordWrap(True)
        name.setTextFormat(Qt.TextFormat.PlainText)
        name.setMinimumHeight(18 if compact else 34)
        layout.addWidget(name)
        meta = QLabel((f"{row['year']} · " if row.get('year') else '') + (row.get('team') or ''))
        meta.setObjectName('muted'); meta.setWordWrap(True); meta.setMinimumHeight(24 if compact else 32)
        layout.addWidget(meta)
        stats = QHBoxLayout()
        tier = QLabel(row['tier']); tier.setObjectName('eyebrow'); stats.addWidget(tier)
        rating = QLabel(str(row['overall'])); rating.setObjectName('count'); stats.addWidget(rating, 0, Qt.AlignmentFlag.AlignRight)
        layout.addLayout(stats)
        pick = QPushButton('Draft player')
        pick.clicked.connect(lambda: workspace.pick(str(row['source_card_id'])))
        layout.addWidget(pick)

class TeamWorkspace(QWidget):
    def __init__(self, owner, kind):
        super().__init__(owner.pages)
        self.owner, self.kind = owner, kind
        self.is_draft = kind in ('draft', 'blacktop')
        if kind == 'blacktop':
            from .blacktop import BlacktopDraftSession
            self.session = BlacktopDraftSession(owner.repository.rows)
        else:
            self.session = TeamSession(owner.repository.rows, kind)
        if self.is_draft:
            from .draft_autosave import DraftAutosave
            self.autosave = DraftAutosave(kind)
        self.choice_dialog = None
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 8, 18, 8)
        root.setSpacing(6)
        heading = QHBoxLayout()
        title = QLabel(TITLES[kind]); title.setObjectName('detailName')
        heading.addWidget(title)
        heading.addStretch()
        if self.is_draft:
            from .draft_audio import DraftSongPlayer, DraftSongControls
            self.draft_song = DraftSongPlayer(self)
            heading.addWidget(DraftSongControls(self.draft_song))
            heading.addSpacing(12)
        self.count = QLabel(); self.count.setObjectName('count'); heading.addWidget(self.count)
        root.addLayout(heading)
        descriptions = {
            'custom': 'Click a card to add it to your team. Choose up to 13 cards; duplicates are allowed.',
            'random': 'One starter at each primary position. Every available tier has an equal chance; each player appears once.',
            'draft': 'Pick a starter and backup at every position, two deep bench players, then a bonus player. Choose one card from each pack.',
            'blacktop': 'Draft a 1v1 through 5v5 pool plus a bonus player. Inject every drafted card into the live Blacktop player pool.',
        }
        title.setToolTip(descriptions[kind])
        toolbar = QHBoxLayout()
        self.custom_toggle = QCheckBox('Include custom cards')
        self.custom_toggle.setChecked(True)
        self.custom_toggle.setToolTip('Include custom cards in future picks. Existing picks stay on your team. Finish the current pack before changing this option.')
        self.custom_toggle.toggled.connect(self.change_custom)
        if kind != 'custom':
            toolbar.addWidget(self.custom_toggle)
        else:
            self.custom_toggle.hide()
        if self.is_draft:
            self.odds = QComboBox()
            for name in ('Baller', 'Default', 'Budget', 'Super Budget'):
                self.odds.addItem(f'{name} Draft', name.lower().replace(' ', '_'))
                self.odds.setItemData(self.odds.count() - 1, self.mode_tooltip(name.lower().replace(' ', '_')), Qt.ItemDataRole.ToolTipRole)
            self.odds.setCurrentIndex(1)
            self.odds.setAccessibleName('Draft odds mode')
            self.odds.setAttribute(Qt.WidgetAttribute.WA_AlwaysShowToolTips, True)
            self.odds.currentIndexChanged.connect(self.change_mode)
            toolbar.addWidget(self.odds)
        toolbar.addStretch()
        self.reset_button = QPushButton('Generate another' if kind == 'random' else 'Start over' if self.is_draft else 'Clear team')
        self.reset_button.clicked.connect(self.reset)
        toolbar.addWidget(self.reset_button)
        for title, method in [('Load lineup', self.load), ('Save lineup', self.save), ('Export PNG', self.export)]:
            button = QPushButton(title); button.clicked.connect(method); toolbar.addWidget(button)
        self.inject_button = QPushButton('Inject team')
        self.inject_button.clicked.connect(self.open_injection)
        toolbar.addWidget(self.inject_button)
        if kind == 'blacktop': self.inject_button.setText('Inject Blacktop team')
        root.addLayout(toolbar)
        if kind == 'blacktop': self.create_blacktop_settings(root)
        if self.is_draft:
            self.autosave_note = QLabel('Drafts save automatically in local storage.')
            self.autosave_note.setObjectName('muted'); self.autosave_note.setWordWrap(True)
            root.addWidget(self.autosave_note)
        self.notice = QLabel(); self.notice.setWordWrap(True); self.notice.setObjectName('notice'); root.addWidget(self.notice)
        if self.is_draft:
            self.notice.setObjectName('muted')
        self.roster_body = QWidget()
        self.roster_layout = QHBoxLayout(self.roster_body) if kind == 'custom' else QGridLayout(self.roster_body)
        self.roster_layout.setSpacing(6 if kind == 'draft' else 12)
        if kind in ('draft', 'blacktop', 'random'):
            self.roster_layout.setContentsMargins(0, 0, 0, 0)
            self.roster_layout.setRowStretch(0, 1)
            if kind == 'draft':
                self.roster_layout.setRowStretch(1, 1)
        if kind != 'custom':
            for column in range(self.session.size + 1 if kind == 'blacktop' else 7 if kind == 'draft' else 5):
                self.roster_layout.setColumnStretch(column, 1)
        self.roster_scroll = QScrollArea(self)
        self.roster_scroll.setWidgetResizable(True)
        self.roster_scroll.setWidget(self.roster_body)
        if kind in ('draft', 'blacktop', 'random'):
            self.roster_scroll.takeWidget()
            self.roster_scroll.hide()
            root.addWidget(self.roster_body, 1)
        else:
            root.addWidget(self.roster_scroll, 1 if kind != 'custom' else 0)
        if kind == 'custom':
            self.roster_scroll.setFixedHeight(300)
            self.create_picker(root)
        if kind == 'random':
            try:
                self.session.generate_random()
            except ValueError as exc:
                self.notice.setText(str(exc))
        self.refresh()

    def create_blacktop_settings(self, root):
        from .blacktop import ROLES
        row = QHBoxLayout()
        row.addWidget(QLabel('Team format'))
        self.team_size = QComboBox()
        self.team_size.setAccessibleName('Blacktop team format')
        for size in range(1, 6): self.team_size.addItem(f'{size}v{size}', size)
        row.addWidget(self.team_size)
        self.solo_controls = QWidget()
        solo = QHBoxLayout(self.solo_controls); solo.setContentsMargins(0, 0, 0, 0)
        solo.addWidget(QLabel('1v1 position'))
        self.solo_group = QButtonGroup(self); self.solo_group.setExclusive(True)
        self.solo_checks = {}
        for index, role in enumerate(ROLES):
            check = QCheckBox(role.title()); check.setChecked(role == 'guard')
            self.solo_group.addButton(check, index); self.solo_checks[role] = check
            solo.addWidget(check)
        row.addWidget(self.solo_controls)
        row.addStretch()
        root.addLayout(row)
        self.team_size.currentIndexChanged.connect(self.change_blacktop_settings)
        self.solo_group.idClicked.connect(self.change_blacktop_settings)

    def change_blacktop_settings(self, *_):
        role = next(role for role, check in self.solo_checks.items() if check.isChecked())
        self.session.configure(size=self.team_size.currentData(), solo_role=role)
        self.refresh()

    def blacktop_round_name(self, mode=None):
        mode = mode or self.session.mode
        return 'Pink Diamond / Diamond' if mode == 'baller' else bonus_round_name(mode)

    def mode_tooltip(self, mode):
        if self.kind != 'blacktop': return self.odds_tooltip(mode)
        lines = ['<div style="font-size:14px;"><b>Blacktop odds per choice</b><table>']
        for tier, chance in draft_odds(mode, 'starter'):
            lines.append(f'<tr><td>{tier}</td><td>{percentage_text(chance)}</td></tr>')
        lines.append('</table><b>' + self.blacktop_round_name(mode) + ' round:</b> ' +
                     ' · '.join(f'{tier} {percentage_text(chance)}' for tier, chance in bonus_odds(mode)))
        lines.append('<br>Five independent choices; players cannot repeat. Position rules apply to every choice.'
                     '<br>5v5 slot 4: 50% Wing, 50% Big, rolled once per draft.'
                     '<br>If a tier is unavailable, another eligible tier is used.</div>')
        return ''.join(lines)

    def save_automatically(self):
        if not self.is_draft: return
        try:
            path = self.autosave.save(self.session)
            if path:
                self.autosave_note.setText('Auto-saved locally · ' + path.name)
                self.autosave_note.setToolTip(str(path))
                self.autosave_note.setStyleSheet('')
        except OSError as exc:
            self.autosave_note.setText('Auto-save failed: ' + str(exc) + ' · Use Save lineup to save another copy.')
            self.autosave_note.setStyleSheet('color: #ffd080;')

    def refresh_blacktop(self):
        clear(self.roster_layout)
        s = self.session
        progress = bool(s.selected or s.pending)
        self.odds.blockSignals(True); self.odds.setCurrentIndex(self.odds.findData(s.mode)); self.odds.blockSignals(False)
        self.odds.setEnabled(not progress); self.custom_toggle.setEnabled(not s.pending)
        self.team_size.blockSignals(True); self.team_size.setCurrentIndex(self.team_size.findData(s.size)); self.team_size.blockSignals(False)
        self.team_size.setEnabled(not progress)
        self.solo_controls.setVisible(s.size == 1)
        for role, check in self.solo_checks.items():
            check.setChecked(role == s.solo_role); check.setEnabled(not progress)
        self.odds.setToolTip(self.mode_tooltip(s.mode))
        self.inject_button.setEnabled(s.complete)
        self.count.setText(f'{len(s.selected)}/{s.size + 1}')
        self.notice.setVisible(True)
        status = (f'Current pack: {s.slot_label(s.pending)}. Click its slot to resume.' if s.pending else
                  f'{self.blacktop_round_name()} bonus round unlocks after {s.size} regular pick(s).' if not s.complete and 'bonus' not in s.selected and len(s.selected) < s.size else
                  'Choose your bonus player.' if not s.complete else
                  f'Team complete · all {s.size + 1} displayed players will inject, highest OVR over highest OVR.')
        if s.size == 5: status = f'Slot 4 rolled {s.flex_role.title()} (50/50 Wing or Big). ' + status
        self.notice.setText(status)
        lineup = dict(s.lineup())
        for col in range(7): self.roster_layout.setColumnStretch(col, 1 if col < len(s.keys) else 0)
        for index, key in enumerate(s.keys):
            row = lineup.get(key)
            title = ('PD / Diamond' if s.mode == 'baller' else self.blacktop_round_name()) + ' round' if key == 'bonus' else s.slot_label(key)
            action = (lambda checked=False, r=row: self.inspect(r)) if row else (lambda checked=False, k=key: self.open_pack(k))
            tile = DraftRosterTile(row, title, 'View card' if row else 'Resume pack' if key == s.pending else 'Open pack', action)
            enabled = bool(row) or (not s.pending or key == s.pending)
            if key == 'bonus': enabled = enabled and all(k in s.selected for k in s.regular_keys)
            tile.setEnabled(enabled)
            self.roster_layout.addWidget(tile, 0, index)

    def hideEvent(self, event):
        if self.is_draft:
            self.draft_song.pause()
        super().hideEvent(event)

    def create_picker(self, root):
        from .desktop import CardModel, CardDelegate, Gallery
        controls = QHBoxLayout()
        self.query = QLineEdit(); self.query.setPlaceholderText('Search players, franchise, theme…')
        self.query.setAccessibleName('Team card search')
        self.query.textChanged.connect(self.filter_cards); controls.addWidget(self.query, 1)
        self.filters = {}
        for key, title in [('tier', 'All tiers'), ('position', 'All positions'), ('theme', 'All themes'), ('team', 'All franchises')]:
            combo = QComboBox(); combo.addItem(title, '')
            for value in sorted({r.get(key) for r in self.session.rows.values() if r.get(key)}, key=tier_rank if key == 'tier' else str.casefold):
                combo.addItem(value, value)
            combo.currentIndexChanged.connect(self.filter_cards)
            combo.setAccessibleName('Team ' + key)
            self.filters[key] = combo; controls.addWidget(combo)
        combo = QComboBox()
        combo.addItem('Custom card: All', '')
        combo.addItem('Custom card: Yes', 'yes')
        combo.addItem('Custom card: No', 'no')
        combo.setAccessibleName('Team custom card')
        combo.currentIndexChanged.connect(self.filter_cards)
        self.filters['custom_card'] = combo
        controls.addWidget(combo)
        root.addLayout(controls)
        self.model = CardModel(self)
        self.gallery = Gallery(); self.gallery.setModel(self.model)
        self.gallery.setItemDelegate(CardDelegate(self.owner.cache, self.gallery))
        self.gallery.clicked.connect(self.add_selected)
        self.gallery.open_requested.connect(self.add_selected)
        root.addWidget(self.gallery, 1)
        actions = QHBoxLayout()
        self.results = QLabel(); actions.addWidget(self.results); actions.addStretch()
        view = QPushButton('View selected card'); view.clicked.connect(self.inspect_selected); actions.addWidget(view)
        self.add_button = QPushButton('Add selected card'); self.add_button.clicked.connect(self.add_selected); actions.addWidget(self.add_button)
        root.addLayout(actions)
        self.filter_cards()

    def open_injection(self):
        from .injection_ui import InjectionPanel
        lineup = self.session.lineup()
        if not lineup:
            QMessageBox.information(self, 'Inject team', 'Choose or generate a lineup first.')
            return
        if self.kind == 'draft' and len(lineup) != 13:
            QMessageBox.information(self, 'Inject team', 'Finish all 13 draft picks before injecting.')
            return
        if self.kind == 'blacktop' and not self.session.complete:
            QMessageBox.information(self, 'Blacktop', 'Finish all regular picks and the bonus round before injecting.')
            return
        panel = InjectionPanel(self.owner, lineup, blacktop=True) if self.kind == 'blacktop' else InjectionPanel(self.owner, lineup)
        self.owner.show_overlay(panel, panel.release)

    def filter_cards(self):
        if self.kind != 'custom':
            return
        self.session.include_custom = True
        query = self.query.text().casefold().strip()
        matches = []
        for row in self.session.pool():
            if query and query not in (row.get('search') or row['name']).casefold():
                continue
            if any(combo.currentData() and combo.currentData() != row.get(key) and not (key == 'position' and combo.currentData() == row.get('secondary_position')) for key, combo in self.filters.items()):
                continue
            matches.append(row)
        matches.sort(key=lambda r: (tier_rank(r['tier']), -(r.get('overall') or 0), r['name']))
        self.model.replace(matches)
        self.results.setText(f'{len(matches):,} cards · Click a card to add it')

    def inspect(self, row):
        self.owner.show_card(row['id'])

    def inspect_selected(self):
        row = self.gallery.currentIndex().data(Qt.ItemDataRole.UserRole)
        if row:
            self.inspect(row)

    def add_selected(self, *_):
        row = self.gallery.currentIndex().data(Qt.ItemDataRole.UserRole)
        if row:
            try:
                self.session.add(str(row['source_card_id'])); self.refresh()
            except ValueError as exc:
                QMessageBox.information(self, 'Team full', str(exc))

    def remove(self, index):
        self.session.custom.pop(index); self.refresh()

    def change_custom(self, enabled):
        self.session.include_custom = enabled
        self.filter_cards()
        self.save_automatically()

    def change_mode(self):
        self.session.mode = self.odds.currentData()
        self.refresh()

    @staticmethod
    def odds_tooltip(mode):
        font_size = 14 if mode == 'super_budget' else 19.5
        mode_name = mode.replace('_', ' ').title()
        lines = [f'<div style="font-size:{font_size}px; font-weight:700;"><b>{mode_name} Draft — odds per choice</b><table cellpadding="3"><tr><th align="left">Tier</th><th>Starter</th><th>Bench</th><th>Deep bench</th></tr>']
        tables = [dict(draft_odds(mode, role)) for role in ('starter', 'backup', 'bench')]
        for tier in tables[0]:
            lines.append('<tr><td align="left" style="white-space:nowrap;">' + tier.replace(' ', '&nbsp;') + '</td>' + ''.join(f'<td style="white-space:nowrap;">{percentage_text(table[tier])}</td>' for table in tables) + '</tr>')
        lines.append('</table><b>' + bonus_round_name(mode) + ' round:</b> ' + ' · '.join(f'{tier} {percentage_text(chance)}' for tier, chance in bonus_odds(mode)))
        lines.append('<br>All five choices roll independently. Players cannot repeat.<br>If a tier has no eligible player, another eligible tier is used.')
        return ''.join(lines) + '</div>'

    def show_odds(self):
        # Retained for callers; odds are available directly on hover.
        self.odds.setToolTip(self.mode_tooltip(self.session.mode))

    def refresh(self):
        if self.kind == 'blacktop':
            self.refresh_blacktop()
            return
        clear(self.roster_layout)
        lineup = dict(self.session.lineup())
        total = 5 if self.kind == 'random' else 13
        self.inject_button.setEnabled(bool(lineup) and (self.kind != 'draft' or len(lineup) == 13))
        self.count.setText(f'{len(self.session.lineup())}/{total}')
        self.notice.setVisible(self.kind == 'draft')
        if self.kind == 'random' and not lineup:
            self.notice.setVisible(True)
        if self.kind == 'draft':
            self.show_odds()
            progress = bool(self.session.selected or self.session.pending)
            self.odds.setEnabled(not progress); self.custom_toggle.setEnabled(not self.session.pending)
            bonus = bonus_round_name(self.session.mode)
            self.notice.setText(f'Current pack: {slot_label(self.session.pending)}. Click its slot to resume.' if self.session.pending else f'{bonus} bonus round unlocks after all 12 regular picks.' if len(lineup) < 12 else 'Choose your bonus player.' if len(lineup) == 12 else 'Team complete — save or export your lineup.')
            keys = tuple(f'{role}|{p}' for role in ('starter', 'backup') for p in POSITIONS) + DRAFT_KEYS[10:]
        else:
            keys = POSITIONS if self.kind == 'random' else tuple(str(i + 1) for i in range(13))
        for index, key in enumerate(keys):
            row = lineup.get(key)
            inspect = (lambda checked=False, r=row: self.inspect(r)) if row else None
            if self.kind == 'custom':
                action = lambda checked=False, i=index: self.remove(i)
                tile = CustomRosterTile(row, slot_label(key), action, inspect)
                tile.action_button.setEnabled(row is not None); tile.setFixedWidth(166)
                self.roster_layout.addWidget(tile)
            elif self.kind == 'random':
                tile = DraftRosterTile(row, key, 'View card', inspect or (lambda: None))
                tile.action_button.setEnabled(row is not None)
                self.roster_layout.addWidget(tile, 0, index)
            else:
                title = (bonus_round_name(self.session.mode) + ' round') if key == 'bonus' else slot_label(key)
                tile = DraftRosterTile(row, title, 'View card' if row else 'Resume pack' if key == self.session.pending else 'Open pack', inspect if row else lambda checked=False, k=key: self.open_pack(k))
                enabled = bool(row) or (not self.session.pending or key == self.session.pending)
                if key.startswith('bench'):
                    enabled = enabled and all(k in lineup for k in POSITION_KEYS)
                if key == 'bonus':
                    enabled = enabled and all(k in lineup for k in DRAFT_KEYS[:-1])
                tile.action_button.setEnabled(enabled)
                grid_row, column = (index // 5, index % 5) if index < 10 else (1, index - 5) if index < 12 else (0, 5)
                self.roster_layout.addWidget(tile, grid_row, column)
        if self.kind == 'custom':
            self.add_button.setEnabled(len(lineup) < 13)

    def open_pack(self, key):
        try:
            cards = self.session.open_pack(key)
        except ValueError as exc:
            QMessageBox.information(self, 'Draft', str(exc)); return
        self.save_automatically()
        self.refresh()
        panel = QFrame(); self.choice_dialog = panel
        panel.setObjectName('content')
        outer = QVBoxLayout(panel)
        margin = 16 if key == 'bonus' else 20
        outer.setContentsMargins(margin, margin, margin, margin)
        outer.setSpacing(8 if key == 'bonus' else 14)
        from .draft_audio import DraftSongControls
        song_heading = QHBoxLayout()
        if key != 'bonus':
            eyebrow = QLabel('ON THE CLOCK'); eyebrow.setObjectName('eyebrow'); song_heading.addWidget(eyebrow)
        song_heading.addStretch()
        song_heading.addWidget(DraftSongControls(self.draft_song))
        outer.addLayout(song_heading)
        if key != 'bonus':
            heading = QLabel('Choose your ' + (self.session.slot_label(key) if self.kind == 'blacktop' else slot_label(key))); heading.setObjectName('detailName'); outer.addWidget(heading)
            hint = QLabel('Five independently rolled choices. Press Esc to view your team; resume this pack to return to the same players.')
            hint.setObjectName('muted'); hint.setWordWrap(True); outer.addWidget(hint)
        if key == 'bonus':
            if self.kind == 'blacktop':
                heading = QLabel(self.blacktop_round_name() + ' round · bonus player')
                heading.setObjectName('detailName'); outer.addWidget(heading)
            preview = LineupPreview(self.session.lineup())
            if self.kind == 'blacktop': preview.setMaximumHeight(150)
            outer.addWidget(preview)
        choices = QHBoxLayout(); choices.setSpacing(14)
        for row in cards:
            choices.addWidget(DraftChoice(self, row, compact=key == 'bonus'), 1)
        outer.addLayout(choices, 1)
        close = QPushButton('Return to lineup · Esc'); close.clicked.connect(self.owner.close_overlay); outer.addWidget(close)
        self.owner.show_overlay(panel, self.pack_closed)

    def pack_closed(self):
        self.choice_dialog = None
        self.refresh()

    def pick(self, cid):
        self.session.pick(cid)
        self.save_automatically()
        if self.choice_dialog:
            self.owner.close_overlay()
        else:
            self.refresh()

    def reset(self):
        if self.kind != 'random' and (self.session.lineup() or self.session.pending):
            if QMessageBox.question(self, 'Start over', 'Clear this lineup and any pending draft choices?', QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
        old = self.session
        if self.kind == 'blacktop':
            from .blacktop import BlacktopDraftSession
            self.session = BlacktopDraftSession(self.owner.repository.rows)
            self.session.configure(size=old.size, solo_role=old.solo_role)
        else:
            self.session = TeamSession(self.owner.repository.rows, self.kind)
        if self.is_draft:
            from .draft_autosave import DraftAutosave
            self.autosave = DraftAutosave(self.kind)
            self.autosave_note.setText('Drafts save automatically in local storage.')
        self.session.mode, self.session.include_custom = old.mode, old.include_custom
        if self.kind == 'random':
            try:
                self.session.generate_random()
            except ValueError as exc:
                self.session = old; QMessageBox.warning(self, 'Random team', str(exc))
        self.refresh()

    def directory(self):
        if self.is_draft:
            from .draft_autosave import autosave_directory
            path = autosave_directory(self.kind)
        else:
            path = project_root() / '.project/user/teams'
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Save lineup', str(self.directory() / f'{self.kind}-{datetime.now():%Y%m%d-%H%M%S}.2k17team'), '2K17 team (*.2k17team)')
        if path:
            try:
                self.session.save(path)
                self.export_image(Path(path).with_suffix('.png'))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, 'Could not save lineup', str(exc)); return
            QMessageBox.information(self, 'Lineup saved', 'Saved the editable team and a PNG preview.')

    def load(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Load lineup', str(self.directory()), '2K17 team (*.2k17team)')
        if not path:
            return
        try:
            from .blacktop import BlacktopDraftSession
            loader = BlacktopDraftSession if self.kind == 'blacktop' else TeamSession
            loaded = loader.load(self.owner.repository.rows, path)
            if loaded.kind != self.kind:
                raise ValueError(f'Open {TITLES[loaded.kind]} to load this team.')
        except (OSError, ValueError, TypeError, KeyError) as exc:
            QMessageBox.warning(self, 'Could not load lineup', str(exc)); return
        if self.session.lineup() or self.session.pending:
            if QMessageBox.question(self, 'Load lineup', 'Replace the current lineup with this saved team?') != QMessageBox.StandardButton.Yes:
                return
        self.session = loaded
        self.custom_toggle.blockSignals(True); self.custom_toggle.setChecked(loaded.include_custom); self.custom_toggle.blockSignals(False)
        if self.is_draft:
            from .draft_autosave import DraftAutosave
            self.autosave = DraftAutosave(self.kind)
            self.save_automatically()
            self.odds.blockSignals(True); self.odds.setCurrentIndex(self.odds.findData(loaded.mode)); self.odds.blockSignals(False)
        self.filter_cards(); self.refresh()

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, 'Export lineup PNG', str(self.directory() / f'{self.kind}-lineup.png'), 'PNG image (*.png)')
        if path:
            try:
                self.export_image(path)
            except OSError as exc:
                QMessageBox.warning(self, 'Could not export', str(exc))

    def export_image(self, path):
        rows = self.session.lineup()
        columns = 5
        count = max(1, (len(rows) + columns - 1) // columns)
        canvas = QPixmap(1500, 110 + count * 480); canvas.fill(QColor('#090f16'))
        painter = QPainter(canvas)
        painter.setPen(QColor('#e8eff7')); painter.setFont(QFont('Segoe UI', 24, QFont.Weight.Bold))
        painter.drawText(QRect(25, 15, 1450, 55), Qt.AlignmentFlag.AlignLeft, 'NBA 2K17 · ' + TITLES[self.kind])
        if self.kind == 'blacktop':
            painter.setFont(QFont('Segoe UI', 11))
            bonus = f'All {len(rows)} displayed players inject · highest OVR first'
            painter.drawText(QRect(25, 68, 1450, 24), Qt.AlignmentFlag.AlignLeft,
                             f'{self.session.size}v{self.session.size} · {self.session.mode.replace("_", " ").title()} · {bonus}')
        for index, (key, row) in enumerate(rows):
            x, y = 20 + (index % columns) * 296, 100 + (index // columns) * 480
            painter.setPen(QColor(TIER_COLORS.get(row['tier'], '#60dce5')))
            painter.drawRect(x, y, 280, 460)
            painter.setFont(QFont('Segoe UI', 12, QFont.Weight.Bold))
            painter.drawText(QRect(x + 10, y + 4, 260, 30), Qt.AlignmentFlag.AlignLeft, (self.session.slot_label(key) if self.kind == 'blacktop' else slot_label(key)))
            if row.get('image_path'):
                art = QPixmap(row['image_path']).scaled(260, 355, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                painter.drawPixmap(x + (280 - art.width()) // 2, y + 36, art)
            painter.setPen(QColor('#e8eff7')); painter.setFont(QFont('Segoe UI', 11))
            painter.drawText(QRect(x + 10, y + 395, 260, 55), Qt.TextFlag.TextWordWrap, f"{row['name']}\n{row['tier']} · {row['overall']} OVR")
        painter.end()
        if not canvas.save(str(path), 'PNG'):
            raise OSError('Unable to write the lineup image')
