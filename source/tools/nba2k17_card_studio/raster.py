"""Deterministic CPU triangle rasterization for portable native-mesh previews.

Geometry and texture samples are native. Shading is explicitly a simplified
offline material evaluation, not a bit-exact recreation of 2K's live shaders.
"""
import numpy as np
from PIL import Image


def render(model, materials, textures, width=650, height=950, select=lambda p:True):
    attrs=model['attributes'];positions=np.array(attrs['POSITION0'],dtype=float)
    # In these UI meshes positions are bind-space; node translations are retained
    # in the JSON for animation, not applied twice to the exported static pose.
    scale=width/110.; screen=np.column_stack((positions[:,0]*scale+width/2,(85.75-positions[:,1])*scale,positions[:,2]))
    colors=np.array(attrs.get('COLOR0',np.ones((len(positions),4))),dtype=float)
    uv=np.array(attrs.get('TEXCOORD0',np.zeros((len(positions),2))))
    normals=np.array(attrs.get('NORMAL0',np.tile([0,0,1,0],(len(positions),1))))[:,:3]
    output=np.zeros((height,width,4),dtype=np.float32);depth=np.full((height,width),-np.inf)
    for primitive in model['primitives']:
        if not select(primitive):continue
        material=materials.get(primitive['material'],{})
        diffuse=material.get('parameters',{}).get('DiffuseColor',[1,1,1])
        if isinstance(diffuse,(float,int)):diffuse=[diffuse]
        if len(diffuse)<3:diffuse=list(diffuse)+[0.]*(3-len(diffuse))
        diffuse=np.array(diffuse[:3]);tex=textures.get(primitive['material'])
        indices=primitive['indices']
        for i in range(0,len(indices),3):
            ids=indices[i:i+3];tri=screen[ids];xy=tri[:,:2]
            x0=max(0,int(np.floor(xy[:,0].min())));y0=max(0,int(np.floor(xy[:,1].min())))
            x1=min(width,int(np.ceil(xy[:,0].max()))+1);y1=min(height,int(np.ceil(xy[:,1].max()))+1)
            if x0>=x1 or y0>=y1:continue
            a,b,c=xy;den=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
            if abs(den)<1e-7:continue
            yy,xx=np.mgrid[y0:y1,x0:x1];xx=xx+.5;yy=yy+.5
            w0=((b[1]-c[1])*(xx-c[0])+(c[0]-b[0])*(yy-c[1]))/den
            w1=((c[1]-a[1])*(xx-c[0])+(a[0]-c[0])*(yy-c[1]))/den;w2=1-w0-w1
            weights=np.stack([w0,w1,w2],axis=-1)
            z=weights@tri[:,2];mask=(weights.min(axis=-1)>=-1e-6)&(z>=depth[y0:y1,x0:x1]-1e-5)
            if not mask.any():continue
            rgba=weights@colors[ids]
            shade=np.ones_like(rgba[:,:,:3])*diffuse
            if tex is not None:
                coords=weights@uv[ids,:2];coords=np.mod(coords,2);coords=np.where(coords>1,2-coords,coords)
                tx=np.clip((coords[:,:,0]*(tex.shape[1]-1)).astype(int),0,tex.shape[1]-1)
                ty=np.clip((coords[:,:,1]*(tex.shape[0]-1)).astype(int),0,tex.shape[0]-1)
                shade*=tex[ty,tx,:3]/255.
            # Preserve real face normals as a conservative metal/gem light model.
            name=primitive['material']
            if any(s in name for s in ['metal','g_card']):
                n=weights@normals[ids];light=np.array([-.35,.6,.72]);light/=np.linalg.norm(light)
                lambert=np.abs(n@light)
                shade*=.7+lambert[:,:,None]*1.05
            if name in {'card_nameplate_mat','card_frame_mat','ribbon_frame_back_mat'}:
                shade*=.08 # native dark frame diffuse is a lit material, not a flat gray fill
            rgba[:,:,:3]*=shade
            # Linear native material/texture product -> sRGB preview.
            rgba[:,:,:3]=np.where(rgba[:,:,:3]<=.0031308,rgba[:,:,:3]*12.92,1.055*np.maximum(rgba[:,:,:3],0)**(1/2.4)-.055)
            rgba=np.clip(rgba,0,1)
            output[y0:y1,x0:x1][mask]=rgba[mask];depth[y0:y1,x0:x1][mask]=z[mask]
    return Image.fromarray(np.round(output*255).astype('uint8'))
