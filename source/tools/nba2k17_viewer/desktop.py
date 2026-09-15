"""Native card archive, Card Studio, team builders and live roster injection."""
from __future__ import annotations

import argparse
from collections import OrderedDict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import traceback

from PySide6.QtCore import Qt, QSize, QRect, QEvent, QAbstractListModel, QModelIndex, QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPixmap, QImageReader, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QFrame, QLabel, QLineEdit,
    QPushButton, QComboBox, QHBoxLayout, QVBoxLayout, QGridLayout, QListView, QStyledItemDelegate,
    QStyle, QAbstractItemView, QScrollArea, QDialog, QTabWidget, QProgressBar, QFileDialog, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QPlainTextEdit, QStackedWidget)

from .archive_repository import ArchiveRepository, ViewerFilter, default_source, project_root
from .hot_zones import zone_states, render_hot_zones
from .presentation import TIER_COLORS, BADGE_COLORS, BADGE_LABELS, attribute_groups, section_values, rating_color, label
from nba2k17_catalog import DEFAULT_SORT, LOW_OVR_SORT

TITLE = "NBA 2K17 MyTEAM"
STYLE = """
QWidget { background: #090f16; color: #e8eff7; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog { background: #090f16; }
QLabel { background: transparent; }
QFrame#topbar { background: #080d13; border-bottom: 1px solid #243342; }
QFrame#sidebar, QFrame#group { background: #0d151f; border: 1px solid #243544; }
QFrame#content { background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #101d2a,stop:0.6 #0a111a,stop:1 #090f16); }
QLabel#brand { font-size: 21px; font-weight: 700; }
QLabel#mark { color: #071115; background: #61dbe2; font-size: 25px; font-weight: 900; border-radius: 5px; }
QLabel#muted { color: #8499af; font-size: 12px; }
QLabel#eyebrow { color: #60dce5; font-size: 10px; font-weight: 800; letter-spacing: 2px; }
QLabel#count { font-size: 25px; font-weight: 700; }
QLabel#detailName { font-size: 28px; font-weight: 700; }
QLabel#groupTitle { font-size: 12px; font-weight: 700; color: #adc0d1; }
QLabel#fieldLabel { color: #8297ac; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#notice { color: #d7bb80; background: #211f19; border: 1px solid #514936; padding: 10px; }
QLineEdit, QComboBox { background: #0a1119; color: #e8eff7; border: 1px solid #2a3c4e; padding: 9px 10px; min-height: 18px; }
QLineEdit:focus, QComboBox:focus { border: 1px solid #60dce5; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox QAbstractItemView { background: #101c29; color: #e8eff7; selection-background-color: #21444c; border: 1px solid #365065; padding: 4px; }
QPushButton { background: #122330; border: 1px solid #335664; padding: 9px 15px; color: #bdeff3; font-weight: 600; }
QPushButton:hover { background: #1a3441; border-color: #60dce5; }
QPushButton:focus { border: 1px solid #60dce5; }
QPushButton:disabled { color: #53687c; border-color: #253746; background: #0e1822; }
QPushButton#reset { border: 0; background: transparent; padding: 2px; color: #60dce5; font-size: 12px; }
QListView { background: transparent; border: 0; outline: 0; }
QScrollArea { background: transparent; border: 0; }
QScrollArea > QWidget > QWidget { background: transparent; }
QScrollBar:vertical { background: #0b131d; width: 10px; margin: 0; }
QScrollBar::handle:vertical { background: #344b60; min-height: 40px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
QScrollBar:horizontal { background: #0b131d; height: 10px; margin: 0; }
QScrollBar::handle:horizontal { background: #344b60; min-width: 40px; border-radius: 4px; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
QTabWidget::pane { border: 0; border-top: 1px solid #283c50; background: #0c141f; }
QTabBar::tab { background: transparent; color: #91a6ba; padding: 12px 14px; border-bottom: 2px solid transparent; }
QTabBar::tab:selected { color: #7be6ed; border-bottom: 2px solid #62dce5; }
QTabBar::tab:hover { color: #e5f6ff; }
QProgressBar { background: #1b2c3a; border: 0; height: 4px; max-height: 4px; }
QProgressBar::chunk { background: #60dce5; }
QToolTip { background: #15283a; color: #e5eff8; border: 1px solid #42637c; padding: 6px; }
"""


def text(value, kind=None, *, wrap=False):
    widget = QLabel(str(value))
    widget.setTextFormat(Qt.TextFormat.PlainText)
    if kind:
        widget.setObjectName(kind)
    widget.setWordWrap(wrap)
    return widget


def vertical(parent=None, margins=0, spacing=10):
    layout = QVBoxLayout(parent)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    return layout


def scroll_widget(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    return scroll


def app_icon():
    # This same multi-size ICO is embedded in the EXE's Windows resources.
    return QIcon(str(Path(__file__).resolve().parent / 'assets/myteam17.ico'))


class CustomCardSelectionDelegate(QStyledItemDelegate):
    """Use the whole name/OVR/status cell as the card's checkbox target."""

    def editorEvent(self, event, model, option, index):
        if event.type() == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton or not option.rect.contains(event.position().toPoint()):
                return False
        elif event.type() == QEvent.Type.KeyPress:
            if event.key() != Qt.Key.Key_Space or event.isAutoRepeat():
                return False
        elif event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return True
        else:
            return super().editorEvent(event, model, option, index)
        checkbox = index.siblingAtColumn(0)
        checked = checkbox.data(Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked.value
        return model.setData(checkbox, Qt.CheckState.Unchecked.value if checked else Qt.CheckState.Checked.value,
                             Qt.ItemDataRole.CheckStateRole)


class ThumbnailCache:
    def __init__(self, capacity=180):
        self.items, self.capacity = OrderedDict(), capacity

    def get(self, path, size: QSize):
        if not path:
            return QPixmap()
        cache_key = (path, size.width(), size.height())
        if cache_key in self.items:
            self.items.move_to_end(cache_key)
            return self.items[cache_key]
        reader = QImageReader(path)
        reader.setAutoTransform(True)
        original = reader.size()
        if original.isValid():
            reader.setScaledSize(original.scaled(size, Qt.AspectRatioMode.KeepAspectRatio))
        pixmap = QPixmap.fromImage(reader.read())
        self.items[cache_key] = pixmap
        while len(self.items) > self.capacity:
            self.items.popitem(last=False)
        return pixmap


class CardModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows = []

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.rows)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or not 0 <= index.row() < len(self.rows):
            return None
        row = self.rows[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return row
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.AccessibleTextRole):
            return f"{row['name']} · {row['overall'] if row['overall'] is not None else 'Unknown'} OVR · {row['tier'] or 'Unknown tier'} · {row['collection'] or ''}"
        if role == Qt.ItemDataRole.ToolTipRole:
            return f"{row['name']}\n{row['collection'] or 'Collection unknown'}\nCard #{row['source_card_id']}\n" + ("Click to view card" if row['image_path'] else "Artwork unavailable")
        return None

    def replace(self, rows):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()


class CardDelegate(QStyledItemDelegate):
    def __init__(self, cache, parent=None):
        super().__init__(parent)
        self.cache = cache

    def sizeHint(self, option, index):
        return QSize(204, 338)

    def paint(self, painter, option, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if not row:
            return
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        box = option.rect.adjusted(8, 5, -8, -7)
        art = QRect(box.x(), box.y(), box.width(), box.height() - 67)
        accent = QColor(TIER_COLORS.get(row['tier'], '#708aa1'))
        active = bool(option.state & (QStyle.StateFlag.State_MouseOver | QStyle.StateFlag.State_Selected | QStyle.StateFlag.State_HasFocus))
        painter.fillRect(art, QColor('#09121c'))
        pixmap = self.cache.get(row['image_path'], QSize(art.width() * 2, art.height() * 2))
        if not pixmap.isNull():
            size = pixmap.size().scaled(art.size(), Qt.AspectRatioMode.KeepAspectRatio)
            target = QRect(art.x() + (art.width() - size.width()) // 2, art.y() + (art.height() - size.height()) // 2, size.width(), size.height())
            painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
            painter.drawPixmap(target, pixmap)
        else:
            painter.setPen(QPen(accent.darker(160), 1))
            painter.drawRect(art.adjusted(0, 0, -1, -1))
            painter.setPen(accent)
            painter.setFont(QFont('Segoe UI', 27, QFont.Weight.Bold))
            painter.drawText(art.adjusted(14, 12, -14, -art.height() + 62), Qt.AlignmentFlag.AlignLeft, str(row['overall']) if row['overall'] is not None else '—')
            painter.setFont(QFont('Segoe UI', 12, QFont.Weight.DemiBold))
            painter.drawText(art.adjusted(16, 68, -16, -65), Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, row['name'])
            painter.setFont(QFont('Segoe UI', 9))
            painter.setPen(QColor('#8499ad'))
            painter.drawText(art.adjusted(10, art.height() - 63, -10, -20), Qt.AlignmentFlag.AlignCenter, 'PNG NOT RECOVERED\nCard data available')
        if active:
            painter.setPen(QPen(QColor('#73e4eb'), 2))
            painter.drawRect(art.adjusted(0, 0, -1, -1))
        y = art.bottom() + 8
        painter.setFont(QFont('Segoe UI', 10, QFont.Weight.DemiBold))
        painter.setPen(QColor('#ecf4fc'))
        name_box = QRect(box.x(), y, box.width() - 31, 20)
        painter.drawText(name_box, Qt.AlignmentFlag.AlignVCenter, painter.fontMetrics().elidedText(row['name'], Qt.TextElideMode.ElideRight, name_box.width()))
        painter.setPen(accent)
        painter.drawText(QRect(box.right() - 30, y, 30, 20), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, str(row['overall']) if row['overall'] is not None else '—')
        painter.setFont(QFont('Segoe UI', 8))
        painter.setPen(QColor('#8b9fb3'))
        subtitle = ' · '.join(str(v) for v in (row['year'], row['team']) if v) or 'Identity details incomplete'
        painter.drawText(QRect(box.x(), y + 22, box.width(), 17), painter.fontMetrics().elidedText(subtitle, Qt.TextElideMode.ElideRight, box.width()))
        painter.setPen(accent.darker(110))
        collection = row['collection'] or row['tier'] or 'Unknown collection'
        painter.drawText(QRect(box.x(), y + 40, box.width(), 17), painter.fontMetrics().elidedText(collection, Qt.TextElideMode.ElideRight, box.width()))
        painter.restore()


class Gallery(QListView):
    open_requested = Signal(QModelIndex)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setGridSize(QSize(204, 338))
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setMouseTracking(True)
        self.setAccessibleName('Card gallery')
        self._wheel_remainder = 0

    def wheelEvent(self, event):
        delta = event.angleDelta().y()
        if not delta:
            super().wheelEvent(event)
            return
        # One physical wheel notch moves one card row, independent of the
        # Windows scroll-lines setting. Accumulate high-resolution wheel ticks.
        if delta * self._wheel_remainder < 0:
            self._wheel_remainder = 0
        self._wheel_remainder += delta
        rows = int(self._wheel_remainder / 120)
        self._wheel_remainder -= rows * 120
        bar = self.verticalScrollBar()
        bar.setValue(bar.value() - rows * self.gridSize().height())
        event.accept()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space) and self.currentIndex().isValid():
            self.open_requested.emit(self.currentIndex())
        else:
            super().keyPressEvent(event)


class LoadSignals(QObject):
    ready = Signal(object)
    error = Signal(str)


class LoadArchive(QRunnable):
    def __init__(self, path):
        super().__init__()
        self.path = path
        self.signals = LoadSignals()

    def run(self):
        try:
            self.signals.ready.emit(ArchiveRepository(self.path))
        except Exception as error:
            self.signals.error.emit(str(error))


def value_group(title, rows, *, ratings=False):
    panel = QFrame()
    panel.setObjectName('group')
    layout = vertical(panel, 14, 10)
    layout.addWidget(text(title.upper(), 'groupTitle'))
    for key, value in rows:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        field = text(label(key), wrap=True)
        row_layout.addWidget(field, 1)
        shown = 'Missing' if value is None else json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        rating = text(shown, wrap=not ratings)
        if ratings:
            rating.setStyleSheet(f'color: {rating_color(value)}; font-weight: 700;')
        rating.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row_layout.addWidget(rating)
        layout.addWidget(row)
    return panel


class CardDialog(QWidget):
    finished = Signal()

    def accept(self):
        self.finished.emit()

    def __init__(self, repository, uid, cache, parent=None):
        super().__init__(parent)
        self.repository, self.uid, self.cache = repository, uid, cache
        self.setWindowTitle('Card details — ' + TITLE)
        self.setWindowIcon(app_icon())
        self.resize(1220, 810)
        self.setMinimumSize(900, 610)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(25)
        art_column = QWidget()
        art_column.setFixedWidth(280)
        art_layout = vertical(art_column, 0, 16)
        self.art = QLabel()
        self.art.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.art.setFixedSize(280, 405)
        self.art.setWordWrap(True)
        art_layout.addWidget(self.art)
        art_layout.addWidget(text('PLAYER VERSIONS', 'fieldLabel'))
        self.variants = QComboBox()
        self.variants.setAccessibleName('Player versions')
        for row in repository.variants(uid):
            self.variants.addItem(f"{row['overall'] if row['overall'] is not None else '—'} · {row['tier'] or 'Unknown'} · {row['collection'] or row['source_card_id']}", row['id'])
        self.variants.setCurrentIndex(self.variants.findData(uid))
        self.variants.currentIndexChanged.connect(lambda: self.render(self.variants.currentData()))
        art_layout.addWidget(self.variants)
        art_layout.addStretch()
        close = QPushButton('Back  ·  Esc')
        close.clicked.connect(self.accept)
        art_layout.addWidget(close)
        layout.addWidget(art_column)
        content = QWidget()
        right = vertical(content, 0, 12)
        heading = QHBoxLayout()
        names = QVBoxLayout()
        self.collection = text('', 'eyebrow', wrap=True)
        self.name = text('', 'detailName', wrap=True)
        self.chips = text('', 'muted', wrap=True)
        names.addWidget(self.collection)
        names.addWidget(self.name)
        names.addWidget(self.chips)
        heading.addLayout(names, 1)
        self.overall = text('')
        self.overall.setFixedSize(75, 75)
        self.overall.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading.addWidget(self.overall)
        right.addLayout(heading)
        self.tabs = QTabWidget()
        right.addWidget(self.tabs, 1)
        layout.addWidget(content, 1)
        self.render(uid)

    def render(self, uid):
        if not uid:
            return
        self.uid = uid
        card = self.repository.cards[uid]
        row = self.repository.row_by_id[uid]
        identity = card['identity']['values']
        color = TIER_COLORS.get(row['tier'], '#7e95aa')
        self.name.setText(row['name'])
        self.collection.setText((row['collection'] or row['theme'] or 'COLLECTION UNKNOWN').upper())
        self.chips.setText('  ·  '.join(str(v) for v in (row['tier'], row['position'], row['secondary_position'], row['year'], row['team']) if v))
        if row['jersey_number'] is not None:
            self.chips.setText(self.chips.text() + f"  ·  Jersey #{row['jersey_number']}")
        self.overall.setText(f"{row['overall'] if row['overall'] is not None else '—'}\nOVR")
        self.overall.setStyleSheet(f'border: 2px solid {color}; border-radius: 37px; color: {color}; font-weight: 800; font-size: 21px;')
        pixmap = self.cache.get(row['image_path'], QSize(560, 810))
        self.art.clear()
        self.art.setStyleSheet('background: #0b1520;')
        if not pixmap.isNull():
            self.art.setPixmap(pixmap.scaled(self.art.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        else:
            self.art.setText('ARTWORK UNAVAILABLE\n\n' + row['name'])
            self.art.setStyleSheet(f'border: 1px solid {color}; color: {color}; background: #0d1a28; font-size: 17px;')
        selected = self.tabs.currentIndex()
        while self.tabs.count():
            widget = self.tabs.widget(0)
            self.tabs.removeTab(0)
            widget.deleteLater()
        self.tabs.addTab(self._attributes(card), 'Attributes')
        self.tabs.addTab(self._tendencies(card), 'Tendencies')
        self.tabs.addTab(self._badges(card), 'Badges')
        self.tabs.addTab(self._information(card), 'Information')
        self.tabs.addTab(self._hot_zones(card), 'Hot zones')
        self.tabs.setCurrentIndex(max(0, selected))

    def _attributes(self, card):
        body = QWidget()
        layout = vertical(body, 8, 12)
        groups = attribute_groups(card)
        grid = QGridLayout()
        grid.setSpacing(12)
        columns = [QVBoxLayout(), QVBoxLayout()]
        for i, (title, rows) in enumerate(groups):
            columns[i % 2].addWidget(value_group(title, rows, ratings=True))
        for i, column in enumerate(columns):
            column.addStretch()
            grid.addLayout(column, 0, i)
            grid.setColumnStretch(i, 1)
        layout.addLayout(grid)
        if not groups:
            layout.addWidget(text('Attributes unavailable.', 'notice', wrap=True))
        layout.addStretch()
        return scroll_widget(body)

    def _tendencies(self, card):
        body = QWidget()
        layout = vertical(body, 8, 12)
        values = section_values(card, 'tendencies')
        if values:
            rows = sorted(values.items())
            half = (len(rows) + 1) // 2
            grid = QHBoxLayout()
            grid.addWidget(value_group('Tendencies', rows[:half], ratings=True), 1)
            grid.addWidget(value_group('Tendencies continued', rows[half:], ratings=True), 1)
            layout.addLayout(grid)
        else:
            layout.addWidget(text('Tendencies unavailable.', 'notice', wrap=True))
        layout.addStretch()
        return scroll_widget(body)

    def _badges(self, card):
        body = QWidget()
        layout = vertical(body, 8, 14)
        badges = section_values(card, 'badges')
        grid = QHBoxLayout()
        columns = [QVBoxLayout(), QVBoxLayout()]
        for i, level in enumerate(BADGE_LABELS):
            keys = [name for name, value in badges.items() if value == level]
            names = [label(name) for name in keys]
            panel = QFrame()
            panel.setObjectName('group')
            panel_layout = vertical(panel, 14, 10)
            heading = text(BADGE_LABELS[level].upper())
            heading.setStyleSheet(f'color: {BADGE_COLORS[level]}; font-size: 12px; font-weight: 800;')
            panel_layout.addWidget(heading)
            panel_layout.addWidget(text(f'{len(names)} badge' + ('' if len(names) == 1 else 's'), 'muted'))
            if names:
                for name in sorted(names):
                    badge = text(name, wrap=True)
                    badge.setStyleSheet(f'color: {BADGE_COLORS[level]};')
                    panel_layout.addWidget(badge)
            else:
                panel_layout.addWidget(text('None', 'muted', wrap=True))
            columns[i % 2].addWidget(panel)
        for column in columns:
            column.addStretch()
            grid.addLayout(column, 1)
        layout.addLayout(grid)
        unknown = [(name, value) for name, value in badges.items() if value not in BADGE_LABELS]
        if unknown:
            layout.addWidget(value_group('Other badges', unknown))
        layout.addStretch()
        return scroll_widget(body)

    def _information(self, card):
        body = QWidget()
        layout = vertical(body, 8, 12)
        identity = card['identity']['values']
        fields = ('name', 'overall', 'tier', 'primary_position', 'secondary_position', 'team', 'theme', 'collection', 'card_year', 'represented_season', 'height', 'weight', 'age', 'origin', 'jersey_number')
        metadata = section_values(card, 'gameplay_metadata')
        information = [(k, identity.get(k)) for k in fields]
        information.append(('shooting_hand', metadata.get('dominant_hand')))
        layout.addWidget(value_group('Card information', information))
        available = [(k, v) for k, v in metadata.items() if v is not None and k != 'dominant_hand']
        if available:
            layout.addWidget(value_group('Additional information', available))
        layout.addStretch()
        return scroll_widget(body)

    def _hot_zones(self, card):
        body = QWidget()
        layout = vertical(body, 10, 12)
        states = zone_states(card)
        if states is not None:
            try:
                chart = QLabel()
                chart.setObjectName('hotZoneChart')
                image = render_hot_zones(states)
                chart.setPixmap(QPixmap.fromImage(image))
                chart.setFixedSize(image.size())
                chart.setAccessibleName('Card-specific hot-zone chart. Blue cold, gray neutral, red hot.')
                chart.setToolTip('Blue: cold · Gray: neutral · Red: hot')
                layout.addWidget(chart, alignment=Qt.AlignmentFlag.AlignHCenter)
                legend = text('Blue = cold (0)   ·   Gray = neutral (1)   ·   Red = hot (2)', 'muted')
                legend.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(legend)
            except (OSError, ValueError) as error:
                layout.addWidget(text(f'Hot-zone artwork could not be loaded: {error}', 'notice', wrap=True))
        else:
            layout.addWidget(text('Hot zones unavailable.', 'notice', wrap=True))
        layout.addStretch()
        return scroll_widget(body)

class ViewerWindow(QMainWindow):
    loaded = Signal()

    def __init__(self, path):
        super().__init__()
        self.path, self.repository = Path(path), None
        self.cache = ThumbnailCache()
        self.setWindowTitle(TITLE + ' — Offline Card Archive')
        self.setWindowIcon(app_icon())
        self.resize(1560, 960)
        self.setMinimumSize(1030, 690)
        self.model = CardModel(self)
        central = QWidget()
        root = vertical(central, 0, 0)
        self.setCentralWidget(central)
        self.studio = None
        self.team_workspaces = {}
        self.pages = QStackedWidget()
        top = QFrame()
        top.setObjectName('topbar')
        header = QHBoxLayout(top)
        header.setContentsMargins(26, 17, 26, 17)
        header.setSpacing(14)
        brand = QVBoxLayout()
        brand.setSpacing(3)
        brand.addWidget(text('MyTEAM Archive', 'brand'))
        brand.addWidget(text('NBA 2K17  ·  Offline card collection', 'muted'))
        header.addLayout(brand)
        header.addStretch()
        self.archive_button = QPushButton('Card archive')
        self.archive_button.clicked.connect(lambda: self.show_workspace(False))
        self.studio_button = QPushButton('Card Studio')
        self.studio_button.clicked.connect(lambda: self.show_workspace(True))
        self.search = QLineEdit()
        self.search.setPlaceholderText('Search players, collections, themes…')
        self.search.setAccessibleName('Search cards')
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(300)
        self.search.setMaximumWidth(510)
        self.search.setToolTip('Search all saved cards · Ctrl+F or /')
        search_policy = self.search.sizePolicy()
        search_policy.setRetainSizeWhenHidden(True)
        self.search.setSizePolicy(search_policy)
        header.addWidget(self.search, 1)
        header.addWidget(self.archive_button)
        header.addWidget(self.studio_button)
        root.addWidget(top)
        team_bar = QHBoxLayout()
        team_bar.setContentsMargins(26, 8, 26, 8)
        team_bar.addWidget(text('TEAM MODES', 'eyebrow'))
        self.team_buttons = {}
        for kind, title in [('custom', 'Create a Team'), ('random', 'Random Team'), ('draft', 'Draft a Team')]:
            button = QPushButton(title)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, k=kind: self.show_team(k))
            self.team_buttons[kind] = button
            team_bar.addWidget(button)
        self.custom_manager = QPushButton('Manage custom cards…')
        self.custom_manager.clicked.connect(self.manage_custom_cards)
        team_bar.addWidget(self.custom_manager)
        team_bar.addStretch()
        root.addLayout(team_bar)
        root.addWidget(self.pages, 1)
        archive_page = QWidget()
        self.archive_page = archive_page
        self.pages.addWidget(archive_page)
        root = vertical(archive_page, 0, 0)
        body = QHBoxLayout()
        body.setContentsMargins(25, 24, 20, 5)
        body.setSpacing(23)
        sidebar = QFrame()
        sidebar.setObjectName('sidebar')
        side = vertical(sidebar, 17, 15)
        filter_heading = QHBoxLayout()
        filter_heading.addWidget(text('Filters', 'groupTitle'))
        filter_heading.addStretch()
        self.reset = QPushButton('Reset')
        self.reset.setObjectName('reset')
        self.reset.clicked.connect(self.reset_filters)
        filter_heading.addWidget(self.reset)
        side.addLayout(filter_heading)
        self.filters = {}
        for key, title, default in (('tier', 'Gem tier', 'All tiers'), ('team', 'Franchise', 'All franchises'),
                ('position', 'Position', 'All positions'), ('theme', 'Theme', 'All themes'),
                ('collection', 'Collection', 'All collections'), ('custom_card', 'Custom card', 'All cards'), ('minimum_overall', 'Minimum overall', 'Any overall')):
            field = QVBoxLayout()
            field.setSpacing(6)
            field.addWidget(text(title.upper(), 'fieldLabel'))
            combo = QComboBox()
            combo.addItem(default, '')
            combo.setAccessibleName(title)
            combo.setMaxVisibleItems(18)
            combo.currentIndexChanged.connect(self.apply_filters)
            self.filters[key] = combo
            field.addWidget(combo)
            side.addLayout(field)
        self.filters['custom_card'].addItem('Yes', 'yes')
        self.filters['custom_card'].addItem('No', 'no')
        for value in (99, 95, 90, 85, 80, 75, 70):
            self.filters['minimum_overall'].addItem(f'{value}+', value)
        side.addStretch()
        sidebar_scroll = scroll_widget(sidebar)
        sidebar_scroll.setFixedWidth(232)
        body.addWidget(sidebar_scroll)
        content = QFrame()
        content.setObjectName('content')
        collection = vertical(content, 0, 14)
        bar = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(5)
        heading.addWidget(text('CARD DATABASE', 'eyebrow'))
        self.count = text('Loading archive…', 'count')
        heading.addWidget(self.count)
        self.subtitle = text('Loading cards…', 'muted', wrap=True)
        heading.addWidget(self.subtitle)
        bar.addLayout(heading, 1)
        self.sort = QComboBox()
        self.sort.addItems([DEFAULT_SORT, LOW_OVR_SORT, 'Player: A to Z', 'Card ID'])
        self.sort.setToolTip('Gem tiers rank highest first, then OVR within each tier. Pink Diamond precedes Diamond even with a lower OVR. Onyx free agents follow the ranked gem tiers.')
        self.sort.setAccessibleName('Sort cards')
        self.sort.currentIndexChanged.connect(self.apply_filters)
        bar.addWidget(self.sort)
        self.import_cards = QPushButton('Import custom cards…')
        self.import_cards.setToolTip('Import Card Studio’s Export Card + Data (.2k17custom), keeping the original PNG and all authored data.')
        self.import_cards.setEnabled(False)
        self.import_cards.clicked.connect(self.import_custom_cards)
        bar.addWidget(self.import_cards)
        self.reload = QPushButton('Reload archive')
        self.reload.clicked.connect(self.load_archive)
        bar.addWidget(self.reload)
        collection.addLayout(bar)
        self.loading = QProgressBar()
        self.loading.setRange(0, 0)
        collection.addWidget(self.loading)
        self.empty = text('', 'muted', wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.hide()
        collection.addWidget(self.empty)
        self.gallery = Gallery()
        self.gallery.setModel(self.model)
        self.gallery.setItemDelegate(CardDelegate(self.cache, self.gallery))
        self.gallery.clicked.connect(self.open_card)
        self.gallery.open_requested.connect(self.open_card)
        collection.addWidget(self.gallery, 1)
        body.addWidget(content, 1)
        root.addLayout(body, 1)
        bottom = QHBoxLayout()
        bottom.setContentsMargins(27, 10, 27, 12)
        self.status = text('', 'muted')
        bottom.addWidget(self.status, 1)
        open_button = QPushButton('Open archive…')
        open_button.setObjectName('reset')
        open_button.clicked.connect(self.choose_archive)
        bottom.addWidget(open_button)
        bottom.addSpacing(18)
        bottom.addWidget(text('MyTEAM Archive + Card Studio', 'muted'))
        root.addLayout(bottom)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(180)
        self.search_timer.timeout.connect(self.apply_filters)
        self.search.textChanged.connect(lambda: self.search_timer.start())
        for shortcut in ('Ctrl+F', '/'):
            QShortcut(QKeySequence(shortcut), self).activated.connect(self.focus_search)
        QShortcut(QKeySequence('Ctrl+R'), self).activated.connect(self.load_archive)
        self.overlay_history = []
        self.overlay = QFrame(central)
        self.overlay.setObjectName('sidebar')
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(18, 18, 18, 18)
        self.overlay_pages = QStackedWidget()
        overlay_layout.addWidget(self.overlay_pages)
        self.overlay.hide()
        self.overlay_escape = QShortcut(QKeySequence('Esc'), self)
        self.overlay_escape.setEnabled(False)
        self.overlay_escape.activated.connect(self.close_overlay)
        QTimer.singleShot(0, self.load_archive)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'overlay'):
            self.overlay.setGeometry(self.centralWidget().rect())

    def show_overlay(self, panel, on_close=None):
        self.overlay_history.append((panel, on_close, QApplication.focusWidget()))
        self.overlay_pages.addWidget(panel)
        self.overlay_pages.setCurrentWidget(panel)
        self.overlay.setGeometry(self.centralWidget().rect())
        self.overlay.show()
        self.overlay.raise_()
        self.overlay_escape.setEnabled(True)
        panel.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        panel.setFocus()
        return panel

    def close_overlay(self):
        if not self.overlay_history:
            return
        if getattr(self.overlay_history[-1][0], 'busy', False):
            return
        panel, callback, focus = self.overlay_history.pop()
        self.overlay_pages.removeWidget(panel)
        panel.hide()
        panel.deleteLater()
        if self.overlay_history:
            self.overlay_pages.setCurrentWidget(self.overlay_history[-1][0])
        else:
            self.overlay.hide()
            self.overlay_escape.setEnabled(False)
        if callback:
            callback()
        if focus:
            try:
                focus.setFocus()
            except RuntimeError:
                pass

    def show_card(self, uid):
        panel = CardDialog(self.repository, uid, self.cache, self.overlay_pages)
        panel.finished.connect(self.close_overlay)
        return self.show_overlay(panel)

    def show_workspace(self, studio):
        if studio and self.studio is None:
            from nba2k17_card_studio.app import locations, load_presets
            from nba2k17_card_studio.ui import StudioWindow
            try:
                pack, resources = locations()
                self.studio = StudioWindow(pack, resources, load_presets(resources, self.repository))
                self.studio.setParent(self.pages)
                self.studio.setWindowFlags(Qt.WindowType.Widget)
                self.studio.exit_action.triggered.disconnect()
                self.studio.exit_action.setText('Return to card archive')
                self.studio.exit_action.triggered.connect(lambda: self.show_workspace(False))
                for action in self.studio.actions.values():
                    action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
                self.pages.addWidget(self.studio)
            except Exception as exc:
                self.studio = None
                QMessageBox.critical(self, 'Card Studio', str(exc))
                return
        self.pages.setCurrentWidget(self.studio if studio else self.archive_page)
        self.search.setVisible(not studio)

    def show_team(self, kind):
        if self.repository is None:
            return
        from .team_ui import TeamWorkspace
        if kind not in self.team_workspaces:
            workspace = TeamWorkspace(self, kind)
            self.team_workspaces[kind] = workspace
            self.pages.addWidget(workspace)
        self.pages.setCurrentWidget(self.team_workspaces[kind])
        self.search.hide()

    def closeEvent(self, event):
        manager = getattr(self, 'injection_manager', None)
        if manager is not None and manager.busy:
            event.ignore()
            return
        if any(getattr(panel, 'busy', False) for panel, _, _ in self.overlay_history):
            event.ignore()
            return
        if self.studio is not None and not self.studio.close():
            event.ignore()
            return
        if manager is not None: manager.shutdown()
        event.accept()

    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def choose_archive(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Open a saved NBA 2K17 audit or catalog', str(self.path.parent), 'Card archives (*.json *.sqlite)')
        if path:
            self.path = Path(path)
            self.load_archive()

    @Slot()
    def manage_custom_cards(self):
        if self.repository is None:
            return
        from .custom_cards import inventory, set_state, set_teams, set_years, parse_year
        dialog = QDialog(self)
        dialog.setWindowTitle('Manage custom cards')
        dialog.resize(1020, 560)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('Click a card name, OVR, or status to select it. Team and year changes save automatically.'))
        table = QTableWidget(0, 5)
        table.setObjectName('customCardTable')
        table.setHorizontalHeaderLabels(['Select card', 'OVR', 'Status', 'Team', 'Year'])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        selection_delegate = CustomCardSelectionDelegate(table)
        for column in (0, 1, 2):
            table.setItemDelegateForColumn(column, selection_delegate)
        table.setStyleSheet('''
            QTableWidget::indicator { width: 18px; height: 18px; border: 1px solid #839bb1; background: #101c29; }
            QTableWidget::indicator:checked { background: #60dce5; border: 2px solid #e8eff7; }
        ''')
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        table.setColumnWidth(4, 100)
        table.verticalHeader().setDefaultSectionSize(46)
        layout.addWidget(table)
        selection_count = QLabel('')
        selection_count.setObjectName('customCardSelectionCount')
        layout.addWidget(selection_count)
        notice = QLabel('')
        notice.setObjectName('customCardActionResult')
        layout.addWidget(notice)
        action_buttons = {}
        def selected_rows():
            return [row for row in range(table.rowCount())
                    if table.item(row, 0) is not None and
                    table.item(row, 0).checkState() == Qt.CheckState.Checked]
        def update_selection():
            rows = selected_rows()
            selection_count.setText(f'{len(rows)} card(s) selected' if rows else 'No cards selected. Click a card to select it.')
            for action, button in action_buttons.items():
                status = {'disabled': 'Enabled', 'enabled': 'Disabled'}.get(action)
                button.setEnabled(any(status is None or table.item(row, 2).text() == status for row in rows))
        table.itemChanged.connect(update_selection)
        def update_card_label(item, card):
            name = ' · '.join(str(value) for value in (card['name'], card.get('year')) if value)
            item.setText(name)
            item.setToolTip('\n'.join(str(value) for value in (name, card.get('collection'), card['id']) if value))
        def change_year(editor, card, item):
            try:
                year = parse_year(editor.text())
                if year == parse_year(card.get('year')):
                    return
                set_years(self.path, {card['id']: year})
                card['year'] = year
                editor.setText(str(year) if year is not None else '')
                update_card_label(item, card)
                changed[0] = True
                notice.setText(f"Saved: {card['name']} — {year if year is not None else 'year cleared'}")
            except Exception as exc:
                editor.setText(str(card['year']) if card.get('year') is not None else '')
                QMessageBox.warning(dialog, 'Custom card year', str(exc))
        def change_team(combo, card):
            team = combo.currentData()
            if not team or team == card.get('team'):
                return
            try:
                set_teams(self.path, {card['id']: team})
                card['team'] = team
                changed[0] = True
                notice.setText(f"Saved: {card['name']} — {team}")
            except Exception as exc:
                combo.blockSignals(True)
                combo.setCurrentIndex(max(0, combo.findData(card.get('team') or '')))
                combo.blockSignals(False)
                QMessageBox.warning(dialog, 'Custom card team', str(exc))
        def populate():
            entries = inventory(self.path)
            teams = sorted({row['team'] for row in getattr(self.repository, 'rows', []) if row.get('team')} |
                           {card['team'] for card in entries if card.get('team')}, key=str.casefold)
            table.blockSignals(True)
            table.setRowCount(len(entries))
            for row, card in enumerate(entries):
                item = QTableWidgetItem()
                update_card_label(item, card)
                item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(Qt.CheckState.Unchecked)
                item.setData(Qt.ItemDataRole.UserRole, card['id'])
                table.setItem(row, 0, item)
                for column, value in ((1, str(card['overall'])), (2, 'Disabled' if card['disabled'] else 'Enabled')):
                    cell = QTableWidgetItem(value)
                    cell.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                    table.setItem(row, column, cell)
                combo = QComboBox()
                if not card.get('team'):
                    combo.addItem('Choose team…', '')
                for team in teams:
                    combo.addItem(team, team)
                combo.setCurrentIndex(max(0, combo.findData(card.get('team') or '')))
                combo.currentIndexChanged.connect(lambda index, c=combo, entry=card: change_team(c, entry))
                table.setCellWidget(row, 3, combo)
                year_editor = QLineEdit(str(card['year']) if card.get('year') is not None else '')
                year_editor.setObjectName('customCardYear')
                year_editor.setPlaceholderText('Year')
                year_editor.setToolTip('Enter a four-digit year, or leave blank to clear it. Press Enter or leave this field to save.')
                year_editor.editingFinished.connect(lambda e=year_editor, entry=card, i=item: change_year(e, entry, i))
                table.setCellWidget(row, 4, year_editor)
            table.blockSignals(False)
            update_selection()
        changed = [False]
        def apply(action):
            status = {'disabled': 'Enabled', 'enabled': 'Disabled'}.get(action)
            ids = [table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in selected_rows()
                   if status is None or table.item(row, 2).text() == status]
            if not ids:
                notice.setText('Select cards to apply this action.')
                return
            if action == 'deleted' and QMessageBox.question(dialog, 'Permanently remove cards',
                    f'Delete {len(ids)} selected custom card(s), including their imported artwork and editable packages? This cannot be undone here.',
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            try:
                set_state(self.path, ids, action)
                changed[0] = True
                populate()
                verb = {'disabled': 'Disabled', 'enabled': 'Re-enabled', 'deleted': 'Removed'}[action]
                notice.setText(f'{verb} {len(ids)} custom card(s).')
            except Exception as exc:
                QMessageBox.warning(dialog, 'Custom cards', str(exc))
        actions = QHBoxLayout()
        for title, action in [('Disable selected', 'disabled'), ('Re-enable selected', 'enabled'), ('Permanently remove selected', 'deleted')]:
            button = QPushButton(title)
            button.setAutoDefault(False)
            button.clicked.connect(lambda checked=False, a=action: apply(a))
            action_buttons[action] = button
            actions.addWidget(button)
        done = QPushButton('Done')
        done.setAutoDefault(False)
        done.clicked.connect(dialog.accept)
        actions.addWidget(done)
        layout.addLayout(actions)
        populate()
        dialog.exec()
        if changed[0]:
            self.load_archive()

    def import_custom_cards(self):
        if self.repository is None:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, 'Import Card Studio cards', '',
            'Card Studio card + data (*.2k17custom)')
        if not paths:
            return
        from .custom_import import import_package
        imported, duplicates, errors = [], [], []
        for path in paths:
            try:
                result = import_package(path, self.repository)
                (duplicates if result['duplicate'] else imported).append(result['name'])
            except Exception as exc:
                errors.append(f'{Path(path).name}: {exc}')
        message = f'{len(imported)} card(s) imported. {len(duplicates)} already imported.'
        if imported or duplicates:
            self.reset_filters()
            self.load_archive()
            message += '\nOriginal PNGs, all authored data, and editable Studio packages are saved with the catalog.'
        if errors:
            QMessageBox.warning(self, 'Custom card import', message + '\n\n' + '\n'.join(errors))
        else:
            QMessageBox.information(self, 'Custom card import', message)

    @Slot()
    def load_archive(self):
        if hasattr(self, 'loader') and self.reload.isEnabled() is False:
            return
        self.reload.setEnabled(False)
        self.import_cards.setEnabled(False)
        self.loading.show()
        self.status.setText('Reading saved archive…')
        self.loader = LoadArchive(self.path)
        self.loader.signals.ready.connect(self.archive_loaded)
        self.loader.signals.error.connect(self.archive_error)
        QThreadPool.globalInstance().start(self.loader)

    @Slot(object)
    def archive_loaded(self, repository):
        self.repository = repository
        if self.studio is not None:
            from nba2k17_card_studio.app import load_presets
            self.studio.presets = load_presets(self.studio.resources, repository)
        for button in self.team_buttons.values():
            button.setEnabled(True)
        for workspace in self.team_workspaces.values():
            from .teams import TeamSession
            try:
                workspace.session = TeamSession.from_data(repository.rows, workspace.session.data())
            except ValueError:
                QMessageBox.information(self, 'Team archive changed', 'A team references cards unavailable in this archive. Its workspace has been reset; saved lineup files are unchanged.')
                workspace.session = TeamSession(repository.rows, workspace.kind)
            workspace.filter_cards()
            workspace.refresh()
        self.cache.items.clear()
        facets = repository.facets()
        for key, options in facets.items():
            combo = self.filters[key]
            previous = combo.currentData()
            first = combo.itemText(0)
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(first, '')
            for option in options:
                combo.addItem(option, option)
            combo.setCurrentIndex(max(0, combo.findData(previous)))
            combo.blockSignals(False)
        self.reload.setEnabled(True)
        self.import_cards.setEnabled(True)
        self.loading.hide()
        self.empty.hide()
        self.status.setText('')
        self.apply_filters()
        self.loaded.emit()

    @Slot(str)
    def archive_error(self, error):
        self.reload.setEnabled(True)
        self.import_cards.setEnabled(self.repository is not None)
        self.loading.hide()
        self.empty.setText('The archive could not be opened.\n' + error + '\nUse “Open archive…” to select the saved audit or cards.sqlite.')
        self.empty.show()
        self.status.setText('Archive unavailable — no files were changed')
        if self.repository is None:
            self.count.setText('No archive loaded')

    @Slot()
    def apply_filters(self):
        if self.repository is None:
            return
        options = {key: combo.currentData() or (0 if key == 'minimum_overall' else '') for key, combo in self.filters.items()}
        rows = self.repository.filter(ViewerFilter(search=self.search.text(), sort=self.sort.currentText(), **options))
        self.model.replace(rows)
        self.gallery.scrollToTop()
        self.count.setText(f'{len(rows):,} cards')
        self.subtitle.setText('Click a card to view details')
        self.empty.setText('No cards match these filters. Try a broader search or Reset.')
        self.empty.setVisible(not rows)

    def reset_filters(self):
        self.search.clear()
        for combo in self.filters.values():
            combo.blockSignals(True)
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
        self.sort.setCurrentIndex(0)
        self.apply_filters()

    def open_card(self, index):
        row = index.data(Qt.ItemDataRole.UserRole)
        if row and self.repository:
            self.show_card(row['id'])


def main(argv=None):
    parser = argparse.ArgumentParser(description='NBA 2K17 MyTEAM archive, Card Studio and roster injection')
    parser.add_argument('--source', '--database', type=Path, default=None)
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--ui-smoke-test', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--migrate-from', type=Path, help='Copy personal data from an older portable app folder')
    args = parser.parse_args(argv)
    path = args.source or default_source()
    if args.migrate_from:
        from .release_update import migrate
        result = migrate(args.migrate_from, project_root())
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
        return 0
    if args.smoke_test:
        repository = ArchiveRepository(path)
        result = {**repository.summary, 'source': str(path), 'database_rebuilt': False, 'listener': False}
        from .speech_identity import source_value
        from .runtime_fields import runtime_definition
        speech_field = next(f for f in runtime_definition()['fields'] if f['field'] == 'gameplay_metadata.speech_id')
        assert (speech_field['offset'], speech_field['size']) == (0x200, 2)
        speech_templates = [c['injection']['reusable_roster_template'] for c in repository.cards.values()
                            if c['injection'].get('reusable_roster_template')]
        assert all(t['defaults']['gameplay_metadata']['speech_id'] == source_value(t) for t in speech_templates)
        result['source_speech_cards'] = len(speech_templates)
        result['speech_field_width'] = 2
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(result, indent=2), encoding='utf-8')
        if sys.stdout:
            print(json.dumps(result, indent=2))
        return 0
    if os.name == 'nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Local.NBA2K17.MyTEAM.Viewer')
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName(TITLE)
    app.setWindowIcon(app_icon())
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = ViewerWindow(path)
    if args.ui_smoke_test:
        def verify_ui():
            try:
                assert window.model.rowCount() > 0
                first = next((r['id'] for r in window.model.rows if r['image_path'] and r['name'] != '2KMTCentral 2nd Birthday' and
                    any(v == 'HALL_OF_FAME' for v in window.repository.cards[r['id']]['badges']['values'].values())), window.model.rows[0]['id'])
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    app.processEvents()
                    window.grab().save(str(args.report.parent / 'gallery.png'))
                dialog = window.show_card(first)
                assert dialog.tabs.count() == 5 and not dialog.isWindow()
                dialog.show()
                app.processEvents()
                if args.report:
                    dialog.grab().save(str(args.report.parent / 'card-details.png'))
                    dialog.tabs.setCurrentIndex(2)
                    app.processEvents()
                    dialog.grab().save(str(args.report.parent / 'badges.png'))
                    dialog.tabs.setCurrentIndex(4)
                    app.processEvents()
                    assert dialog.findChild(QLabel, 'hotZoneChart') is not None
                    dialog.grab().save(str(args.report.parent / 'hot-zones.png'))
                window.close_overlay()
                hot_zone_examples = [
                    ('hot-zones-barkley.png', next((r['id'] for r in window.model.rows if r['name'] == 'Charles Barkley'), None)),
                    ('hot-zones-mixed.png', next((r['id'] for r in window.model.rows if set(zone_states(window.repository.cards[r['id']]) or ()) == {0, 1, 2}), None)),
                ]
                for filename, example in hot_zone_examples:
                    if example:
                        example_dialog = window.show_card(example)
                        example_dialog.tabs.setCurrentIndex(4)
                        example_dialog.show()
                        app.processEvents()
                        assert example_dialog.findChild(QLabel, 'hotZoneChart') is not None
                        if args.report:
                            example_dialog.grab().save(str(args.report.parent / filename))
                        window.close_overlay()
                estimated = next((r['id'] for r in window.model.rows if r.get('estimated_badges')), None)
                if estimated:
                    estimated_dialog = window.show_card(estimated)
                    estimated_dialog.tabs.setCurrentIndex(2)
                    estimated_dialog.show()
                    app.processEvents()
                    assert not any('estimated' in widget.text().lower() or '†' in widget.text() or '*' in widget.text() for widget in estimated_dialog.findChildren(QLabel))
                    if args.report:
                        estimated_dialog.grab().save(str(args.report.parent / 'estimated-badges.png'))
                    window.close_overlay()
                if args.report:
                    args.report.parent.mkdir(parents=True, exist_ok=True)
                    nav_positions = [b.geometry() for b in (window.archive_button, window.studio_button)]
                    window.studio_button.click()
                    app.processEvents()
                    assert nav_positions == [b.geometry() for b in (window.archive_button, window.studio_button)]
                    assert window.studio is not None and not window.studio.isWindow()
                    assert window.pages.currentWidget() is window.studio
                    studio = window.studio
                    studio.doc.spec['first_name'] = 'Workspace retained'
                    window.archive_button.click()
                    assert window.pages.currentIndex() == 0
                    window.studio_button.click()
                    assert window.studio is studio and studio.doc.spec['first_name'] == 'Workspace retained'
                    parent = next(c for c in studio.presets['cards'] if c['summary']['name'] == 'Stephen Curry')
                    studio.doc.import_parent(parent)
                    studio.commit()
                    assert studio.doc.overall_result().displayed is not None
                    assert studio.overall.isReadOnly()
                    assert studio.overall.text() == f'{studio.doc.overall_result().normalized * 100:.2f}'
                    studio.edit_data()
                    assert len([k for s,k in studio.data_editor.fields if s == 'tendencies']) == 84
                    assert len([k for s,k in studio.data_editor.fields if s == 'signatures']) == 44
                    assert len(studio.data_editor.document.data['tendencies']['values']) == 84
                    assert not any('NBA 2K16 animation' in label.text() for label in studio.data_editor.findChildren(QLabel))
                    studio.data_editor.tabs.setCurrentIndex(2)
                    app.processEvents()
                    window.grab().save(str(args.report.parent / 'integrated-studio-tendencies.png'))
                    studio.data_editor.fields[('attributes', 'intangibles')].setValue(25)
                    assert studio.data_editor.overall_live.text() == f"OVR {studio.data_editor.document.overall_result().normalized * 100:.2f}"
                    studio.finish_data()
                    assert studio.doc.spec['overall'] == studio.doc.overall_result().displayed
                    assert studio.doc.authored_data()['identity']['values']['overall'] == studio.doc.spec['overall']
                    studio.doc.spec['first_name'] = ''
                    studio.timer.stop()
                    studio.start_render()
                    if studio.worker: studio.worker.wait(10000)
                    app.processEvents()
                    window.grab().save(str(args.report.parent / 'integrated-studio.png'))
                    studio.saved = studio.doc.clone()
                    window.archive_button.click()
                    for kind in ('custom', 'random', 'draft'):
                        window.team_buttons[kind].click()
                        app.processEvents()
                        workspace = window.team_workspaces[kind]
                        assert window.pages.currentWidget() is workspace
                        if kind == 'custom':
                            workspace.session.add(str(window.repository.rows[0]['source_card_id']))
                        elif kind == 'random':
                            assert len(workspace.session.lineup()) == 5
                        else:
                            from .teams import DRAFT_KEYS
                            workspace.open_pack('starter|PG')
                            app.processEvents()
                            saved_options = list(workspace.session.options)
                            assert not workspace.choice_dialog.isWindow()
                            assert workspace.choice_dialog.width() > window.width() * .9
                            window.grab().save(str(args.report.parent / 'draft-choices.png'))
                            window.close_overlay()
                            workspace.open_pack('starter|PG')
                            assert workspace.session.options == saved_options
                            workspace.pick(saved_options[0])
                            for key in DRAFT_KEYS[1:-1]:
                                pack = workspace.session.open_pack(key)
                                workspace.session.pick(str(pack[0]['source_card_id']))
                            workspace.open_pack('bonus')
                            app.processEvents()
                            from .team_ui import LineupPreview
                            assert len(workspace.choice_dialog.findChild(LineupPreview).rows) == 12
                            window.grab().save(str(args.report.parent / 'diamond-round-preview.png'))
                            workspace.pick(workspace.session.options[0])
                            assert len(workspace.session.lineup()) == 13
                        workspace.refresh()
                        app.processEvents()
                        workspace.export_image(args.report.parent / f'{kind}-team-export.png')
                        window.grab().save(str(args.report.parent / f'{kind}-team.png'))
                        workspace.inject_button.click()
                        app.processEvents()
                        panel = window.overlay_pages.currentWidget()
                        assert not panel.isWindow() and panel.table.columnCount() == 4
                        assert not panel.inject_button.isEnabled() and panel.kind.count() == 2
                        assert panel.browse.isEnabled() and panel.find_button.isEnabled()
                        window.grab().save(str(args.report.parent / f'{kind}-injection.png'))
                        window.close_overlay()
                        window.studio_button.click()
                        assert window.pages.currentWidget() is studio
                        window.archive_button.click()
                        assert window.pages.currentWidget() is window.archive_page
                    args.report.write_text(json.dumps({'ui_ready': True, 'cards': window.model.rowCount(), 'tabs': 5, 'team_modes': 3, 'injection_panels': 3, 'full_draft_verified': True, 'native_studio_overall': True, 'database_rebuilt': False}), encoding='utf-8')
                app.exit(0)
            except Exception:
                if args.report:
                    args.report.write_text(json.dumps({'ui_ready': False, 'error': traceback.format_exc()}), encoding='utf-8')
                app.exit(1)
        window.loaded.connect(verify_ui)
        QTimer.singleShot(30000, lambda: app.exit(2))
    window.show()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
