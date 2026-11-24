#!/usr/bin/env python3
"""Read-only check for known PyMuPDF save usage. Never rewrite user project automatically."""
import argparse
from pathlib import Path
import re

ap=argparse.ArgumentParser()
ap.add_argument('--project',default='.',type=Path)
a=ap.parse_args()
p=a.project/'handler_media.py'
if not p.is_file():ap.error('找不到 handler_media.py')
s=p.read_text(encoding='utf-8')
try:
    block=s.split('def mask_pdf(',1)[1].split('\ndef restore_pdf(',1)[0]
except IndexError:
    raise SystemExit('檔案結構和已知版本不同，無法靜態判斷。')
for ln in block.splitlines():
    if 'doc.save(' in ln:
        print('目前 mask_pdf 儲存：',ln.strip())
        if 'garbage=' not in ln:
            print('[注意] 建議對新輸出檔使用 doc.save(output_path, garbage=4, deflate=True)')
            print('        這能減少未引用的舊物件，但不能替代 PDF 安全稽核。')
        else:
            print('[資訊] 已指定 garbage 參數；仍須執行內容與底層物件檢查。')
        break
else:
    print('找不到 doc.save()；請人工檢查 mask_pdf()。')
