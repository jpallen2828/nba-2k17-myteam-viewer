"""Card-space helpers calibrated against original 650x950 card PNG coordinates."""
from PIL import Image,ImageDraw,ImageChops

CANVAS=(650,950)
NATIVE_SCALE=(.978,.99)
NATIVE_OFFSET=(14,9)
TEXT_SLOPE=.145
TEAM_PANEL=((484,451),(632,506),(632,648),(484,670))
TEAM_FRAME=((477,440),(639,501),(639,655),(477,678))
PLAYER_CLIP=((0,0),(650,0),(650,648),(0,742))
PORTRAIT_DEFAULT={'portrait_x':39,'portrait_y':5,'portrait_height':1264}


def polygon_mask(points):
    mask=Image.new('L',(1300,1900));ImageDraw.Draw(mask).polygon([(round(x*2),round(y*2)) for x,y in points],fill=255)
    return mask.resize(CANVAS,Image.Resampling.LANCZOS)


def clip_layer(image,points):
    result=image.copy();result.putalpha(ImageChops.multiply(result.getchannel('A'),polygon_mask(points)));return result


def native_layer(image):
    sx,sy=NATIVE_SCALE;dx,dy=NATIVE_OFFSET
    return image.transform(CANVAS,Image.Transform.AFFINE,(1/sx,0,-dx/sx,0,1/sy,-dy/sy),Image.Resampling.BICUBIC)


def team_layer(path):
    """Crop the native square logo into the panel, retaining circular proportions.

    The game's UVs clip a square logo; stretching its corners to the panel would
    distort it. Registration against Curry gives 234.5px at (439.8,450.3).
    """
    logo=Image.open(path).convert('RGBA')
    if logo.width!=logo.height:
        square=Image.new('RGBA',(max(logo.size),max(logo.size)))
        square.alpha_composite(logo,((square.width-logo.width)//2,(square.height-logo.height)//2));logo=square
    sx=logo.width/234.5;sy=logo.height/234.5
    image=logo.transform(CANVAS,Image.Transform.AFFINE,(sx,0,-439.8*sx,0,sy,-450.3*sy),Image.Resampling.BICUBIC)
    return clip_layer(image,TEAM_PANEL)


def source_component_layer(entry,bbox_field='source_bbox'):
    """Recorded source-card position, including the 2px transparent padding."""
    sx,sy=entry['source']['roi'][:2];bx,by=entry[bbox_field][:2]
    return ((sx+bx-2)*2,(sy+by-2)*2)
