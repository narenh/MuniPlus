"""Usage: python3 tools/map_dark.py map.pdf map-dark.pdf ['{"bg":"#111111"}']
Needs pikepdf and numpy. Rerun whenever SFMTA publishes a new map.pdf.

Recolour the Muni Metro map PDF's vector content for dark mode.
Every colour-setting operator is removed and replaced with an explicit DeviceRGB
colour chosen per painted object (rules below). Geometry/text untouched."""
import pikepdf, sys, json, numpy as np
from pikepdf import Operator as Op

src, dst = sys.argv[1], sys.argv[2]
cfg = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
def hx(h): h=h.lstrip('#'); return tuple(int(h[i:i+2],16)/255 for i in (0,2,4))

BG      = hx(cfg.get('bg', '#000000'))
WHITE   = hx('#FFFFFF')
HEADER  = hx(cfg.get('header', '#1A2B3A'))
PANEL   = hx(cfg.get('panel', '#1C1C1E'))
PANEL_LN= hx(cfg.get('panel_line', '#48484A'))
GREY2   = hx('#8E8E93')     # was PANTONE Cool Gray 9 (secondary)
CAPSULE = cfg.get('capsule', 'swap')   # 'swap' = dark fill/white outline, 'white' = white fill/bg outline
LINE = {   # by line, app dark-mode palette
 '/New J Orange': hx('#FAA633'), 'J': hx('#FAA633'),
 '/PANTONE 549 C': hx('#579EBF'),
 '/PANTONE 254 C': hx('#B243AF'),
 '/PANTONE 348 C': hx('#008552'),
 '/PANTONE 301 C': hx('#266BBC'),
 '/PANTONE 200 C': hx('#D60F45'),
 '/PANTONE Process Blue C': hx('#0082BE'),   # BART: unchanged (as rendered from the original separation)
}
LINE.update({k: hx(v) for k,v in cfg.get('line', {}).items()})
# N labels: the app's dark N (#266BBC) is only ~3.9:1 on black, so its text is lifted; the line keeps the app colour
TEXT_LINE = {k: hx(v) for k,v in {'/PANTONE 301 C': '#6FA3E0', **cfg.get('text_line', {})}.items()}
ORANGE_BALL = hx('#F7931D'); RED_STITCH = hx('#DE1A22')   # icon colours unchanged

pdf = pikepdf.open(src); page = pdf.pages[0]; H = float(page.MediaBox[3])

def mul(a,b):
    a1,b1,c1,d1,e1,f1=a; a2,b2,c2,d2,e2,f2=b
    return (a1*a2+b1*c2, a1*b2+b1*d2, c1*a2+d1*c2, c1*b2+d1*d2, e1*a2+f1*c2+e2, e1*b2+f1*d2+f2)

def analyse(ops, cs, ctm0=(1,0,0,1,0,0)):
    """returns list of paint records: dict(i, start, kind, key, box, text)"""
    st=[]; ctm=ctm0; fill=('gray',(0.0,)); stroke=('gray',(0.0,)); fcs=scs=None; pts=[]; start=None
    recs=[]
    for i,(operands,op) in enumerate(ops):
        op=str(op)
        if op=='q': st.append((ctm,fill,stroke,fcs,scs))
        elif op=='Q': ctm,fill,stroke,fcs,scs=st.pop()
        elif op=='cm': ctm=mul(tuple(map(float,operands)),ctm)
        elif op=='cs': fcs=cs[str(operands[0])]
        elif op=='CS': scs=cs[str(operands[0])]
        elif op in('scn','sc'): fill=(fcs,tuple(round(float(x),3) for x in operands))
        elif op in('SCN','SC'): stroke=(scs,tuple(round(float(x),3) for x in operands))
        elif op=='k': fill=('cmyk',tuple(round(float(x),3) for x in operands))
        elif op=='K': stroke=('cmyk',tuple(round(float(x),3) for x in operands))
        elif op=='g': fill=('gray',(round(float(operands[0]),3),))
        elif op=='G': stroke=('gray',(round(float(operands[0]),3),))
        elif op in('m','l','c','v','y','re'):
            if start is None: start=i
            v=list(map(float,operands))
            if op=='re': v=[v[0],v[1],v[0]+v[2],v[1]+v[3]]
            for j in range(0,len(v),2):
                x,y=v[j],v[j+1]; pts.append((ctm[0]*x+ctm[2]*y+ctm[4], ctm[1]*x+ctm[3]*y+ctm[5]))
        elif op in ('f','F','f*','S','s','B','B*','b','b*','n'):
            if op!='n' and pts:
                a=np.array(pts); box=(a[:,0].min(),H-a[:,1].max(),a[:,0].max(),H-a[:,1].min())
                if op not in('S','s'): recs.append(dict(i=i,start=start,kind='F',key=fill,box=box))
                if op in('S','s','B','B*','b','b*'): recs.append(dict(i=i,start=start,kind='S',key=stroke,box=box))
            pts=[]; start=None
        elif op in ('Tj','TJ',"'",'"'):
            s=operands[-1]
            t=bytes(s).decode('latin1') if op!='TJ' else ''.join(bytes(x).decode('latin1') for x in s if isinstance(x,pikepdf.String))
            recs.append(dict(i=i,start=i,kind='T',key=fill,box=None,text=t))
    return recs

NEUTRAL_DARK = lambda k: k[0] in('/BLACK',) or (k[0]=='cmyk' and k[1][3]>=0.7) or (k[0]=='gray' and k[1][0]<0.5)
NEUTRAL_WHITE= lambda k: (k[0]=='/Opaque White') or (k[0]=='cmyk' and max(k[1])==0) or (k[0]=='gray' and k[1][0]>=0.99)
def inside(b, r, tol=1.0): return b[0]>=r[0]-tol and b[1]>=r[1]-tol and b[2]<=r[2]+tol and b[3]<=r[3]+tol
def size(b): return (b[2]-b[0], b[3]-b[1])
LEGEND = (1690,2180,2310,2360)

def base(rec):
    k=rec['key']; name=k[0]
    if rec['kind']=='T' and name in TEXT_LINE: return TEXT_LINE[name]
    if name in LINE: return LINE[name]
    if name=='cmyk' and k[1]==(0.0,0.5,1.0,0.1): return LINE['J']        # J line drawn in device CMYK
    if name=='cmyk' and k[1]==(0.0,0.5,1.0,0.0): return ORANGE_BALL
    if name=='cmyk' and k[1]==(0.0,1.0,1.0,0.0): return RED_STITCH
    if name=='/PANTONE Cool Gray 9 C': return GREY2
    if NEUTRAL_DARK(k): return WHITE
    if NEUTRAL_WHITE(k): return WHITE
    raise SystemExit('unmapped colour %r'%(k,))

def plan_page(recs):
    # icon tiles (BART/Caltrain): black-on-white or white-on-black tiles -> swap neutrals inside
    tiles=[]
    for r in recs:
        if r['kind']=='F' and r['box'] is not None:
            w,h=size(r['box'])
            if (NEUTRAL_DARK(r['key']) or NEUTRAL_WHITE(r['key'])) and ((40<=w<=46 and 28<=h<=34) or (46<=w<=52 and 46<=h<=52)):
                tiles.append(r['box'])
    out={}
    for r in recs:
        c=base(r); k=r['key']; b=r['box']
        inleg = b is not None and inside(b, LEGEND)
        knock = PANEL if inleg else BG
        if b is not None and any(inside(b,t) for t in tiles) and (NEUTRAL_DARK(k) or NEUTRAL_WHITE(k)):
            c = knock if NEUTRAL_WHITE(k) else WHITE
        if r['kind']=='F' and b is not None and size(b)[0]>2000 and size(b)[1]<300 and b[1]<1: c=HEADER
        if r['kind']=='F' and b is not None and inleg and size(b)[0]>600: c=PANEL      # legend panel
        if r['kind']=='S' and b is not None and inleg and size(b)[0]>600: c=PANEL_LN   # legend border
        if r['kind']=='F' and NEUTRAL_DARK(k) and b is not None and size(b)[0]>250: pass  # black badges -> white (base)
        if r['kind']=='T' and NEUTRAL_WHITE(k) and r['text'].strip() in ('Embarcadero','Balboa Park'): c=BG
        # shared-station markers: white fill + BLACK ring (24-26pt); and legend copy (19pt)
        if CAPSULE=='swap' and r['kind']=='F' and NEUTRAL_WHITE(k) and b is not None and 18<=size(b)[0]<=27 and 18<=size(b)[1]<=27:
            if r.get('shared'): c=knock
        # stray white station dot near Yerba Buena (16pt @1814,739): white-on-white in the original, i.e. invisible -> keep invisible
        if NEUTRAL_WHITE(k) and b is not None and abs(b[0]-1814)<2 and abs(b[1]-739)<2: c=BG
        # rich black icon seams (basketball/baseball) -> knockout
        if k[0]=='cmyk' and k[1] in ((0.746,0.676,0.668,0.898),) :
            c = WHITE if (b is not None and size(b)[0]>45) else knock   # baseball rim white, basketball seams dark
        out[(r['i'],r['kind'])]=c
    return out

def mark_shared(recs):
    # a white disc is "shared" when a BLACK stroke ring of the same box follows it
    rings=[r['box'] for r in recs if r['kind']=='S' and NEUTRAL_DARK(r['key']) and r['box'] is not None and 18<=size(r['box'])[0]<=27]
    for r in recs:
        if r['kind']=='F' and NEUTRAL_WHITE(r['key']) and r['box'] is not None:
            if any(abs(r['box'][0]-q[0])<4 and abs(r['box'][1]-q[1])<4 and abs(size(q)[0]-size(r['box'])[0])<4 for q in rings): r['shared']=True

def rewrite(ops, recs, colours, prelude=None):
    by_start={}; 
    for r in recs: by_start.setdefault(r['start'],[]).append(r)
    COLOR_OPS={'cs','CS','sc','scn','SC','SCN','k','K','g','G','rg','RG'}
    new=[]; cur=[None,None]; st=[]
    if prelude: new+=prelude
    for i,(operands,op) in enumerate(ops):
        o=str(op)
        if i in by_start:
            for r in by_start[i]:
                c=colours[(r['i'],r['kind'])]; slot=1 if r['kind']=='S' else 0
                if cur[slot]!=c:
                    new.append(([*map(lambda x: round(x,4), c)], Op('RG' if slot else 'rg'))); cur[slot]=c
        if o in COLOR_OPS: continue
        if o=='q': st.append(list(cur))
        if o=='Q': cur=st.pop()
        new.append((operands,op))
    return pikepdf.unparse_content_stream(new)

def csmap(res): return {str(k):str(v[1]) for k,v in res.ColorSpace.items()} if '/ColorSpace' in res else {}

# page
ops=list(pikepdf.parse_content_stream(page))
recs=analyse(ops, csmap(page.Resources)); mark_shared(recs)
cols=plan_page(recs)
W=float(page.MediaBox[2])
prelude=[([],Op('q')),([*BG],Op('rg')),([0,0,W,H],Op('re')),([],Op('f')),([],Op('Q'))]
page.Contents = pdf.make_stream(rewrite(ops, recs, cols, prelude))
# station capsules (form XObjects Fm0..Fm10: white fill + BLACK outline)
for name,xo in page.Resources.XObject.items():
    xops=list(pikepdf.parse_content_stream(xo)); xr=analyse(xops, csmap(xo.Resources))
    xc={}
    for r in xr:
        white = NEUTRAL_WHITE(r['key'])
        if CAPSULE=='swap': xc[(r['i'],r['kind'])] = BG if white else WHITE
        else: xc[(r['i'],r['kind'])] = WHITE if white else BG
    xo.write(rewrite(xops, xr, xc))
    if '/ColorSpace' in xo.Resources: del xo.Resources['/ColorSpace']
del page.Resources['/ColorSpace']
pdf.save(dst)
# dump plan for the report
if cfg.get('dump'):
    import collections
    agg=collections.Counter()
    for r in recs: agg[(r['kind'],str(r['key']),'#%02X%02X%02X'%tuple(round(x*255) for x in cols[(r['i'],r['kind'])]))]+=1
    for k,v in sorted(agg.items()): print(v,*k)
