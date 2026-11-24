"""Safe launcher: pinned synthetic fixture only, full PDF security gate, no production edits."""
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
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def safe_env():
    env = os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5': '1', 'PII_PDF_EXPORT_V3': '0',
        'PII_OCR_BACKEND': 'rapid', 'PII_NER_BACKEND': 'ac',
        'PII_FACE_ENABLED': '1', 'PII_MEDIA_FP_PROFILE': 'synthetic_face_test',
        'PII_MEDIA_FP_REPORT': '0', 'PII_ZH_SINGLE_PASS': '1',
        'PII_MEDIA_CACHE': '1', 'PII_MEDIA_CACHE_STATS': '0'
    })
    return env


def classify(execution_ok, security_ok, samples):
    if not execution_ok:
        return 'RUN_FAILED'
    if not security_ok:
        return 'SECURITY_GATE_FAILED'
    if samples <= 0:
        return 'NO_PROFILE_SAMPLES'
    return 'PROFILE_OK'


def write_reports(report, dest):
    dest.mkdir(parents=True, exist_ok=True)
    (dest / 'v651_code_hotspots_summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    prof = report.get('hotspots', {})
    lines = ['# V6.5.1：`handler_code.detect()` 內部熱點分析', '',
             f"- 狀態：**{report['status']}**",
             '- 固定七頁合成 PDF、原本 2×+3× OCR、原本 V5 PDF 輸出。',
             '- 不修改正式專案檔案；只在子程序中包裝函式並抽樣。',
             '- cProfile 有測量開銷，因此執行時間不可直接視為正式版速度。',
             '- 不記錄 OCR 字串、函式參數、私有 map、圖片或雜湊。', '',
             f"- `handler_code.detect` 總呼叫：{prof.get('total_calls','-')}；其中 cProfile 抽樣：{prof.get('profiled_calls','-')}",
             f"- 本次完整處理時間（含分析開銷）：{report.get('elapsed_s','-')} 秒", 
             f"- 27/27：{report.get('text_pass','-')}；17/17：{report.get('face_pass','-')}；PDF 安全：{report.get('pdf_structure_pass',False)}", '',
             '## 函式自身耗時（self time）前 20 名', '',
             '| 檔案 | 函式 | 總呼叫 | 自身秒數 | 累積秒數 |',
             '|---|---|---:|---:|---:|']
    for row in prof.get('top_self_time', [])[:20]:
        lines.append(f"| `{row['module_file']}` | `{row['function']}` | {row['total_calls']} | {row['self_s']} | {row['cumulative_s']} |")
    lines.extend(['', '## 累積耗時（cumulative time）前 20 名', '',
                  '| 檔案 | 函式 | 總呼叫 | 自身秒數 | 累積秒數 |',
                  '|---|---|---:|---:|---:|'])
    for row in prof.get('top_cumulative_time', [])[:20]:
        lines.append(f"| `{row['module_file']}` | `{row['function']}` | {row['total_calls']} | {row['self_s']} | {row['cumulative_s']} |")
    lines.extend(['', '## 解讀限制', '',
                  '此處的累積時間是重疊的，不可加總；僅取樣部分呼叫，也未驗證任何新加速方案。',
                  '只有確定真正的慢函式後才考慮局部修改，後續需重新進行像素等價與安全回歸。',
                  'GT 及人臉框覆蓋是固定合成案例，不代表真實文件偵測率。'])
    (dest / 'v651_code_hotspots_summary.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def main(argv=None):
    p = argparse.ArgumentParser(description='PII V6.5.1 code.detect hotspots (synthetic only)')
    p.add_argument('--project-root', default='.')
    p.add_argument('--source', default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    p.add_argument('--results-dir', default='v651_hotspots_results')
    p.add_argument('--profile-every', type=int, default=4)
    p.add_argument('--max-profiled-calls', type=int, default=80)
    p.add_argument('--timeout', type=int, default=1500)
    p.add_argument('--check-only', action='store_true')
    a = p.parse_args(argv)
    root = Path(a.project_root).resolve()
    src = (root / a.source).resolve()
    dest = (root / a.results_dir).resolve()
    tool = Path(__file__).resolve().parent
    if tool.parent != root:
        raise RuntimeError('Unzip PII_V651_Code_Hotspots under the project root')
    if not (root / 'handler_media.py').is_file():
        raise RuntimeError('handler_media.py missing')
    if not src.is_file() or sha256(src) != FIXTURE_SHA256:
        raise RuntimeError('source is not the pinned 7-page synthetic fixture')
    kit = root / 'PII_V5_Security_Acceptance'
    if not all((kit / x).is_file() for x in ('v5_acceptance.py', 'run_7page_pdf_test.py','expected_text_and_faces.json')):
        raise RuntimeError('V5 security acceptance kit missing')
    sys.path.insert(0, str(kit))
    import v5_acceptance as gate
    gate.check_setup(root, src, kit)
    if a.profile_every < 1 or not 1 <= a.max_profiled_calls <= 2000 or a.timeout < 10:
        raise ValueError('invalid sample frequency, cap, or timeout')
    if a.check_only:
        print('[V6.5.1] CHECK_OK: fixed source SHA256, project, V5 kit')
        return 0
    report = {
        'schema': 'v651_code_hotspots_ab_1', 'status': 'INCOMPLETE',
        'synthetic_fixture_verified': True, 'production_code_changed': False,
        'only_synthetic_documents': True, 'dual_zoom_kept': True,
        'sample_every': a.profile_every, 'max_profiled_calls': a.max_profiled_calls,
        'warning': 'Profiling only; measurement overhead applies. Not an acceleration claim.',
    }
    with tempfile.TemporaryDirectory(prefix='pii_v651_') as tdir:
        out = Path(tdir) / 'masked_run'
        out.mkdir()
        cmd = [sys.executable, str(tool / 'v651_child.py'), '--root', str(root), '--source', str(src),
               '--runner', str(kit / 'run_7page_pdf_test.py'),
               '--expected', str(kit / 'expected_text_and_faces.json'),
               '--out', str(out), '--profile-every', str(a.profile_every),
               '--max-profiled-calls', str(a.max_profiled_calls)]
        start = time.perf_counter()
        try:
            result = subprocess.run(cmd, cwd=root, env=safe_env(), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    check=False, timeout=a.timeout)
            return_code = result.returncode
        except subprocess.TimeoutExpired:
            return_code = None
        report['elapsed_s'] = round(time.perf_counter() - start, 3)
        report['execution_ok'] = return_code == 0
        profile = out / 'v651_hotspots_privatefree.json'
        pdf = out / 'pii_face_7page_masked.pdf'
        audit = out / 'pdf_face_7page_audit.json'
        security = False
        if return_code == 0 and all(x.is_file() for x in (profile, pdf, audit)):
            try:
                report['hotspots'] = json.loads(profile.read_text(encoding='utf-8'))
                if report['hotspots'].get('schema') != 'v651_code_hotspots_1':
                    raise ValueError('profile schema mismatch')
                gt = gate.audit_status(audit)
                pdf_status = gate.check_pdf(pdf)
                report['text_pass'] = f"{gt.get('text_regions_pass',0)}/{gt.get('text_regions_expected',27)}"
                report['face_pass'] = f"{gt.get('face_map_boxes_pass',0)}/{gt.get('face_map_boxes_checked',17)}"
                report['pdf_structure_pass'] = bool(pdf_status.get('pass'))
                security = bool(gt.get('automated_pass') and pdf_status.get('pass'))
            except Exception:
                report['error_type'] = 'VALIDATION_FAILED'
        report['status'] = classify(return_code == 0, security, report.get('hotspots',{}).get('profiled_calls',0))
    write_reports(report, dest)
    print('[V6.5.1] status:', report['status'])
    print('[V6.5.1] report:', dest / 'v651_code_hotspots_summary.md')
    return 0 if report['status'] == 'PROFILE_OK' else 2


if __name__ == '__main__':
    raise SystemExit(main())
