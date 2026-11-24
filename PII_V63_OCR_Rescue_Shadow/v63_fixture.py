"""V6.3: ephemeral, deterministic, fully synthetic raster-PDF challenge pages.

Ground-truth glyph geometry is calculated at draw time.  The PDF itself has no
text layer.  No genuine identifying values are used.  Fixture and GT are
always written by the caller inside TemporaryDirectory and deleted afterward.
"""
from __future__ import annotations
from io import BytesIO
from pathlib import Path
import random
from PIL import Image, ImageDraw, ImageFilter, ImageFont
try:
    import pymupdf as fitz
except ImportError:
    import fitz

WIDTH, HEIGHT = 1000, 1300
FONTS = [
    r'C:\Windows\Fonts\msjh.ttc', r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\mingliu.ttc',r'C:\Windows\Fonts\arial.ttf',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]
RECIPES = ('micro_email', 'tiny_phone','faint_email','local_blur',
           'jpeg_artifacts','watermark','mixed_small','clean_control')

def font(size: int, preferred: int=0):
    paths = FONTS[preferred:] + FONTS[:preferred]
    for path in paths:
        if Path(path).is_file():
            try: return ImageFont.truetype(path, max(6,int(size)))
            except (OSError, ValueError): pass
    raise RuntimeError('找不到中文字型。請確認 C:\\Windows\\Fonts\\msjh.ttc 或 msyh.ttc')

def _value(draw, gt, *, label, text, xy, fontsize, color=(20,20,20), role='anchor'):
    x,y=xy
    ft=font(fontsize)
    draw.text((x,y),text,font=ft,fill=color)
    bb=draw.textbbox((x,y),text,font=ft)
    if bb[2]<=bb[0] or bb[3]<=bb[1]: raise ValueError('invalid glyph bbox')
    gt.append({'label':label, 'role':role,
               'bbox_pdf':[round(v/2,3) for v in bb]})
    return bb

def _effect(img, rect, effect, rng):
    """Perturb only candidate PII area, so page-level quality can stay high."""
    if effect=='local_blur':
        x0,y0,x1,y1=rect
        region=img.crop((max(0,x0-3),max(0,y0-3),min(WIDTH,x1+4),min(HEIGHT,y1+4)))
        region=region.filter(ImageFilter.GaussianBlur(rng.choice([.6,1.0,1.4])))
        img.paste(region,(max(0,x0-3),max(0,y0-3)))
    if effect=='jpeg_artifacts':
        mem=BytesIO()
        img.save(mem,format='JPEG',quality=rng.choice([26,38,50]),subsampling=2)
        mem.seek(0)
        with Image.open(mem) as im: return im.convert('RGB')
    return img

def render(index: int, seed: int, *, split: str) -> tuple[Image.Image,dict]:
    """Draw one challenge with known anchor PII and independently labelled probe PII."""
    rng=random.Random(f'{split}:{seed}:{index}:v63')
    recipe=RECIPES[index%len(RECIPES)]
    image=Image.new('RGB',(WIDTH,HEIGHT),(249,249,248))
    d=ImageDraw.Draw(image)
    gt=[]
    big=font(31); medium=font(23)
    d.text((85,60),'合成個資測試  OCR RECOVERY',font=font(39),fill=(12,12,12))
    d.text((86,117),'本頁僅包含虛構範例，不涉及真實使用者',font=medium,fill=(25,25,25))
    # Enough clean content to make the global quality gate optimistic in some tests.
    headings=['DOC-ID 2026-TEST','Data Protection Research','Page scan verification',
              'This is synthetic information','Review mode only',
              'LOCAL DOCUMENT PROCESSING','No network service is needed',
              'Printed form and appendix']
    for k,t in enumerate(headings):
        d.text((86,170+k*58),t,font=font(26),fill=(30,30,30))
    d.rectangle((69,700,920,703),fill=(30,30,30))
    d.text((86,721),'CONFIDENTIAL TEST FORM',font=font(28),fill=(34,34,34))
    d.text((86,778),'基本資料 / 只供自動測試',font=font(27),fill=(34,34,34))
    # These are synthetic, non-user anchor PII and are counted separately.
    _value(d,gt,label='PERSON',text='王小明',xy=(135,836),fontsize=30,role='anchor')
    _value(d,gt,label='TW_PHONE',text='0912-345-678',xy=(135,888),fontsize=30,role='anchor')
    _value(d,gt,label='EMAIL',text=f'demo{seed%71:02d}@example.com',xy=(135,940),fontsize=28,role='anchor')
    # Local PII challenge far from the labels; plain 2× feature metrics may not
    # detect it even when page-level quality looks strong.
    base_sizes={
        'micro_email':(9,12),'tiny_phone':(8,12),'faint_email':(13,18),
        'local_blur':(12,17),'jpeg_artifacts':(10,15),
        'watermark':(12,17),'mixed_small':(9,14),'clean_control':(23,27)
    }
    lo,hi=base_sizes[recipe]
    size=rng.randint(lo,hi)
    probe_label= 'EMAIL' if recipe in ('micro_email','faint_email','jpeg_artifacts','mixed_small') else 'TW_PHONE'
    probe_value=(f'probe{seed%67:02d}@example.com' if probe_label=='EMAIL' else '0922-456-789')
    x=rng.choice([118,290,480]); y=rng.choice([1070,1120,1160]);
    # Keep probe wholly on-page even at max font size.
    x=min(x,WIDTH-375)
    if recipe=='watermark':
        for iy in range(1040,1240,14): d.line((95,iy,900,iy+16),fill=(228,228,226),width=2)
    color=(22,22,22)
    if recipe=='faint_email': color=(rng.randint(155,199),)*3
    if recipe=='watermark': color=(115,115,115)
    bb=_value(d,gt,label=probe_label,text=probe_value,xy=(x,y),fontsize=size,color=color,role='probe')
    image=_effect(image,bb,recipe,rng)
    return image,{'case':f'{split}_{index+1:02d}_{recipe}', 'recipe':recipe,'font_px':size,
                  'expected_text':gt}

def build(pdf_path:Path, *, count:int, seed:int, split:str) -> list[dict]:
    if not 1<=count<=100: raise ValueError('page count must be 1..100')
    if split not in ('discovery','holdout'): raise ValueError('invalid split')
    labels=[]
    with fitz.open() as doc:
        for i in range(count):
            img,label=render(i,seed,split=split)
            b=BytesIO();img.save(b,format='PNG')
            p=doc.new_page(width=WIDTH/2,height=HEIGHT/2)
            p.insert_image(p.rect,stream=b.getvalue(),keep_proportion=False)
            labels.append(label)
        doc.save(str(pdf_path),garbage=3,deflate=True)
    return labels
