"""Normal 2K16 Card Studio layout, retaining only 2K17-specific card controls."""
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QIcon, QIntValidator
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QGroupBox, QGridLayout, QLabel,
    QComboBox, QDoubleSpinBox, QSplitter, QFormLayout, QLineEdit, QCheckBox, QStackedWidget)
from .canvas import Canvas

STYLE = '''
QMainWindow, QWidget { background: #151b24; color: #e5ebf3; }
QMenuBar, QMenu { background: #111720; color: #e5ebf3; }
QGroupBox { border: 1px solid #344257; border-radius: 6px; margin-top: 10px; padding: 9px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPushButton { background: #26364a; border: 1px solid #49617f; border-radius: 4px; padding: 8px 12px; }
QPushButton:hover { background: #304761; }
QPushButton:disabled { background: #1b2532; color: #667387; border-color: #2d3b4d; }
QPushButton#primaryButton { background: #176b78; border-color: #43b7c5; font-weight: 700; }
QPushButton#primaryButton:hover { background: #208393; }
QDoubleSpinBox, QSpinBox, QComboBox, QLineEdit { background: #0e141c; border: 1px solid #3c4b60; padding: 6px; }
QStatusBar { background: #0d1219; color: #b6c3d3; }
'''


EXTRA_LOGOS = frozenset({'Eastern Conference', 'Team USA', 'Western Conference'})


def logo_category(row):
    """Visible authoring set, independent of native texture-number ordering.

    Charlotte is logo031, not a conference logo. The user's Current set also
    includes both conferences and the verified native Team USA primary logo.
    All original assets remain available when rendering saved projects.
    """
    if row['identification'] == 'visually_verified_primary':
        return 'Current'
    if row['identification'] == 'visually_verified_supplemental' and row['name'] in {'Team USA', 'Seattle SuperSonics'}:
        return 'Current'
    return None


def current_logo_options(assets):
    """NBA locations A–Z first, followed by the three extras A–Z.

    Verified NBA names already start with city/location (including Golden State,
    Indiana, and Utah); the full name breaks ties such as the Los Angeles teams.
    """
    rows = [row for row in assets['logos']
            if row['default_selectable'] and logo_category(row) == 'Current']
    return sorted(rows, key=lambda row: (row['name'] in EXTRA_LOGOS, row['name'].casefold()))


def promotion_options(assets):
    names={row['name'] for row in assets['promotions']} | {'Free Agent'}
    return [('No promotion logo','')] + [(name,name) for name in sorted(names,key=str.casefold)]


def fill_logos(window, selected=''):
    previous = window.logo.blockSignals(True)
    window.logo.clear()
    window.logo.addItem('No team logo', '')
    for row in current_logo_options(window.assets):
        # Decode once when changing sets, never during pointer movement or sync.
        icon = window.logo_icons.get(row['id'])
        if icon is None:
            icon = QIcon(str(window.pack/row['file']))
            window.logo_icons[row['id']] = icon
        window.logo.addItem(icon, row['name'], row['id'])
    window.logo.setCurrentIndex(max(0, window.logo.findData(selected)))
    window.logo.blockSignals(previous)


def build_layout(w):
    w.logo_icons = {}
    w.data_editor = None
    w.stack = QStackedWidget()
    center = QWidget()
    layout = QVBoxLayout(center)
    layout.setContentsMargins(10, 8, 10, 8)
    workflow = QGroupBox('Create Card')
    grid = QGridLayout(workflow)
    w.workflow_grid = grid
    grid.addWidget(QLabel('Card tier'), 0, 0)
    w.tier = QComboBox()
    for row in reversed(w.assets['tiers']): w.tier.addItem(row['name'], row['id'])
    w.tier.setMinimumWidth(210)
    w.tier.setObjectName('templateCombo')
    grid.addWidget(w.tier, 0, 1, 1, 2)
    w.tier.currentIndexChanged.connect(lambda: w.change('tier', w.tier.currentData()))
    grid.addWidget(QLabel('Player size'), 0, 3)
    w.size = QDoubleSpinBox()
    w.size.setRange(5, 500); w.size.setDecimals(2); w.size.setSingleStep(.25)
    w.size.setSuffix(' %'); w.size.setKeyboardTracking(False); w.size.setObjectName('playerSizeInput')
    w.size.valueChanged.connect(lambda v: w.change('portrait_height', round(1264*v/100)))
    grid.addWidget(w.size, 0, 4)
    w.summary = QLabel()
    w.summary.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    grid.addWidget(w.summary, 0, 5, 1, 2)
    edit = w.button('Edit Attributes'); edit.setObjectName('editAttributesButton')
    grid.addWidget(edit, 0, 7)
    grid.addWidget(QLabel('Logo set'), 1, 0)
    w.logo_category = QComboBox()
    w.logo_category.setObjectName('logoCategoryCombo'); w.logo_category.setMinimumWidth(115)
    w.logo_category.addItems(['Current'])
    grid.addWidget(w.logo_category, 1, 1)
    grid.addWidget(QLabel('Team logo'), 1, 2)
    w.logo = QComboBox(); w.logo.setObjectName('logoCombo'); w.logo.setMinimumWidth(245)
    w.logo.setPlaceholderText('Saved logo (not in current set)')
    w.logo.setIconSize(QSize(22, 22)); w.logo.setMaxVisibleItems(20)
    grid.addWidget(w.logo, 1, 3, 1, 2)
    w.logo_category.currentIndexChanged.connect(w.change_logo_category)
    w.logo.currentIndexChanged.connect(lambda: w.change('team_logo', w.logo.currentData() or ''))
    fill_logos(w)
    grid.addWidget(QLabel('Card background'), 2, 0)
    w.background = QComboBox(); w.background.setObjectName('backgroundCombo'); w.background.setMinimumWidth(245)
    w.background.addItem('Native tier background', True); w.background.addItem('No background', False)
    grid.addWidget(w.background, 2, 1, 1, 2)
    w.background.currentIndexChanged.connect(lambda: w.change('background', w.background.currentData()))
    grid.addWidget(QLabel('Promotion logo'), 2, 3)
    w.promotion = QComboBox(); w.promotion.setObjectName('promotionCombo'); w.promotion.setMinimumWidth(245)
    for label,value in promotion_options(w.assets):w.promotion.addItem(label,value)
    grid.addWidget(w.promotion, 2, 4, 1, 3)
    w.promotion.currentIndexChanged.connect(lambda: w.change('theme', w.promotion.currentData()))
    for column, (name, obj) in enumerate([
        ('Import Image','importButton'), ('Remove Background','removeBackgroundButton'),
        ('Delete PNG','deleteButton'), ('Open Project','openButton'), ('Save Project','saveButton'),
        ('Export PNG','primaryButton'), ('Export Card + Data','exportCustomButton'),
        ('Undo','undoButton'), ('Redo','redoButton')]):
        button = w.button(name); button.setObjectName(obj); grid.addWidget(button, 3, column)
    grid.setColumnStretch(8, 1)
    layout.addWidget(workflow)
    splitter = QSplitter(Qt.Orientation.Horizontal)
    w.canvas = Canvas(); splitter.addWidget(w.canvas)
    w.canvas.moved.connect(w.move_portrait); w.canvas.resized.connect(w.scale_portrait); w.canvas.deleted.connect(w.delete_image)
    side = QWidget(); side.setMinimumWidth(230); side.setMaximumWidth(320)
    w.player_controls = side
    box = QVBoxLayout(side)
    position = QGroupBox('Player Position'); form = QFormLayout(position)
    w.x = QDoubleSpinBox(); w.y = QDoubleSpinBox()
    for name, widget, key in [('X', w.x, 'portrait_x'), ('Y', w.y, 'portrait_y')]:
        widget.setRange(-8192, 8192); widget.setDecimals(1); widget.setSingleStep(1)
        widget.setKeyboardTracking(False); widget.setAccessibleName('Portrait '+name)
        widget.valueChanged.connect(lambda v, k=key: w.change(k, v)); form.addRow(name, widget)
    box.addWidget(position)
    text = QGroupBox('Card Text'); form = QFormLayout(text)
    w.overall = QLineEdit(); w.overall.setMaxLength(5); w.overall.setValidator(QIntValidator(0, 99, w.overall))
    w.overall.setObjectName('overallInput'); w.overall.setPlaceholderText('97')
    w.overall.editingFinished.connect(lambda: None if w.overall.isReadOnly() else w.change('overall', int(w.overall.text() or '0')))
    form.addRow('OVR', w.overall)
    w.position = QLineEdit(); w.position.setMaxLength(2); w.position.setObjectName('positionInput'); w.position.setPlaceholderText('C')
    w.position.editingFinished.connect(lambda: w.change_position(w.position.text()))
    form.addRow('Position', w.position)
    w.player_name = QLineEdit(); w.player_name.setObjectName('playerNameInput'); w.player_name.setMaxLength(90)
    w.player_name.setPlaceholderText('KAREEM ABDUL-JABBAR'); w.player_name.editingFinished.connect(w.change_player_name)
    form.addRow('Player Name', w.player_name)
    w.year = QLineEdit(); w.year.setObjectName('cardYearInput'); w.year.setMaxLength(4)
    w.year.setValidator(QIntValidator(0, 9999, w.year)); w.year.setPlaceholderText('e.g. 2021')
    w.year.editingFinished.connect(w.change_year)
    form.addRow('Season / year', w.year)
    # 2K17 prints the jersey number and supports the Dynamic Duo marker.
    w.jersey = QLineEdit(); w.jersey.setMaxLength(3); w.jersey.setObjectName('jerseyNumberInput')
    w.jersey.editingFinished.connect(lambda: w.change('jersey_number', w.jersey.text()))
    form.addRow('Jersey #', w.jersey)
    w.duo = QCheckBox('Dynamic Duo marker'); w.duo.toggled.connect(lambda v: w.change('dynamic_duo', v))
    form.addRow(w.duo)
    box.addWidget(text)
    w.text_warning = QLabel(); w.text_warning.setWordWrap(True); w.text_warning.setStyleSheet('color: #f0bd63; padding: 4px;'); w.text_warning.hide(); box.addWidget(w.text_warning)
    hint = QLabel('Drag the player directly on the card, use X/Y, or press the arrow keys. Hold Shift for 10-pixel nudges. Hold Ctrl and use the mouse wheel to resize.')
    hint.setWordWrap(True); hint.setStyleSheet('color: #aebbd0; padding: 4px;'); box.addWidget(hint)
    w.player_status = QLabel('No player image imported'); w.player_status.setWordWrap(True)
    w.player_status.setStyleSheet('color: #8fa0b5; padding: 4px;'); box.addWidget(w.player_status); box.addStretch(1)
    splitter.addWidget(side); splitter.setSizes([830, 260]); splitter.setStretchFactor(0, 1)
    splitter.setCollapsible(0, False); splitter.setCollapsible(1, False)
    layout.addWidget(splitter, 1)
    w.stack.addWidget(center); w.setCentralWidget(w.stack)
