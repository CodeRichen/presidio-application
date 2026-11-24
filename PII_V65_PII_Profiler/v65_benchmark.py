#!/usr/bin/env python3
"""V6.5 read-only PII detection profiling and optional existing-cache A/B.

Only a pinned synthetic 7-page fixture is permitted. Test output in temp dir.
Persistent reports include numeric aggregates only; no OCR strings/hashes/crops.
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

V5_DIR = 'PII_V5_Security_Acceptance'


def load_gate(root):
    kit = root / V5_DIR
    if not (kit / 'v5_acceptance.py').is_file():
        raise FileNotFoundError(f'V5 kit missing: {kit}')
    sys.path.insert(0, str(kit))
    import v5_acceptance
    return v5_acceptance, kit


def child_env():
    env = os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5': '1', 'PII_PDF_EXPORT_V3': '0',
        'PII_OCR_BACKEND': 'rapid', 'PII_NER_BACKEND': 'ac',
        'PII_FACE_ENABLED': '1', 'PII_MEDIA_FP_PROFILE': 'synthetic_face_test',
        'PII_MEDIA_FP_REPORT': '0', 'PII_ZH_SINGLE_PASS': '1',
        'PII_MEDIA_CACHE_STATS': '0',
    })
    return env


def perform_one(root, source, kit, tool, mode, iteration, scratch, timeout, gate):
    run_dir = scratch / f'{iteration:02d}_{mode}'
    run_dir.mkdir()
    cmd = [sys.executable, str(tool / 'v65_child.py'),
           '--root', str(root), '--source', str(source),
           '--expected', str(kit / 'expected_text_and_faces.json'),
           '--runner', str(kit / 'run_7page_pdf_test.py'),
           '--out', str(run_dir), '--cache', mode]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=str(root), env=child_env(),
                              stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, check=False, timeout=timeout)
        return_code = proc.returncode
    except subprocess.TimeoutExpired:
        return_code = None
    result = {'mode': mode, 'elapsed_s': round(time.perf_counter() - t0, 3),
              'execution_ok': return_code == 0, 'safe': False}
    masked = run_dir / 'pii_face_7page_masked.pdf'
    audit = run_dir / 'pdf_face_7page_audit.json'
    profile = run_dir / 'v65_profile.json'
    if return_code != 0 or not masked.is_file() or not audit.is_file() or not profile.is_file():
        result['reason'] = 'TIMEOUT' if return_code is None else 'RUN_FAILED'
        return result, masked
    try:
        status = gate.audit_status(audit)
        structure = gate.check_pdf(masked)
        prof = json.loads(profile.read_text(encoding='utf-8'))
        if prof.get('schema') != 'pii_v65_profile_v1' or prof.get('cache_trial') != mode:
            raise ValueError('profile schema mismatch')
        result['ground_truth'] = status
        result['pdf_structure'] = structure
        result['profile'] = prof
        result['safe'] = bool(status.get('automated_pass') and structure.get('pass'))
        if not result['safe']:
            result['reason'] = 'SECURITY_GATE_FAILED'
    except Exception:
        result['reason'] = 'AUDIT_OR_PROFILE_ERROR'
    return result, masked


def summarize(runs, comparison=None):
    if not runs or any(not item.get('safe') for item in runs):
        return {'status': 'SECURITY_GATE_FAILED'}
    on = [r for r in runs if r['mode'] == 'on']
    off = [r for r in runs if r['mode'] == 'off']
    if not on:
        return {'status': 'INCOMPLETE'}
    summary = {
        'status': 'PROFILE_ONLY',
        'on_median_s': round(statistics.median(r['elapsed_s'] for r in on), 3),
        'on_runs': len(on), 'off_runs': len(off),
    }
    # Never claim a new speedup: the cache is already present in production.
    if off:
        if len(on) != len(off) or not comparison or not all(v.get('all_identical') for v in comparison):
            summary['status'] = 'PIXEL_DIFFERENCE_OR_INCOMPLETE'
            return summary
        on_s = summary['on_median_s']
        off_s = round(statistics.median(r['elapsed_s'] for r in off), 3)
        summary['off_median_s'] = off_s
        summary['existing_cache_benefit_pct'] = round((off_s - on_s) / off_s * 100, 2) if off_s else 0.0
        summary['status'] = 'CACHE_AB_SYNTHETIC_ONLY'
    return summary


def write_reports(report, dest):
    dest.mkdir(parents=True, exist_ok=True)
    jpath = dest / 'v65_pii_profile_summary.json'
    mpath = dest / 'v65_pii_profile_summary.md'
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# V6.5：PII 偵測瓶頸分析（固定合成 PDF）', '',
             f"- 狀態：**{report['status']}**",
             '- 正式程式未修改，所有 OCR 仍保留 2×+3×，V5 安全 PDF 輸出保留。',
             '- 報告只含統計數字，不保存文字、個資字串、雜湊、裁圖或私人對照表。',
             '- 已知 PII 驗證不能保證真實文件沒有漏偵。', '',
             '| 模式 | 完整處理時間（秒） | 27/27 | 17/17 | PDF 安全 |',
             '|---|---:|---|---|---|']
    for row in report['runs']:
        gt = row.get('ground_truth', {})
        lines.append(f"| {row['mode']} | {row['elapsed_s']} | {gt.get('text_regions_pass','-')}/27 | {gt.get('face_map_boxes_pass','-')}/17 | {row.get('safe',False)} |")
    lines.extend(['', '## 偵測階段（inclusive 時間，包含下層呼叫，不能直接加總）', '',
                  '| 模式 | 函式 | 呼叫次數 | 累積秒數 | p95 毫秒 | 重複輸入比例 |',
                  '|---|---|---:|---:|---:|---:|'])
    for row in report['runs']:
        prof = row.get('profile', {})
        for name, metric in prof.get('functions', {}).items():
            lines.append(f"| {row['mode']} | `{name}` | {metric['calls']} | {metric['inclusive_s']} | {metric['p95_ms']} | {metric['repeated_input_ratio_pct']}% |")
    lines.extend(['', '## 已存在的頁面快取命中', '',
                  '| 模式 | 命中 | 未命中 | 無法判定 | 2× 呼叫 | 3× 呼叫 |',
                  '|---|---:|---:|---:|---:|---:|'])
    for row in report['runs']:
        prof = row.get('profile', {})
        z = prof.get('zoom_calls', {})
        lines.append(f"| {row['mode']} | {prof.get('media_cache_preexisting_hits',0)} | {prof.get('media_cache_preexisting_misses',0)} | {prof.get('media_cache_unknown_calls',0)} | {z.get('zoom2',0)} | {z.get('zoom3',0)} |")
    s = report.get('comparison', {})
    lines.extend(['', '## 結論', '',
                  f"- 比較狀態：**{s.get('status','-')}**",
                  f"- 快取開啟中位數：{s.get('on_median_s','-')} s；關閉中位數：{s.get('off_median_s','未測')} s",
                  f"- **現有**快取估計效益：{s.get('existing_cache_benefit_pct','未測')}%（若有進行 A/B；不是新加速方案）。",
                  '- 重複輸入比例跨不同頁/階段計算，不等於可以跨文件安全快取。',
                  '- 每次測試的輸出 PDF 與含原始裁圖的 map 都只存在暫存目錄；若遭強制結束請檢查系統暫存目錄。',
                  '- 如果 `code_detect` 佔大宗，下一步剖析規則比對；若 `presidio_matches` 佔大宗，再分析 NER；不可先假設某項快取有效。', ''])
    mpath.write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser(description='V6.5 PII latency profiler on pinned synthetic PDF')
    ap.add_argument('--project-root', default='.')
    ap.add_argument('--source', default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    ap.add_argument('--results-dir', default='v65_pii_results')
    ap.add_argument('--repeats', type=int, choices=range(1, 5), default=1)
    ap.add_argument('--compare-cache', action='store_true', help='Also compare existing cache OFF to ON; no production edits')
    ap.add_argument('--timeout', type=int, default=1500)
    ap.add_argument('--check-only', action='store_true')
    args = ap.parse_args(argv)
    root = Path(args.project_root).resolve()
    tool = Path(__file__).resolve().parent
    if tool.parent != root:
        raise RuntimeError('Unzip the PII_V65_PII_Profiler folder directly under project root')
    source = (root / args.source).resolve()
    dest = (root / args.results_dir).resolve()
    gate, kit = load_gate(root)
    gate.check_setup(root, source, kit)
    fixed = json.loads((kit / 'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(fixed.get('samples', [])) != 7 or sum(len(s['expected_text']) for s in fixed['samples']) != 27:
        raise RuntimeError('fixed Ground Truth was modified')
    if args.compare_cache and '_pii_media_cache_var' not in (root / 'handler_media.py').read_text(encoding='utf-8'):
        raise RuntimeError('the current handler_media.py does not expose the V1 page-scoped PII cache')
    if args.check_only:
        print('[V6.5] CHECK_OK: fixed SHA-256, V5 kit, existing PDF output and GT were verified')
        return 0
    report = {'schema': 'pii_v65_ab_1', 'status': 'INCOMPLETE',
              'fixture_sha256_verified': True, 'only_synthetic_documents': True,
              'production_code_changed': False, 'dual_zoom_kept': True,
              'existing_cache_on_is_production_baseline': True,
              'private_maps_erased_on_normal_exit': True,
              'runs': [], 'pixel_comparison': [], 'comparison': {},
              'warning': 'Profile results only. No new optimization installed, and GUI responsiveness not tested.'}
    with tempfile.TemporaryDirectory(prefix='pii_v65_profile_') as tmp:
        scratch = Path(tmp)
        for n in range(args.repeats):
            print(f'[V6.5] {n+1}/{args.repeats}：目前正式版設定（cache ON）', flush=True)
            a, p_a = perform_one(root, source, kit, tool, 'on', 2*n, scratch, args.timeout, gate)
            report['runs'].append(a)
            if not a['safe']:
                report['status'] = 'SECURITY_GATE_FAILED'
                break
            if args.compare_cache:
                print(f'[V6.5] {n+1}/{args.repeats}：對照組（僅子程序 cache OFF）', flush=True)
                b, p_b = perform_one(root, source, kit, tool, 'off', 2*n+1, scratch, args.timeout, gate)
                report['runs'].append(b)
                if not b['safe']:
                    report['status'] = 'SECURITY_GATE_FAILED'
                    break
                pair = gate.compare_page_pixels(p_a, p_b, dpi=150)
                report['pixel_comparison'].append(pair)
                if not pair['all_identical']:
                    report['status'] = 'PIXEL_DIFFERENCE'
                    break
        if report['status'] == 'INCOMPLETE' and len(report['runs']) == args.repeats * (2 if args.compare_cache else 1):
            report['comparison'] = summarize(report['runs'], report['pixel_comparison'])
            report['status'] = report['comparison']['status']
        if not report['comparison']:
            report['comparison'] = {'status': report['status']}
    write_reports(report, dest)
    print('[V6.5] 狀態：', report['status'])
    print('[V6.5] 報告：', dest / 'v65_pii_profile_summary.md')
    return 0 if report['status'] in ('PROFILE_ONLY', 'CACHE_AB_SYNTHETIC_ONLY') else 2


if __name__ == '__main__':
    raise SystemExit(main())
