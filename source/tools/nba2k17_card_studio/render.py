"""Offline custom-card compositor with source-PNG calibrated layer positions.

Input is an explicit custom-card specification. It never edits canonical cards.
"""
import json
from pathlib import Path
from PIL import Image,ImageDraw
from .bitmap import text_image
from .layout import CANVAS,TEXT_SLOPE,TEAM_PANEL,TEAM_FRAME,PLAYER_CLIP,PORTRAIT_DEFAULT,native_layer,team_layer,clip_layer,source_component_layer


def fit_asset(path,size):
    image=Image.open(path).convert('RGBA');bounds=image.getbbox()
    if bounds is None:raise ValueError('Empty asset: '+str(path))
    image=image.crop(bounds);image.thumbnail(size,Image.Resampling.LANCZOS)
    return image


def render_card(pack:Path,spec:dict,*,return_layers=False):
    assets=json.loads((pack/'assets.json').read_text(encoding='utf-8'))
    tier=next((r for r in assets['tiers'] if r['id']==spec['tier']),None)
    if not tier:raise ValueError('Unknown tier '+spec['tier'])
    layers={};canvas=Image.new('RGBA',CANVAS)
    def add(name,layer):
        nonlocal canvas
        layers[name]=layer;canvas=Image.alpha_composite(canvas,layer)
    rear=native_layer(Image.open(pack/tier['rear']).convert('RGBA'));add('rear',rear)
    if spec.get('background',True):
        add('background',native_layer(Image.open(pack/'tiers'/tier['id']/'background.png').convert('RGBA')))
    # Only the short left folded edge lies in front of the background plane.
    add('side_fold',clip_layer(rear,[(0,376),(40,376),(40,560),(0,560)]))
    player=next((r for r in assets['players'] if r['id']==str(spec.get('player_id','11802'))),None)
    portrait=spec.get('_portrait')
    if portrait is None and spec.get('player_png'):portrait=Image.open(Path(spec['player_png'])).convert('RGBA')
    elif portrait is None and player:portrait=Image.open(pack/player['file']).convert('RGBA')
    elif portrait is None and spec.get('player_id','11802') is not None:raise ValueError('Unknown player texture')
    art=Image.new('RGBA',CANVAS)
    if portrait is not None:
        bbox=portrait.getbbox()
        if bbox is None:raise ValueError('Empty player texture')
        portrait=portrait.crop(bbox)
        height=round(spec.get('portrait_height',PORTRAIT_DEFAULT['portrait_height']))
        if not 1<=height<=6500:raise ValueError('Portrait height outside supported range')
        width=max(1,round(portrait.width*height/portrait.height))
        if width*height>50_000_000:raise ValueError('Portrait transform is too large')
        portrait=portrait.resize((width,height),Image.Resampling.LANCZOS)
        art.alpha_composite(portrait,(round(spec.get('portrait_x',PORTRAIT_DEFAULT['portrait_x'])),round(spec.get('portrait_y',PORTRAIT_DEFAULT['portrait_y']))))
    add('player',clip_layer(art,PLAYER_CLIP))
    # Logo panel is in front of the portrait, but the logo stays inside its border.
    panel=Image.new('RGBA',CANVAS);draw=ImageDraw.Draw(panel)
    draw.polygon(TEAM_FRAME,fill='#080d10');draw.polygon(TEAM_PANEL,fill='#182027');add('team_panel',panel)
    logo_id=spec.get('team_logo','logo029')
    logo=next((r for r in assets['logos'] if logo_id==r['id'] or logo_id in r['aliases']),None)
    if logo_id and not logo:raise ValueError('Unknown team logo: '+str(logo_id))
    if logo:add('team_logo',team_layer(pack/logo['file']))
    # Keep only the native position tab here. Its old overall raster lost the
    # negative-X vertices; recreate that simple plaque from measured polygons.
    common=native_layer(Image.open(pack/'common/frame_and_overall_plate.png').convert('RGBA'))
    add('position_tab',clip_layer(common,[(0,640),(150,640),(150,760),(0,760)]))
    add('front',native_layer(Image.open(pack/tier['front']).convert('RGBA')))
    if tier['jeweled_icon']:
        shield=Image.new('RGBA',CANVAS);ImageDraw.Draw(shield).polygon([(22,207),(138,207),(138,339),(80,363),(22,343)],fill='#454545' if tier['id']=='onyx' else '#03080b')
        add('gem_shield',shield)
    plaque=Image.new('RGBA',CANVAS);d=ImageDraw.Draw(plaque)
    d.polygon([(2,74),(158,74),(158,213),(80,245),(2,213)],fill='#090e11')
    d.polygon([(14,132),(146,132),(146,206),(80,229),(14,206)],fill='#f5f5f3');add('overall_plaque',plaque)
    textlayer=Image.new('RGBA',CANVAS)
    def put(text,font,x,y,size,target=textlayer,maxwidth=None,color=None,center=None):
        im=text_image(pack/'fonts'/font,text,size)
        if maxwidth and im.width>maxwidth:im=im.resize((maxwidth,im.height),Image.Resampling.LANCZOS)
        if color is not None:
            tinted=Image.new('RGBA',im.size,color);tinted.putalpha(im.getchannel('A'));im=tinted
        if center is not None:x=round(center-im.width/2)
        target.alpha_composite(im,(x,y));return im
    put('Overall','lobstertwo_italic',12,83,37)
    put(str(spec.get('overall',95)),'stratum2_bold',0,144,56,color='black',center=81)
    add('overall_text',textlayer)
    def component(entry,name,bbox_field='source_bbox'):
        image=Image.open(pack/entry['png']).convert('RGBA');placement=entry.get('placement')
        size=(placement['width'],placement['height']) if placement else (image.width*2,image.height*2)
        position=(placement['x'],placement['y']) if placement else source_component_layer(entry,bbox_field)
        image=image.resize(size,Image.Resampling.LANCZOS)
        layer=Image.new('RGBA',CANVAS);layer.alpha_composite(image,position);add(name,layer)
    if tier['jeweled_icon']:component(next(r for r in assets['icons'] if r['name']==tier['name']),'gem')
    if spec.get('dynamic_duo',False) and spec.get('theme')!='Free Agent':component(next(r for r in assets['icons'] if r['name']=='Dynamic Duo'),'duo')
    theme=spec.get('theme','Current')
    if theme=='Free Agent':component(next(r for r in assets['icons'] if r['name']=='Free Agent'),'free_agent')
    elif theme:
        promo=next((r for r in assets['promotions'] if r['name']==theme),None)
        if not promo:raise ValueError('Unknown promotion: '+theme)
        component(promo,'promotion','crop_bbox')
    position=Image.new('RGBA',CANVAS)
    pos=text_image(pack/'fonts/stratum2_medium',spec.get('position','PG'),33)
    if pos.width>75:pos=pos.resize((75,pos.height),Image.Resampling.LANCZOS)
    w,h=pos.size
    pos=pos.transform((w+12,h+10),Image.Transform.AFFINE,(1,.08,-.08*h,.145,1,-.145*(w+12)),Image.Resampling.BICUBIC)
    position.alpha_composite(pos,(49,683));add('position_text',position)
    lettering=Image.new('RGBA',CANVAS)
    put('#','stratum2_medium',32,782,34,target=lettering)
    put(str(spec.get('jersey_number','30')),'stratum2_bold',0,782,93,target=lettering,maxwidth=142,center=144)
    put(spec.get('first_name','STEPHEN').upper(),'stratum2_medium',231,784,27,target=lettering,maxwidth=396)
    put(spec.get('last_name','Curry'),'stratum2_bold',234,822,52,target=lettering,maxwidth=393)
    lettering=lettering.transform(CANVAS,Image.Transform.AFFINE,(1,0,0,TEXT_SLOPE,1,0),Image.Resampling.BICUBIC)
    add('nameplate_text',lettering)
    return (canvas,layers) if return_layers else canvas


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--assets',type=Path,required=True);parser.add_argument('--spec',type=Path,required=True);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    spec=json.loads(args.spec.read_text(encoding='utf-8'));image=render_card(args.assets,spec)
    args.output.parent.mkdir(parents=True,exist_ok=True);image.save(args.output)


if __name__=='__main__':main()
