#!/usr/bin/env python3
# coding: utf-8
"""V6.5.3 balanced AB/BA PyTorch threads, first/second full-PDF passes.
Only one SHA-256-fixed synthetic PDF; fail closed; no formal production changes.
Sensitive intermediate files are created under TemporaryDirectory and removed
on normal exit. Output report contains numeric metrics and fixed status codes.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

V5_NAME = 'PII_V5_Security_Acceptance'
PASS_NAMES = ('first', 'second')
STATUS_SUCCESS = ('PROMISING_WARM_AB_ONLY', 'NO_REPRODUCIBLE_GAIN',
                  'INSUFFICIENT_REPEATS')


def require_gate(root, source):
    gate_dir = root / V5_NAME
    if not (gate_dir / 'v5_acceptance.py').is_file():
        raise FileNotFoundError('Missing installed V5 security acceptance toolkit')
    sys.path.insert(0, str(gate_dir))
    import v5_acceptance as gate
    gate.check_setup(root, source, gate_dir)
    expected = json.loads((gate_dir / 'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(expected['samples']) != 7 or sum(len(s['expected_text']) for s in expected['samples']) != 27:
        raise RuntimeError('Unexpected fixture Ground Truth')
    return gate, gate_dir


def child_environment():
    env = os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5':'1', 'PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid', 'PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1', 'PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0', 'PII_MEDIA_CACHE':'1',
        'PII_ZH_SINGLE_PASS':'1',
    })
    return env


def run_trial(root, source, gate_dir, tool_dir, threads, trial_dir, timeout, gate):
    trial_dir.mkdir(parents=True, exist_ok=False)
    cmd = [sys.executable, str(tool_dir/'v653_child.py'),
           '--project-root', str(root), '--source', str(source),
           '--expected', str(gate_dir/'expected_text_and_faces.json'),
           '--runner', str(gate_dir/'run_7page_pdf_test.py'),
           '--results-dir', str(trial_dir), '--threads', str(threads)]
    start = time.perf_counter()
    try:
        child = subprocess.run(cmd, cwd=str(root), env=child_environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
        rc = child.returncode
    except subprocess.TimeoutExpired:
        rc = None

    summary = {'trial_threads':threads,
               'child_elapsed_s':round(time.perf_counter()-start, 4),
               'return_code':rc, 'safe':False, 'reason':None, 'passes':{}}
    if rc is None:
        summary['reason']='TIMEOUT'
        return summary
    if rc != 0:
        summary['reason']='CHILD_RUN_FAILED'
        return summary
    path = trial_dir/'v653_child_stats.json'
    if not path.is_file():
        summary['reason']='MISSING_CHILD_STATS'
        return summary
    try:
        stats = json.loads(path.read_text(encoding='utf-8'))
        if stats.get('schema') != 'pii_v653_child_1' or stats.get('trial_threads') != threads:
            raise ValueError('Unexpected child stats')
        summary['pre_run_setup_s'] = float(stats['pre_run_setup_s'])
        if summary['pre_run_setup_s'] <= 0:
            raise ValueError('Invalid model setup metric')
        summary['torch_before'] = stats['torch_before']
        summary['torch_effective'] = stats['torch_effective']
        for label in PASS_NAMES:
            output_dir = trial_dir/label
            pdf = output_dir/'pii_face_7page_masked.pdf'
            audit_path = output_dir/'pdf_face_7page_audit.json'
            if not pdf.is_file() or not audit_path.is_file():
                raise ValueError('Missing test PDF or audit')
            stats_pass = stats['passes'][label]
            gt = gate.audit_status(audit_path)
            pdf_check = gate.check_pdf(pdf)
            processing_s = float(stats_pass['fixture_processing_s'])
            calls = int(stats_pass['code_detect_calls'])
            code_s = float(stats_pass['code_detect_wall_s'])
            if not (0 < processing_s <= float(stats_pass['fixture_plus_audit_s']) and
                    calls > 0 and 0 <= code_s <= processing_s and
                    gt['automated_pass'] and pdf_check['pass']):
                raise ValueError('Security or instrumentation failed')
            summary['passes'][label] = {
                'processing_s':processing_s,
                'with_audit_s':stats_pass['fixture_plus_audit_s'],
                'code_detect_calls':calls, 'code_detect_wall_s':code_s,
                'code_detect_median_ms':stats_pass['code_detect_median_ms'],
                'text_regions_pass':gt['text_regions_pass'],
                'face_map_boxes_pass':gt['face_map_boxes_pass'],
                'pdf_structure_pass':pdf_check['pass'],
            }
        summary['safe'] = True
    except (ValueError, TypeError, KeyError, OSError, ZeroDivisionError):
        summary['reason']='VALIDATION_FAILED'
    return summary


def strict_pixels(gate, a, b):
    result = gate.compare_page_pixels(a, b, dpi=150)
    return {'pages_compared':result.get('pages_compared'),
            'identical_pages':result.get('identical_pages'),
            'all_identical':bool(result.get('all_identical') and
                                 result.get('pages_compared')==7 and
                                 result.get('identical_pages')==7)}


def percentile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('No data')
    k = (len(ordered)-1) * fraction
    low, high = math.floor(k), math.ceil(k)
    return ordered[low] + (ordered[high]-ordered[low]) * (k-low)


def describe(values):
    vals = list(values)
    return {'median_s':round(statistics.median(vals), 3),
            'min_s':round(min(vals), 3), 'max_s':round(max(vals), 3),
            'p25_s':round(percentile(vals, .25), 3),
            'p75_s':round(percentile(vals, .75), 3)}


def classify(baselines, candidates, pair_pixels, threads, min_pairs=5):
    if not baselines or len(baselines)!=len(candidates) or len(pair_pixels)!=len(baselines):
        return {'status':'INCOMPLETE'}
    if not all(r.get('safe') for r in baselines+candidates):
        return {'status':'SECURITY_GATE_FAILED'}
    for x in pair_pixels:
        if not all(x.get(k,{}).get('all_identical') for k in ('first','second','baseline_passes','candidate_passes')):
            return {'status':'PIXEL_DIFFERENCE'}
    if not all(r.get('torch_effective')==threads for r in candidates):
        return {'status':'TUNING_NOT_APPLIED'}
    if any(r.get('torch_effective')==threads for r in baselines):
        return {'status':'NO_EFFECTIVE_CHANGE'}
    for pass_name in PASS_NAMES:
        calls = {r.get('passes',{}).get(pass_name,{}).get('code_detect_calls')
                 for r in baselines+candidates}
        if len(calls)!=1 or None in calls:
            return {'status':'INSTRUMENTATION_MISMATCH'}

    out={'status':'INCOMPLETE', 'pairs':len(baselines), 'candidate_threads':threads}
    if all('pre_run_setup_s' in r for r in baselines+candidates):
        out['pre_run_setup_s'] = {
            'baseline':describe(float(r['pre_run_setup_s']) for r in baselines),
            'candidate':describe(float(r['pre_run_setup_s']) for r in candidates),
        }
    for label in PASS_NAMES:
        base = [float(r['passes'][label]['processing_s']) for r in baselines]
        cand = [float(r['passes'][label]['processing_s']) for r in candidates]
        base_detect = [float(r['passes'][label]['code_detect_wall_s']) for r in baselines]
        cand_detect = [float(r['passes'][label]['code_detect_wall_s']) for r in candidates]
        b, c = statistics.median(base), statistics.median(cand)
        bd, cd = statistics.median(base_detect), statistics.median(cand_detect)
        improvements = [100*(x-y)/x for x,y in zip(base,cand)]
        out[label] = {
            'baseline':describe(base), 'candidate':describe(cand),
            'median_improvement_pct':round(100*(b-c)/b, 2),
            'paired_improvements_pct':[round(v,2) for v in improvements],
            'positive_pairs':sum(v>0 for v in improvements),
            'median_code_detect_baseline_s':round(bd,3),
            'median_code_detect_candidate_s':round(cd,3),
            'median_code_detect_improvement_pct':round(100*(bd-cd)/bd,2) if bd>0 else 0,
        }
    out['first_second_gap_baseline_s'] = round(out['first']['baseline']['median_s'] -
                                              out['second']['baseline']['median_s'],3)
    out['first_second_gap_candidate_s'] = round(out['first']['candidate']['median_s'] -
                                               out['second']['candidate']['median_s'],3)
    if len(baselines)<min_pairs:
        out['status']='INSUFFICIENT_REPEATS'
        return out
    warm = out['second']
    if (warm['median_improvement_pct']>=10 and
        warm['positive_pairs']>=math.ceil(.8*len(baselines)) and
        warm['median_code_detect_improvement_pct']>0):
        out['status']='PROMISING_WARM_AB_ONLY'
    else:
        out['status']='NO_REPRODUCIBLE_GAIN'
    return out


def write_report(report, output):
    output.mkdir(parents=True,exist_ok=True)
    (output/'v653_torch_stability_summary.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    cmp = report.get('comparison',{})
    lines = [
        '# V6.5.3：PyTorch 執行緒冷／暖處理穩定性實驗', '',
        f"**狀態：{report['status']}**", '',
        '- 同一份固定七頁合成 PDF，2×+3× OCR、V5 PDF 安全輸出與 PII 偵測規則均維持不變。',
        '- 原始與候選組依 AB/BA 平衡順序交錯；每個子程序連續執行第一次與第二次完整 PDF 遮蔽。',
        '- `pre_run_setup_s` 為載入 Torch、handler_code 與測試程式的準備時間；不包含在遮蔽 `processing_s` 內。',
        '- 第一次／第二次不是純模型載入計時；第二次同文件可能受 OS 檔案快取、模型暖機及其他快取影響。',
        '- 報告只輸出統計，不保存 PII 原文、文字雜湊、裁圖、PDF 或私人還原表。',
        '- **沒有修改正式程式，未測正式 GUI，不得直接部署執行緒調整。**', '',
        '| 指標 | 第一次處理 | 同程序第二次處理 |',
        '|---|---:|---:|',
    ]
    for label, title in (('baseline','原始設定'), ('candidate','候選執行緒')):
        first=cmp.get('first',{}).get(label,{}).get('median_s','—')
        second=cmp.get('second',{}).get(label,{}).get('median_s','—')
        lines.append(f'| {title}中位數（秒） | {first} | {second} |')
    lines.append(f"| 候選相對改善 | {cmp.get('first',{}).get('median_improvement_pct','—')}% | {cmp.get('second',{}).get('median_improvement_pct','—')}% |")
    lines.append(f"| 候選比原版快的配對數 | {cmp.get('first',{}).get('positive_pairs','—')} | {cmp.get('second',{}).get('positive_pairs','—')} |")
    lines += ['', f"- 配對數：{cmp.get('pairs',0)}。建議至少 5 配對才進行穩定性判定。",
              f"- 啟動前準備時間中位數（原版 / 候選）：{cmp.get('pre_run_setup_s',{}).get('baseline',{}).get('median_s','—')} / {cmp.get('pre_run_setup_s',{}).get('candidate',{}).get('median_s','—')} 秒。",
              f"- 第二次 `code_detect` 中位數改善：{cmp.get('second',{}).get('median_code_detect_improvement_pct','—')}%。",
              '- 每一輪都驗收 27/27 已標註文字區域、17/17 已記錄人臉框、PDF 結構與七頁逐像素一致。',
              '- 17/17 不是所有實際人臉的獨立召回率；固定合成 GT 也不能證明真實文件零漏判。',
              '- **安全註記：若測試被強制中止，請檢查系統暫存資料夾 `pii_v653_` 前綴的目錄，清除可能殘留的私有 map。**', '',
              '## 每組次序與完整處理時間', '',
              '| 組次 | 次序 | 原版第一次/第二次（秒） | 候選第一次/第二次（秒） | 安全驗收 |',
              '|---|---|---|---|---|']
    for pair in report.get('pairs',[]):
        a=pair.get('baseline',{}); b=pair.get('candidate',{})
        fmt=lambda obj: '/'.join(str(obj.get('passes',{}).get(p,{}).get('processing_s','—')) for p in PASS_NAMES)
        lines.append(f"| {pair['pair']} | {pair['order']} | {fmt(a)} | {fmt(b)} | {pair.get('safety_pass')} |")
    (output/'v653_torch_stability_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser(description='PyTorch thread first/second pass stability A/B (synthetic only)')
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--source', default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    ap.add_argument('--results-dir',default='v653_torch_stability_results')
    ap.add_argument('--threads',type=int,choices=[1,2,4,6,8,12,16],default=4)
    ap.add_argument('--repeats',type=int,choices=range(1,7),default=3)
    ap.add_argument('--timeout',type=int,default=1800, help='seconds per child with two full passes')
    ap.add_argument('--check-only',action='store_true')
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve()
    tool_dir=Path(__file__).resolve().parent
    source=(root/args.source).resolve()
    destination=(root/args.results_dir).resolve()
    if tool_dir.parent!=root:
        raise RuntimeError('PII_V653_Torch_Stability must be in the project root')
    gate,gate_dir=require_gate(root,source)
    if args.check_only:
        print('CHECK_OK: V5 toolkit and synthetic PDF SHA-256 verified; no real data processed')
        return 0
    report={'schema':'pii_v653_torch_stability_1','status':'INCOMPLETE',
            'fixture_sha256_verified':True, 'production_code_changed':False,
            'only_synthetic_documents':True,'dual_zoom_kept':True,
            'temporary_private_maps_deleted_on_normal_exit':True,
            'selected_threads':args.threads,'repeats':args.repeats,
            'pairs':[],'comparison':{},
            'warning':'First/second passes can differ for many reasons. GUI not tested. Never use this tool on real documents.'}
    try:
        with tempfile.TemporaryDirectory(prefix='pii_v653_') as tmp:
            scratch=Path(tmp)
            for i in range(args.repeats):
                # V6.5.2 already counterbalanced AB / BA. Keep that design.
                order=[0,args.threads] if i%2==0 else [args.threads,0]
                pair={'pair':i+1,'order':'AB' if i%2==0 else 'BA',
                      'baseline':{},'candidate':{},'pixel_checks':{},'safety_pass':False}
                paths={}
                for idx,thread_count in enumerate(order):
                    print(f'[V6.5.3] Pair {i+1}/{args.repeats} {pair["order"]}; '
                          f'Torch threads={thread_count or "default"}; first+second PDF',flush=True)
                    trial_dir=scratch/f'pair{i+1:02d}_{idx:01d}_{"base" if thread_count==0 else "candidate"}'
                    r=run_trial(root,source,gate_dir,tool_dir,thread_count,trial_dir,args.timeout,gate)
                    key='baseline' if thread_count==0 else 'candidate'
                    pair[key]=r
                    paths[key]=trial_dir
                    if not r.get('safe'):
                        report['status']='SECURITY_GATE_FAILED'
                        break
                    if thread_count and r.get('torch_effective')!=thread_count:
                        report['status']='TUNING_NOT_APPLIED'
                        break
                if report['status']!='INCOMPLETE':
                    report['pairs'].append(pair)
                    break
                for label, mode_a, mode_b in (
                    ('first','baseline/first','candidate/first'),
                    ('second','baseline/second','candidate/second'),
                    ('baseline_passes','baseline/first','baseline/second'),
                    ('candidate_passes','candidate/first','candidate/second')):
                    def pdf_path(mode):
                        which, pass_name = mode.split('/')
                        return paths[which]/pass_name/'pii_face_7page_masked.pdf'
                    pair['pixel_checks'][label]=strict_pixels(gate,pdf_path(mode_a),pdf_path(mode_b))
                    if not pair['pixel_checks'][label]['all_identical']:
                        report['status']='PIXEL_DIFFERENCE'
                        break
                pair['safety_pass'] = (report['status']=='INCOMPLETE')
                report['pairs'].append(pair)
                if report['status']!='INCOMPLETE':
                    break
            if report['status']=='INCOMPLETE' and len(report['pairs'])==args.repeats:
                bases=[p['baseline'] for p in report['pairs']]
                cands=[p['candidate'] for p in report['pairs']]
                pix=[p['pixel_checks'] for p in report['pairs']]
                report['comparison']=classify(bases,cands,pix,args.threads)
                report['status']=report['comparison']['status']
            else:
                report['comparison']={'status':report['status']}
    finally:
        write_report(report,destination)
    print('[V6.5.3] Status:',report['status'])
    print('[V6.5.3] Reports:',destination/'v653_torch_stability_summary.md')
    return 0 if report['status'] in STATUS_SUCCESS else 2


if __name__=='__main__':
    raise SystemExit(main())
