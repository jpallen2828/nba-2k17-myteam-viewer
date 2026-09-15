"""Single-canvas desktop workflow modelled on the 2K16 Card Studio."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
from PIL import Image, ImageDraw
from PySide6.QtCore import Qt, QThread, Signal, QTimer, QPointF, QRectF, QSize, QSettings
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPainter, QPixmap, QIcon
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QProgressDialog, QPushButton,
    QScrollArea, QSlider, QSpinBox, QSplitter, QVBoxLayout, QWidget)
from .document import Document, export_png, load_portrait, open_document, png_bytes, save_document
from .render import render_card
from .data_editor import DataEditor

from .main_layout import STYLE, build_layout, fill_logos, current_logo_options
from .canvas import Canvas, prepare_scene, scene_key


def qimage(image):
    image=image.convert('RGBA')
    return QImage(image.tobytes(),image.width,image.height,image.width*4,QImage.Format.Format_RGBA8888).copy()


class RenderWorker(QThread):
    result=Signal(int,object,str)
    def __init__(self,pack,doc,revision,parent):
        super().__init__(parent);self.pack=pack;self.doc=doc;self.revision=revision
    def run(self):
        try:self.result.emit(self.revision,prepare_scene(self.pack,self.doc),'')
        except Exception as exc:self.result.emit(self.revision,None,str(exc))


class BackgroundWorker(QThread):
    progress=Signal(str)
    result=Signal(object,str)
    def __init__(self,image,models,parent):super().__init__(parent);self.image=image;self.models=models
    def run(self):
        try:
            from .background import remove_background
            image=remove_background(self.image,self.models,self.progress.emit,self.isInterruptionRequested)
            self.result.emit(image,'')
        except Exception as exc:self.result.emit(None,str(exc))


class MaskCanvas(QWidget):
    """Small alpha-only repair brush; imported RGB pixels never change."""
    changed=Signal()
    def __init__(self,original,result):
        super().__init__();self.original=original;self.mask=result.getchannel('A');self.original_alpha=original.getchannel('A')
        self.mode='Erase';self.brush=30;self.target=QRectF();self.history=[];self.drawing=False;self.last=None
        self.setMinimumSize(550,450)
    def result(self):
        from PIL import ImageChops
        im=self.original.copy();im.putalpha(ImageChops.darker(self.mask,self.original_alpha));return im
    def paintEvent(self,event):
        p=QPainter(self);p.fillRect(self.rect(),QColor('#0e141d'))
        im=self.result();scale=min((self.width()-20)/im.width,(self.height()-20)/im.height)
        self.target=QRectF((self.width()-im.width*scale)/2,(self.height()-im.height*scale)/2,im.width*scale,im.height*scale)
        p.save();p.setClipRect(self.target)
        for y in range(int(self.target.top()),int(self.target.bottom())+16,16):
            for x in range(int(self.target.left()),int(self.target.right())+16,16):
                p.fillRect(x,y,16,16,QColor('#a5adb5' if (x//16+y//16)%2 else '#7c8792'))
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform);p.drawImage(self.target,qimage(im));p.restore()
    def stroke(self,position):
        if not self.target.contains(position):return
        scale=self.original.width/self.target.width();x=(position.x()-self.target.left())*scale;y=(position.y()-self.target.top())*scale
        radius=self.brush*scale/2;draw=ImageDraw.Draw(self.mask);color=0 if self.mode=='Erase' else 255
        if self.last:draw.line([self.last,(x,y)],fill=color,width=max(1,round(radius*2)))
        draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=color);self.last=(x,y);self.update()
    def mousePressEvent(self,event):
        if event.button()==Qt.MouseButton.LeftButton:
            self.history.append(self.mask.copy());self.history=self.history[-12:];self.drawing=True;self.last=None;self.stroke(event.position())
    def mouseMoveEvent(self,event):
        if self.drawing:self.stroke(event.position())
    def mouseReleaseEvent(self,event):self.drawing=False;self.last=None;self.changed.emit()
    def undo(self):
        if self.history:self.mask=self.history.pop();self.update()


class MaskEditor(QDialog):
    def __init__(self,original,result,parent):
        super().__init__(parent);self.resize(900,780);self.setWindowTitle('Review cutout — alpha-only editing')
        box=QVBoxLayout(self);box.addWidget(QLabel('Review the edges. Erase or restore with the brush; original player colors are preserved.'))
        self.canvas=MaskCanvas(original,result);box.addWidget(self.canvas,1)
        row=QHBoxLayout();mode=QComboBox();mode.addItems(['Erase','Restore']);mode.currentTextChanged.connect(lambda v:setattr(self.canvas,'mode',v));row.addWidget(mode)
        row.addWidget(QLabel('Brush size'));size=QSlider(Qt.Orientation.Horizontal);size.setRange(2,120);size.setValue(30)
        size.valueChanged.connect(lambda v:setattr(self.canvas,'brush',v));row.addWidget(size)
        undo=QPushButton('Undo brush');undo.clicked.connect(self.canvas.undo);row.addWidget(undo);box.addLayout(row)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText('Use cutout')
        buttons.accepted.connect(self.accept);buttons.rejected.connect(self.reject);box.addWidget(buttons)


class LogoPicker(QDialog):
    def __init__(self,pack,assets,current,parent):
        super().__init__(parent);self.setWindowTitle('Choose native team logo');self.resize(850,620)
        self.pack=pack;self.assets=assets;self.selected=current;layout=QVBoxLayout(self)
        layout.addWidget(QLabel('NBA teams A–Z, then Eastern Conference, Team USA, and Western Conference.'))
        row=QHBoxLayout();self.group=QComboBox();self.group.addItems(['Current']);row.addWidget(self.group)
        self.search=QLineEdit();self.search.setPlaceholderText('Search team name or native logo ID…');row.addWidget(self.search,1);layout.addLayout(row)
        self.list=QListWidget();self.list.setIconSize(QSize(54,54));layout.addWidget(self.list,1)
        self.group.currentIndexChanged.connect(self.fill);self.search.textChanged.connect(self.fill);self.list.itemDoubleClicked.connect(self.choose)
        buttons=QDialogButtonBox(QDialogButtonBox.StandardButton.Ok|QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.choose);buttons.rejected.connect(self.reject);layout.addWidget(buttons);self.fill()
    def fill(self,*_):
        self.list.clear();none=QListWidgetItem('No team logo');none.setData(Qt.ItemDataRole.UserRole,'');self.list.addItem(none)
        query=self.search.text().casefold()
        for logo in current_logo_options(self.assets):
            text=f"{logo['name']}  ·  {logo['aliases'][0]}"
            if query not in text.casefold():continue
            item=QListWidgetItem(text);item.setData(Qt.ItemDataRole.UserRole,logo['id'])
            pix=QPixmap(str(self.pack/logo['file'])).scaled(54,54,Qt.AspectRatioMode.KeepAspectRatio,Qt.TransformationMode.SmoothTransformation)
            item.setIcon(QIcon(pix));self.list.addItem(item)
            if self.selected==logo['id'] or self.selected in logo['aliases']:self.list.setCurrentItem(item)
        if self.list.currentRow()<0:self.list.setCurrentRow(0)
    def choose(self,*_):
        if self.list.currentItem():self.selected=self.list.currentItem().data(Qt.ItemDataRole.UserRole);self.accept()


class StudioWindow(QMainWindow):
    def __init__(self,pack,resources,presets):
        super().__init__();self.pack=pack;self.resources=resources;self.presets=presets
        self.assets=json.loads((pack/'assets.json').read_text(encoding='utf-8'))
        self.doc=Document();self.path=None;self.saved=self.doc.clone();self.history=[self.doc.clone()];self.history_index=0
        self.revision=0;self.completed=-1;self.worker=None;self.bg_worker=None;self.updating=False
        self.resize(1120,820);self.setStyleSheet(STYLE)
        self.requested_scene_key=None;self.displayed_scene_key=None
        self.timer=QTimer(self);self.timer.setSingleShot(True);self.timer.setInterval(16);self.timer.timeout.connect(self.start_render)
        self.create_actions();self.create_ui();self.sync();self.queue_render()

    def create_actions(self):
        self.actions={}
        entries=[('New','Ctrl+N',self.new),('Open Project','Ctrl+O',self.open),('Save Project','Ctrl+S',self.save),
            ('Default Save Folder...','',self.choose_save_folder),('Save As','Ctrl+Shift+S',lambda:self.save(True)),('Import Image','Ctrl+I',self.import_image),
            ('Remove Background','Ctrl+Shift+R',self.remove_background),('Delete PNG','',self.delete_image),
            ('Export PNG','Ctrl+E',self.export),('Export Card + Data','Ctrl+Shift+E',lambda:self.export(True)),
            ('Undo','Ctrl+Z',self.undo),('Redo','Ctrl+Y',self.redo),('Edit Attributes','',self.edit_data)]
        file=self.menuBar().addMenu('File');edit=self.menuBar().addMenu('Edit')
        for name,shortcut,callback in entries:
            action=QAction(name,self);action.triggered.connect(lambda checked=False,f=callback:f())
            if shortcut:action.setShortcut(shortcut)
            self.actions[name]=action
            if name not in ('Save As','Edit Attributes'):
                (edit if name in ('Undo','Redo') else file).addAction(action)
            else:self.addAction(action)
        self.exit_action=file.addAction('Exit');self.exit_action.triggered.connect(self.close)
        # Advanced 2K17 tools stay off the normal 2K16-style workspace.
        edit.addSeparator()
        repair=edit.addAction('Edit transparency mask');repair.triggered.connect(self.edit_mask)
        self.actions['Edit transparency mask']=repair
        samples=edit.addMenu('Recovered player cutouts')
        names={'11763':'Dirk Nowitzki','11798':'Andre Iguodala','11799':'Draymond Green','11800':'Klay Thompson','11801':'Kevin Durant','11802':'Stephen Curry'}
        for entry in self.assets['players']:
            pid=entry['id'];name=names.get(pid,pid);action=samples.addAction(name)
            action.triggered.connect(lambda checked=False,p=pid,n=name:self.use_sample(p,n))
        help_menu=self.menuBar().addMenu('Help')
        action=help_menu.addAction('Usage Instructions');action.setShortcut('F1');action.triggered.connect(self.help)
        about=help_menu.addAction('About')
        about.triggered.connect(lambda:QMessageBox.information(self,'About','NBA 2K17 Card Studio\u005cnOffline custom card authoring using original game assets.'))

    def button(self,name):
        button=QPushButton(name);action=self.actions[name];button.clicked.connect(action.trigger)
        action.changed.connect(lambda:button.setEnabled(action.isEnabled()))
        if name=='Export PNG':button.setObjectName('primaryButton')
        return button

    def create_ui(self):
        build_layout(self)

    def change_logo_category(self):
        if self.updating:return
        fill_logos(self)
        self.change('team_logo','')

    def sync_transform(self):
        self.updating=True
        for widget,key in [(self.x,'portrait_x'),(self.y,'portrait_y')]:
            widget.setValue(self.doc.spec[key])
        self.size.setValue(self.doc.spec['portrait_height']/1264*100)
        self.updating=False
        self.canvas.set_transform(self.doc.spec)

    def sync(self):
        self.doc.match_logo_team(self.assets)
        self.updating=True;s=self.doc.spec
        self.tier.setCurrentIndex(self.tier.findData(s['tier']))
        self.overall.setText(str(s['overall']));self.position.setText(s['position'])
        overall_result=self.doc.overall_result()
        if overall_result.normalized is not None:
            self.overall.setText(f'{overall_result.normalized * 100:.2f}')
        year = self.doc.data['identity']['values'].get('card_year')
        self.year.setText('' if year is None else str(year))
        self.overall.setReadOnly(overall_result.displayed is not None)
        self.overall.setToolTip(overall_result.tooltip)
        self.background.setCurrentIndex(self.background.findData(s['background']))
        self.promotion.setCurrentIndex(self.promotion.findData(s['theme']))
        self.duo.setChecked(s['dynamic_duo']);self.duo.setEnabled(s['theme']!='Free Agent')
        self.player_name.setText(' '.join(x for x in (s['first_name'],s['last_name']) if x))
        self.jersey.setText(s['jersey_number'])
        logo=next((r for r in self.assets['logos'] if s['team_logo']==r['id'] or s['team_logo'] in r['aliases']),None)
        self.logo_category.setCurrentText('Current')
        # Do not erase a saved, now-hidden logo or falsely show "No team logo".
        # The placeholder is not an extra selectable logo; replacing it is an
        # explicit user edit and Undo can still restore the original artwork.
        index=self.logo.findData(logo['id'] if logo else '')
        self.logo.setCurrentIndex(index)
        self.logo.setToolTip(f"Saved logo: {logo['name']}. Preserved in this project; choose a Current logo to replace it." if logo and index<0 else '')
        has=bool(self.doc.portrait or s['player_id'])
        if not has:self.player_status.setText('No player image imported')
        for name in ('Delete PNG','Remove Background','Edit transparency mask'):self.actions[name].setEnabled(has)
        self.actions['Undo'].setEnabled(self.history_index>0)
        self.actions['Redo'].setEnabled(self.history_index<len(self.history)-1)
        self.update_title();self.updating=False;self.sync_transform()

    def update_title(self):
        name=self.path.name if self.path else 'Untitled.2k17card'
        dirty=self.doc!=self.saved
        self.setWindowTitle(f'{name}{" *" if dirty else ""} — NBA 2K17 Card Studio')
        tier=self.doc.spec['tier'].replace('_',' ').title()
        self.summary.setText(f'{name}{" • unsaved" if dirty else ""} | {tier} | 650 × 950')

    def change_position(self,value):
        value=value.upper().strip()
        if value not in ('PG','SG','SF','PF','C'):
            self.text_warning.setText('Use PG, SG, SF, PF or C.');self.text_warning.show();self.sync();return
        self.text_warning.hide();self.change('position',value)

    def change_player_name(self):
        value=self.player_name.text().strip()
        if value==' '.join(x for x in (self.doc.spec['first_name'],self.doc.spec['last_name']) if x):return
        from .bitmap import font_assets
        chars=set.intersection(*(set(json.loads((self.pack/'fonts'/f/'font.json').read_text(encoding='utf-8'))['glyphs']) for f in ('stratum2_medium','stratum2_bold')))
        unsupported=sorted({ch for ch in value.upper()+value if ch not in chars})
        if unsupported:
            self.text_warning.setText('Original 2K17 font lacks: '+', '.join(unsupported));self.text_warning.show();return
        self.text_warning.hide()
        parts=value.split(' ',1)
        self.doc.spec['first_name']=parts[0] if len(parts)>1 else ''
        self.doc.spec['last_name']=parts[-1]
        self.commit()

    def change_year(self):
        if self.updating:return
        value = int(self.year.text()) if self.year.text().strip() else None
        identity = self.doc.data['identity']
        if identity['values'].get('card_year') == value:return
        identity['values']['card_year'] = value
        identity['statuses']['card_year'] = 'CUSTOM_AUTHORED' if value is not None else 'MISSING'
        self.commit()

    def change(self,key,value):
        if self.updating or self.doc.spec[key]==value:return
        if key in ('first_name','last_name','jersey_number'):
            fonts=['stratum2_medium','stratum2_bold'];chars=set.intersection(*(set(json.loads((self.pack/'fonts'/f/'font.json').read_text(encoding='utf-8'))['glyphs']) for f in fonts))
            unsupported=[ch for ch in value.upper()+value if ch not in chars]
            if unsupported:self.error('The original 2K17 font does not contain: '+', '.join(repr(c) for c in sorted(set(unsupported))));self.sync();return
        self.doc.spec[key]=value
        if key=='team_logo':self.doc.match_logo_team(self.assets)
        self.commit()

    def commit(self):
        self.doc.recalculate_overall()
        if self.doc!=self.history[self.history_index]:
            self.history=self.history[:self.history_index+1];self.history.append(self.doc.clone());self.history=self.history[-80:];self.history_index=len(self.history)-1
        self.sync();self.queue_render()

    def queue_render(self):
        self.canvas.set_transform(self.doc.spec)
        key=scene_key(self.doc)
        if key==self.requested_scene_key:return
        self.requested_scene_key=key;self.revision+=1
        self.timer.start()
    def start_render(self):
        if self.worker and self.worker.isRunning():return
        self.worker=RenderWorker(self.pack,self.doc.clone(),self.revision,self)
        self.worker.scene_key=self.requested_scene_key
        self.worker.result.connect(self.rendered);self.worker.finished.connect(self.render_finished);self.worker.start()
    def rendered(self,revision,image,error):
        self.completed=revision
        if revision!=self.revision:return
        if error:self.statusBar().showMessage('Preview error: '+error);return
        self.canvas.set_scene(image);self.canvas.set_transform(self.doc.spec)
        self.displayed_scene_key=self.requested_scene_key
        dimensions=image['dimensions']
        self.player_status.setText(f'Player image: {dimensions[0]} × {dimensions[1]} px • transparent PNG' if dimensions else 'No player image imported')
        self.statusBar().showMessage('Ready · Offline custom authoring · Original 2K17 assets · Catalog unchanged')
    def render_finished(self):
        old=self.worker;self.worker=None
        if old:old.deleteLater()
        if self.completed!=self.revision:self.timer.start()

    def move_portrait(self,dx,dy,commit):
        if not (self.doc.portrait or self.doc.spec['player_id']):return
        for key,delta in [('portrait_x',dx),('portrait_y',dy)]:self.doc.spec[key]=max(-8192,min(8192,self.doc.spec[key]+delta))
        if commit:self.commit()
        else:self.sync_transform()
    def scale_portrait(self,factor):self.change('portrait_height',max(63,min(6320,round(self.doc.spec['portrait_height']*factor))))
    def undo(self):
        self.finish_data()
        if self.history_index>0:self.history_index-=1;self.doc=self.history[self.history_index].clone();self.sync();self.queue_render()
    def redo(self):
        self.finish_data()
        if self.history_index<len(self.history)-1:self.history_index+=1;self.doc=self.history[self.history_index].clone();self.sync();self.queue_render()

    def choose_logo(self):
        picker=LogoPicker(self.pack,self.assets,self.doc.spec['team_logo'],self)
        if picker.exec()==QDialog.DialogCode.Accepted:self.change('team_logo',picker.selected)
    def use_sample(self,player_id,name):
        self.doc.portrait=None;self.doc.original_portrait=None
        self.doc.portrait_name=name+' — native cutout';self.doc.spec['player_id']=player_id
        self.doc.spec.update(portrait_x=39,portrait_y=5,portrait_height=1264);self.commit()

    def import_image(self):
        path,_=QFileDialog.getOpenFileName(self,'Import player image','','Images (*.png *.jpg *.jpeg *.webp *.bmp)')
        if not path:return
        try:self.doc.portrait=load_portrait(path)
        except Exception as exc:self.error(str(exc));return
        self.doc.original_portrait=None;self.doc.portrait_name=Path(path).name;self.doc.spec.update(player_id=None,portrait_x=39,portrait_y=5,portrait_height=900);self.commit()
    def delete_image(self):
        if not (self.doc.portrait or self.doc.spec['player_id']):return
        self.doc.portrait=None;self.doc.original_portrait=None;self.doc.portrait_name='';self.doc.spec['player_id']=None;self.commit()
    def current_portrait(self):
        if self.doc.portrait:return Image.open(BytesIO(self.doc.portrait)).convert('RGBA')
        player=next(r for r in self.assets['players'] if r['id']==self.doc.spec['player_id'])
        return Image.open(self.pack/player['file']).convert('RGBA')

    def edit_mask(self):
        if not (self.doc.portrait or self.doc.spec['player_id']):return
        current=self.current_portrait()
        original=Image.open(BytesIO(self.doc.original_portrait)).convert('RGBA') if self.doc.original_portrait else current
        self.review_cutout(original,current)
    def review_cutout(self,original,image):
        dialog=MaskEditor(original,image,self)
        if dialog.exec()==QDialog.DialogCode.Accepted:
            result=dialog.canvas.result()
            if result.getbbox() is None:self.error('The entire player is transparent; keep some of the player before applying.');return
            if not self.doc.original_portrait:self.doc.original_portrait=png_bytes(original)
            self.doc.portrait=png_bytes(result);self.doc.spec['player_id']=None;self.doc.portrait_name=(self.doc.portrait_name or 'Player').removesuffix(' (cutout)')+' (cutout)';self.commit()
    def remove_background(self):
        if self.bg_worker or not (self.doc.portrait or self.doc.spec['player_id']):return
        self.bg_original=Image.open(BytesIO(self.doc.original_portrait)).convert('RGBA') if self.doc.original_portrait else self.current_portrait()
        self.progress=QProgressDialog('Preparing local model…','Cancel',0,0,self)
        self.progress.setWindowTitle('Remove Background');self.progress.setWindowModality(Qt.WindowModality.ApplicationModal);self.progress.setMinimumDuration(0)
        self.bg_worker=BackgroundWorker(self.bg_original,self.resources/'models',self)
        self.bg_worker.progress.connect(self.progress.setLabelText);self.bg_worker.result.connect(self.background_done)
        self.bg_worker.finished.connect(self.background_finished);self.progress.canceled.connect(self.bg_worker.requestInterruption)
        self.bg_result=None;self.bg_worker.start();self.progress.show()
    def background_done(self,image,error):self.bg_result=(image,error)
    def background_finished(self):
        cancelled=self.bg_worker.isInterruptionRequested();self.progress.close();self.bg_worker.deleteLater();self.bg_worker=None
        image,error=self.bg_result or (None,'Background removal did not return a result.')
        if cancelled:return
        if error:self.error(error)
        elif image:self.review_cutout(self.bg_original,image)

    def edit_data(self):
        if self.data_editor:return
        self.data_editor=DataEditor(self.doc,self.presets,self)
        self.data_editor.done_requested.connect(self.finish_data)
        self.stack.addWidget(self.data_editor);self.stack.setCurrentWidget(self.data_editor)

    def finish_data(self):
        if self.data_editor is None:return
        editor=self.data_editor
        editor.flush()
        self.doc=editor.document;self.data_editor=None
        self.stack.setCurrentIndex(0);self.stack.removeWidget(editor);editor.deleteLater()
        self.commit()

    def error(self,text):QMessageBox.warning(self,'NBA 2K17 Card Studio',text)
    def guard(self):
        self.finish_data()
        if self.doc==self.saved:return True
        result=QMessageBox.question(self,'Unsaved project','Save the current project before continuing?',QMessageBox.StandardButton.Save|QMessageBox.StandardButton.Discard|QMessageBox.StandardButton.Cancel)
        return self.save() if result==QMessageBox.StandardButton.Save else result==QMessageBox.StandardButton.Discard
    def load(self,path):
        self.finish_data()
        doc=open_document(path)
        original=doc.clone()
        doc.recalculate_overall()
        # Validate external asset IDs and font characters before replacing state.
        render_card(self.pack,doc.render_spec())
        self.doc=doc;self.path=Path(path);self.saved=original;self.history=[doc.clone()];self.history_index=0;self.sync();self.queue_render()
    def new(self):
        if not self.guard():return
        self.doc=Document();self.path=None;self.saved=self.doc.clone();self.history=[self.doc.clone()];self.history_index=0;self.sync();self.queue_render()
    def open(self):
        if not self.guard():return
        path,_=QFileDialog.getOpenFileName(self,'Open editable project','','2K17 Cards (*.2k17card *.2k17custom)')
        if path:
            try:self.load(path)
            except Exception as exc:self.error('Could not open project: '+str(exc))
    def save_folder(self):
        folder = QSettings('NBA2K17', 'CardStudio').value('default_save_folder', str(Path.home()/'Documents'))
        return Path(str(folder))

    def choose_save_folder(self):
        selected = QFileDialog.getExistingDirectory(self, 'Choose default save folder', str(self.save_folder()))
        if selected:
            settings = QSettings('NBA2K17', 'CardStudio')
            settings.setValue('default_save_folder', selected)
            settings.sync()
            self.statusBar().showMessage('Default save folder: ' + selected)

    def save(self,save_as=False):
        self.finish_data()
        path=self.path
        if save_as or path is None or path.suffix!='.2k17card':
            selected,_=QFileDialog.getSaveFileName(self,'Save editable project',str((self.save_folder()/path.name) if save_as and path else (path or self.save_folder()/'Untitled.2k17card')),'2K17 Card Project (*.2k17card)')
            if not selected:return False
            path=Path(selected)
            if path.suffix.lower()!='.2k17card':path=path.with_suffix('.2k17card')
            if str(path)!=selected and path.exists() and QMessageBox.question(self,'Replace existing project?',f'{path.name} already exists. Replace it?')!=QMessageBox.StandardButton.Yes:return False
        try:save_document(path,self.doc)
        except Exception as exc:self.error('Could not save project: '+str(exc));return False
        self.path=path;self.saved=self.doc.clone();self.update_title();self.statusBar().showMessage('Project saved: '+str(path));return True
    def export(self,with_data=False):
        self.finish_data()
        suffix='.2k17custom' if with_data else '.png';name=' '.join(x for x in (self.doc.spec['first_name'],self.doc.spec['last_name']) if x) or 'Custom Card'
        name=''.join(c for c in name if c not in '<>:"/\\|?*')
        path,_=QFileDialog.getSaveFileName(self,'Export Card + Data' if with_data else 'Export PNG',str(self.save_folder()/(name+suffix)),f'2K17 Custom Card (*{suffix})' if with_data else 'PNG image (*.png)')
        if not path:return
        target=Path(path)
        if target.suffix.lower()!=suffix:target=target.with_suffix(suffix)
        if str(target)!=path and target.exists() and QMessageBox.question(self,'Replace existing export?',f'{target.name} already exists. Replace it?')!=QMessageBox.StandardButton.Yes:return
        try:
            image=render_card(self.pack,self.doc.render_spec())
            if with_data:save_document(target,self.doc,image)
            else:export_png(target,image)
        except Exception as exc:self.error('Export failed: '+str(exc));return
        self.statusBar().showMessage('Exported: '+str(target))
    def help(self):
        QMessageBox.information(self,'NBA 2K17 Card Studio — 1.0',
            'Create a custom card using recovered 2K17 templates, bitmap fonts, team logos and promotion icons.\n\n'
            'Import a player image or choose a recovered cutout. Drag it on the card, adjust X/Y and size, and edit the card text. Background removal runs locally; review and repair its transparency before accepting.\n\n'
            'Save Project embeds your portrait and data in .2k17card. Export PNG creates a transparent 650 × 950 image. Export Card + Data writes .2k17custom, containing card.png, data.json and the editable project. You can reopen both formats here.\n\n'
            'Edit Attributes imports from the recovered catalog without changing your card design. Source data is preserved. Custom edits never update official cards.\n\n'
            'OVR updates automatically from NBA 2K17 attributes, primary position, height and durability. Hover OVR to see the unrounded result or any missing inputs. Use the Card archive to import custom cards, build teams and access game injection.\n\n'
            'Background removal: BiRefNet General Lite (MIT). All processing is offline.')
    def closeEvent(self,event):
        if self.bg_worker and self.bg_worker.isRunning():
            self.error('Cancel background removal and wait for it to finish before closing.');event.ignore();return
        if not self.guard():event.ignore();return
        self.timer.stop()
        if self.worker:self.worker.wait(10000)
        event.accept()
