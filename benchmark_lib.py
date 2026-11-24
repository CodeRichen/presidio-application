"""No-model scoring helpers for the synthetic OCR/PII benchmark."""
from __future__ import annotations
import unicodedata
import math
from PIL import Image, ImageChops, ImageDraw


def compact(text):
    text = unicodedata.normalize("NFKC", str(text))
    for old in "‐‑‒–—−﹣":
        text = text.replace(old, "-")
    return "".join(text.split()).lower()


def ocr_joined_text(words):
    lines = {}
    for w in words:
        lines.setdefault(w["line_id"], []).append(w)
    return "".join("".join(str(w["text"]) for w in lines[k]) for k in sorted(lines))


def mask_from_boxes(size, boxes, padding=0):
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    w, h = size
    for box in boxes:
        x0, y0, x1, y1 = [float(x) for x in box[:4]]
        if x1 <= x0 or y1 <= y0:
            continue
        left = max(0, min(w, int(math.floor(x0 - padding))))
        top = max(0, min(h, int(math.floor(y0 - padding))))
        right = max(0, min(w, int(math.ceil(x1 + padding))))
        bottom = max(0, min(h, int(math.ceil(y1 + padding))))
        if left < right and top < bottom:
            draw.rectangle((left, top, right - 1, bottom - 1), fill=255)
    return mask


def count_ink(image):
    return image.point(lambda value: 255 if value > 127 else 0).histogram()[255]


def fraction_covered(ink_mask, redaction_mask):
    total = count_ink(ink_mask)
    if not total:
        return 0.0
    covered = count_ink(ImageChops.multiply(ink_mask, redaction_mask))
    return covered / total


def union_masks(mask_a, mask_b):
    return ImageChops.lighter(mask_a, mask_b)


def draw_redacted(image, redaction_mask):
    result = image.convert("RGB").copy()
    result.paste((0,0,0), mask=redaction_mask)
    return result


def score_one_case(image, case, data_dir, words, boxes, pad=0, threshold=.98):
    redact = mask_from_boxes(image.size, boxes, pad)
    text = compact(ocr_joined_text(words))
    detections = []
    for target in case["targets"]:
        with Image.open(data_dir / target["mask_path"]) as m:
            coverage = fraction_covered(m.convert("L"), redact)
        detections.append({"id": target["id"],"label":target["type"],
                           "expected":target["text"],"text_found":compact(target["text"]) in text,
                           "ink_coverage":round(coverage,6), "fully_covered":coverage >= threshold})
    with Image.open(data_dir / case["safe_mask"]) as m:
        safe_loss = fraction_covered(m.convert("L"), redact)
    return {"targets":detections,"safe_ink_covered_fraction":round(safe_loss,6),"redaction_mask":redact}
