#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.4 safe ORT thread-count A/B on a fixed synthetic seven-page PDF.

Full end-to-end input/output security gate. No production code edits.
Do not run on private documents or upload scratch maps.
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

V5_DIR='PII_V5_Security_Acceptance'
FIXTURE_SHA256='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def load_gate(project_root):
    gate_dir=project_root/V5_DIR
    if not (gate_dir/'v5_acceptance.py').is_file():
        raise FileNotFoundError('Please restore the V5 security acceptance kit under project root')
    sys.path.insert(0,str(gate_dir))
    import v5_acceptance as gate
    return gate,gate_dir

def child_env():
    env=os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5':'1','PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1','PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1',
    })
    # Do not set undocumented ORT environment variables.
    return env

def perform_one(project_root,source,gate_dir,tool_dir,trial,idx,scratch,timeout,gate):
    run_dir=scratch / f'{idx:02d}_{"baseline" if trial==0 else "ort"+str(trial)}'
    run_dir.mkdir()
    cmd=[sys.executable,str(tool_dir/'v64_child.py'),
         '--project-root',str(project_root),'--source',str(source),
         '--expected',str(gate_dir/'expected_text_and_faces.json'),
         '--runner',str(gate_dir/'run_7page_pdf_test.py'),
         '--results-dir',str(run_dir),'--threads',str(trial)]
    start=time.perf_counter()
    try:
        p=subprocess.run(cmd,cwd=str(project_root),env=child_env(),
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,check=False,timeout=timeout)
        code=p.returncode
    except subprocess.TimeoutExpired:
        code=None
    result={'trial_threads':trial,'elapsed_s':round(time.perf_counter()-start,3),
            'return_code':code,'execution_ok':code==0,
            'safe':False,'reason':None}
    masked=run_dir/'pii_face_7page_masked.pdf'
    audit=run_dir/'pdf_face_7page_audit.json'
    stats=run_dir/'v64_sessions.json'
    if code is None:
        result['reason']='TIMEOUT'
        return result,masked
    if code!=0 or not masked.is_file() or not audit.is_file():
        result['reason']='RUN_OR_OUTPUT_FAILED'
        return result,masked
    try:
        result['ground_truth']=gate.audit_status(audit)
        result['pdf_structure']=gate.check_pdf(masked)
        result['pdf_bytes']=masked.stat().st_size
        if stats.is_file():
            counts=json.loads(stats.read_text(encoding='utf-8'))
            result.update({k:counts[k] for k in ('ort_session_calls','ort_sessions_tuned','preexisting_session_options')})
        result['safe']=bool(result['ground_truth'].get('automated_pass')
                            and result['pdf_structure'].get('pass'))
        if not result['safe']:
            result['reason']='SECURITY_GATE_FAILED'
    except Exception:
        result['reason']='AUDIT_ERROR'
    return result,masked

def classify(runs, pairs, reference_pairs, trial):
    if len(runs)!=len(pairs) or not runs:
        return {'status':'INCOMPLETE'}
    if any(not r.get('safe') for r in runs+pairs):
        return {'status':'SECURITY_GATE_FAILED'}
    if any(not x.get('all_identical') for x in reference_pairs):
        return {'status':'PIXEL_DIFFERENCE'}
    if any(x.get('ort_sessions_tuned',0)==0 for x in pairs):
        return {'status':'TUNING_NOT_APPLIED'}
    baseline=statistics.median(x['elapsed_s'] for x in runs)
    candidate=statistics.median(x['elapsed_s'] for x in pairs)
    reduction=100*(baseline-candidate)/baseline if baseline else 0.0
    return {'status':'PROMISING_AB_ONLY' if reduction>=10 else 'NO_CONFIRMED_GAIN',
            'baseline_median_s':round(baseline,3),
            'candidate_median_s':round(candidate,3),
            'improvement_pct':round(reduction,2),
            'threads':trial,'samples_per_mode':len(runs)}

def write_report(report,dest):
    dest.mkdir(parents=True,exist_ok=True)
    (dest/'v64_ort_ab_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# V6.4：ONNX Runtime CPU 執行緒 A/B', '',
           f"- 整體狀態：**{report['status']}**",
           '- 同一份七頁合成 PDF；2×+3× OCR 與 V5 PDF 輸出均保留',
           '- 獨立程序測試，**沒有修改正式程式或模型**',
           '- 只有通過 27/27、17/17、PDF 結構及逐頁像素一致才評估速度',
           '- ORT 執行緒補丁僅作用於該測試子程序；不保證所有模型都有採用 ORT',
           '',
           '| 模式 | 執行秒數（每次） | 驗收通過 |', '|---|---|---|']
    for label,grp in [('baseline',report.get('baseline',[])),('candidate',report.get('candidate',[]))]:
        lines.append(f"| {label} | {', '.join(str(x.get('elapsed_s')) for x in grp)} | {all(x.get('safe') for x in grp) if grp else '—'} |")
    s=report.get('comparison',{})
    lines+=['',f"- 結論：**{s.get('status','—')}**",
            f"- 中位數處理時間：原版 {s.get('baseline_median_s','—')} 秒；候選 {s.get('candidate_median_s','—')} 秒",
            f"- 中位數改善：{s.get('improvement_pct','—')}%（未計 GUI 載入與操作）",
            '- 所有私有還原 map 與測試 PDF 在測試結束時從暫存目錄清除；不要強制終止程序，避免殘留。',
            '- 即使 PROMISING_AB_ONLY，也需要更多資料、重複測試及正式 GUI 回歸後才能採用。','']
    (dest/'v64_ort_ab_summary.md').write_text('\n'.join(lines),encoding='utf-8')

def main(argv=None):
    ap=argparse.ArgumentParser(description='V6.4 ORT CPU tuning, fixed synthetic PDF only')
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--source',default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    ap.add_argument('--results-dir',default='v64_ort_results')
    ap.add_argument('--threads',type=int,choices=[1,2,4,6,8,12,16],default=4)
    ap.add_argument('--repeats',type=int,choices=range(1,6),metavar='1..5',default=2)
    ap.add_argument('--timeout',type=int,default=1200)
    ap.add_argument('--check-only',action='store_true')
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve()
    source=(root/args.source).resolve()
    dest=(root/args.results_dir).resolve()
    tool_dir=Path(__file__).resolve().parent
    if root not in tool_dir.parents and root!=tool_dir.parent:
        # Permit relocation only inside the currently selected project root.
        raise RuntimeError('Please place the V6.4 tool folder inside the project root')
    gate,gate_dir=load_gate(root)
    gate.check_setup(root,source,gate_dir)
    expected=json.loads((gate_dir/'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(expected['samples'])!=7 or sum(len(s['expected_text']) for s in expected['samples'])!=27:
        raise RuntimeError('invalid fixed fixture Ground Truth')
    if args.check_only:
        print('CHECK_OK: SHA-256, V5 installed, expected fixture and test tools present')
        return 0
    report={'schema':'pii_v64_ort_ab_1','status':'INCOMPLETE',
            'fixture_sha256_verified':True,'production_code_changed':False,
            'only_synthetic_documents':True,'dual_zoom_kept':True,
            'temporary_private_maps_deleted_on_normal_exit':True,
            'selected_threads':args.threads,
            'repeats':args.repeats,'baseline':[],'candidate':[],
            'pixel_comparison':[],'comparison':{},
            'note':'This is not proof of production safety; GUI lag was not tested.'}
    with tempfile.TemporaryDirectory(prefix='pii_v64_ab_') as td:
        scratch=Path(td)
        for i in range(args.repeats):
            # Same order per pair; process isolation avoids persistent ML caches.
            print(f'[V6.4] 第 {i+1}/{args.repeats} 組：執行原版（輸出僅保留在暫存路徑）',flush=True)
            a,base_pdf=perform_one(root,source,gate_dir,tool_dir,0,2*i,scratch,args.timeout,gate)
            report['baseline'].append(a)
            if not a.get('safe'):
                report['status']='SECURITY_GATE_FAILED'
                break
            print(f'[V6.4] 第 {i+1}/{args.repeats} 組：執行 ORT 執行緒={args.threads}',flush=True)
            b,test_pdf=perform_one(root,source,gate_dir,tool_dir,args.threads,2*i+1,scratch,args.timeout,gate)
            report['candidate'].append(b)
            if not b.get('safe'):
                report['status']='SECURITY_GATE_FAILED'
                break
            comparison=gate.compare_page_pixels(base_pdf,test_pdf,dpi=150)
            report['pixel_comparison'].append(comparison)
            if not comparison['all_identical']:
                report['status']='PIXEL_DIFFERENCE'
                break
            if b.get('ort_sessions_tuned',0)<=0:
                report['status']='TUNING_NOT_APPLIED'
                break
        if len(report['candidate'])==args.repeats and len(report['baseline'])==args.repeats:
            report['comparison']=classify(report['baseline'],report['candidate'],report['pixel_comparison'],args.threads)
            report['status']=report['comparison']['status']
        elif not report['comparison']:
            report['comparison']={'status':report['status']}
    write_report(report,dest)
    print('[V6.4] 狀態:',report['status'])
    print('[V6.4] 摘要:',dest/'v64_ort_ab_summary.md')
    return 0 if report['status'] in ('PROMISING_AB_ONLY','NO_CONFIRMED_GAIN') else 2

if __name__=='__main__':
    raise SystemExit(main())
