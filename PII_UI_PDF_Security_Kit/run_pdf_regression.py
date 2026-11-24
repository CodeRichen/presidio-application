#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run the REAL handler_media.mask_pdf on the synthetic 3-page sample, then audit it.

Does not modify the source PDF, user's app configuration, or sensitive-item lists.
NOTE: Uses an isolated map file containing synthetic crops and removes it after the test.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def main():
    ap=argparse.ArgumentParser()
    root=Path(__file__).resolve().parent
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--engine',choices=['rapid','windows'],default='rapid')
    ap.add_argument('--output-dir',type=Path,default=root/'results/real_pipeline')
    ap.add_argument('--ocr-images',action='store_true')
    args=ap.parse_args()
    project=args.project.resolve()
    if not (project/'handler_media.py').is_file():ap.error('專案根目錄找不到 handler_media.py')
    os.chdir(project)
    sys.path.insert(0,str(project))
    os.environ['PII_OCR_BACKEND']=args.engine
    os.environ.setdefault('PII_NER_BACKEND','ac')
    os.environ.setdefault('PII_C_LONG_MODE','chunk')
    os.environ.setdefault('PII_MEDIA_CACHE','1')
    os.environ.setdefault('PII_ZH_SINGLE_PASS','1')

    # Import core to apply the same media preprocess wrapping as the UI.
    # Import is intended to be possible in the project's normal Windows .venv.
    import core
    import handler_media
    from audit_pdf_security import audit
    src=root/'fixtures/synthetic_scanned_pii_3pages.pdf'
    source_sha=hashlib.sha256(src.read_bytes()).hexdigest()
    gt=json.loads((root/'fixtures/synthetic_scanned_pii_ground_truth.json').read_text(encoding='utf-8'))
    outdir=args.output_dir.resolve()
    outdir.mkdir(parents=True,exist_ok=True)
    dest=outdir/f'synthetic_scanned_masked_{args.engine}.pdf'
    map_file=outdir/'TEMP_map_contains_original_synthetic_crops.json'
    print('[開始] 使用真實 handler_media.mask_pdf()，模型會在本機執行')
    try:
        t0=time.perf_counter()
        entries=handler_media.mask_pdf(str(src),str(dest),str(map_file))
        elapsed=time.perf_counter()-t0
        result=audit(src,dest,manifest=gt,ocr_images=args.ocr_images)
        if hashlib.sha256(src.read_bytes()).hexdigest()!=source_sha:
            result['status']='FAIL'
            result['findings'].append({'status':'FAIL','code':'SOURCE_MODIFIED','detail':'執行後原始 PDF 的 SHA256 發生改變'})
    finally:
        # Do not leave recoverable crops on disk (only synthetic in this kit).
        if map_file.exists():map_file.unlink()
    result['handler_media_mask_pdf_seconds']=round(elapsed,4)
    result['handler_media_redaction_entries']=len(entries or [])
    result['ocr_engine']=args.engine
    report=outdir/f'pdf_security_real_{args.engine}.json'
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'[時間] 3 頁 PDF 完整 handler_media.mask_pdf：{elapsed:.3f} 秒')
    print('[遮蔽框] ',len(entries or []))
    print('[稽核狀態] ',result['status'])
    for f in result['findings']:
        print(f"  [{f['status']}] {f['code']}")
    print('[報告] ',report)
    print('[輸出] ',dest)
    print('[清理] 已移除可還原裁切對照表')
    return 2 if result['status']=='FAIL' else 0

if __name__=='__main__':raise SystemExit(main())
