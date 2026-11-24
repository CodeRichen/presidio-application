"""Generate wholly synthetic raster PDFs and independent label geometry in temp storage."""
from __future__ import annotations
from io import BytesIO
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    import pymupdf as fitz
except ImportError:
    import fitz

W,H=1000,1300
# Conservative candidates: common Windows fonts, then Linux dev fallback.
FONTS=[
    r'C:\Windows\Fonts\msjh.ttc',r'C:\Windows\Fonts\msyh.ttc',
    r'C:\Windows\Fonts\mingliu.ttc',r'C:\Windows\Fonts\arial.ttf',
    '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc',
    '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
]


def font(size):
    for path in FONTS:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except (OSError, ValueError):
                continue
    raise RuntimeError('找不到可用的 TrueType / CJK 字型；請檢查 Windows 字型')


def draw_record(draw, gt, x, y, size, identifier, color):
    ft=font(size)
    vals=[('PERSON','王小明' if identifier%2 else '李雅婷'),
          ('TW_PHONE','0912-345-678'),
          ('EMAIL',f'demo{identifier:02d}@example.com')]
    labels=['姓名：','電話：','Email：']
    for idx,((label,value),tag) in enumerate(zip(vals,labels)):
        yy=y+idx*int(size*2.65)
        draw.text((x,yy),tag,font=ft,fill=color)
        prefix=draw.textbbox((x,yy),tag,font=ft)
        xx=prefix[2]+max(8,size//3)
        draw.text((xx,yy),value,font=ft,fill=color)
        # Tight independent glyph bounding box, stored without PII values.
        b=draw.textbbox((xx,yy),value,font=ft)
        gt.append({'label':label,'bbox_pdf':[round(b[0]/2,3),round(b[1]/2,3),round(b[2]/2,3),round(b[3]/2,3)]})


def build(path:Path):
    cases=[
      ('large_clean',[(130,295,38,1)],'normal'),
      ('small_text',[(130,330,17,2)],'normal'),
      ('tiny_text',[(130,345,12,3)],'normal'),
      ('low_contrast',[(130,320,25,4)],'low_contrast'),
      ('blurred_scan',[(130,320,27,5)],'blur'),
      ('large_title_tiny_pii',[(130,410,13,6)],'mixed'),
      ('mixed_sizes',[(85,220,35,7),(85,720,15,8)],'normal'),
      ('multi_records',[(90,230,29,9),(90,740,23,10)],'normal'),
    ]
    page_gt=[]
    with fitz.open() as doc:
        for name, blocks, effect in cases:
            bg=(250,250,250)
            if effect=='low_contrast': bg=(228,228,228)
            img=Image.new('RGB',(W,H),bg)
            d=ImageDraw.Draw(img)
            if effect=='mixed':
                for a in range(0,900,72):
                    d.rectangle((30+a//7,70+a//9,800,74+a//9),fill=(240,240,240))
            title='合成文件 OCR 測試  SAMPLE'
            d.text((85,95),title,font=font(35),fill=(15,15,15))
            d.text((85,155),'以下姓名電話及信箱均為虛構測試資料',font=font(19),fill=(25,25,25))
            if effect=='mixed':
                d.text((85,248),'大型標題 LARGE HEADING',font=font(48),fill=(10,10,10))
            if name=='multi_records':
                d.text((90,680),'第二份資料',font=font(30),fill=(40,40,40))
            if effect=='low_contrast': color=(145,145,145)
            else: color=(22,22,22)
            gt=[]
            for x,y,size,identifier in blocks:
                draw_record(d,gt,x,y,size,identifier,color)
            if effect=='blur': img=img.filter(ImageFilter.GaussianBlur(1.8))
            buffer=BytesIO();img.save(buffer,format='PNG')
            p=doc.new_page(width=W/2,height=H/2)
            p.insert_image(p.rect,stream=buffer.getvalue(),keep_proportion=False)
            page_gt.append({'case':name,'expected_text':gt})
        doc.save(str(path),garbage=3,deflate=True)
    return page_gt
