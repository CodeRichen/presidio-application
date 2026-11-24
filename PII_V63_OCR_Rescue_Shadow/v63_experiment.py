#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""V6.3: real OCR rescue hunting + fixed held-out split; shadow-only.

Always run BOTH 2× and 3× OCR. The V6.2 rule is only evaluated as a
counterfactual using 2× features; never used to redact / skip on user PDFs.
"""
from __future__ import annotations
import argparse,hashlib,json,subprocess,sys,tempfile,time
from pathlib import Path
from v63_fixture import build

SCHEMA='v63_ocr_rescue_shadow_1'

def sha256(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda:f.read(1<<20),b''): h.update(b)
    return h.hexdigest()

def summarize(rows):
    total=lambda key:sum(int(x[key]) for x in rows)
    counts={'pages':len(rows),'ground_truth_total':total('gt_count'),
            'probe_pii_total':total('gt_probe_count'),
            'zoom2_gt_pass':total('zoom2_gt_pass'),
            'dual_gt_pass':total('dual_gt_pass'),
            'candidate_gt_pass':total('candidate_gt_pass'),
            'rescues_total':total('rescues_total'),
            'rescues_in_probes':total('rescues_in_probes'),
            'dangerous_skips':total('dangerous_skips'),
            'dual_gt_gaps':total('dual_gt_gaps'),
            'fallback_skipped_estimated':sum(x['would_skip_3x'] for x in rows),
            'selective_3x_saved_s_estimate':round(sum(x['omitted_3x_s_estimate'] for x in rows),3),
            'measured_2x_s':round(sum(x['zoom2_s'] for x in rows),3),
            'measured_3x_s':round(sum(x['zoom3_s'] for x in rows),3)}
    return counts

def verdict(groups):
    if any(g['status']!='RECORDED' for g in groups.values()):return 'INCOMPLETE'
    s=[x['summary'] for x in groups.values()]
    if any(x['dangerous_skips'] for x in s):return 'UNSAFE_SKIP_FOUND'
    if any(x['dual_gt_gaps'] for x in s):return 'BASELINE_GROUND_TRUTH_GAPS'
    if sum(x['rescues_in_probes'] for x in s)==0:return 'NO_CONFIRMED_3X_RESCUES'
    if groups['holdout']['summary']['rescues_in_probes']==0:return 'DISCOVERY_RESCUES_ONLY'
    return 'RESCUES_OBSERVED_SHADOW_ONLY'

def render_markdown(report):
    lines=['# V6.3：2× 漏判 / 3× 補救 — OCR 影子測試','',
           '**狀態：'+report['status']+'**','',
           '每頁仍實際執行 2× 與 3× OCR，任何省時都是模擬估計；**不得部署真實文件**。',
           '資料均為臨時生成的合成影像；標註於繪圖時獨立產生，報告不包含原始值、OCR 文字或影像。',
           '使用 V6.2 原有的門檻（僅 2× 特徵做決策），未依此份 GT 自動調整門檻。','',
           '| 資料集 | 頁數 | 已標註 PII | 2× 命中 | 雙解析度命中 | 3× 補救數 | 危險跳過 | 估計略過頁 |',
           '|---|---:|---:|---:|---:|---:|---:|---:|']
    for name,v in report['groups'].items():
        if v['status']!='RECORDED':
            lines.append(f'| {name} | — | — | — | — | — | — | {v["status"]} |')
            continue
        s=v['summary'];lines.append(f"| {name} | {s['pages']} | {s['ground_truth_total']} | {s['zoom2_gt_pass']} | {s['dual_gt_pass']} | {s['rescues_in_probes']} | {s['dangerous_skips']} | {s['fallback_skipped_estimated']} |")
    for name,v in report['groups'].items():
        if v['status']!='RECORDED':continue
        lines+=['',f'## {name} 逐頁','',
                '| 合成情境 | 類型 | 2× / GT | 雙解析度 / GT | 3× 補救 | 跳過 3×？ | 危險跳過 |',
                '|---|---|---:|---:|---:|---|---:|']
        for c in v['cases']:
            lines.append(f"| {c['case']} | {c['recipe']} | {c['zoom2_gt_pass']}/{c['gt_count']} | {c['dual_gt_pass']}/{c['gt_count']} | {c['rescues_in_probes']} | {'是' if c['would_skip_3x'] else '否'} | {c['dangerous_skips']} |")
    lines+=['','## 判讀','',
       '- `UNSAFE_SKIP_FOUND`：有 2× 漏、雙解析度補救，但策略仍跳過 3× → **拒絕部署**。',
       '- `BASELINE_GROUND_TRUTH_GAPS`：雙解析度也漏掉已知 PII → 先修 OCR 或資料。',
       '- `NO_CONFIRMED_3X_RESCUES`：未找到真正的補救案例，不能驗證補救觸發能力。',
       '- `DISCOVERY_RESCUES_ONLY`：只有探索資料有補救案例，保留獨立驗證缺口。',
       '- `RESCUES_OBSERVED_SHADOW_ONLY`：探索與留出測試皆有補救，且未見新增漏判；仍非生產安全證明。',
       '- `INCOMPLETE`：子程序失敗或逾時，不能得出結論。','',
       '**限制**：這些是固定合成版型下的留出種子，不是實際使用者文件的代表性抽樣。',
       'GT 矩形 95% 覆蓋率只是框匹配代理指標，不是最終 PDF 的黑框覆蓋驗收。',
       '面對真實 PII 仍需保留目前的 2×+3× 和 V5 安全輸出。']
    return '\n'.join(lines)+'\n'

def main(argv=None):
    p=argparse.ArgumentParser(description='V6.3: 真實 2x/3x OCR 補救案例影子診斷；僅合成資料')
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v63_ocr_results')
    p.add_argument('--train-count',type=int,default=16)
    p.add_argument('--holdout-count',type=int,default=8)
    p.add_argument('--seed',type=int,default=20261120)
    p.add_argument('--timeout',type=int,default=2400,help='每組最多秒數')
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args(argv)
    if not (1<=a.train_count<=100 and 1<=a.holdout_count<=100):p.error('頁數需 1..100')
    if a.timeout<60:p.error('--timeout 至少 60 秒')
    root=Path(a.project_root).resolve()
    if not (root/'handler_media.py').is_file():p.error('找不到 handler_media.py，請在專案根目錄執行')
    if not (root/'pdf_secure_output.py').is_file():p.error('找不到 pdf_secure_output.py')
    report={'schema':SCHEMA,'status':'INCOMPLETE',
            'shadow_mode':True,'production_code_changed':False,
            'only_ephemeral_synthetic_documents':True,
            'zoom3_always_executed':True,'policy_safe_for_production':False,
            'coverage_threshold':.95,'seed':a.seed,
            'warning':'Do not turn off 3x OCR on any real document based on this experiment.',
            'groups':{}}
    with tempfile.TemporaryDirectory(prefix='pii_v63_rescue_') as td:
        scratch=Path(td)
        jobs=[('discovery',a.train_count,a.seed),
              ('holdout',a.holdout_count,a.seed+1000003)]
        for name,n,seed in jobs:
            fixture=scratch/(name+'.pdf')
            try:
                gt=build(fixture,count=n,seed=seed,split=name)
                try: import pymupdf as fitz
                except ImportError: import fitz
                with fitz.open(str(fixture)) as pdf:
                    if len(pdf)!=len(gt) or any(page.get_text('text').strip() for page in pdf):
                        raise ValueError('合成影像 PDF 有文字層或頁數錯誤')
                if a.check_only:
                    print(f'[PASS] {name}: {len(gt)} 頁影像 PDF，GT={sum(len(z["expected_text"]) for z in gt)}，無文字層',flush=True)
                    continue
                gt_path=scratch/(name+'_gt.json')
                gt_path.write_text(json.dumps(gt,ensure_ascii=False),encoding='utf8')
                log=scratch/(name+'_result.json')
                cmd=[sys.executable,str(Path(__file__).resolve().parent/'v63_child.py'),
                     '--root',str(root),'--fixture',str(fixture),
                     '--ground-truth',str(gt_path),'--sha256',sha256(fixture),
                     '--report',str(log)]
                print(f'[V6.3] {name}: {n} 頁，真實執行 2× 和 3×（決策僅影子模擬）',flush=True)
                start=time.perf_counter()
                try:
                    code=subprocess.run(cmd,cwd=str(root),stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                        timeout=a.timeout).returncode
                except subprocess.TimeoutExpired:
                    report['groups'][name]={'status':'TIMEOUT'};break
                if code!=0 or not log.exists():
                    report['groups'][name]={'status':'ERROR','reason':'child_failed_without_private_logs'};break
                rows=json.loads(log.read_text(encoding='utf8'))['cases']
                if len(rows)!=n:raise RuntimeError('unmatched result rows')
                stats=summarize(rows)
                report['groups'][name]={'status':'RECORDED',
                  'wall_s':round(time.perf_counter()-start,3),'summary':stats,'cases':rows}
                print(f"[V6.3] {name}：2× GT {stats['zoom2_gt_pass']}/{stats['ground_truth_total']}，雙解析度 {stats['dual_gt_pass']}/{stats['ground_truth_total']}，補救 {stats['rescues_in_probes']}，危險跳過 {stats['dangerous_skips']}",flush=True)
            except Exception as e:
                report['groups'][name]={'status':'ERROR','error_type':type(e).__name__}
                break
    if a.check_only: return 0
    report['status']=verdict(report['groups']) if len(report['groups'])==2 else 'INCOMPLETE'
    outdir=(root/a.results_dir).resolve()
    outdir.mkdir(parents=True,exist_ok=True)
    (outdir/'v63_rescue_summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    (outdir/'v63_rescue_summary.md').write_text(render_markdown(report),encoding='utf8')
    print(f'[V6.3] 報告：{outdir / "v63_rescue_summary.json"}',flush=True)
    print('[V6.3] 狀態：'+report['status']+'；正式程式完全未修改',flush=True)
    return 2 if report['status'] in {'INCOMPLETE','UNSAFE_SKIP_FOUND'} else 0

if __name__=='__main__':
    raise SystemExit(main())
