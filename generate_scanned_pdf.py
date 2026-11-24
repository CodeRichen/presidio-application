"""Generate fully synthetic raster-only scanned PDFs; no real private information."""
from __future__ import annotations
import io
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont
import fitz

BASE = Path(__file__).resolve().parent
FONT_FILE = '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'
FONT_BOLD = '/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc'
PAGE_W, PAGE_H = 1200, 1600


def font(size, bold=False):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_FILE, size, index=0)


def create_page(title, fields, kind, page_index):
    # A synthetic single image, not PDF text: scan-like raster image page.
    if kind == 'clear':
        bg, fg, rule, accent = (255, 255, 255), (30, 44, 62), (217, 224, 231), (235, 242, 249)
        title_font, row_font = font(53, True), font(41)
    elif kind == 'low_contrast':
        bg, fg, rule, accent = (248, 248, 247), (148, 150, 151), (233, 233, 232), (243, 244, 243)
        title_font, row_font = font(49, True), font(39)
    else:
        bg, fg, rule, accent = (255, 255, 255), (36, 39, 44), (226, 226, 226), (246, 247, 248)
        title_font, row_font = font(48, True), font(36)
    img = Image.new('RGB', (PAGE_W, PAGE_H), bg)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((38, 50, PAGE_W-38, PAGE_H-100), radius=17, outline=rule, width=2)
    d.rectangle((40, 52, PAGE_W-40, 178), fill=accent)
    d.text((82, 84), title, font=title_font, fill=fg)
    d.text((83, 185), '僅供個資遮蔽模型測試｜所有資料均為合成', font=font(24), fill=(155,155,155))
    ground = []
    y = 275
    label_x, value_x = 105, 390
    for row in fields:
        label, parts = row['label'], row['parts']
        d.text((label_x,y), label, font=row_font, fill=fg)
        x = value_x
        for part in parts:
            val, sensitive, label_type = part
            bb = d.textbbox((x,y),val,font=row_font)
            d.text((x,y), val, font=row_font, fill=fg)
            if sensitive:
                # Raster-to-PDF scale is exactly 2 px / pt.
                ground.append({'label':label_type,'text':val,'bbox_pt':[round(v/2,1) for v in bb]})
            x += d.textlength(val,font=row_font)
        d.line((84,y+83,1110,y+83),fill=rule,width=2)
        y += 111
    d.text((90, PAGE_H-160), f'測試頁 {page_index} / 3  ·  synthetic OCR redaction dataset', font=font(24), fill=(166,166,166))
    return img, ground


def main():
    pages = [
        ('員工聯絡資料｜清晰掃描', [
            {'label':'姓名','parts':[('林家宇', True, 'PERSON')]},
            {'label':'姓名','parts':[('陳雅婷', True, 'PERSON')]},
            {'label':'手機','parts':[('0912-345-678', True, 'TW_PHONE')]},
            {'label':'電子信箱','parts':[('demo@example.com', True, 'EMAIL')]},
            {'label':'部門','parts':[('研發部', False, None)]},
            {'label':'備註','parts':[('請於週五回覆', False, None)]},
        ],'clear'),
        ('診所聯絡表｜低對比掃描', [
            {'label':'姓名','parts':[('王怡君', True, 'PERSON')]},
            {'label':'手機','parts':[('0988-123-456', True, 'TW_PHONE')]},
            {'label':'電子信箱','parts':[('qa.team@example.net', True, 'EMAIL')]},
            {'label':'負責單位','parts':[('行政部', False, None)]},
            {'label':'處理狀態','parts':[('已完成確認', False, None)]},
        ],'low_contrast'),
        ('小字混排紀錄｜小字掃描', [
            {'label':'合成紀錄 A','parts':[('林家宇', True, 'PERSON'),('／', False, None),('0912-345-678',True,'TW_PHONE')]},
            {'label':'合成紀錄 B','parts':[('陳雅婷',True,'PERSON'),('demo@example.com',True,'EMAIL')]},
            {'label':'確認訊息','parts':[('這是一份純合成測試文件',False,None)]},
        ],'small'),
    ]
    doc = fitz.open()
    manifest = {'source':'synthetic', 'description':'Raster-only, no actual personal information', 'page_count':len(pages),'pages':[]}
    for i,(title,rows,kind) in enumerate(pages,1):
        img,gt=create_page(title,rows,kind,i)
        page=doc.new_page(width=PAGE_W/2,height=PAGE_H/2)
        data=io.BytesIO();img.save(data,format='PNG')
        page.insert_image(page.rect,stream=data.getvalue())
        manifest['pages'].append({'page':i,'condition':kind,'expected':gt})
    output=BASE/'synthetic_scanned_pii_3pages.pdf'
    doc.save(str(output),garbage=4,deflate=True)
    doc.close()
    (BASE/'synthetic_scanned_pii_ground_truth.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(output)


if __name__=='__main__':
    main()
