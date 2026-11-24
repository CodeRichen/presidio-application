#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9.2 — synthetic-only integration in the actual ui.App event loop.

NO original code or environments changed. DO NOT use on real documents.
This launcher disables all original GUI tabs and mounts a synthetic-only test tab.
"""
from __future__ import annotations
import argparse
import ast
import importlib.util
import json
import math
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

EXPECTED_SHA = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
ALLOWED_MODES = ('hybrid', 'cpu', 'pair')
HEARTBEAT_MS = 100


def source_hooks(root: Path) -> dict:
    """Read-only AST validation, never import production source."""
    checks = {}
    try:
        ui = ast.parse((root / 'ui.py').read_text(encoding='utf-8-sig'))
        core = ast.parse((root / 'core.py').read_text(encoding='utf-8-sig'))
        media = ast.parse((root / 'handler_media.py').read_text(encoding='utf-8-sig'))
        backend = ast.parse((root / 'ocr_backend.py').read_text(encoding='utf-8-sig'))
    except (OSError, SyntaxError, UnicodeError):
        return {'sources_parseable': False}
    checks['sources_parseable'] = True
    app = next((n for n in ui.body if isinstance(n, ast.ClassDef) and n.name == 'App'), None)
    methods = {n.name for n in app.body if isinstance(n, ast.FunctionDef)} if app else set()
    checks['gui_app_and_jobs'] = {'_build', '_start', '_poll', 'run_files', 'run_scan', 'run_text'}.issubset(methods)
    checks['core_media_dispatch'] = all(any(isinstance(n, ast.FunctionDef) and n.name == f for n in core.body)
                                        for f in ('dispatch_files', '_mask_media'))
    checks['media_mask_pdf'] = any(isinstance(n, ast.FunctionDef) and n.name == 'mask_pdf' for n in media.body)
    checks['rapid_word_box'] = any(isinstance(n, ast.FunctionDef) and n.name == 'rapid_ocr_words' for n in backend.body) and any(isinstance(n, ast.keyword) and n.arg == 'return_word_box' for n in ast.walk(backend))
    return checks


def validate_destination(root: Path, value: str) -> Path:
    """Output only beneath the project, never in source, venv, or private directories."""
    if not value or '\x00' in value:
        raise ValueError('INVALID_RESULTS_DIRECTORY')
    result = (root / value).resolve()
    if root == result or root not in result.parents:
        raise ValueError('RESULTS_MUST_BE_WITHIN_PROJECT')
    rel = result.relative_to(root)
    deny = {'presidio', 'real', '.venv', '.venv_ocr_gpu', '.git', 'pii_face_pdf_7page_test',
            'pii_v5_security_acceptance', 'pii_v6676_full_pdf_hybrid_ab',
            'pii_v6692_official_gui_synthetic_integration'}
    if any(part.lower() in deny for part in rel.parts):
        raise ValueError('PROTECTED_RESULTS_DIRECTORY')
    return result


def preflight(root: Path, result_dir: Path) -> dict:
    checks = source_hooks(root)
    required_files = (
        root / 'PII_V6676_Full_PDF_Hybrid_AB' / 'v6676_benchmark.py',
        root / 'PII_V6676_Full_PDF_Hybrid_AB' / 'v6676_child.py',
        root / 'PII_V6676_Full_PDF_Hybrid_AB' / 'v667_gpu_worker.py',
        root / 'PII_V5_Security_Acceptance' / 'v5_acceptance.py',
        root / '.venv' / 'Scripts' / 'python.exe',
        root / '.venv_ocr_gpu' / 'Scripts' / 'python.exe',
    )
    checks['v6676_and_venvs_present'] = all(x.is_file() for x in required_files)
    fixture = root / 'PII_Face_PDF_7page_Test' / 'pii_face_7page_scan_source.pdf'
    if fixture.is_file():
        import hashlib
        h = hashlib.sha256()
        with fixture.open('rb') as stream:
            for block in iter(lambda: stream.read(1048576), b''):
                h.update(block)
        checks['fixed_synthetic_fixture'] = h.hexdigest() == EXPECTED_SHA
    else:
        checks['fixed_synthetic_fixture'] = False
    checks['windows'] = os.name == 'nt'
    checks['results_directory_safe'] = bool(result_dir != root and root in result_dir.parents)
    return checks


def filtered_trial(record: dict) -> dict:
    """No raw source text, output path, exception string, PII, or filenames."""
    allowed = ('safe', 'mode', 'reason', 'child_status', 'child_stage', 'processing_s',
               'run_and_audit_s', 'preflight_s', 'child_elapsed_s', 'worker_ocr_calls',
               'worker_ocr_wall_s', 'preflight_pairs', 'preflight_equal_pairs',
               'text_regions_pass', 'face_map_boxes_pass', 'pdf_structure_pass')
    out = {k: record[k] for k in allowed if k in record and isinstance(record[k], (str, bool, int, float, type(None)))}
    route = record.get('provider_evidence')
    if isinstance(route, dict):
        events = route.get('node_events') or {}
        out['provider_route_verified'] = bool(events.get('det_cuda', 0) > 0 and
            events.get('cls_cpu', 0) > 0 and events.get('rec_cpu', 0) > 0 and
            events.get('cls_cuda', 0) == 0 and events.get('rec_cuda', 0) == 0)
    return out


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = (len(s) - 1) * q
    lo = math.floor(idx)
    hi = math.ceil(idx)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def summarize_heartbeat(values: list[float]) -> dict:
    return {
        'samples': len(values), 'interval_ms': HEARTBEAT_MS,
        'p95_lag_ms': round(percentile(values, .95), 3),
        'p99_lag_ms': round(percentile(values, .99), 3),
        'max_lag_ms': round(max(values, default=0), 3),
        'over_100ms': sum(x > 100 for x in values),
        'over_250ms': sum(x > 250 for x in values),
        'over_500ms': sum(x > 500 for x in values),
    }


def next_run_index(result_dir: Path) -> int:
    # Never overwrite reports.
    for n in range(1, 1000):
        if not (result_dir / f'v6692_run_{n:03d}.json').exists() and not (result_dir / f'v6692_run_{n:03d}.md').exists():
            return n
    raise RuntimeError('TOO_MANY_REPORTS')


def write_reports(result_dir: Path, record: dict) -> int:
    """Record contains only explicitly generated summary fields."""
    result_dir.mkdir(parents=True, exist_ok=True)
    index = next_run_index(result_dir)
    data = result_dir / f'v6692_run_{index:03d}.json'
    md = result_dir / f'v6692_run_{index:03d}.md'
    encoded = json.dumps(record, ensure_ascii=False, indent=2) + '\n'
    with data.open('x', encoding='utf-8') as out:
        out.write(encoded)
    lines = [
        '# V6.6.9.2 正式 UI 事件迴圈・合成 PDF 專用整合', '',
        f"- 狀態：**{record['status']}**",
        f"- 模式：**{record['mode']}**",
        '- 使用真實 ui.App 事件佇列，但只允許固定合成七頁 PDF。',
        '- 不修改正式 ui.py / core.py，不允許處理真實文件。',
        '- 目前不是正式 GPU 部署或真實文件安全驗收。',
        '- 單模式沒有 CPU/GPU 像素比對；pair 模式才有。', '',
        f"- 點擊到 GUI 完成回呼：{record['gui']['click_to_finish_s']} 秒",
        f"- Tk p95 / p99 / 最大延遲：{record['gui']['p95_lag_ms']} / {record['gui']['p99_lag_ms']} / {record['gui']['max_lag_ms']} ms",
        f"- 延遲 >500ms 次數：{record['gui']['over_500ms']}",
        f"- CPU/GPU 七頁像素完全一致：{record.get('pixel_identical')}",
    ]
    for mode, trial in record.get('trials', {}).items():
        lines.append(f"- {mode}：safe={trial.get('safe')}, processing_s={trial.get('processing_s')}, child_elapsed_s={trial.get('child_elapsed_s')}, PII={trial.get('text_regions_pass')}, face={trial.get('face_map_boxes_pass')}")
    with md.open('x', encoding='utf-8') as out:
        out.write('\n'.join(lines) + '\n')
    return index


def do_experiment(root: Path, mode: str, notify=None, timeout: int = 1800) -> dict:
    """Worker-thread-only. Calls the real V6676 safety gate; no user file paths."""
    if mode not in ALLOWED_MODES:
        raise ValueError('INVALID_TEST_MODE')
    tool = root / 'PII_V6676_Full_PDF_Hybrid_AB'
    spec = importlib.util.spec_from_file_location('v6692_existing_v6676', tool / 'v6676_benchmark.py')
    if not spec or not spec.loader:
        raise RuntimeError('BENCHMARK_LOADER_UNAVAILABLE')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = root / 'PII_Face_PDF_7page_Test' / 'pii_face_7page_scan_source.pdf'
    gate, gate_dir, expected, app_python, gpu_python = mod.require_setup(root, source, tool)
    modes = ['hybrid', 'cpu'] if mode == 'pair' else [mode]
    results = {'status': 'INCOMPLETE', 'mode': mode, 'trials': {}, 'pixel_identical': None}
    with tempfile.TemporaryDirectory(prefix='pii_v6692_') as scratch:
        paths = {}
        for current in modes:
            if notify:
                notify('GPU_FULL_PDF' if current == 'hybrid' else 'CPU_FULL_PDF')
            dest = Path(scratch) / current
            record = mod.run_trial(root, tool, gate, gate_dir, expected, app_python, gpu_python,
                                   source, current, dest, timeout)
            results['trials'][current] = filtered_trial(record)
            paths[current] = dest
            if not record.get('safe'):
                results['status'] = 'SAFETY_OR_EXECUTION_FAILED'
                return results
        if mode == 'pair':
            if notify:
                notify('PIXEL_COMPARISON')
            try:
                cpu_pdf = paths['cpu'] / 'pdf_result' / 'pii_face_7page_masked.pdf'
                gpu_pdf = paths['hybrid'] / 'pdf_result' / 'pii_face_7page_masked.pdf'
                results['pixel_identical'] = mod.strictly_identical_pages(gate, cpu_pdf, gpu_pdf)
            except (OSError, ValueError, RuntimeError, TypeError, KeyError):
                results['status'] = 'PIXEL_COMPARE_FAILED'
                return results
            results['status'] = 'SYNTHETIC_GUI_PAIR_PASS' if results['pixel_identical'] else 'PIXEL_DIFFERENCE'
        else:
            results['status'] = 'SINGLE_MODE_SYNTHETIC_GATE_PASS'
    return results


def launch_gui(root: Path, result_dir: Path) -> None:
    import tkinter as tk
    from tkinter import ttk
    sys.path.insert(0, str(root))
    # ui.py is the user's original module. This process only patches functions in memory.
    import ui
    from ui import App
    # No warmup needed in this GUI process: V6676 child initializes independently.
    ui.core.warmup = lambda: None
    original_build = App._build

    def diagnostic_build(self):
        original_build(self)
        # Make all real-file tabs inaccessible in this test-only instance.
        for index in range(self.nb.index('end')):
            self.nb.tab(index, state='disabled')
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text='V6.6.9.2 合成測試')
        self.nb.select(tab)
        ttk.Label(tab, text='只測固定七頁合成 PDF；不能選取或處理真實文件。', foreground='#9a3412').pack(anchor='w', padx=12, pady=10)
        self.v6692_mode = tk.StringVar(value='hybrid')
        bar = ttk.Frame(tab)
        bar.pack(anchor='w', padx=12)
        for name, title in [('hybrid', '混合 GPU'), ('cpu', 'CPU'), ('pair', 'CPU/GPU 像素 A/B')]:
            ttk.Radiobutton(bar, text=title, value=name, variable=self.v6692_mode).pack(side='left', padx=8)
        self.v6692_button = ttk.Button(tab, text='開始固定合成 PDF 安全測試', command=self.v6692_start)
        self.v6692_button.pack(anchor='w', padx=12, pady=12)
        self.v6692_note = tk.StringVar(value='待測試（不支援真實文件）。')
        ttk.Label(tab, textvariable=self.v6692_note, wraplength=840, justify='left').pack(anchor='w', padx=12)
        self.v6692_lags = []
        self.v6692_last_pulse = time.perf_counter()
        self.after(HEARTBEAT_MS, self.v6692_pulse)

    def pulse(self):
        now = time.perf_counter()
        extra = max(0., (now - self.v6692_last_pulse) * 1000 - HEARTBEAT_MS)
        self.v6692_lags.append(extra)
        if len(self.v6692_lags) > 12000:
            del self.v6692_lags[:len(self.v6692_lags) - 12000]
        self.v6692_last_pulse = now
        self.after(HEARTBEAT_MS, self.v6692_pulse)

    def start(self):
        if self.busy:
            return
        mode = self.v6692_mode.get()
        if mode not in ALLOWED_MODES:
            return
        started = time.perf_counter()
        start_sample = len(self.v6692_lags)
        self.v6692_button.state(['disabled'])
        self.v6692_note.set('執行安全測試中；請勿關閉視窗。')

        def notify(stage):
            if stage in ('GPU_FULL_PDF', 'CPU_FULL_PDF', 'PIXEL_COMPARISON'):
                self.q.put(('status', f'合成資料測試：{stage}'))

        def work():
            try:
                return do_experiment(root, mode, notify)
            except Exception:
                return {'status': 'EXPERIMENT_EXCEPTION', 'mode': mode, 'trials': {}, 'pixel_identical': None}

        def done(report):
            report['schema'] = 'pii_v6692_official_ui_synthetic_only_v1'
            report['synthetic_only'] = True
            report['not_production_gpu_enablement'] = True
            report['production_files_modified'] = False
            report['gui'] = summarize_heartbeat(self.v6692_lags[start_sample:])
            report['gui']['click_to_finish_s'] = round(time.perf_counter() - started, 3)
            try:
                number = write_reports(result_dir, report)
                self.v6692_note.set(f"{report['status']}；報告序號 {number:03d}；耗時 {report['gui']['click_to_finish_s']} s")
            except (OSError, ValueError, RuntimeError):
                self.v6692_note.set('測試已完成，但安全統計報告未能寫入；請檢查輸出目錄。')
            finally:
                self.v6692_button.state(['!disabled'])

        self._start(work, '合成 PDF 安全測試中…', done)
        if not self.busy:
            self.v6692_button.state(['!disabled'])

    App._build = diagnostic_build
    App.v6692_start = start
    App.v6692_pulse = pulse
    # Disable production document entry points as a second safety barrier.
    App.run_files = lambda self: None
    App.run_scan = lambda self: None
    App.run_text = lambda self: None
    instance = App()
    instance.mainloop()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--project-root', default='.')
    parser.add_argument('--results-dir', default='v6692_gui_results')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    root = Path(args.project_root).resolve()
    try:
        result_dir = validate_destination(root, args.results_dir)
    except ValueError as exc:
        print('V6692_STATUS:', exc)
        return 2
    checks = preflight(root, result_dir)
    if not all(checks.values()):
        print('V6692_STATUS: PREFLIGHT_REVIEW_REQUIRED')
        for k, v in checks.items():
            if not v:
                print('CHECK_FAILED:', k)
        return 2
    if args.check_only:
        print('V6692_STATUS: SYNTHETIC_GUI_HOOKS_READY')
        print('NO_GUI_STARTED; NO_MODEL_OR_PDF_PROCESSED')
        return 0
    launch_gui(root, result_dir)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
