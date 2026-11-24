#!/usr/bin/env python3
# coding: utf-8
"""Synthetic-only OCR A/B benchmark. No production source is changed."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()

def omitted_black_pixels(a, b, dpi=90):
    """Count pixels black in baseline but unmasked in candidate, no pixels exported."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    rows = []
    with fitz.open(a) as before, fitz.open(b) as after:
        if len(before) != len(after):
            raise ValueError('A/B 頁數不同')
        for x, y in zip(before, after):
            pa = x.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
            pb = y.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
            if (pa.width, pa.height, pa.n) != (pb.width, pb.height, pb.n):
                raise ValueError('A/B 影像尺寸不同')
            aa = memoryview(pa.samples)
            bb = memoryview(pb.samples)
            # Keep output only aggregate counts; no pixel information persisted.
            omitted = 0
            changed = 0
            for i in range(0,len(aa),3):
                orig_dark = max(aa[i],aa[i+1],aa[i+2]) <= 10
                curr_dark = max(bb[i],bb[i+1],bb[i+2]) <= 10
                if orig_dark and not curr_dark:
                    omitted += 1
                if aa[i:i+3] != bb[i:i+3]:
                    changed += 1
            rows.append({'changed_pixels': changed,
                         'baseline_black_lost_pixels': omitted})
    return rows

def main(argv=None):
    p=argparse.ArgumentParser(description='固定七頁合成 PDF：雙解析度與 2x OCR A/B 實驗，不改正式程式')
    p.add_argument('--project-root', default='.')
    p.add_argument('--source',default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    p.add_argument('--results-dir',default='v6_ocr_experiment_results')
    p.add_argument('--timeout',type=int,default=1200)
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    source=(root/a.source).resolve()
    out=(root/a.results_dir).resolve()
    v5kit=root/'PII_V5_Security_Acceptance'
    expected=v5kit/'expected_text_and_faces.json'
    if not (root/'handler_media.py').is_file() or not (root/'pdf_secure_output.py').is_file():
        p.error('請從 presidio_app_allen 專案根目錄執行（需 handler_media.py 和 pdf_secure_output.py）')
    if not (v5kit/'v5_acceptance.py').is_file() or not (v5kit/'run_7page_pdf_test.py').is_file() or not expected.is_file():
        p.error('缺少前一步已使用的 PII_V5_Security_Acceptance 測試包')
    if not source.is_file() or sha256(source) != FIXTURE_SHA256:
        p.error('只接受原始固定 SHA-256 的七頁合成 PDF；不接受真實文件')
    if a.check_only:
        print('[PASS] 固定合成資料與 V5 驗收工具均已找到；尚未執行 OCR 或修改檔案')
        return 0
    sys.path.insert(0,str(v5kit))
    from v5_acceptance import check_pdf, audit_status
    report={'schema':'v6_ocr_safe_ab_1','status':'INCOMPLETE',
            'only_synthetic_fixture':True,'fixture_sha256_verified':True,
            'production_code_changed':False,'dual_zoom':{},'zoom2_only':{},
            'warning':'Candidate must NOT be used for real documents; known fixture coverage does not guarantee real-world recall.'}
    with tempfile.TemporaryDirectory(prefix='pii_v6_synthetic_') as td:
        scratch=Path(td)
        for mode in ('dual_zoom','zoom2_only'):
            mode_dir=scratch/mode
            mode_dir.mkdir()
            cmd=[sys.executable,str(Path(__file__).resolve().parent/'v6_child.py'),
                '--root',str(root),'--toolkit',str(v5kit),'--source',str(source),
                '--expected',str(expected),'--outdir',str(mode_dir),'--mode',mode]
            print(f'[V6] 執行 {mode}（合成 PDF；輸出及還原 map 僅留於暫存目錄）',flush=True)
            start=time.perf_counter()
            try:
                # Original handler may print sensitive data; never collect logs.
                done=subprocess.run(cmd,cwd=str(root),stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    timeout=a.timeout,check=False)
            except subprocess.TimeoutExpired:
                report[mode]={'execution_ok':False,'reason':'timeout'}
                break
            record={'execution_ok':done.returncode==0,'elapsed_s':round(time.perf_counter()-start,3)}
            output=mode_dir/'pii_face_7page_masked.pdf'
            audit=mode_dir/'pdf_face_7page_audit.json'
            stat=mode_dir/'v6_stage_counts.json'
            if done.returncode!=0 or not all(x.is_file() for x in (output,audit,stat)):
                record['reason']='runner_failed_or_missing_output'
                report[mode]=record
                break
            try:
                record['stages']=json.loads(stat.read_text(encoding='utf-8'))
                record['ground_truth']=audit_status(audit)
                record['pdf_structure']=check_pdf(output)
                record['output_bytes']=output.stat().st_size
                report[mode]=record
            except Exception as e:
                record['reason']='verification_error_'+type(e).__name__
                report[mode]=record
                break
            print(f"[V6] {mode}: {record['elapsed_s']}s; text {record['ground_truth']['text_regions_pass']}/27; faces {record['ground_truth']['face_map_boxes_pass']}/17",flush=True)
        else:
            report['pixel_diff_per_page']=omitted_black_pixels(
                scratch/'dual_zoom'/'pii_face_7page_masked.pdf',
                scratch/'zoom2_only'/'pii_face_7page_masked.pdf')
            baseline=report['dual_zoom']
            candidate=report['zoom2_only']
            if not baseline['ground_truth']['automated_pass'] or not baseline['pdf_structure']['pass']:
                report['status']='FAIL_BASELINE'
            else:
                passed=candidate['ground_truth']['automated_pass'] and candidate['pdf_structure']['pass']
                report['status']='PASS_SYNTHETIC_ONLY' if passed else 'FAIL_CANDIDATE'
    out.mkdir(parents=True,exist_ok=True)
    (out/'v6_ocr_ab_summary.json').write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    base=report.get('dual_zoom',{})
    cand=report.get('zoom2_only',{})
    btext=base.get('ground_truth',{})
    ctext=cand.get('ground_truth',{})
    rows=report.get('pixel_diff_per_page',[])
    lines=['# V6 OCR 雙解析度 vs 2x-only 合成資料對照', '',
           f"- 結果：**{report['status']}**（PASS_SYNTHETIC_ONLY 也不代表可用於真實文件）", '',
           '| 指標 | 原始雙解析度 | 僅 2x（實驗用） |','|---|---:|---:|',
           f"| 總執行秒數 | {base.get('elapsed_s','—')} | {cand.get('elapsed_s','—')} |",
           f"| 文字 PII 98% 遮蔽 | {btext.get('text_regions_pass','—')}/27 | {ctext.get('text_regions_pass','—')}/27 |",
           f"| 已記錄人臉框 98% 遮蔽 | {btext.get('face_map_boxes_pass','—')}/17 | {ctext.get('face_map_boxes_pass','—')}/17 |",
           f"| PDF 安全結構 | {base.get('pdf_structure',{}).get('pass','—')} | {cand.get('pdf_structure',{}).get('pass','—')} |",
           '', f"- 3x 偵測新增遮蔽框（每頁）：{base.get('stages',{}).get('extra_boxes_per_page','—')}",
           f"- 減少高解析度 OCR 呼叫：{cand.get('stages',{}).get('fallback_skipped','—')} 次", 
           f"- 原版黑色區域在候選版失去覆蓋的像素（每頁，90 DPI）：{[r['baseline_black_lost_pixels'] for r in rows]}",
           '', '**注意：**只使用固定合成 PDF；PII 漏遮或未知人臉仍可能存在。候選策略不得直接部署於真實資料。',
           '還原對照表和中間產物已在暫存目錄刪除。', '']
    (out/'v6_ocr_ab_summary.md').write_text('\n'.join(lines),encoding='utf-8')
    print(f"[V6] {report['status']}；報告：{out/'v6_ocr_ab_summary.json'}",flush=True)
    print('[隱私] 僅分享 v6_ocr_ab_summary.json 與 .md；不要分享 PRIVATE_restore_map_DO_NOT_SHARE.json',flush=True)
    return 0 if report['status']=='PASS_SYNTHETIC_ONLY' else 2

if __name__=='__main__':
    raise SystemExit(main())
