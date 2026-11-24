# -*- coding: utf-8 -*-
"""Locate V6 page-6 zoom3-only redaction (synthetic fixture only). No production writes."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def sha256(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            h.update(chunk)
    return h.hexdigest()

def setup(root):
    fixture=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    expected=root/'PII_V5_Security_Acceptance'/'expected_text_and_faces.json'
    if not fixture.is_file() or sha256(fixture)!=SHA:
        raise ValueError('只接受前述固定七頁合成 PDF，SHA-256 必須一致')
    for file in (expected,root/'handler_media.py',root/'pdf_secure_output.py'):
        if not file.is_file():
            raise FileNotFoundError('缺少測試 Ground Truth 或專案模組：'+file.name)
    return fixture,expected

def markdown(data):
    lines=['# V6.1 第六頁 3× OCR 額外遮蔽框定位', '',
           f"- 狀態：**{data['status']}**（不代表該框安全或可省略）",
           '- 僅使用 SHA-256 驗證的七頁合成 PDF；未更動正式程式',
           f"- 2× 偵測框數：{data['primary_boxes']}",
           f"- 3× 偵測框數：{data['fallback_boxes']}",
           f"- 3× 合併後新增框數：**{data['additional_count']}**",
           f"- 與先前 V6 觀察新增 1 框一致：{data['count_consistent_with_v6']}",
           f"- 本頁 2× / 3× OCR 耗時（秒）：{data['primary_ocr_s']} / {data['fallback_ocr_s']}",
           '', '| 新增框 | 偵測類別 | 來源 | 相對位置（左、上、右、下；%） | 已知 PII 區域重疊 |',
           '|---|---|---|---|---|']
    for i,x in enumerate(data['additional_3x_boxes'],1):
        bb=', '.join(str(k) for k in x['bbox_percent_xyxy'])
        overlap=x['overlap_known_text_label'] or '無（仍須人工確認）'
        lines.append(f"| {i} | {x['label']} | {x['source_type']} | {bb} | {overlap} |")
    if not data['additional_3x_boxes']:
        lines.append('| — | 本次未重現 | — | — | — |')
    lines.extend(['', '## 必要解讀',
      '- 只有標籤、百分比位置及幾何重疊資訊；不輸出原始 OCR 字詞、PII、還原圖或未遮蔽頁面。',
      '- 「不在已知 Ground Truth」不等於不是個資；必須依原始合成資料人工確認。',
      '- 若新增框是敏感內容，正式系統必須保留此 3× 覆蓋；若屬誤判，也不能僅憑單一案例全面取消 3×。',
      '- 建議正式系統繼續保留雙解析度 OCR；本診斷不部署選擇性 OCR。', ''])
    return '\n'.join(lines)

def main(argv=None):
    p=argparse.ArgumentParser(description='V6.1：只針對合成 PDF 第六頁定位 3× OCR 新增的框')
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v61_ocr_results')
    p.add_argument('--check-only',action='store_true')
    p.add_argument('--timeout',type=int,default=900)
    args=p.parse_args(argv)
    root=Path(args.project_root).resolve()
    try:
        fixture,expected=setup(root)
    except (ValueError,FileNotFoundError) as e:
        p.error(str(e))
    if args.check_only:
        print('[PASS] 固定合成 PDF、原始 Ground Truth、handler_media.py 均已找到；尚未執行 OCR')
        return 0
    dest=(root/args.results_dir).resolve()
    with tempfile.TemporaryDirectory(prefix='v61_ocr_locate_') as td:
        private=Path(td)/'report.json'
        cmd=[sys.executable,str(Path(__file__).resolve().parent/'v61_child.py'),
             '--root',str(root),'--fixture',str(fixture), '--expected',str(expected),
             '--report',str(private)]
        try:
            # Never capture OCR/NER logs because they may contain unmasked strings.
            r=subprocess.run(cmd,cwd=str(root),stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                             timeout=args.timeout,check=False)
        except subprocess.TimeoutExpired:
            print('[ERROR] OCR 診斷超時；未寫出包含敏感資訊的檔案')
            return 2
        if not private.exists():
            print('[ERROR] 子程序未產出安全摘要。請先確認與 V6 相同的模型環境。')
            return 2
        data=json.loads(private.read_text(encoding='utf-8'))
        if r.returncode or data.get('status')!='MANUAL_REVIEW_REQUIRED':
            print('[ERROR] 診斷失敗，錯誤類型：'+str(data.get('error_type','Unknown')))
            return 2
        dest.mkdir(parents=True,exist_ok=True)
        (dest/'v61_page6_extra.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        (dest/'v61_page6_extra.md').write_text(markdown(data),encoding='utf-8')
    print(f"[完成] 找到 {data['additional_count']} 個 3× 新增框，請人工判斷是否為真實 PII")
    print('[結果] '+str(dest/'v61_page6_extra.json'))
    print('[提醒] 不要將正常模式的原始 PDF、私有還原 map 或 OCR 原文上傳')
    return 0

if __name__=='__main__':
    raise SystemExit(main())
