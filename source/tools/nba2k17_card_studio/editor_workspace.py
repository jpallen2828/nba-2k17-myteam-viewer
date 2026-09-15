"""Full-window player editor with the 2K16 workspace organization and 2K17 data."""
from copy import deepcopy
import json
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QScrollArea, QFrame, QGroupBox, QGridLayout, QFormLayout, QComboBox,
    QCheckBox, QLineEdit, QSpinBox, QDoubleSpinBox, QCompleter, QMessageBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView)
from nba2k17_viewer.hot_zones import ZONE_NAMES

BADGE_CHOICES = [('None','NONE'),('Bronze','BRONZE'),('Silver','SILVER'),('Gold','GOLD'),('Hall of Fame','HALL_OF_FAME')]
COLORS = {'BRONZE':'#c48951','SILVER':'#c3ccd6','GOLD':'#f2d168','HALL_OF_FAME':'#c08bea','PERSONALITY':'#70c9e9'}
ATTRIBUTE_GROUPS = [
    ('Inside Scoring','standing_layup driving_layup post_fadeaway post_hook post_control draw_foul'),
    ('Jump Shooting','shot_close contested_shot_mid_range open_shot_mid_range off_dribble_shot_mid_range contested_shot_three open_shot_three off_dribble_shot_three free_throw'),
    ('Playmaking','ball_control passing_vision passing_iq passing_accuracy'),
    ('Rebounding','boxout offensive_rebound defensive_rebound'),
    ('Defense','lateral_quickness pass_perception block shot_contest steal defensive_consistency on_ball_defense_iq pick_and_roll_defense_iq help_defense_iq low_post_defense_iq'),
    ('Athleticism','standing_dunk driving_dunk contact_dunk speed speed_with_ball acceleration vertical strength stamina hustle shot_iq hands reaction_time offensive_consistency potential intangibles'),
    ('Durability','overall_durability')]
TENDENCY_GROUPS = [
    ('Freelance','shoot standing_layup driving_layup standing_dunk driving_dunk flashy_dunk alley_oop putback_dunk crash'),
    ('Inside Shots','hop_step_layup euro_step_layup spin_layup floater step_through_shot under_basket_shot shot_close'),
    ('Close Shot Locations','shot_close_left shot_close_middle shot_close_right'),
    ('Mid-Range Shot Locations','shot_mid_range_left shot_mid_range_left_center shot_mid_range_center shot_mid_range_right_center shot_mid_range_right'),
    ('Three-Point Shot Locations','shot_three_left shot_three_left_center shot_three_center shot_three_right_center shot_three_right'),
    ('Jump Shots','shot_mid_range shot_three contested_jumper stepback_jumper spin_jumper pull_up_in_transition use_glass'),
    ('Drive Setup','drive drive_right_vs_left triple_threat_pump_fake triple_threat_jab_step triple_threat_idle triple_threat_shoot setup_with_sizeup setup_with_hesitation no_setup_dribble'),
    ('Driving Dribble Moves','driving_crossover driving_spin driving_stepback driving_half_spin driving_double_crossover driving_behind_the_back driving_dribble_hesitation driving_in_and_out no_driving_dribble_move attack_strong_on_drive'),
    ('Passing / Usage','dish_to_open_man touches'),
    ('Post','post_up post_back_down post_aggressive_backdown post_face_up post_spin post_drive post_drop_step post_hop_step post_stepback_shot post_fade_left post_fade_right post_hook_left post_hook_right post_hop_shot post_shimmy_shot post_up_and_under roll_vs_pop shoot_from_post'),
    ('Passing / Defense','flashy_pass alley_oop_pass pass_interception take_charge on_ball_steal contest_shot block_shot foul hard_foul')]


class DataEditor(QWidget):
    done_requested = Signal()

    def __init__(self, document, presets, parent=None):
        super().__init__(parent)
        self.document = document.clone()
        from nba2k17_viewer.hand_policy import match_dunk_hand
        match_dunk_hand(self.document.data)
        from .option_defaults import complete_options
        complete_options(self.document.data)
        self.presets = presets
        from nba2k17_viewer.runtime_fields import runtime_definition
        definitions=runtime_definition()['fields']
        from pathlib import Path
        from nba2k17_viewer import runtime_fields
        self.table_schema=json.loads((Path(runtime_fields.__file__).parent/'assets/studio-player-fields.json').read_text(encoding='utf-8'))
        for section in ('gear','signatures'):
            self.document.data.setdefault(section,{'values':{},'statuses':{}})
            self.presets['schema'].setdefault(section,{})
            for entry in self.table_schema[section]:
                self.presets['schema'][section].setdefault(entry['key'],{})['label']=entry['label']
        self.signature_fields={f['field'].split('.',1)[1]:f for f in definitions if f['field'].startswith('signatures.')}
        for field in definitions:
            if field['field'].startswith('tendencies.'):
                key=field['field'].split('.',1)[1]
                self.presets['schema']['tendencies'].setdefault(key,{'label':key.replace('_',' ').title()})
        self.presets['schema'].setdefault('signatures',{})
        for key in self.signature_fields:
            self.presets['schema']['signatures'].setdefault(key,{'label':key.replace('_',' ').title()})
        self.document.data.setdefault('signatures',{'values':{},'statuses':{}})
        self.text_callbacks = []
        self.fields = {}
        root = QVBoxLayout(self)
        top = QHBoxLayout()
        title = QLabel('Player Data'); title.setObjectName('playerDataTitle'); title.setStyleSheet('font-size: 22px; font-weight: 700;')
        top.addWidget(title)
        self.overall_live = QLabel(); self.overall_live.setObjectName('overallLiveDisplay')
        self.overall_live.setStyleSheet('font-size: 22px; font-weight: 700; color: #35d7ff;')
        top.addWidget(self.overall_live); top.addSpacing(12); top.addStretch(1)
        self.overall_note = QLabel()
        self.overall_note.setStyleSheet('color: #9fb0c5;'); top.addWidget(self.overall_note)
        done = QPushButton('Done'); done.setObjectName('primaryButton'); done.clicked.connect(self.done_requested.emit); top.addWidget(done)
        root.addLayout(top)
        self.tabs = QTabWidget(); root.addWidget(self.tabs)
        self.populate()
        dunk = self.fields.get(('gameplay_metadata', 'dominant_dunk_hand'))
        if dunk is not None:
            dunk.setEnabled(False)
            dunk.setToolTip('Matches shooting hand automatically.')

    def page(self, name):
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget(); layout = QVBoxLayout(content); scroll.setWidget(content)
        self.tabs.addTab(scroll, name)
        return layout

    def keys(self, section):
        return sorted(set(self.presets['schema'][section]) | {k for k in self.document.data[section]['values'] if not k.startswith('source_')})

    def label(self, section, key):
        text = self.presets['schema'][section].get(key, {}).get('label', key)
        if text == key or '_' in text: text = key.replace('_',' ').title().replace(' Iq',' IQ').replace(' Cm',' (cm)')
        if section == 'hot_zones' and key.startswith('hot_zone_'):
            text = ZONE_NAMES[int(key[-2:])-1].replace('_',' ').title()
        return text

    def populate(self):
        from .document import apply_selected_promotion
        apply_selected_promotion(self.document.data, self.document.spec)
        tab = max(0,self.tabs.currentIndex())
        while self.tabs.count():
            old=self.tabs.widget(0);self.tabs.removeTab(0);old.deleteLater()
        self.fields={};self.text_callbacks=[]
        self.overall_live.setText(f"OVR {self.document.spec['overall']}")
        self.build_vitals(self.page('Vitals'))
        self.grouped(self.page('Attributes'),'attributes',ATTRIBUTE_GROUPS)
        self.grouped(self.page('Tendencies'),'tendencies',TENDENCY_GROUPS)
        for title, accessory in [('Shoes / Gear',False),('Accessories',True)]:
            page=self.page(title)
            entries=[f for f in self.table_schema['gear'] if f['group'].startswith(('Accessories','Accessory'))==accessory]
            self.editable_tables(page,'gear',entries)
            note=QLabel('Gear edits are saved with your card. Game injection mapping is pending testing.')
            note.setWordWrap(True); page.addWidget(note);page.addStretch(1)
        animations=self.page('Signature Animations')
        self.editable_tables(animations,'signatures',self.table_schema['signatures'])
        note=QLabel('Select an animation for each field.')
        note.setWordWrap(True);animations.addWidget(note)
        animations.addStretch(1)
        badges=self.page('Badges')
        personality=[k for k in self.keys('badges') if self.presets['schema']['badges'].get(k,{}).get('personality')]
        group=QGroupBox('Personality Badges (blue, no tier)');grid=QGridLayout(group)
        for i,key in enumerate(personality):grid.addWidget(self.control('badges',key),i//4,i%4)
        badges.addWidget(group)
        self.grid_group(badges,'Gameplay Badges','badges',[k for k in self.keys('badges') if k not in personality])
        badges.addStretch(1)
        hot=self.page('Hot Zones');self.grid_group(hot,'Court Zones','hot_zones',self.keys('hot_zones'))
        hint=QLabel('Cold = blue   •   Neutral = gray   •   Hot = red   •   Unknown is not saved as neutral')
        hint.setStyleSheet('color: #9fb0c5;');hot.addWidget(hint);hot.addStretch(1)
        self.tabs.setCurrentIndex(min(tab,self.tabs.count()-1))
        self.update_overall()

    def update_overall(self):
        result = self.document.recalculate_overall()
        self.overall_live.setText(f"OVR {result.normalized * 100:.2f}" if result.normalized is not None else f"OVR {self.document.spec['overall']}")
        self.overall_live.setToolTip(result.tooltip)
        self.overall_note.setText('OVR updates automatically.' if result.displayed is not None
                                  else 'Enter attributes, height and durability to calculate OVR.')
        self.overall_note.setToolTip(result.tooltip)
        widget = self.fields.get(('identity', 'overall'))
        if widget is not None:
            widget.blockSignals(True)
            widget.setDecimals(2 if result.normalized is not None else 0)
            widget.setValue(result.normalized * 100 if result.normalized is not None else self.document.spec['overall'])
            widget.setReadOnly(result.displayed is not None)
            widget.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons if result.displayed is not None
                                    else QSpinBox.ButtonSymbols.UpDownArrows)
            widget.setToolTip(result.tooltip)
            widget.blockSignals(False)

    def build_vitals(self, layout):
        group=QGroupBox('Card Identity');form=QFormLayout(group)
        for key,label in [('name','Player name'),('card_year','Season / year')]:form.addRow(label,self.visual(key) if key=='name' else self.control('identity',key))
        self.parent_combo=QComboBox();self.parent_combo.setEditable(True);self.parent_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.parent_combo.setObjectName('parentCardCombo');self.parent_combo.setMaxVisibleItems(18)
        self.parent_combo.addItem('Select an existing card to import…',None)
        selected=0
        for i,card in enumerate(self.presets['cards']):
            row=card['summary']
            label=f"{row['name']} • {row.get('overall') or '?'} {row.get('tier') or ''} • {row.get('year') or ''} {row.get('team') or ''} • {row.get('collection') or row.get('theme') or ''} [#{row['source_card_id']}]"
            self.parent_combo.addItem(label,i)
            if row.get('source_card_id')==self.document.parent.get('source_card_id'):selected=i+1
        self.parent_combo.setCurrentIndex(selected)
        completer=QCompleter(self.parent_combo.model(),self.parent_combo)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive);completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self.parent_combo.setCompleter(completer);self.parent_combo.activated.connect(self.import_parent)
        form.addRow('Import data from existing card',self.parent_combo)
        for key,label in [('overall','Overall'),('tier','Gem tier'),('theme','Theme')]:form.addRow(label,self.visual(key))
        for key,label in [('collection','Collection'),('franchise','Franchise')]:form.addRow(label,self.control('identity',key))
        layout.addWidget(group)
        group=QGroupBox('Player Vitals');grid=QGridLayout(group)
        order=['primary_position','secondary_position','height','weight','height_cm','age','college','country','jersey_number']
        for i,key in enumerate(order):
            grid.addWidget(QLabel(self.label('identity',key)),i//2,(i%2)*2)
            grid.addWidget(self.visual(key) if key in ('primary_position','jersey_number') else self.control('identity',key),i//2,(i%2)*2+1)
        layout.addWidget(group)
        self.grid_group(layout,'In-Game Vitals / Play Types','gameplay_metadata',self.keys('gameplay_metadata'),columns=2)
        shown=set(order)|{'name','first_name','last_name','overall','tier','theme','card_year','collection','franchise'}
        remaining=[k for k in self.keys('identity') if k not in shown]
        self.grid_group(layout,'Additional Recovered Vitals','identity',remaining,columns=2)
        note=QLabel('Hover a field for its source status. Your edits are marked CUSTOM_AUTHORED.\nPartially checked personality badges mean Unknown, not absent.')
        note.setWordWrap(True);note.setStyleSheet('color: #9fb0c5;');layout.addWidget(note);layout.addStretch(1)

    def visual(self,key):
        spec_key={'name':None,'primary_position':'position'}.get(key,key)
        value=' '.join(x for x in (self.document.spec['first_name'],self.document.spec['last_name']) if x) if key=='name' else self.document.spec[spec_key]
        if key in ('tier','theme','primary_position'):
            from .document import TIERS
            widget=QComboBox()
            if key=='tier':options=[(x.replace('_',' ').title(),x) for x in TIERS]
            elif key=='primary_position':options=[(x,x) for x in ('PG','SG','SF','PF','C')]
            else:
                from .app import locations
                pack,_=locations();assets=json.loads((pack/'assets.json').read_text(encoding='utf-8'))
                from .main_layout import promotion_options
                options=promotion_options(assets)
            for text,data in options:widget.addItem(text,data)
            widget.setCurrentIndex(widget.findData(value))
            widget.currentIndexChanged.connect(lambda:self.visual_changed(key,widget.currentData()))
        elif key=='overall':
            widget=QDoubleSpinBox();widget.setDecimals(0);widget.setRange(0,99);widget.setValue(value);widget.setKeyboardTracking(False)
            widget.valueChanged.connect(lambda v:self.visual_changed(key,v))
        else:
            widget=QLineEdit(str(value));widget.setMaxLength(3 if key=='jersey_number' else 90)
            callback=lambda:self.visual_changed(key,widget.text().strip())
            widget.editingFinished.connect(callback);self.text_callbacks.append(callback)
        self.fields[('identity',key)]=widget
        widget.setToolTip('Custom card text / appearance. Parent import does not replace this.')
        return widget

    def visual_changed(self,key,value):
        if key=='name':
            old=' '.join(x for x in (self.document.spec['first_name'],self.document.spec['last_name']) if x)
            if old==value:return
            parts=value.split(' ',1);self.document.spec['first_name']=parts[0] if len(parts)>1 else '';self.document.spec['last_name']=parts[-1]
        else:self.document.spec[{'primary_position':'position'}.get(key,key)]=int(value) if key=='overall' else value
        if key=='theme':
            from .document import apply_selected_promotion
            apply_selected_promotion(self.document.data,self.document.spec)
            collection=self.fields.get(('identity','collection'))
            if collection is not None:
                collection.setText(value)
                self.refresh_tooltip('identity','collection')
        if key in ('overall','primary_position'):self.update_overall()

    def import_parent(self,index):
        selected=self.parent_combo.itemData(index)
        if selected is None:return
        self.flush()
        # Like 2K16, preserve authored identity while borrowing gameplay data.
        identity=deepcopy(self.document.data['identity'])
        self.document.import_parent(self.presets['cards'][selected])
        for key,status in identity['statuses'].items():
            if status=='CUSTOM_AUTHORED':
                self.document.data['identity']['values'][key]=identity['values'].get(key)
                self.document.data['identity']['statuses'][key]=status
        self.populate()

    def grouped(self,layout,section,groups):
        keys=self.keys(section);used=set()
        for title,members in groups:
            selected=[k for k in members.split() if k in keys];used.update(selected)
            self.grid_group(layout,title,section,selected)
        self.grid_group(layout,'Additional Recovered Fields',section,[k for k in keys if k not in used])
        layout.addStretch(1)

    def grid_group(self,layout,title,section,keys,columns=3):
        if not keys:return
        group=QGroupBox(title);grid=QGridLayout(group)
        for i,key in enumerate(keys):
            label=QLabel(self.label(section,key));label.setWordWrap(True)
            grid.addWidget(label,i//columns,(i%columns)*2)
            grid.addWidget(self.control(section,key),i//columns,(i%columns)*2+1)
        for column in range(columns):grid.setColumnStretch(column*2+1,1)
        layout.addWidget(group)

    def control(self,section,key):
        value=self.document.data[section]['values'].get(key)
        entry=self.presets['schema'][section].get(key,{})
        from scanner.player_codec import is_unknown_badge_value
        if section=='badges' and (is_unknown_badge_value(value) or
                self.document.data[section]['statuses'].get(key) in ('MISSING','CONFLICT')):
            value='NONE'
            self.document.data[section]['values'][key]=value
            self.document.data[section]['statuses'][key]='CUSTOM_AUTHORED'
        if section=='badges' and entry.get('personality'):
            widget=QCheckBox(self.label(section,key));widget.setTristate(False);widget.setStyleSheet('color: #70c9e9;')
            state=Qt.CheckState.Unchecked if value in ('NONE','UNEQUIPPED',False) else Qt.CheckState.Checked
            widget.setCheckState(state)
            widget.checkStateChanged.connect(lambda state:self.changed(section,key,'PERSONALITY' if state==Qt.CheckState.Checked else 'NONE'))
        elif section in ('badges','hot_zones'):
            widget=QComboBox()
            choices=BADGE_CHOICES if section=='badges' else [('Unknown',None),('Cold',0),('Neutral',1),('Hot',2)]
            for text,data in choices:
                widget.addItem(text,data)
                if data in COLORS:widget.setItemData(widget.count()-1,QColor(COLORS[data]),Qt.ItemDataRole.ForegroundRole)
            widget.setCurrentIndex(max(0,widget.findData(value)))
            widget.currentIndexChanged.connect(lambda:self.changed(section,key,widget.currentData()))
        elif section=='signatures' and key in self.signature_fields and (value is None or type(value) is int):
            widget=QSpinBox();widget.setRange(-1,self.signature_fields[key]['raw_max'])
            widget.setSpecialValueText('None selected');widget.setKeyboardTracking(False)
            widget.setValue(value if type(value) is int else -1)
            widget.valueChanged.connect(lambda v:self.changed(section,key,None if v<0 else v))
        elif section in ('attributes','tendencies'):
            widget=QSpinBox();widget.setRange(-1,max(100 if section=='tendencies' else 99,value if type(value)is int else 0))
            widget.setSpecialValueText('Unknown');widget.setKeyboardTracking(False);widget.setValue(value if type(value)is int else -1)
            widget.valueChanged.connect(lambda v:self.changed(section,key,None if v<0 else v))
        else:
            text='' if value is None else json.dumps(value) if isinstance(value,(list,dict)) else str(value)
            widget=QLineEdit(text);widget.setPlaceholderText('Unknown');widget.setMaxLength(2048)
            callback=lambda:self.text_changed(section,key,widget,value)
            widget.editingFinished.connect(callback);self.text_callbacks.append(callback)
        self.fields[(section,key)]=widget
        widget.setAccessibleName(self.label(section,key));self.refresh_tooltip(section,key)
        return widget

    def editable_tables(self, layout, section, entries):
        groups={}
        for entry in entries:groups.setdefault(entry['group'],[]).append(entry)
        for title,rows in groups.items():
            box=QGroupBox(title); box_layout=QVBoxLayout(box)
            table=QTableWidget(len(rows),2)
            table.setObjectName('playerTable_'+section+'_'+title.replace(' ','_'))
            table.setHorizontalHeaderLabels(['Field','Value'])
            table.verticalHeader().hide()
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeMode.Stretch)
            table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeMode.Stretch)
            table.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
            for row,entry in enumerate(rows):
                table.setItem(row,0,QTableWidgetItem(entry['label']))
                table.setCellWidget(row,1,self.option_control(section,entry))
                table.setRowHeight(row,34)
            table.setFixedHeight(table.horizontalHeader().sizeHint().height()+34*len(rows)+4)
            box_layout.addWidget(table);layout.addWidget(box)

    def option_control(self,section,entry):
        key=entry['key'];value=self.document.data[section]['values'].get(key)
        widget=QComboBox();widget.setEditable(False)
        # Use a scrolling list rather than the platform's full-height menu popup.
        widget.setStyleSheet('QComboBox { combobox-popup: 0; }')
        widget.setMaxVisibleItems(18)
        widget.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        widget.setMinimumContentsLength(20)
        options=entry.get('options',[])
        labels=[option['label'] for option in options]
        for option in options:
            label=option['label']
            if labels.count(label)>1:label+=f" (ID {option['value']})"
            widget.addItem(label,option['value'])
        if value is not None:
            index=widget.findData(value)
            if index<0:
                # Keep older typed options and unknown numeric IDs intact. Selecting
                # a catalog option replaces them; merely opening/closing never does.
                text=('ID '+str(value)) if type(value) is int else str(value)
                if section=='gear' and key in ('shoe_home','shoe_away') and type(value) is int:
                    from nba2k17_viewer.gear_layout import shoe_model_labels
                    text=shoe_model_labels().get(str(value),text)
                widget.addItem(text,value);index=widget.count()-1
            widget.setCurrentIndex(index)
        def commit(*_):
            self.changed(section,key,widget.currentData())
        self.fields[(section,key)]=widget
        widget.setAccessibleName(entry['label'])
        widget.currentIndexChanged.connect(commit)
        self.text_callbacks.append(commit)
        self.refresh_tooltip(section,key)
        return widget

    def refresh_tooltip(self,section,key):
        widget=self.fields[(section,key)]
        status=self.document.data[section]['statuses'].get(key,'MISSING')
        display_status = 'Reconstructed' if 'estimat' in str(status).lower() else status
        widget.setToolTip(f'{self.label(section,key)}\nSource / status: {display_status}')
        if section=='badges' and isinstance(widget,QComboBox):widget.setStyleSheet('QComboBox { color: '+COLORS.get(widget.currentData(),'#e5ebf3')+'; }')

    def changed(self,section,key,value):
        if value==self.document.data[section]['values'].get(key):return
        self.document.data[section]['values'][key]=value
        self.document.data[section]['statuses'][key]='CUSTOM_AUTHORED' if value is not None else 'MISSING'
        self.refresh_tooltip(section,key)
        if section=='gameplay_metadata' and key=='dominant_hand':
            from nba2k17_viewer.hand_policy import match_dunk_hand
            match_dunk_hand(self.document.data)
            dunk = self.fields.get(('gameplay_metadata', 'dominant_dunk_hand'))
            if dunk is not None:
                dunk.blockSignals(True)
                if isinstance(dunk, QComboBox):
                    dunk.setCurrentIndex(dunk.findData(value))
                else:
                    dunk.setText('' if value is None else str(value))
                dunk.blockSignals(False)
        if section=='identity' and key in ('height','height_cm'):
            from .overall_calculator import height_from_text, number
            other='height_cm' if key=='height' else 'height'
            cm=height_from_text(value) if key=='height' else number(value)
            converted=cm if other=='height_cm' else (f'{cm:g} cm' if cm is not None else None)
            self.document.data['identity']['values'][other]=converted
            self.document.data['identity']['statuses'][other]='CUSTOM_AUTHORED'
            widget=self.fields.get(('identity',other))
            if widget is not None:
                widget.setText('' if converted is None else str(converted))
                self.refresh_tooltip('identity',other)
        if section=='attributes' or (section=='identity' and key in ('height','height_cm')):
            self.update_overall()

    def text_changed(self,section,key,widget,original):
        text=widget.text().strip();value=text or None
        if value is not None:
            try:
                if section=='identity' and key=='height_cm':
                    value=float(value)
                    if not 120<=value<=260:raise ValueError('Invalid height')
                elif isinstance(original,(list,dict)):value=json.loads(value)
                elif type(original)in (int,float):value=type(original)(value)
            except ValueError:
                widget.setText(json.dumps(original) if isinstance(original,(list,dict)) else str(original));return
        self.changed(section,key,value)

    def flush(self):
        for callback in self.text_callbacks:callback()
