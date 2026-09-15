"""Portable native bitmap-font rendering from converted glyph/kerning tables."""
import json
from functools import lru_cache
from PIL import Image


@lru_cache(maxsize=16)
def font_assets(font_directory):
    return (json.loads((font_directory/'font.json').read_text(encoding='utf-8')),
            Image.open(font_directory/'atlas.png').convert('RGBA'))


def text_image(font_directory,text,pixel_height=30):
    meta,atlas=font_assets(font_directory)
    pen=5.;placements=[];baseline=70
    for i,char in enumerate(text):
        if char not in meta['glyphs']:raise ValueError('Unsupported native font character: '+repr(char))
        glyph=meta['glyphs'][char]
        if i:pen+=meta['kerning'].get(text[i-1]+char,0)
        placements.append((pen,glyph,char));pen+=glyph['advance']
    canvas=Image.new('RGBA',(max(20,round(pen+40)),150))
    for x,glyph,char in placements:
        x0,y0,x1,y1=glyph['quad']
        if char==' ' or x1<=x0 or y1<=y0:continue
        patch=atlas.transform((round(x1-x0),round(y1-y0)),Image.Transform.EXTENT,glyph['uv_pixels'],Image.Resampling.BICUBIC)
        canvas.alpha_composite(patch,(round(x+x0),round(baseline+y0)))
    box=canvas.getbbox()
    if box is None:return Image.new('RGBA',(max(1,round(pen)),1))
    canvas=canvas.crop(box);factor=pixel_height/meta['native_pixel_size']
    return canvas.resize((max(1,round(canvas.width*factor)),max(1,round(canvas.height*factor))),Image.Resampling.LANCZOS)
