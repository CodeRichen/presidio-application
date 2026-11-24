#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One-pass V6.2 shadow benchmark with independent synthetic GT.

It ALWAYS performs both zooms; selective decisions are counterfactual only.
Never used as production redaction logic. No private map or raw OCR values saved.
"""
from __future__ import annotations
import argparse,hashlib,json,subprocess,sys,tempfile,time
from pathlib import Path
from v62_fixture import build

SEVEN_PDF_SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'


def sha256(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for part in iter(lambda:f.read(1<<20),b''): h.update(part)
    return h.hexdigest()


def seven_gt(path, doc_path):
    try: import pymupdf as fitz
    except ImportError: import fitz
    base=json.loads(Path(path).read_text(encoding='utf8'))['samples']
    out=[]
    with fitz.open(str(doc_path)) as doc:
        if len(base)!=7 or len(doc)!=7: raise ValueError('seven fixture page count mismatch')
        for i,(x,page) in enumerate(zip(base,doc)):
            w,h=x['size_px']
            out.append({'case':f'legacy_seven_page_{i+1}',
                 'expected_text':[{'label':z['label'],
                    'bbox_pdf':[round(z['bbox_px'][0]*page.rect.width/w,3),
                                round(z['bbox_px'][1]*page.rect.height/h,3),
                                round(z['bbox_px'][2]*page.rect.width/w,3),
                                round(z['bbox_px'][3]*page.rect.height/h,3)]}
                    for z in x['expected_text']]})
    return out


def summarize_cases(rows):
    total_gt=sum(x['ground_truth_count'] for x in rows)
    dual=sum(x['dual']['pass_count'] for x in rows)
    sel=sum(x['selective_shadow']['pass_count'] for x in rows)
    primary=sum(x['zoom2']['pass_count'] for x in rows)
    skip=sum(x['would_skip_3x'] for x in rows)
    extra=sum(x['3x_extra_boxes'] for x in rows)
    return {
        'pages':len(rows),'ground_truth_total':total_gt,
        'zoom2_gt_pass':primary,'dual_gt_pass':dual,'selective_gt_pass':sel,
        'fallback_skipped_estimated':skip,'fallback_extra_boxes':extra,
        'measured_2x_s':round(sum(x['zoom2_s'] for x in rows),3),
        'measured_3x_s':round(sum(x['zoom3_s'] for x in rows),3),
        'selective_3x_saved_s_estimate':round(sum(x['omitted_3x_s_estimate'] for x in rows),3),
        'new_gt_misses_against_dual':sum(x['new_miss_against_dual'] for x in rows),
    }


def main(argv=None):
    p=argparse.ArgumentParser(description='V6.2 影子模式：2x、3x 都執行，只模擬選擇性略過，僅限合成 PDF')
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v62_ocr_results')
    p.add_argument('--include-seven-page',action='store_true',help='另外測試原有固定 SHA-256 的七頁合成 PDF')
    p.add_argument('--timeout',type=int,default=1800)
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    if not (root/'handler_media.py').is_file(): p.error('請在 presidio_app_allen 專案根目錄執行，找不到 handler_media.py')
    if not (root/'pdf_secure_output.py').is_file(): p.error('找不到 pdf_secure_output.py')
    if a.timeout<60: p.error('timeout 必須大於或等於 60 秒')
    oldpdf=root/'PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf'
    oldgt=root/'PII_V5_Security_Acceptance/expected_text_and_faces.json'
    if a.include_seven_page:
        if not oldpdf.is_file() or sha256(oldpdf)!=SEVEN_PDF_SHA:
            p.error('固定七頁合成 PDF 不存在或 SHA-256 不符；禁止使用其他文件')
        if not oldgt.is_file(): p.error('找不到 PII_V5_Security_Acceptance/expected_text_and_faces.json')
    # Need fixtures only in TemporaryDirectory. We do NOT create raw PII source in report folder.
    report={'schema':'v62_ocr_shadow_1','status':'INCOMPLETE',
            'production_code_changed':False,'only_synthetic_documents':True,
            'all_zoom3_still_executed':True,
            'policy_is_safe_for_production':False,
            'warning':'A page-quality gate cannot prove no unknown PII was missed. This is a shadow simulation only.',
            'groups':{}}
    with tempfile.TemporaryDirectory(prefix='pii_v62_synthetic_') as td:
        scratch=Path(td)
        fixture=scratch/'v62_hardcases.pdf'
        gt=build(fixture)
        try: import pymupdf as fitz
        except ImportError: import fitz
        with fitz.open(str(fixture)) as doc:
            if len(doc)!=len(gt) or any(page.get_text('text').strip() for page in doc):
                p.error('合成文件驗證失敗（需要只有掃描影像的 PDF）')
        if a.check_only:
            print(f'[PASS] 已生成並驗證 {len(gt)} 頁全新合成影像 PDF；無文字層，未呼叫模型或改動正式程式')
            if a.include_seven_page: print('[PASS] 七頁合成 PDF 的來源 SHA-256 已核對')
            return 0
        jobs=[('hardcases',fixture,gt)]
        if a.include_seven_page: jobs.append(('legacy_seven',oldpdf,seven_gt(oldgt,oldpdf)))
        for name,src,items in jobs:
            gt_path=scratch/(name+'_gt.json')
            gt_path.write_text(json.dumps(items,ensure_ascii=False),encoding='utf8')
            out=scratch/(name+'_diagnostics.json')
            cmd=[sys.executable,str(Path(__file__).resolve().parent/'v62_child.py'),
                 '--root',str(root),'--fixture',str(src),'--gt',str(gt_path),
                 '--sha256',sha256(src),'--report',str(out),
                 '--profile',('legacy_synthetic' if name=='legacy_seven' else 'none')]
            print(f'[V6.2] {name}: {len(items)} 頁，分別執行 2× 與 3× OCR，條件式略過僅作影子模擬',flush=True)
            start=time.perf_counter()
            try:
                # Do NOT capture logs from model/handler: could contain recognized PII.
                rc=subprocess.run(cmd,cwd=str(root),stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=a.timeout).returncode
            except subprocess.TimeoutExpired:
                report['groups'][name]={'status':'TIMEOUT'}
                break
            if rc!=0 or not out.is_file():
                report['groups'][name]={'status':'ERROR','reason':'child_failed_without_logging_private_data'}
                break
            details=json.loads(out.read_text(encoding='utf8'))
            rows=details['cases']
            stats=summarize_cases(rows)
            report['groups'][name]={'status':'RECORDED','wall_s':round(time.perf_counter()-start,3),
                'summary':stats,'cases':rows}
            print(f"[V6.2] {name}: baseline={stats['dual_gt_pass']}/{stats['ground_truth_total']}, "
                  f"candidate={stats['selective_gt_pass']}/{stats['ground_truth_total']}, "
                  f"would-skip={stats['fallback_skipped_estimated']}/{stats['pages']}",flush=True)
        else:
            complete=all(x['status']=='RECORDED' for x in report['groups'].values())
            if complete:
                allstats=[x['summary'] for x in report['groups'].values()]
                if any(s['dual_gt_pass']<s['ground_truth_total'] for s in allstats):
                    report['status']='BASELINE_GROUND_TRUTH_GAPS'
                elif any(s['new_gt_misses_against_dual']>0 for s in allstats):
                    report['status']='CANDIDATE_LOSES_KNOWN_PII'
                else:
                    report['status']='SHADOW_SYNTHETIC_ONLY'
    outdir=(root/a.results_dir).resolve();outdir.mkdir(parents=True,exist_ok=True)
    jpath=outdir/'v62_ocr_shadow_summary.json'
    jpath.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    lines=['# V6.2 OCR 安全性影子模式報告','','**狀態：'+report['status']+'**','',
           '本測試只分析合成影像 PDF；所有頁面仍實際執行 2×+3×，節省時間為推估，不是實測加速。',
           '**任何結果均不得用來直接部署跳過 3× 的策略於真實文件。**','',
           '| 資料組 | 頁數 | 獨立 GT 雙解析度 | 影子選擇性 | 估計略過 3× 次數 | 推估省時 |',
           '|---|---:|---:|---:|---:|---:|']
    for name,x in report['groups'].items():
        if x['status']!='RECORDED':
            lines.append(f'| {name} | — | — | — | — | {x["status"]} |')
            continue
        s=x['summary']
        lines.append(f"| {name} | {s['pages']} | {s['dual_gt_pass']}/{s['ground_truth_total']} | {s['selective_gt_pass']}/{s['ground_truth_total']} | {s['fallback_skipped_estimated']} | {s['selective_3x_saved_s_estimate']:.2f}s |")
    for name,x in report['groups'].items():
        if x['status']!='RECORDED':continue
        lines+=['',f'## {name} 逐頁結果','',
                '| 案例 | 雙解析度 GT | 選擇性 GT | 模擬跳過 3× | 3× 額外框 | 判定理由 |',
                '|---|---:|---:|---|---:|---|']
        for row in x['cases']:
            lines.append(f"| {row['case']} | {row['dual']['pass_count']}/{row['ground_truth_count']} | {row['selective_shadow']['pass_count']}/{row['ground_truth_count']} | {'是' if row['would_skip_3x'] else '否'} | {row['3x_extra_boxes']} | {', '.join(row['reasons'])} |")
    lines+=['','**注意**：GT 只包含明確標註的合成個資；GT 命中率不能證明沒有未知個資。','只保存數量、座標覆蓋率和耗時，不保存 OCR 字詞或私有還原 map。']
    (outdir/'v62_ocr_shadow_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    print('[V6.2] 報告：',jpath,flush=True)
    print('[V6.2] 狀態：',report['status'],'（研究專用；不修改正式 OCR）',flush=True)
    # Incomplete and candidate misses count as experiment error. GT gaps mean baseline needs improvement.
    return 0 if report['status']=='SHADOW_SYNTHETIC_ONLY' else 2

if __name__=='__main__':
    raise SystemExit(main())
