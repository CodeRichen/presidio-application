"""Offline diagnostic for OCR-to-PII, avoiding printing original PII text.
Usage: python diagnose_ocr_pii.py .\ocr_samples\sample_ocr_small.png
"""
import argparse
import re
from PIL import Image
import handler_media as h
from ocr_backend import rapid_ocr_words

EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')
PHONE = re.compile(r'09\d{2}[- ]?\d{3}[- ]?\d{3}')

def main():
    p = argparse.ArgumentParser()
    p.add_argument('image')
    args = p.parse_args()
    img = Image.open(args.image).convert('RGB')
    prepared, scale = h.preprocess_for_ocr(img)
    words = rapid_ocr_words(prepared)
    lines = h._group_lines(words)
    print('OCR 行數：', len(lines))
    for i, line in enumerate(lines):
        spaced = ' '.join(w['text'] for w in line)
        compact = ''.join(w['text'] for w in line)
        print(f'行 {i+1}: spaced_email={bool(EMAIL.search(spaced))}, compact_email={bool(EMAIL.search(compact))}; '
              f'spaced_phone={bool(PHONE.search(spaced))}, compact_phone={bool(PHONE.search(compact))}')
    boxes = h.find_sensitive_boxes_in_words(words, scale=scale)
    print('PII 遮蔽框數量：', len(boxes))
    for box in boxes:
        print('  類型=', box[4], '座標=', tuple(round(float(x), 1) for x in box[:4]), '來源=', box[7])
    print('說明：有 EMAIL 辨識但沒有對應的 EMAIL 遮蔽框，表示 OCR -> PII 判斷/座標仍有問題。')

if __name__ == '__main__':
    main()
