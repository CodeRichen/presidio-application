"""Generate a reproducible synthetic Traditional-Chinese OCR/PII benchmark.
No internet/model dependencies. Uses installed system fonts (not redistributed).
"""
from __future__ import annotations
import argparse
import json
import random
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont, ImageFilter

W, H = 1120, 590
CONDITIONS = ["clear", "low_contrast", "small_text", "blur", "tilted", "jpeg_artifacts", "low_resolution", "two_column", "wrapped_email"]
NAMES_A = ["林家宇", "王怡君", "陳雅婷", "張雅雯"]
NAMES_B = ["黃子豪", "李佩珊", "吳志明", "周佳穎"]
PHONES = ["0912-345-678", "0988-123-456", "0933-456-789", "0977-111-222"]
EMAILS = ["demo@example.com", "qa.team@example.net", "user.test@example.org", "contact@example.com"]


def select_font(size: int, override: str = ""):
    fonts = [override, "C:/Windows/Fonts/msjh.ttc", "C:/Windows/Fonts/msjhbd.ttc",
             "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/mingliu.ttc",
             "/usr/share/fonts/truetype/arphic-bkai00mp/bkai00mp.ttf",
             "/usr/share/fonts/truetype/arphic-bsmi00lp/bsmi00lp.ttf"]
    for f in fonts:
        if f and Path(f).is_file():
            try:
                return ImageFont.truetype(f, size)
            except OSError:
                pass
    raise RuntimeError("找不到能顯示中文的字型；請指定 --font C:/Windows/Fonts/msjh.ttc")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=Path(__file__).parent / "data")
    parser.add_argument("--font", default="")
    args = parser.parse_args()
    out = args.out
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "masks").mkdir(parents=True, exist_ok=True)
    cases = []
    for kind in CONDITIONS:
        for idx in range(4):
            case_id = f"{kind}_{idx+1:02d}"
            base_bg = (251, 251, 249)
            if kind == "low_contrast":
                ink = (165, 169, 167)
            else:
                ink = (32, 43, 57)
            image = Image.new("RGB", (W, H), base_bg)
            safe_mask = Image.new("L", (W, H), 0)
            targets = []
            big = select_font(35 if kind != "small_text" else 19, args.font)
            heading_font = select_font(25 if kind != "small_text" else 17, args.font)
            d = ImageDraw.Draw(image)
            if kind == "low_contrast":
                d.rectangle((24, 45, W-24, H-33), outline=(224, 224, 220), width=1)
            else:
                d.rectangle((24, 45, W-24, H-33), outline=(225, 229, 231), width=1)

            # Ground-truth ink masks use the same font and pixel coordinates as the rendered text.
            def safe(text, x, y, f=None):
                f = f or big
                d.text((x,y), text, font=f, fill=ink)
                ImageDraw.Draw(safe_mask).text((x,y), text, font=f, fill=255)
            def pii(value, label, entity_id, pieces):
                m = Image.new("L", (W,H), 0)
                md = ImageDraw.Draw(m)
                for text, x, y in pieces:
                    d.text((x,y), text, font=big, fill=ink)
                    md.text((x,y), text, font=big, fill=255)
                targets.append({"id": entity_id, "type": label, "text": value, "mask": m})

            safe(f"合成 OCR 測試  |  {case_id}", 58, 67, heading_font)
            if kind == "two_column":
                safe("姓名：", 65, 178)
                pii(NAMES_A[idx], "PERSON", "person_a", [(NAMES_A[idx], 177, 178)])
                safe("電話：", 65, 288)
                pii(PHONES[idx], "TW_PHONE", "phone", [(PHONES[idx], 177, 288)])
                safe("聯絡人：", 555, 178)
                pii(NAMES_B[idx], "PERSON", "person_b", [(NAMES_B[idx], 707, 178)])
                safe("Email：", 555, 288)
                pii(EMAILS[idx], "EMAIL", "email", [(EMAILS[idx], 707, 288)])
            else:
                safe("姓名：", 58, 170)
                pii(NAMES_A[idx], "PERSON", "person_a", [(NAMES_A[idx], 180, 170)])
                safe("電話：", 415, 170)
                pii(PHONES[idx], "TW_PHONE", "phone", [(PHONES[idx], 536, 170)])
                safe("聯絡人：", 58, 285)
                pii(NAMES_B[idx], "PERSON", "person_b", [(NAMES_B[idx], 214, 285)])
                safe("Email：", 415, 285)
                if kind == "wrapped_email":
                    cut = EMAILS[idx].index("@") + 1
                    pii(EMAILS[idx], "EMAIL", "email", [(EMAILS[idx][:cut], 548, 285), (EMAILS[idx][cut:], 548, 337)])
                else:
                    pii(EMAILS[idx], "EMAIL", "email", [(EMAILS[idx], 548, 285)])
            safe("部門：研發部    備註：請於週五回覆", 58, 430, heading_font)
            safe("Synthetic records / not real people", 58, 486, heading_font)

            masks = [item.pop("mask") for item in targets]
            if kind == "blur":
                image = image.filter(ImageFilter.GaussianBlur(radius=1.6))
            elif kind == "tilted":
                angle = 8 if idx % 2 == 0 else -7
                image = image.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=base_bg)
                masks = [m.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False) for m in masks]
                safe_mask = safe_mask.rotate(angle, resample=Image.Resampling.BILINEAR, expand=False)
            elif kind == "low_resolution":
                image = image.resize((520, 274), Image.Resampling.BILINEAR).resize((W,H), Image.Resampling.BILINEAR)
                masks = [m.resize((520,274),Image.Resampling.BILINEAR).resize((W,H),Image.Resampling.BILINEAR) for m in masks]
                safe_mask = safe_mask.resize((520,274),Image.Resampling.BILINEAR).resize((W,H),Image.Resampling.BILINEAR)
            elif kind == "jpeg_artifacts":
                import io
                buf = io.BytesIO()
                image.save(buf, format="JPEG", quality=35, subsampling=2)
                buf.seek(0)
                image = Image.open(buf).convert("RGB")

            for item, m in zip(targets, masks):
                m = m.point(lambda v: 255 if v >= 60 else 0)
                bbox = m.getbbox()
                if bbox is None:
                    raise AssertionError(case_id + " mask empty")
                path = f"masks/{case_id}_{item['id']}.png"
                m.save(out / path)
                item["mask_path"] = path
                item["bbox"] = list(bbox)
            image_path = f"images/{case_id}.png"
            image.save(out / image_path)
            safe_path = f"masks/{case_id}_safe.png"
            safe_mask.point(lambda v: 255 if v>=60 else 0).save(out / safe_path)
            cases.append({"id":case_id,"condition":kind,"image":image_path,"safe_mask":safe_path,
                          "targets":targets})
    meta = {"version":1, "synthetic_only":True,"conditions":CONDITIONS,
            "notes":"Text ground truth + glyph ink masks; 4 sensitive entities per image. No real PII.",
            "cases":cases}
    (out / "labels.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"完成 {len(cases)} 張合成圖片、{sum(len(c['targets']) for c in cases)} 個標註區域。")

if __name__ == "__main__":
    main()
