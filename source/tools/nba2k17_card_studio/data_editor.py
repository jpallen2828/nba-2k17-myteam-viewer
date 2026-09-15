"""2K17-only source-aware player data editor, independent of game injection."""
from copy import deepcopy
import json
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
    QHeaderView)
from nba2k17_viewer.hot_zones import render_hot_zones, ZONE_NAMES
from .document import SECTIONS

BADGE_CHOICES = [('None', 'NONE'), ('Bronze', 'BRONZE'),
                 ('Silver', 'SILVER'), ('Gold', 'GOLD'), ('Hall of Fame', 'HALL_OF_FAME')]
COLORS = {'BRONZE':'#c48951', 'SILVER':'#c3ccd6', 'GOLD':'#f2d168',
          'HALL_OF_FAME':'#c08bea', 'PERSONALITY':'#70c9e9'}
VISUAL_KEYS = {'name','first_name','last_name','overall','tier','primary_position','jersey_number','theme'}


class ParentPicker(QDialog):
    def __init__(self, presets, parent=None):
        super().__init__(parent); self.setWindowTitle('Import data from an existing 2K17 card')
        self.resize(880,600); self.presets=presets; self.selected=None
        layout=QVBoxLayout(self)
        note=QLabel('Copies card data only. Your card artwork and authored card text stay unchanged.\nImported values remain editable.')
        note.setWordWrap(True); layout.addWidget(note)
        self.search=QLineEdit(); self.search.setPlaceholderText('Search player, tier, overall, team or collection…')
        self.search.setAccessibleName('Search parent cards');layout.addWidget(self.search)
        self.cards=QListWidget(); layout.addWidget(self.cards)
        self.search.textChanged.connect(self.filter)
        self.cards.itemDoubleClicked.connect(self.choose)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Import selected data')
        buttons.accepted.connect(self.choose); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.filter('')

    def filter(self, text):
        self.cards.clear(); terms=text.casefold().split()
        for index, card in enumerate(self.presets['cards']):
            row=card['summary']
            label=f"{row['name']}  •  {row.get('overall') or '?'} {row.get('tier') or ''}  •  {row.get('year') or ''} {row.get('team') or ''}  •  {row.get('collection') or row.get('theme') or ''}  [#{row['source_card_id']}]"
            if all(term in label.casefold() for term in terms):
                item=QListWidgetItem(label); item.setData(Qt.ItemDataRole.UserRole,index);self.cards.addItem(item)
        if self.cards.count(): self.cards.setCurrentRow(0)

    def choose(self, *_):
        item=self.cards.currentItem()
        if item:
            self.selected=self.presets['cards'][item.data(Qt.ItemDataRole.UserRole)];self.accept()


from .editor_workspace import DataEditor
