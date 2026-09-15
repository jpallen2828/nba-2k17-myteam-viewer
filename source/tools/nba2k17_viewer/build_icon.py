"""Convert user-selected artwork into a genuine, multi-resolution Windows ICO.

Contain (not crop or stretch) the source on a transparent square. Explicit small
taskbar/title-bar frames and large Explorer frames are encoded in one ICO.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from PIL import Image, ImageOps

ICON_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def icon_canvas(source, size=256):
    with Image.open(source) as image:
        image=ImageOps.exif_transpose(image).convert('RGBA')
        image.thumbnail((size,size),Image.Resampling.LANCZOS)
        canvas=Image.new('RGBA',(size,size))
        canvas.paste(image,((size-image.width)//2,(size-image.height)//2))
        return canvas


def build_icon(source,output):
    source,output=Path(source),Path(output)
    canvas=icon_canvas(source)
    output.parent.mkdir(parents=True,exist_ok=True)
    canvas.save(output,format='ICO',sizes=[(size,size) for size in ICON_SIZES])
    with Image.open(output) as icon:
        assert icon.format=='ICO'
        assert icon.ico.sizes()=={(size,size) for size in ICON_SIZES}
        for size in ICON_SIZES:
            frame=icon.ico.getimage((size,size))
            assert frame.size==(size,size) and frame.mode=='RGBA' and frame.getbbox()
    return {'source':str(source.resolve()),'source_sha256':sha256(source.read_bytes()).hexdigest(),
            'output':str(output.resolve()),'ico_sha256':sha256(output.read_bytes()).hexdigest(),
            'sizes':list(ICON_SIZES),'fit':'contain; transparent padding; no crop or distortion'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--source',type=Path,default=Path(__file__).resolve().parents[2]/'assets/application_icons/myteam-source.png')
    args=parser.parse_args()
    print(json.dumps(build_icon(args.source,args.output)))


if __name__=='__main__':main()
