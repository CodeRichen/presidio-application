#!/usr/bin/env python3
# coding: utf-8
"""V6.5.2 isolated PyTorch CPU intra-op A/B; fixture-specific fail-closed check.
No app edits, no original OCR text in reports; confidential intermediate files
are deleted by TemporaryDirectory on normal exit.
"""
from __future__ import annotations
import argparse, json, os, statistics, subprocess, sys, tempfile, time
from pathlib import Path

V5_DIR = 'PII_V5_Security_Acceptance'

def require_gate(root, source):
    gate_dir = root/V5_DIR
    if not (gate_dir/'v5_acceptance.py').is_file():
        raise FileNotFoundError('Missing PII_V5_Security_Acceptance toolkit')
    sys.path.insert(0, str(gate_dir))
    import v5_acceptance as gate
    gate.check_setup(root, source, gate_dir)
    expected = json.loads((gate_dir/'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(expected['samples']) != 7 or sum(len(s['expected_text']) for s in expected['samples']) != 27:
        raise RuntimeError('unexpected synthetic Ground Truth')
    return gate, gate_dir

def test_env():
    e = os.environ.copy()
    e.update({
        'PII_PDF_EXPORT_V5':'1', 'PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid', 'PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1', 'PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0', 'PII_MEDIA_CACHE':'1', 'PII_ZH_SINGLE_PASS':'1',
    })
    return e

def execute_one(root, source, gate_dir, tool_dir, threads, idx, tmpdir, timeout, gate):
    dest = tmpdir/f'{idx:02d}_{"baseline" if not threads else "torch"+str(threads)}'
    dest.mkdir()
    cmd = [sys.executable, str(tool_dir/'v652_child.py'), '--project-root', str(root),
           '--source', str(source), '--expected', str(gate_dir/'expected_text_and_faces.json'),
           '--runner', str(gate_dir/'run_7page_pdf_test.py'), '--results-dir', str(dest),
           '--threads', str(threads)]
    start = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=test_env(), stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              check=False, timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        rc = None
    report = {'trial_threads':threads, 'elapsed_s':round(time.perf_counter()-start,3),
              'return_code':rc, 'safe':False, 'reason':None}
    pdf = dest/'pii_face_7page_masked.pdf'
    audit = dest/'pdf_face_7page_audit.json'
    stats = dest/'v652_torch_stats.json'
    if rc is None: report['reason']='TIMEOUT'; return report,pdf
    if rc != 0 or not pdf.is_file() or not audit.is_file() or not stats.is_file():
        report['reason']='CHILD_RUN_FAILED'; return report,pdf
    try:
        s = json.loads(stats.read_text(encoding='utf-8'))
        for key in ('torch_before','torch_effective','code_detect_calls','code_detect_wall_s','code_detect_median_ms'):
            report[key] = s[key]
        report['ground_truth'] = gate.audit_status(audit)
        report['pdf_structure'] = gate.check_pdf(pdf)
        report['pdf_bytes'] = pdf.stat().st_size
        report['safe'] = (report['ground_truth']['automated_pass'] and report['pdf_structure']['pass']
                          and report['code_detect_calls'] > 0)
        if not report['safe']: report['reason']='SECURITY_OR_INSTRUMENTATION_FAILURE'
    except Exception:
        report['reason']='AUDIT_OR_STATS_FAILURE'
    return report,pdf

def classify(baselines, candidates, pixels, threads):
    if not baselines or len(baselines) != len(candidates) or len(pixels) != len(candidates):
        return {'status':'INCOMPLETE'}
    if not all(x.get('safe') for x in baselines+candidates):
        return {'status':'SECURITY_GATE_FAILED'}
    if not all(x.get('all_identical') and x.get('pages_compared')==7 for x in pixels):
        return {'status':'PIXEL_DIFFERENCE'}
    if any(x.get('torch_effective') != threads for x in candidates):
        return {'status':'TUNING_NOT_APPLIED'}
    if any(x.get('torch_effective') == threads for x in baselines):
        return {'status':'NO_EFFECTIVE_CHANGE'}
    base = statistics.median(x['elapsed_s'] for x in baselines)
    cand = statistics.median(x['elapsed_s'] for x in candidates)
    base_detect = statistics.median(x['code_detect_wall_s'] for x in baselines)
    cand_detect = statistics.median(x['code_detect_wall_s'] for x in candidates)
    delta = 100*(base-cand)/base if base else 0
    delta_detect = 100*(base_detect-cand_detect)/base_detect if base_detect else 0
    status = 'PROMISING_AB_ONLY' if delta>=10 and delta_detect>0 else 'NO_CONFIRMED_GAIN'
    return {'status':status, 'baseline_median_s':round(base,3),'candidate_median_s':round(cand,3),
            'improvement_pct':round(delta,2),
            'baseline_detect_median_s':round(base_detect,3),
            'candidate_detect_median_s':round(cand_detect,3),
            'detect_improvement_pct':round(delta_detect,2),
            'threads':threads,'samples_per_mode':len(baselines)}

def write_report(report, dest):
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'v652_torch_ab_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2),encoding='utf-8')
    lines=['# V6.5.2：PyTorch 中文 NER CPU 執行緒安全 A/B','',
           f"- 狀態：**{report['status']}**",'- 僅固定七頁合成 PDF、完整雙解析度 OCR、V5 安全 PDF 輸出；正式檔案未修改。',
           '- 實際獨立程序端到端測速；未測 GUI 回應性。',
           '- 固定合成測試 PII、人臉與 PDF 結構驗證不能代表真實文件辨識率。','',
           '| 模式 | 每次完整秒數 | 每次 code_detect 秒數 | 有效 Torch threads | 安全通過 |',
           '|---|---|---|---|---|']
    for name,grp in (('baseline',report['baseline']),('candidate',report['candidate'])):
        fmt=lambda key: ', '.join(str(r.get(key,'—')) for r in grp) or '—'
        lines.append(f"| {name} | {fmt('elapsed_s')} | {fmt('code_detect_wall_s')} | {fmt('torch_effective')} | {all(r.get('safe') for r in grp) if grp else '—'} |")
    cmp=report.get('comparison',{})
    lines+=['',f"- 判定：**{cmp.get('status',report['status'])}**",
            f"- 完整時間中位數：{cmp.get('baseline_median_s','—')} → {cmp.get('candidate_median_s','—')} 秒；改善 {cmp.get('improvement_pct','—')}%。",
            f"- `code_detect` 中位數：{cmp.get('baseline_detect_median_s','—')} → {cmp.get('candidate_detect_median_s','—')} 秒；改善 {cmp.get('detect_improvement_pct','—')}%。",
            '- PROMISING_AB_ONLY 只代表此合成 A/B 有加速訊號，仍不得直接更動正式推論設定。',
            '- 沒有保存 OCR 原文或圖片。暫存的 PDF 與私有 map 在正常結束時清除；切勿強制終止。','']
    (dest/'v652_torch_ab_summary.md').write_text('\n'.join(lines),encoding='utf-8')

def main(argv=None):
    ap=argparse.ArgumentParser(description='Torch CPU thread A/B, seven-page synthetic PDF only')
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--source',default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    ap.add_argument('--results-dir',default='v652_torch_results')
    ap.add_argument('--threads',type=int,choices=[1,2,4,6,8,12,16],default=4)
    ap.add_argument('--repeats',type=int,choices=range(1,6),default=2)
    ap.add_argument('--timeout',type=int,default=1200)
    ap.add_argument('--check-only',action='store_true')
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve(); source=(root/args.source).resolve(); output=(root/args.results_dir).resolve()
    tool_dir=Path(__file__).resolve().parent
    if tool_dir.parent != root: raise RuntimeError('Place PII_V652_Torch_AB in the project root')
    gate,gate_dir=require_gate(root,source)
    if args.check_only:
        print('CHECK_OK: V5 security gate installed; fixed synthetic PDF SHA-256 verified')
        return 0
    report={'schema':'pii_v652_torch_ab_v1','status':'INCOMPLETE','fixture_sha256_verified':True,
            'production_code_changed':False,'only_synthetic_documents':True,
            'dual_zoom_kept':True,'temporary_private_maps_deleted_on_normal_exit':True,
            'selected_threads':args.threads,'repeats':args.repeats,'baseline':[],'candidate':[],
            'pixel_comparison':[],'comparison':{},
            'note':'End-to-end measurement includes model initialization; GUI lag not measured.'}
    with tempfile.TemporaryDirectory(prefix='pii_v652_torch_') as tmp:
        scratch=Path(tmp)
        for i in range(args.repeats):
            # Counterbalance process order across repeat pairs: AB, BA, AB...
            order=[0,args.threads] if i%2==0 else [args.threads,0]
            pair={}
            for trial in order:
                print(f'[V6.5.2] 第 {i+1}/{args.repeats} 組：Torch threads={trial or "default"}', flush=True)
                r,pdf=execute_one(root,source,gate_dir,tool_dir,trial,i*2+(0 if trial==0 else 1),scratch,args.timeout,gate)
                report['baseline' if trial==0 else 'candidate'].append(r)
                pair[trial]=pdf
                if not r.get('safe'):
                    report['status']='SECURITY_GATE_FAILED';break
                if trial==args.threads and r.get('torch_effective')!=args.threads:
                    report['status']='TUNING_NOT_APPLIED';break
            if report['status']!='INCOMPLETE': break
            comparison=gate.compare_page_pixels(pair[0],pair[args.threads],dpi=150)
            report['pixel_comparison'].append(comparison)
            if not comparison['all_identical']:
                report['status']='PIXEL_DIFFERENCE';break
        if len(report['baseline'])==args.repeats and len(report['candidate'])==args.repeats:
            report['comparison']=classify(report['baseline'],report['candidate'],report['pixel_comparison'],args.threads)
            report['status']=report['comparison']['status']
        else:
            report['comparison']={'status':report['status']}
    write_report(report,output)
    print('[V6.5.2] 狀態:',report['status'])
    print('[V6.5.2] 報告:',output/'v652_torch_ab_summary.md')
    return 0 if report['status'] in ('PROMISING_AB_ONLY','NO_CONFIRMED_GAIN') else 2
if __name__ == '__main__':
    raise SystemExit(main())
