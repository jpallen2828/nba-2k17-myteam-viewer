"""Exactly reconstruct allowed mutable bits; retain all structural/unknown bytes."""
def verified_mutation(before, after, fields):
    if len(before)!=len(after):return False
    allowed=bytearray(len(before))
    protected=((0x58,0x64),(0x70,0xc8),(0x150,0x158),(0x3e0,0x3f1))
    for field in fields:
        if field.get('region','player')!='player':continue
        offset,size=field['offset'],field['size']
        if offset<0 or offset+size>len(before):continue
        shift=field.get('shift',0);bits=field.get('bits',size*8)
        if shift<0 or bits<=0 or shift+bits>size*8:continue
        for bit in range(offset*8+shift,offset*8+shift+bits):
            byte=bit//8
            if not any(start<=byte<end for start,end in protected):allowed[byte]|=1<<(bit%8)
    return all((old^new)&~mask==0 for old,new,mask in zip(before,after,allowed))
