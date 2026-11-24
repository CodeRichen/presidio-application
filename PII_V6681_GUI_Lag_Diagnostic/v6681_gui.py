#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.8.1: isolated Tk heartbeat and single-mode synthetic PDF safety diagnostic.

NOT the user's real ui.py/core.py; never edits production or accepts external PDFs.
Always uses V6676's SHA256-gated fixed synthetic fixture and safety-gated run_trial.
All child OCR/PDF/map artifacts remain in an auto-removed TemporaryDirectory.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import queue
import statistics
import tempfile
import threading
import time
from pathlib import Path

KIT = 'PII_V6681_GUI_Lag_Diagnostic'
SAMPLE_MS = 100
ALLOWED_MODES = ('hybrid', 'cpu', 'idle')
ALLOWED_STAGES = ('READY', 'GUI_IDLE', 'GPU_FULL_PDF', 'CPU_FULL_PDF', 'RESULT_RENDER')


def quantile(values, pct):
    if not values:
        return None
    v = sorted(float(x) for x in values)
    i = min(len(v) - 1, max(0, math.ceil((len(v) - 1) * pct)))
    return round(v[i], 3)


def validate_destination(root: Path, destination: Path):
    root, destination = root.resolve(), destination.resolve()
    if destination == root or root not in destination.parents:
        raise ValueError('RESULTS_DIR_MUST_BE_INSIDE_PROJECT')
    rel = destination.relative_to(root)
    protected = {'.venv', '.venv_ocr_gpu', '.git', 'presidio',
                 'PII_V5_Security_Acceptance', 'PII_V6676_Full_PDF_Hybrid_AB', KIT}
    if any(p in protected for p in rel.parts):
        raise ValueError('RESULTS_DIR_OVERLAPS_PROTECTED_PATH')
    return destination


def load_benchmark(root: Path):
    tool = root / 'PII_V6676_Full_PDF_Hybrid_AB'
    src = tool / 'v6676_benchmark.py'
    if not src.is_file():
        raise RuntimeError('V6676_BENCHMARK_MISSING')
    spec = importlib.util.spec_from_file_location('v6681_v6676', src)
    if spec is None or spec.loader is None:
        raise RuntimeError('V6676_IMPORT_FAILED')
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m, tool


def require_fixture(root, benchmark, tool):
    source = root / 'PII_Face_PDF_7page_Test' / 'pii_face_7page_scan_source.pdf'
    gate, gate_dir, expected, app_python, gpu_python = benchmark.require_setup(root, source, tool)
    return (source, gate, gate_dir, expected, app_python, gpu_python)


def allowlisted_trial(trial):
    # Keep private string and filesystem paths out of reports.
    keys = ('mode', 'return_code', 'safe', 'reason', 'child_status', 'child_stage',
            'error_type', 'preflight_pairs', 'preflight_equal_pairs',
            'processing_s', 'run_and_audit_s', 'preflight_s', 'child_elapsed_s',
            'worker_ocr_calls', 'worker_ocr_wall_s', 'text_regions_pass',
            'face_map_boxes_pass', 'pdf_structure_pass')
    out = {k: trial[k] for k in keys if k in trial}
    if trial.get('mode') == 'hybrid':
        ev = trial.get('provider_evidence') or {}
        sess, nodes = ev.get('sessions') or {}, ev.get('node_events') or {}
        out['provider_evidence'] = {
            'sessions': {k: int(sess.get(k, 0)) for k in ('det', 'cls', 'rec')},
            'node_events': {k: int(nodes.get(k, 0)) for k in (
                'det_cuda', 'det_cpu', 'cls_cpu', 'cls_cuda', 'rec_cpu', 'rec_cuda')},
            'profiles_read': int(ev.get('profiles_read', 0)),
        }
    return out


def execute_single(benchmark, root, tool, setup, mode, timeout):
    if mode not in ('cpu', 'hybrid'):
        raise ValueError('INVALID_MODE')
    source, gate, gate_dir, expected, app_python, gpu_python = setup
    with tempfile.TemporaryDirectory(prefix='pii_v6681_') as tmp:
        trial = benchmark.run_trial(root, tool, gate, gate_dir, expected,
                                    app_python, gpu_python, source, mode,
                                    Path(tmp) / mode, timeout)
        return allowlisted_trial(trial)


def heartbeat_statistics(samples, mode, wall_s):
    """Lag is excess beyond Tk.after(100), not time spent doing Python work.

    Samples are (relative_sec, stage, lag_ms). Stage is coarse *observation*,
    never proof of which process caused an OS/Tk scheduler spike.
    """
    lags = [max(0., float(s[2])) for s in samples]
    top = sorted((s for s in samples if s[2] >= 100), key=lambda s: s[2], reverse=True)[:12]
    counts = {str(th): sum(v > th for v in lags) for th in (100, 250, 500, 1000)}
    return {
        'scheduled_interval_ms': SAMPLE_MS,
        'sample_count': len(lags),
        'max_lag_ms': round(max(lags), 3) if lags else None,
        'p50_lag_ms': quantile(lags, .50),
        'p95_lag_ms': quantile(lags, .95),
        'p99_lag_ms': quantile(lags, .99),
        'lag_count_above_ms': counts,
        'largest_spikes': [
            {'relative_s': round(float(t), 3),
             'observed_stage': stage if stage in ALLOWED_STAGES else 'READY',
             'lag_ms': round(float(lag), 3)}
            for t, stage, lag in top
        ],
        'observed_seconds': round(float(wall_s), 3),
        'responsiveness_review': ('GUI_SCHEDULING_REVIEW' if counts['500'] > 0
                                  else 'NO_OVER_500MS_SPIKE_OBSERVED'),
    }


def compile_report(mode, trial, click_duration_s, samples, run_index):
    if mode not in ALLOWED_MODES:
        raise ValueError('INVALID_MODE')
    if mode == 'idle':
        status = 'IDLE_GUI_MEASURED'
    elif trial and trial.get('safe') is True:
        status = 'SINGLE_MODE_SYNTHETIC_SAFETY_PASS'
    else:
        status = 'SINGLE_MODE_EXECUTION_OR_SAFETY_FAILED'
    return {
        'schema': 'pii_v6681_gui_lag_single_mode_v1',
        'status': status,
        'mode': mode,
        'run_index': run_index,
        'synthetic_only': True,
        'not_production_gui': True,
        'production_files_changed': False,
        'fixed_fixture_required': True,
        'dual_zoom_kept': mode != 'idle',
        'pdf_security_gate_executed': bool(trial and trial.get('safe')),
        'trial': trial if mode != 'idle' else None,
        'gui': {'click_to_finish_s': round(float(click_duration_s), 3),
                **heartbeat_statistics(samples, mode, click_duration_s)},
        'pdf_pixel_equality_checked_in_this_run': False,
        'warning': ('Single-mode isolated Tk test; NOT project ui.py. '
                    'Single-mode PDF structural/text/face safety gate uses V6676. '
                    'No CPU-vs-hybrid PDF pixel comparison in this single run. '
                    'Stage association does not prove cause of Tk scheduling lag. '
                    'Do not use real documents, model files or private PII.'),
    }


def write_report(directory: Path, summary):
    directory.mkdir(parents=True, exist_ok=True)
    idx = summary['run_index']
    json_path = directory / f'v6681_run_{idx:02d}.json'
    md_path = directory / f'v6681_run_{idx:02d}.md'
    if json_path.exists() or md_path.exists():
        raise FileExistsError('RESULT_ALREADY_EXISTS_DO_NOT_OVERWRITE')
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    g, trial = summary['gui'], summary.get('trial') or {}
    lines = [
        '# V6.6.8.1 隔離 GUI 單模式心跳診斷', '',
        f"- 狀態：**{summary['status']}**",
        f"- 執行模式：**{summary['mode']}**",
        '- 只執行固定 SHA-256 合成 PDF；不使用正式 GUI、不修改正式程式。',
        '- 單模式不執行 CPU／GPU 輸出像素比較；不可將本報告視為正式 GUI 安全驗收。',
        '', '| 指標 | 觀測值 |', '|---|---:|',
        f"| 點擊到隔離 GUI 完成回呼 | {g['click_to_finish_s']} s |",
        f"| PDF 純處理 | {trial.get('processing_s','—')} s |",
        f"| PDF 子程序總耗時 | {trial.get('child_elapsed_s','—')} s |",
        f"| 固定測試 PDF 安全 Gate | {trial.get('safe','—')} |",
        f"| Tk 最大額外排程延遲 | {g['max_lag_ms']} ms |",
        f"| Tk p95 / p99 額外延遲 | {g['p95_lag_ms']} / {g['p99_lag_ms']} ms |",
        f"| Tk 大於 500 ms 次數 | {g['lag_count_above_ms']['500']} |",
        f"| Tk 排程狀態 | {g['responsiveness_review']} |",
        '', '事件時間與階段只是觀測到延遲時最近的階段標籤，不足以判定卡頓根因。',
        '不儲存原始 OCR、PII、影像、PDF、框座標、原始 profile 或 map。',
    ]
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return json_path, md_path


def next_run_index(results_dir):
    for i in range(1, 1000):
        if not (results_dir / f'v6681_run_{i:02d}.json').exists() and not (
                results_dir / f'v6681_run_{i:02d}.md').exists():
            return i
    raise RuntimeError('TOO_MANY_RUNS')


def run_gui(root, bm, tool, setup, results, timeout, idle_s):
    import tkinter as tk
    from tkinter import messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title('V6.6.8.1｜隔離 GUI 心跳診斷（僅合成 PDF）')
            self.geometry('740x460')
            self.queue = queue.Queue()
            self.busy = False
            self.finished_mode = None
            self.running_mode = None
            self.phase = 'READY'
            self.start = 0.0
            self.due = 0.0
            self.samples = []
            self.run_index = None
            self.info = tk.StringVar(value='選擇單一模式；正式 ui.py 不會被修改。')
            tk.Label(self, text='V6.6.8.1｜單模式 GUI 延遲診斷',
                     font=('Segoe UI', 16, 'bold')).pack(pady=(16, 8))
            tk.Label(self, textvariable=self.info, wraplength=690).pack(pady=8)
            frame = tk.Frame(self)
            frame.pack(pady=8)
            self.buttons = []
            for mode, caption in [('hybrid','只測混合 GPU PDF'),
                                  ('cpu','只測 CPU PDF'),
                                  ('idle',f'無負載觀察 {idle_s} 秒')]:
                b = tk.Button(frame, text=caption, width=20,
                              command=lambda m=mode: self.start_run(m))
                b.pack(side='left', padx=4)
                self.buttons.append(b)
            self.logbox = tk.Text(self, height=14, state='disabled', wrap='word')
            self.logbox.pack(padx=15, pady=10, fill='both', expand=True)
            self.protocol('WM_DELETE_WINDOW', self.close)
            self.after(SAMPLE_MS, self.pulse)

        def log(self, msg):
            self.logbox.configure(state='normal')
            self.logbox.insert('end', msg + '\n')
            self.logbox.see('end')
            self.logbox.configure(state='disabled')

        def close(self):
            if self.busy:
                messagebox.showwarning('執行中', '測試正在執行，請在完成後正常關閉。')
                return
            self.destroy()

        def start_run(self, mode):
            if self.busy:
                return
            assert mode in ALLOWED_MODES
            self.busy = True
            self.running_mode = mode
            self.run_index = next_run_index(results)
            self.samples = []
            self.start = time.perf_counter()
            self.due = self.start + SAMPLE_MS / 1000
            self.phase = 'GUI_IDLE' if mode == 'idle' else ('GPU_FULL_PDF' if mode == 'hybrid' else 'CPU_FULL_PDF')
            for b in self.buttons:
                b.configure(state='disabled')
            self.info.set('測試進行中；只記錄時間、階段和安全驗收統計，不儲存文件內容。')
            self.log('開始：' + mode + '，結果會存到 v6681_run_XX。')
            if mode == 'idle':
                self.after(int(idle_s * 1000), lambda: self.queue.put({'kind':'done','trial':None}))
            else:
                def job():
                    try:
                        trial = execute_single(bm, root, tool, setup, mode, timeout)
                        self.queue.put({'kind':'done', 'trial':trial})
                    except BaseException as exc:
                        e = type(exc).__name__
                        safe = e if e in {'RuntimeError','ValueError','TypeError','OSError',
                                          'KeyError','AssertionError','TimeoutError'} else 'OTHER_EXCEPTION'
                        self.queue.put({'kind':'error','error_type':safe})
                threading.Thread(target=job, name='synthetic_pdf_trial', daemon=True).start()

        def pulse(self):
            now = time.perf_counter()
            if self.busy:
                self.samples.append((max(0., now - self.start), self.phase,
                                     max(0., (now - self.due) * 1000)))
            self.due = now + SAMPLE_MS / 1000
            try:
                while True:
                    msg = self.queue.get_nowait()
                    if msg['kind'] in {'done','error'} and self.busy:
                        self.phase = 'RESULT_RENDER'
                        trial = msg.get('trial')
                        if msg['kind'] == 'error':
                            trial = {'mode':self.running_mode, 'safe':False,
                                     'reason':'RUNNER_EXCEPTION', 'error_type':msg['error_type']}
                        # This approximates GUI completion callback scheduling, not the
                        # exact moment of the underlying worker's child exit.
                        elapsed = time.perf_counter() - self.start
                        summary = compile_report(self.running_mode, trial, elapsed,
                                                 self.samples, self.run_index)
                        try:
                            write_report(results, summary)
                        except (OSError, ValueError, FileExistsError):
                            self.log('安全報告寫入失敗；請勿視為通過。')
                        else:
                            self.log('結果：' + summary['status'])
                            self.log('點擊→完成回呼：' + str(summary['gui']['click_to_finish_s']) + ' s')
                            self.log('最大 Tk 排程延遲：' + str(summary['gui']['max_lag_ms']) + ' ms')
                            self.log('>500 ms 次數：' + str(summary['gui']['lag_count_above_ms']['500']))
                        self.busy = False
                        self.phase = 'READY'
                        for b in self.buttons:
                            b.configure(state='normal')
                        self.info.set('完成；你可以進行下一個單模式測試。請上傳 v6681_run_XX.json/.md。')
            except queue.Empty:
                pass
            self.after(SAMPLE_MS, self.pulse)

    App().mainloop()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--check-only', action='store_true')
    p.add_argument('--results-dir', default='v6681_gui_results')
    p.add_argument('--timeout', type=int, default=1800)
    p.add_argument('--idle-seconds', type=int, default=30)
    args = p.parse_args(argv)
    if not (60 <= args.timeout <= 7200 and 10 <= args.idle_seconds <= 300):
        raise ValueError('INVALID_TIMEOUT_OR_IDLE_SECONDS')
    kit = Path(__file__).resolve().parent
    root = kit.parent
    if kit.name != KIT:
        raise RuntimeError('KIT_LOCATION_INCORRECT')
    results = validate_destination(root, root / args.results_dir)
    bm, tool = load_benchmark(root)
    setup = require_fixture(root, bm, tool)
    if args.check_only:
        print('CHECK_OK: fixture SHA256, V5 safety gate, V6676 and 2 isolated Windows Python environments')
        print('NOTE: no model was run; not an acceptance test for formal ui.py')
        return 0
    run_gui(root, bm, tool, setup, results, args.timeout, args.idle_seconds)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
