#!/usr/bin/env python3
# coding: utf-8
"""V6.6.1 read-only OCR-stage profile, fixed synthetic seven-page PDF only.

The official production code is never changed. Every actual PDF and private
restore map remains in TemporaryDirectory and is deleted on normal exit.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

KIT = 'PII_V5_Security_Acceptance'
PHASES = ('pdf_render','pixmap_to_pil','preprocess','ocr_backend','pii_boxes','other_collect')


def require_gate(root, source):
    kit = root / KIT
    if not (kit/'v5_acceptance.py').is_file():
        raise FileNotFoundError('MISSING_V5_SECURITY_ACCEPTANCE')
    sys.path.insert(0, str(kit))
    import v5_acceptance as gate
    gate.check_setup(root, source, kit)
    expected = json.loads((kit/'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(expected['samples']) != 7 or sum(len(row['expected_text']) for row in expected['samples']) != 27:
        raise RuntimeError('INVALID_GROUND_TRUTH_FIXTURE')
    return gate, kit


def child_env():
    env = os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5':'1', 'PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid', 'PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1', 'PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0', 'PII_MEDIA_CACHE':'1',
        'PII_ZH_SINGLE_PASS':'1',
        'PII_V661_SAMPLE_EVERY': '2',
    })
    return env


def check_profile(profile, processing):
    """Reject silently missing hooks and double counted metrics."""
    if profile.get('schema') != 'pii_v661_ocr_profile_1':
        return False
    rows = profile.get('collections')
    if not isinstance(rows, list) or len(rows) != 14:
        return False
    if len({(r.get('page'),r.get('zoom')) for r in rows}) != 14:
        return False
    if {r.get('page') for r in rows} != set(range(1,8)):
        return False
    if {r.get('zoom') for r in rows} != {2.0,3.0}:
        return False
    if profile.get('scanned_pages') != 7 or profile.get('secure_export_calls') != 1:
        return False
    for zoom in ('2x','3x'):
        group = profile.get('by_zoom',{}).get(zoom,{})
        if group.get('pages') != 7:
            return False
        if any(group.get('phase_calls',{}).get(p) != 7 for p in PHASES[:5]):
            return False
        if float(group.get('collect_total',-1)) <= 0:
            return False
    if int(profile.get('code_detect_calls',0)) <= 0:
        return False
    hot=profile.get('hotspots',{})
    if hot.get('schema') != 'pii_v661_ocr_hotspots_1':
        return False
    for zoom in ('2x','3x'):
        if hot.get('calls_by_zoom',{}).get(zoom)!=7:
            return False
        if not (1 <= hot.get('profiled_calls_by_zoom',{}).get(zoom,0) <= 7):
            return False
    if hot.get('unique_static_functions',0)<=0:
        return False
    for r in rows:
        total = float(r['collect_total_s'])
        if total <= 0 or not 0.0 <= sum(float(r[p+'_s']) for p in PHASES[:5]) <= total + 0.25:
            return False
    collect_total = sum(float(profile['by_zoom'][z]['collect_total']) for z in ('2x','3x'))
    export_total = float(profile['secure_export_wall_s'])
    return 0.0 < collect_total <= processing + 2.0 and export_total >= 0.0 and collect_total + export_total <= processing + 2.0


def single_run(root, source, kit, tool, trial_dir, timeout, gate):
    trial_dir.mkdir(parents=True,exist_ok=False)
    cmd = [sys.executable, str(tool/'v661_child.py'),
           '--project-root',str(root), '--source',str(source),
           '--runner',str(kit/'run_7page_pdf_test.py'),
           '--expected',str(kit/'expected_text_and_faces.json'),
           '--results-dir',str(trial_dir)]
    started = time.perf_counter()
    try:
        p=subprocess.run(cmd,cwd=str(root),env=child_env(),stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         timeout=timeout,check=False)
        rc=p.returncode
    except subprocess.TimeoutExpired:
        rc=None
    out={'execution_ok':False,'safe':False,'elapsed_s':round(time.perf_counter()-started,3),
         'reason':None}
    if rc is None:
        out['reason']='TIMEOUT'
        return out
    if rc != 0:
        out['reason']='CHILD_RUN_FAILED'
        return out
    audit=trial_dir/'pdf_face_7page_audit.json'
    pdf=trial_dir/'pii_face_7page_masked.pdf'
    metric_path=trial_dir/'v661_child_metrics.json'
    if not all(x.is_file() for x in (audit,pdf,metric_path)):
        out['reason']='MISSING_OUTPUT'
        return out
    try:
        profile=json.loads(metric_path.read_text(encoding='utf-8'))
        gt=gate.audit_status(audit)
        structure=gate.check_pdf(pdf)
        processing=float(profile.get('fixture_processing_s',-1))
        valid=(gt['automated_pass'] and structure['pass'] and
               gt['text_regions_pass']==27 and gt['face_map_boxes_pass']==17)
        if not valid:
            out['reason']='SECURITY_GATE_FAILED'
            return out
        if processing <= 0 or not check_profile(profile, processing):
            out['reason']='INSTRUMENTATION_FAILED'
            return out
        # Never copy private map, OCR contents, PDF or command output to report.
        out.update({'execution_ok':True,'safe':True,
                    'processing_s':round(processing,3),
                    'with_audit_s':profile['fixture_plus_audit_s'],
                    'pre_run_setup_s':profile['pre_run_setup_s'],
                    'text_regions_pass':27,'face_boxes_pass':17,
                    'pdf_structure_pass':True,'profile':profile})
    except (ValueError,KeyError,TypeError,OSError,OverflowError):
        out['reason']='REPORT_VALIDATION_FAILED'
    return out


def strict_pixels(gate, first, second):
    result=gate.compare_page_pixels(first,second,dpi=150)
    return {'pages_compared':result['pages_compared'],
            'identical_pages':result['identical_pages'],
            'all_identical':bool(result['all_identical'] and
                                 result['pages_compared']==7 and result['identical_pages']==7)}


def summary_for(runs):
    if not runs:
        return {}
    aggregated={}
    totals={}
    for label in ('2x','3x'):
        aggregated[label]={}
        for phase in PHASES + ('collect_total',):
            vals=[float(r['profile']['by_zoom'][label][phase]) for r in runs]
            aggregated[label][phase+'_median_s']=round(statistics.median(vals),4)
    values={phase:sum(aggregated[label][phase+'_median_s'] for label in ('2x','3x'))
            for phase in PHASES}
    total=sum(values.values())
    for phase,v in values.items():
        totals[phase]={'median_approx_s':round(v,3),
                       'share_of_collect_pct':round(100*v/total,2) if total>0 else 0}
    processing_median=statistics.median(float(r['processing_s']) for r in runs)
    export_median=statistics.median(float(r['profile']['secure_export_wall_s']) for r in runs)
    collect_median=statistics.median(sum(r['profile']['by_zoom'][label]['collect_total']
                               for label in ('2x','3x')) for r in runs)
    # 'Other processing' includes face recognition, mapping, redactions, file IO,
    # and excludes the separate audit. This is an approximation, not a precise
    # whole-program accounting for parallel worker operations.
    return {'backend_hotspots':runs[0]['profile']['hotspots'],
            'processing_median_s':round(processing_median,3),
            'collect_median_s':round(collect_median,3),
            'export_median_s':round(export_median,3),
            'other_processing_approx_s':round(max(0.0,processing_median-collect_median-export_median),3),
            'by_zoom':aggregated,'phases':totals,
            'dominant_collect_phase':max(values,key=values.get)}


def write_report(report, dest):
    dest.mkdir(parents=True,exist_ok=True)
    (dest/'v661_ocr_hotspots_summary.json').write_text(
        json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    s=report.get('summary',{})
    lines=['# V6.6.1：OCR 流程細部效能診斷（僅合成 PDF）','',
           f"**狀態：{report['status']}**",'',
           '- 保留 2×+3× OCR、原本 PII / 人臉偵測和 V5 安全輸出；正式程式未修改。',
           '- 只使用固定 SHA-256 七頁合成 PDF；中間 PDF 和私有 map 正常結束時會刪除。',
           '- 各階段採暫時性計時包裝；量測有額外開銷，**不是加速 A/B**。',
           '- `pii_boxes` 包含 `code_detect`，不可重複加總。',
           '- `ocr_backend` 包含 OCR 引擎呼叫及其轉換；不等同純模型推論時間。',
           '- `other_collect` 是 OCR 收集函式中其餘時間（座標轉換／IP 補抓等）。',
           '- 不能從 27/27 和 17/17 推論真實文件沒有漏判。',
           '',
           '| 指標 | 數值 |','|---|---:|',
           f"| 測試次數 | {len(report.get('runs',[]))} |",
           f"| 七頁 PDF 純處理中位數 | {s.get('processing_median_s','—')} 秒 |",
           f"| 2×+3× OCR 收集合計 | {s.get('collect_median_s','—')} 秒 |",
           f"| V5 安全輸出 | {s.get('export_median_s','—')} 秒 |",
           f"| 其他處理估計 | {s.get('other_processing_approx_s','—')} 秒 |",
           '', '## OCR 細分結果（所有頁面合計）','',
           '| 階段 | 2× 中位數 (秒) | 3× 中位數 (秒) | 收集時間占比 |',
           '|---|---:|---:|---:|']
    labels={'pdf_render':'PDF 畫面渲染','pixmap_to_pil':'Pixmap 轉 PIL',
            'preprocess':'OCR 影像前處理','ocr_backend':'OCR 引擎辨識',
            'pii_boxes':'PII 偵測與座標框','other_collect':'收集函式其餘處理'}
    for phase in PHASES:
        z2=s.get('by_zoom',{}).get('2x',{}).get(phase+'_median_s','—')
        z3=s.get('by_zoom',{}).get('3x',{}).get(phase+'_median_s','—')
        share=s.get('phases',{}).get(phase,{}).get('share_of_collect_pct','—')
        lines.append(f'| {labels[phase]} | {z2} | {z3} | {share}% |')
    lines+=['',f"- 收集函式中占比最高的項目：`{s.get('dominant_collect_phase','—')}`。",
            '', '## 各輪狀態', '',
            '| 輪次 | 處理秒數 | 2× 頁數 | 3× 頁數 | 27/27 | 17/17 | PDF 結構 |',
            '|---|---:|---:|---:|---|---|---|']
    for i,r in enumerate(report.get('runs',[]),1):
        p=r.get('profile',{})
        lines.append(f"| {i} | {r.get('processing_s','—')} | "
                     f"{p.get('by_zoom',{}).get('2x',{}).get('pages','—')} | "
                     f"{p.get('by_zoom',{}).get('3x',{}).get('pages','—')} | "
                     f"{r.get('text_regions_pass','—')}/27 | "
                     f"{r.get('face_boxes_pass','—')}/17 | "
                     f"{r.get('pdf_structure_pass','—')} |")
    hot=s.get('backend_hotspots',{})
    lines+=['', '## OCR 呼叫的 cProfile 熱點（僅抽樣，非純推論計時）', '',
            f"- 每頁抽樣間隔：{hot.get('sample_every_page','—')}",
            f"- 實際 OCR 呼叫：{hot.get('calls_by_zoom',{})}",
            f"- 取樣 OCR 呼叫：{hot.get('profiled_calls_by_zoom',{})}",
            '', '| 模組檔名 | 函式 | 次數 | self 秒 | cumulative 秒 |',
            '|---|---|---:|---:|---:|']
    for row in hot.get('top_self_time',[])[:15]:
        lines.append(f"| `{row['module_file']}` | `{row['function']}` | {row['calls']} | {row['self_s']} | {row['cumulative_s']} |")
    lines+=['','注意：cProfile 有執行開銷；函式累積時間互相包含，不能相加。OCR 原生函式不一定能拆成偵測與辨識兩階段。']
    lines+=['','## 安全與效能限制','',
            '- 此工具不改動正式 GUI、模型、快取、辨識次數及輸出政策。',
            '- 報告不記錄 OCR 文字、函式參數、裁圖、私人還原表或文字雜湊。',
            '- 逐頁時間只與合成 PDF 相關，不含 PII 原文。',
            '- 若強制終止，需檢查 `%TEMP%` 內 `pii_v661_` 前綴的殘留暫存資料夾。',
            '- 下一步需針對占比最高的階段提出 A/B，通過安全回歸才可能部署。','']
    (dest/'v661_ocr_hotspots_summary.md').write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description='Synthetic-only dual OCR phase profiler, no production changes')
    p.add_argument('--project-root',default='.')
    p.add_argument('--source',default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    p.add_argument('--results-dir',default='v661_ocr_results')
    p.add_argument('--check-only',action='store_true')
    p.add_argument('--repeats',type=int,choices=(1,2,3),default=1)
    p.add_argument('--timeout',type=int,default=900)
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    source=(root/a.source).resolve()
    dest=(root/a.results_dir).resolve()
    tool=Path(__file__).resolve().parent
    if tool.parent!=root or tool.name!='PII_V661_OCR_Backend_Hotspots':
        raise RuntimeError('INSTALL_V66_IN_PROJECT_ROOT')
    gate,kit=require_gate(root,source)
    if a.check_only:
        print('CHECK_OK: V5 gate, synthetic fixture SHA-256, installed tool layout')
        return 0
    report={'schema':'pii_v661_ocr_profile_ab_1','status':'INCOMPLETE',
            'fixture_sha256_verified':True,'production_code_changed':False,
            'only_synthetic_documents':True,'dual_zoom_kept':True,
            'temporary_private_maps_deleted_on_normal_exit':True,
            'repeats':a.repeats,'runs':[],'pixel_comparison':[],
            'summary':{},'warning':'Profiling only; no production speedup claimed; GUI untested.'}
    try:
        with tempfile.TemporaryDirectory(prefix='pii_v661_') as temp:
            scratch=Path(temp)
            prior_pdf=None
            for i in range(a.repeats):
                print(f'[V6.6.1] Profiling synthetic seven-page PDF: {i+1}/{a.repeats}',flush=True)
                trial=scratch/f'run_{i+1:02d}'
                result=single_run(root,source,kit,tool,trial,a.timeout,gate)
                report['runs'].append(result)
                if not result.get('safe'):
                    report['status']=result.get('reason') or 'INCOMPLETE'
                    break
                now_pdf=trial/'pii_face_7page_masked.pdf'
                if prior_pdf is not None:
                    pixels=strict_pixels(gate,prior_pdf,now_pdf)
                    report['pixel_comparison'].append(pixels)
                    if not pixels['all_identical']:
                        report['status']='PIXEL_DIFFERENCE'
                        break
                prior_pdf=now_pdf
            else:
                report['status']='PROFILE_OK_SINGLE' if a.repeats==1 else 'PROFILE_OK_REPEATED'
                report['summary']=summary_for(report['runs'])
    finally:
        write_report(report,dest)
    print('[V6.6.1] Status:',report['status'])
    print('[V6.6.1] Reports:',dest/'v661_ocr_hotspots_summary.md')
    return 0 if report['status'].startswith('PROFILE_OK') else 2

if __name__=='__main__':
    raise SystemExit(main())
