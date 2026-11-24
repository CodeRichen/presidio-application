#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.8: isolated tkinter GUI smoke test of the V6.6.7.6 synthetic-only AB.

This does not patch, import or claim to test production ui.py or core.py.
The existing V6676 safety-gated trial is invoked unchanged. No real files.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

TITLE = 'V6.6.8 隔離 GUI — 固定合成 PDF 安全 A/B'
JSON_NAME = 'v668_gui_smoke_summary.json'
MD_NAME = 'v668_gui_smoke_summary.md'


def project_paths(script: Path):
    kit = script.resolve().parent
    root = kit.parent
    benchmark = root / 'PII_V6676_Full_PDF_Hybrid_AB' / 'v6676_benchmark.py'
    return kit, root, benchmark


def validate_destination(root: Path, dest: Path):
    root = root.resolve()
    dest = dest.resolve()
    if dest == root or root not in dest.parents:
        raise ValueError('RESULTS_DIR_MUST_BE_INSIDE_PROJECT')
    if any(p in {'PII_V5_Security_Acceptance', 'PII_V6676_Full_PDF_Hybrid_AB'} for p in dest.parts):
        raise ValueError('RESULTS_DIR_MUST_NOT_OVERLAP_TOOLKITS')
    return dest


def load_v6676(kit: Path, root: Path, script: Path):
    if kit.parent != root or kit.name != 'PII_V668_Isolated_GUI_Smoke':
        raise RuntimeError('KIT_MUST_BE_IN_PROJECT_ROOT')
    if not script.is_file():
        raise RuntimeError('V6676_BENCHMARK_NOT_FOUND')
    # Keep imports non-invasive and explicit; the project is allowed only
    # after the fixed synthetic fixture is validated by V6676.require_setup.
    import importlib.util
    spec = importlib.util.spec_from_file_location('v668_v6676_benchmark', script)
    if spec is None or spec.loader is None:
        raise RuntimeError('BENCHMARK_IMPORT_FAILED')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_setup(benchmark, root: Path, tool: Path):
    source = root / 'PII_Face_PDF_7page_Test' / 'pii_face_7page_scan_source.pdf'
    gate, gate_dir, expected, app_python, gpu_python = benchmark.require_setup(root, source, tool)
    return source, gate, gate_dir, expected, app_python, gpu_python


def quantile(vals, pct):
    if not vals:
        return None
    values = sorted(float(v) for v in vals)
    rank = math.ceil((len(values) - 1) * pct)
    return round(values[max(0, min(rank, len(values) - 1))], 3)


def redacted_trial(trial):
    keys = ('mode', 'return_code', 'safe', 'reason', 'child_status', 'child_stage',
            'error_type', 'preflight_pairs', 'preflight_equal_pairs',
            'processing_s', 'run_and_audit_s', 'preflight_s',
            'child_elapsed_s', 'worker_ocr_calls', 'worker_ocr_wall_s',
            'text_regions_pass', 'face_map_boxes_pass', 'pdf_structure_pass')
    out = {k: trial[k] for k in keys if k in trial}
    if trial.get('mode') == 'hybrid':
        ev = trial.get('provider_evidence', {})
        sess, nodes = ev.get('sessions', {}), ev.get('node_events', {})
        out['provider_evidence'] = {
            'sessions': {k: int(sess.get(k, 0)) for k in ('det', 'cls', 'rec')},
            'node_events': {k: int(nodes.get(k, 0)) for k in (
                'det_cuda', 'det_cpu', 'cls_cpu', 'cls_cuda', 'rec_cpu', 'rec_cuda')},
            'profiles_read': int(ev.get('profiles_read', 0)),
        }
    return out


def run_one_pair(benchmark, root, tool, setup, emit, timeout=1800):
    """Run ONE synthetic-only, safety-gated pair, with no output PDFs persisted.

    The V6676 run_trial checks the 27 PII regions, 17 recorded face boxes,
    V5 PDF structural safety and actual ORT provider node events.
    """
    source, gate, gate_dir, expected, app_python, gpu_python = setup
    out = {'pair': 1, 'order': 'BA', 'hybrid': {}, 'cpu': {},
           'pixel_identical': False}
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='pii_v668_') as tmp:
        scratch = Path(tmp)
        for mode in ('hybrid', 'cpu'):
            emit({'kind': 'phase', 'mode': mode})
            target = scratch / mode
            trial = benchmark.run_trial(
                root, tool, gate, gate_dir, expected, app_python, gpu_python,
                source, mode, target, timeout)
            out[mode] = redacted_trial(trial)
            if not trial.get('safe'):
                out['status'] = 'SECURITY_OR_EXECUTION_FAILED'
                break
        else:
            emit({'kind': 'phase', 'mode': 'pixel_check'})
            out['pixel_identical'] = bool(benchmark.strictly_identical_pages(
                gate, scratch / 'cpu' / 'pdf_result' / 'pii_face_7page_masked.pdf',
                scratch / 'hybrid' / 'pdf_result' / 'pii_face_7page_masked.pdf'))
            out['status'] = ('ISOLATED_GUI_AB_SAFE' if out['pixel_identical']
                             else 'PDF_PIXEL_DIFFERENCE')
    out['worker_pair_elapsed_s'] = round(time.perf_counter() - start, 3)
    return out


def compile_report(pair, t_click_to_ui, lag_samples):
    safe = pair.get('status') == 'ISOLATED_GUI_AB_SAFE'
    cpu, hybrid = pair.get('cpu', {}), pair.get('hybrid', {})
    delta = None
    if safe and cpu.get('processing_s') and hybrid.get('processing_s'):
        delta = round(100 * (float(cpu['processing_s']) - float(hybrid['processing_s'])) /
                      float(cpu['processing_s']), 2)
    max_lag = round(max(lag_samples), 3) if lag_samples else None
    # A single Tk scheduling spike must be reported as a separate review flag,
    # not conflated with PDF safety or processing correctness.
    gui_status = ('GUI_SCHEDULING_REVIEW' if max_lag is not None and max_lag > 500
                  else 'GUI_SCHEDULING_OBSERVED')
    return {
        'schema': 'pii_v668_isolated_gui_smoke_v1',
        'status': pair.get('status', 'INCOMPLETE'),
        'not_production_gui': True,
        'synthetic_only': True,
        'production_code_modified': False,
        'production_environment_modified': False,
        'dual_zoom_kept': True,
        'pdf_security_gate_executed': safe or bool(cpu.get('text_regions_pass')),
        'comparison': {'cpu_processing_s': cpu.get('processing_s'),
                       'hybrid_processing_s': hybrid.get('processing_s'),
                       'processing_improvement_pct': delta,
                       'pixel_identical': bool(pair.get('pixel_identical'))},
        'gui': {'click_to_ui_finish_s': round(t_click_to_ui, 3),
                'worker_pair_elapsed_s': pair.get('worker_pair_elapsed_s'),
                'heartbeat_interval_ms': 100,
                'heartbeat_samples': len(lag_samples),
                'max_schedule_delay_ms': max_lag,
                'p95_schedule_delay_ms': quantile(lag_samples, .95),
                'responsiveness_review': gui_status},
        'pair': {k: pair[k] for k in ('order', 'status') if k in pair},
        'cpu': cpu, 'hybrid': hybrid,
        'warning': ('Isolated synthetic-only Tk GUI experiment, not the project ui.py. '
                    'Click timing includes V6676 safety audits and 2 separate process launches; '
                    'not a single-document production GUI duration. No real-document use.'),
    }


def write_report(out_dir, summary):
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / JSON_NAME).write_text(json.dumps(summary, indent=2, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    c = summary['comparison']; g = summary['gui']
    lines = [
        '# V6.6.8 隔離 GUI CPU / 混合 GPU 安全 A/B', '',
        f"- 狀態：**{summary['status']}**",
        '- 只執行固定 SHA-256 合成七頁 PDF，保留 2×+3× OCR。',
        '- 這是**隔離 Tkinter 測試介面**，不是正式 `ui.py` 或 `core.py` 的整合驗收。',
        '- 不修改正式程式、虛擬環境或任何 OCR / PII 規則。', '',
        '| 指標 | 觀測值 |', '|---|---:|',
        f"| CPU 純處理 | {c['cpu_processing_s']} 秒 |",
        f"| 混合純處理 | {c['hybrid_processing_s']} 秒 |",
        f"| 純處理改善 | {c['processing_improvement_pct']}% |",
        f"| PDF 七頁像素一致 | {c['pixel_identical']} |",
        f"| GUI 點擊→兩組測試與畫面顯示 | {g['click_to_ui_finish_s']} 秒 |",
        f"| Tk after(100ms) 最大額外延遲 | {g['max_schedule_delay_ms']} ms |",
        f"| Tk after(100ms) p95 額外延遲 | {g['p95_schedule_delay_ms']} ms |",
        '', '- 介面時間是**連續執行混合＋CPU 兩組**的實驗總時間，不是單次 PDF 使用者等待時間。',
        '- 完整 PDF 安全驗收與像素比對沿用 V6.6.7.6；27/27、17/17 僅對固定資料有效。',
        '- 不保留 PDF、截圖、OCR 文字、框座標、檔案路徑、原始 profiling 或私有 map。',
    ]
    (out_dir / MD_NAME).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_gui(root, tool, bm, setup, results, timeout):
    import tkinter as tk
    from tkinter import messagebox
    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(TITLE)
            self.geometry('650x430')
            self.minsize(570, 370)
            self.queue = queue.Queue()
            self.busy = False
            self.start = 0.0
            self.lags = []
            self.tick_due = time.perf_counter() + 0.1
            self.label = tk.StringVar(value='固定七頁合成 PDF。僅隔離 GUI 試驗，不修改正式應用程式。')
            tk.Label(self, text='V6.6.8｜隔離 GUI 安全 A/B', font=('Segoe UI', 16, 'bold')).pack(pady=(22, 10))
            tk.Label(self, textvariable=self.label, wraplength=590).pack(pady=8)
            self.button = tk.Button(self, text='執行一組 CPU／混合 GPU A/B',
                                    command=self.on_click, font=('Segoe UI', 12))
            self.button.pack(pady=10)
            self.box = tk.Text(self, height=11, width=75, state='disabled')
            self.box.pack(padx=18, pady=10, fill='both', expand=True)
            self.protocol('WM_DELETE_WINDOW', self.on_close)
            self.after(100, self.pulse)
        def log(self, text):
            self.box.configure(state='normal')
            self.box.insert('end', text + '\n')
            self.box.see('end')
            self.box.configure(state='disabled')
        def on_close(self):
            if self.busy:
                messagebox.showwarning('測試執行中', '正在執行安全稽核，請等測試結束後再關閉，避免留下 GPU worker。')
                return
            self.destroy()
        def on_click(self):
            if self.busy:
                return
            self.busy = True
            self.start = time.perf_counter()
            self.lags = []
            self.button.configure(state='disabled')
            self.label.set('執行合成 PDF 安全測試中，介面仍會更新；不要關閉視窗。')
            self.log('開始：先混合 GPU，再 CPU，完成後比較 PDF 像素。')
            def job():
                try:
                    answer = run_one_pair(bm, root, tool, setup, self.queue.put, timeout)
                    self.queue.put({'kind':'done','data':answer})
                except BaseException as exc:
                    self.queue.put({'kind':'error', 'error_type':type(exc).__name__})
            threading.Thread(target=job, daemon=True).start()
        def pulse(self):
            now = time.perf_counter()
            if self.busy:
                self.lags.append(max(0.0, (now - self.tick_due) * 1000))
            # Detect scheduler delay rather than interval length.
            self.tick_due = now + 0.1
            try:
                while True:
                    message = self.queue.get_nowait()
                    kind = message.get('kind')
                    if kind == 'phase':
                        names={'hybrid':'混合 GPU 全七頁測試', 'cpu':'CPU 全七頁測試',
                               'pixel_check':'輸出 PDF 逐頁像素比較'}
                        self.log('階段：' + names.get(message.get('mode'),'測試階段'))
                    elif kind == 'done':
                        # Render and measure on main Tk thread, never from worker.
                        pair = message['data']
                        delay = time.perf_counter() - self.start
                        report = compile_report(pair, delay, self.lags)
                        write_report(results, report)
                        self.log('結果：' + report['status'])
                        if report['comparison']['processing_improvement_pct'] is not None:
                            self.log('純處理改善：' + str(report['comparison']['processing_improvement_pct']) + '%')
                        self.log('PDF 逐頁像素一致：' + str(report['comparison']['pixel_identical']))
                        self.log('點擊到實驗結果顯示：' + str(report['gui']['click_to_ui_finish_s']) + ' 秒（兩組 A/B）')
                        self.log('Tk 最大延遲：' + str(report['gui']['max_schedule_delay_ms']) + ' 毫秒')
                        self.label.set('測試完成。請上傳結果資料夾中的 JSON 與 MD。')
                        self.busy = False
                        self.button.configure(state='normal')
                    elif kind == 'error':
                        pair = {'status':'GUI_OR_TEST_ERROR','cpu':{},'hybrid':{},
                                'pixel_identical':False,'worker_pair_elapsed_s':None}
                        report = compile_report(pair, time.perf_counter()-self.start, self.lags)
                        report['safe_error_type'] = (message['error_type'] if message['error_type'] in (
                            'RuntimeError','ValueError','OSError','KeyError','TypeError',
                            'FileNotFoundError','TimeoutError','AssertionError') else 'OTHER_EXCEPTION')
                        write_report(results, report)
                        self.log('測試中斷：GUI_OR_TEST_ERROR；請上傳安全報告，不要用真實文件重試。')
                        self.label.set('測試未通過；正式程式未修改。')
                        self.busy = False
                        self.button.configure(state='normal')
            except queue.Empty:
                pass
            self.after(100, self.pulse)
    App().mainloop()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--check-only', action='store_true')
    ap.add_argument('--results-dir', default='v668_gui_smoke_results')
    ap.add_argument('--timeout', type=int, default=1800)
    args = ap.parse_args(argv)
    script = Path(__file__).resolve()
    kit, root, benchmark_script = project_paths(script)
    dest = validate_destination(root, root / args.results_dir)
    bm = load_v6676(kit, root, benchmark_script)
    setup = get_setup(bm, root, benchmark_script.parent)
    if args.check_only:
        print('CHECK_OK: 固定合成 PDF、V5 驗收、V6676 工具與兩套 Python 環境齊全')
        print('NOTE: 這是隔離 GUI，不是正式 ui.py 或 core.py 整合；尚未執行安全 A/B')
        return 0
    if args.timeout < 60 or args.timeout > 7200:
        raise ValueError('TIMEOUT_OUT_OF_RANGE')
    run_gui(root, benchmark_script.parent, bm, setup, dest, args.timeout)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
