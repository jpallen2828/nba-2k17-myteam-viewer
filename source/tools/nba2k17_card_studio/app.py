"""Executable entry point and reproducible self-check for the offline builder."""
import argparse
import gzip
import json
import os
from pathlib import Path
import sys
import traceback


def locations():
    if getattr(sys,'frozen',False):
        base=Path(sys._MEIPASS)
        return base/'studio_assets',base/'studio_resources'
    project=Path(__file__).resolve().parents[2]
    return project/'data/nba2k17/card_studio_assets/runtime',project/'build/card_studio/resources'


def load_presets(resources, repository=None):
    from .option_defaults import studio_data
    from copy import deepcopy
    from .document import SECTIONS
    with gzip.open(resources/'presets.json.gz','rt',encoding='utf-8') as stream:payload=json.load(stream)
    if repository is not None:
        payload['cards']=[{'summary':{k:v for k,v in row.items() if k not in ('image_path','search')},
                           'data':studio_data(repository.cards[row['id']])}
                          for row in repository.rows]
    for section in SECTIONS:payload['schema'].setdefault(section,{})
    for card in payload['cards']:
        for section in SECTIONS:
            data=card['data'].setdefault(section,{'values':{},'statuses':{}})
            for key in data['values']:
                if key.startswith('source_'):continue
                payload['schema'][section].setdefault(key,{'label':key.replace('_',' ').title()})
    return payload


def demo(window):
    window.doc.spec.update(first_name='Stephen',last_name='Curry',jersey_number='30',overall=99,
        tier='pink_diamond',team_logo='logo029',player_id='11802',dynamic_duo=True,theme='Current')
    window.doc.portrait_name='Stephen Curry — native cutout'
    window.commit()


def run_smoke(window,report,check_background=False):
    from io import BytesIO
    from PIL import Image
    from PySide6.QtWidgets import QApplication
    from .document import Document,open_document,save_document,export_png,TIERS,load_portrait
    from .data_editor import DataEditor,ParentPicker
    from .render import render_card
    report=Path(report);report.parent.mkdir(parents=True,exist_ok=True)
    result={'format':'nba2k17-card-studio-smoke/v1','passed':False}
    try:
        demo(window);window.timer.stop()
        outputs={}
        for tier in TIERS:
            spec=window.doc.render_spec();spec['tier']=tier
            image=render_card(window.pack,spec)
            assert image.size==(650,950) and image.mode=='RGBA'
            assert image.getchannel('A').getextrema()==(0,255)
            outputs[tier]=image.getbbox()
        result['tiers']=outputs
        # Embedded image round-trip, portable project + custom package.
        player=next(r for r in window.assets['players'] if r['id']=='11802')
        window.doc.portrait=load_portrait(window.pack/player['file']);window.doc.spec['player_id']=None
        parent=next(c for c in window.presets['cards'] if c['summary']['name']=='Stephen Curry')
        window.doc.import_parent(parent)
        image=render_card(window.pack,window.doc.render_spec())
        project=report.parent/'qa-curry.2k17card';package=report.parent/'qa-curry.2k17custom';png=report.parent/'qa-curry.png'
        save_document(project,window.doc);save_document(package,window.doc,image);export_png(png,image)
        loaded=open_document(project);custom=open_document(package)
        assert loaded.portrait==window.doc.portrait and loaded.spec==window.doc.spec
        assert loaded.authored_data()==window.doc.authored_data()
        assert render_card(window.pack,loaded.render_spec()).tobytes()==image.tobytes()
        assert custom.spec==loaded.spec and custom.data==loaded.data
        result['project_roundtrip']=True;result['custom_package_roundtrip']=True
        # Exercise actual UI controls without automating the host desktop.
        window.sync();window.commit();initial=window.doc.spec.copy()
        window.tier.setCurrentIndex(window.tier.findData('ruby'))
        assert window.doc.spec['tier']=='ruby'
        assert window.doc.spec['portrait_height']==initial['portrait_height']
        window.undo();assert window.doc.spec['tier']==initial['tier']
        window.redo();assert window.doc.spec['tier']=='ruby'
        window.delete_image();assert window.doc.portrait is None
        window.undo();assert window.doc.portrait is not None
        editor=DataEditor(window.doc,window.presets,window)
        promotion_names=[window.promotion.itemText(i) for i in range(1,window.promotion.count())]
        assert promotion_names==sorted(promotion_names,key=str.casefold)
        assert 'Free Agent' in promotion_names and 'Playoff Performers' in promotion_names
        theme=editor.fields[('identity','theme')]
        assert [theme.itemText(i) for i in range(theme.count())]==['No promotion logo']+promotion_names
        result['promotions_alphabetical']=True
        assert [window.logo_category.itemText(i) for i in range(window.logo_category.count())]==['Current']
        logo_names=[window.logo.itemText(i) for i in range(1,window.logo.count())]
        assert len(logo_names)==34 and len(set(logo_names))==34
        assert 'Charlotte Hornets' in logo_names[:-3]
        assert 'Seattle SuperSonics' in logo_names[:-3]
        assert logo_names[:-3]==sorted(logo_names[:-3],key=str.casefold)
        assert logo_names[-3:]==['Eastern Conference','Team USA','Western Conference']
        for name in logo_names[-3:]:
            window.logo.setCurrentIndex(window.logo.findText(name))
            assert window.doc.spec['team_logo']==window.logo.currentData()
            _,layers=render_card(window.pack,window.doc.render_spec(),return_layers=True)
            assert layers['team_logo'].getbbox()
        result['current_logo_set']={'nba_teams':31,'nba_teams_alphabetical':True,
            'extras':logo_names[-3:],'charlotte_hornets':True,'other_sets_hidden':True}
        result['editor_tabs']=[editor.tabs.tabText(i) for i in range(editor.tabs.count())]
        editor.fields[('attributes','speed')].setValue(91)
        editor.fields[('badges','catch_and_shoot')].setCurrentIndex(editor.fields[('badges','catch_and_shoot')].findData('HALL_OF_FAME'))
        assert editor.document.data['attributes']['values']['speed']==91
        assert editor.document.data['attributes']['statuses']['speed']=='CUSTOM_AUTHORED'
        assert editor.document.data['badges']['values']['catch_and_shoot']=='HALL_OF_FAME'
        assert window.doc.data['attributes']['values'].get('speed')==parent['data']['attributes']['values'].get('speed')
        editor.fields[('hot_zones','hot_zone_01')].setCurrentIndex(3)
        assert editor.document.data['hot_zones']['values']['hot_zone_01']==2
        editor.close();editor.deleteLater()
        picker=ParentPicker(window.presets,window);picker.filter('Stephen Curry');assert picker.cards.count()>1;picker.close();picker.deleteLater()
        result['ui_controls']=True;result['parent_cards']=len(window.presets['cards'])
        # Full-window switch and Done, including a pending line-edit value.
        window.edit_data();assert window.stack.currentWidget() is window.data_editor
        window.data_editor.fields[('identity','card_year')].setText('1998')
        window.data_editor.done_requested.emit()
        assert window.data_editor is None and window.stack.currentIndex()==0
        assert str(window.doc.data['identity']['values']['card_year'])=='1998'
        result['full_window_editor_done']=True
        from .ui import MaskCanvas
        original=Image.new('RGBA',(30,30),'red');cutout=original.copy();cutout.putalpha(Image.new('L',(30,30),50))
        mask_editor=MaskCanvas(original,cutout)
        assert mask_editor.result().getchannel('A').getextrema()==(50,50)
        mask_editor.mask.paste(255,(0,0,30,30))
        assert mask_editor.result().tobytes()==original.tobytes()
        mask_editor.close();result['alpha_restore_brush']=True
        result['fields']={s:len(v) for s,v in window.presets['schema'].items()}
        if check_background:
            from .background import remove_background
            original=Image.open(BytesIO(window.doc.portrait)).convert('RGBA')
            cutout=remove_background(original,window.resources/'models')
            assert cutout.convert('RGB').tobytes()==original.convert('RGB').tobytes()
            assert cutout.getchannel('A').getextrema()==(0,255)
            result['local_background_inference_rgb_preserved']=True
        result['passed']=True
    except Exception:
        result['error']=traceback.format_exc()
    finally:
        window.timer.stop()
        if window.worker:window.worker.wait(10000)
        window.saved=window.doc.clone();window.close();QApplication.processEvents()
        report.write_text(json.dumps(result,indent=2),encoding='utf-8')
    return 0 if result['passed'] else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('project',nargs='?');parser.add_argument('--demo',action='store_true')
    parser.add_argument('--smoke-test',action='store_true');parser.add_argument('--check-background',action='store_true');parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.smoke_test:os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
    from PySide6.QtWidgets import QApplication,QMessageBox
    from PySide6.QtGui import QFont,QIcon
    from .ui import StudioWindow
    if os.name=='nt':
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('Local.NBA2K17.CardStudio')
    app=QApplication(sys.argv[:1]);app.setApplicationName('NBA 2K17 Card Studio');app.setOrganizationName('Offline MyTEAM')
    app.setStyle('Fusion');app.setFont(QFont('Segoe UI',10))
    pack,resources=locations()
    try:
        window=StudioWindow(pack,resources,load_presets(resources))
        icon=resources/'card-studio.ico'
        if icon.exists():app.setWindowIcon(QIcon(str(icon)))
        if args.smoke_test:
            if not args.report:raise ValueError('--report is required for a smoke test')
            return run_smoke(window,args.report,args.check_background)
        if args.project:window.load(args.project)
        elif args.demo:demo(window)
        window.show();return app.exec()
    except Exception:
        error=traceback.format_exc()
        if args.report:args.report.parent.mkdir(parents=True,exist_ok=True);args.report.write_text(error,encoding='utf-8')
        else:QMessageBox.critical(None,'Card Studio could not start',error)
        return 1


if __name__=='__main__':raise SystemExit(main())
