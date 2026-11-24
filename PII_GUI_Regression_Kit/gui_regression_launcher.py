"""Instrument the project's real Tkinter UI without changing its source code.

Run from presidio_app_allen with its virtualenv. This tool only records aggregate
performance metadata, not OCR text, file contents, or restore maps.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time

ROOT = Path(__file__).resolve().parent
if not (ROOT / 'ui.py').exists():
    # Allow scripts to live under PII_GUI_Regression_Kit/ (recommended layout).
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))


def now_iso():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def safe_file_info(name):
    p = Path(name)
    try:
        return {"name": p.name, "extension": p.suffix.lower(),
                "size_bytes": p.stat().st_size, "exists": p.is_file()}
    except OSError:
        return {"name": p.name, "extension": p.suffix.lower(),
                "size_bytes": None, "exists": False}


def atomic_json_write(path: Path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def install_monitor(App, *, results: Path, interval_ms: int,
                    simulate_error_once: bool = False):
    """Wrap only App's background file jobs. Does not affect classification/redaction."""
    import psutil
    original_init = App.__init__
    original_start = App._start
    original_set_busy = App._set_busy
    p = psutil.Process(os.getpid())
    simulated = {"remaining": 1 if simulate_error_once else 0}
    tick_s = interval_ms / 1000

    def heartbeat(self):
        record = getattr(self, '_regression_active', None)
        previous_due = getattr(self, '_regression_next_due', None)
        now = time.perf_counter()
        if record is not None and previous_due is not None and record.get('_ui_done_at') is None:
            lag_ms = max(0.0, (now - previous_due) * 1000)
            record['_lag_ms'].append(round(lag_ms, 2))
        self._regression_next_due = now + tick_s
        try:
            self.after(interval_ms, lambda: heartbeat(self))
        except Exception:
            pass  # UI shutting down

    def new_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._regression_active = None
        self._regression_next_due = time.perf_counter() + tick_s
        self.after(interval_ms, lambda: heartbeat(self))
        print('[GUI-REGRESSION] 效能監測已啟用；僅記錄時間、記憶體及檔名，不保存 OCR/PII 內容。')

    def record_finished(self, record):
        # Called on Tk main thread after original on_done may have populated outputs.
        if record.get('_saved'):
            return
        record['_saved'] = True
        stop = record['_sampler_stop']
        stop.set()
        if record.get('_ui_done_at') is None:
            record['_ui_done_at'] = time.perf_counter()
        t0 = record['_click_at']
        wstart, wend = record.get('_worker_start_at'), record.get('_worker_end_at')
        lags = record['_lag_ms']
        record_out = {
            'test_type': 'manual_real_gui_file_batch',
            'started_at': record['started_at'],
            'run_id': record['run_id'],
            'input_files': record['inputs'],
            'output_files': record.get('outputs', []),
            'input_count': len(record['inputs']),
            'output_count': len(record.get('outputs', [])),
            'status': 'ERROR' if record.get('error_type') else 'OK',
            'exception_type': record.get('error_type'),
            'simulated_error': record.get('simulated_error', False),
            'click_to_ui_s': round(record['_ui_done_at'] - t0, 4),
            'worker_s': round(wend - wstart, 4) if wstart and wend else None,
            'wait_and_ui_s': round((record['_ui_done_at'] - t0) - (wend - wstart), 4)
                if wstart and wend else None,
            'ui_heartbeat_interval_ms': interval_ms,
            'ui_lag_samples': len(lags),
            'ui_lag_max_ms': max(lags, default=0),
            'ui_lag_p95_ms': sorted(lags)[int((len(lags)-1)*.95)] if lags else 0,
            'ui_lag_over_500ms': sum(v > 500 for v in lags),
            'rss_start_mb': round(record['_rss_start']/1048576, 2),
            'rss_peak_mb': round(record['_rss_peak']/1048576, 2),
            'rss_peak_increase_mb': round(max(0, record['_rss_peak']-record['_rss_start'])/1048576, 2),
            'note': 'UI lag is scheduling delay (proxy, not direct click latency). No OCR/PII content stored.'
        }
        target = results / f"run_{record['run_id']}.json"
        try:
            atomic_json_write(target, record_out)
            print(f"[GUI-REGRESSION] {record_out['status']} | UI={record_out['click_to_ui_s']:.2f}s | "
                  f"Worker={record_out['worker_s']}s | lag_max={record_out['ui_lag_max_ms']:.1f}ms | "
                  f"RSS_peak={record_out['rss_peak_mb']}MB")
            print(f'[GUI-REGRESSION] 報告：{target}')
        except Exception as e:
            print(f'[GUI-REGRESSION] 無法儲存測試報告：{type(e).__name__}: {e}')
        finally:
            if getattr(self, '_regression_active', None) is record:
                self._regression_active = None

    def new_start(self, func, msg, on_done):
        # Existing UI file tab uses the literal '處理檔案中…' message.
        if '處理檔案' not in str(msg) or self.busy:
            return original_start(self, func, msg, on_done)
        begun = time.perf_counter()
        stamp = dt.datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        record = {
            'run_id': f'{stamp}_{os.getpid()}', 'started_at': now_iso(),
            '_click_at': begun, '_worker_start_at': None, '_worker_end_at': None,
            '_ui_done_at': None, '_lag_ms': [], '_saved': False,
            '_sampler_stop': threading.Event(),
            '_rss_start': p.memory_info().rss,
            '_rss_peak': p.memory_info().rss,
            'inputs': [safe_file_info(x) for x in getattr(self, 'files', [])],
            'outputs': [], 'error_type': None,
            'simulated_error': False,
        }
        self._regression_active = record

        def sample_rss():
            while not record['_sampler_stop'].wait(.20):
                try:
                    record['_rss_peak'] = max(record['_rss_peak'], p.memory_info().rss)
                except (psutil.Error, OSError):
                    break
        threading.Thread(target=sample_rss, daemon=True).start()

        def measured_func():
            record['_worker_start_at'] = time.perf_counter()
            try:
                if simulated['remaining']:
                    simulated['remaining'] = 0
                    record['simulated_error'] = True
                    raise RuntimeError('GUI 回歸測試：模擬錯誤（沒有處理任何檔案）')
                return func()
            except Exception as e:
                record['error_type'] = type(e).__name__
                raise
            finally:
                record['_worker_end_at'] = time.perf_counter()
                try:
                    record['_rss_peak'] = max(record['_rss_peak'], p.memory_info().rss)
                except (psutil.Error, OSError):
                    pass

        def measured_on_done(out):
            record['outputs'] = [safe_file_info(f) for f in (out or [])]
            return on_done(out)

        try:
            return original_start(self, measured_func, msg, measured_on_done)
        except Exception:
            record['_sampler_stop'].set()
            self._regression_active = None
            raise

    def new_set_busy(self, busy, msg=''):
        was = getattr(self, 'busy', False)
        r = original_set_busy(self, busy, msg)
        rec = getattr(self, '_regression_active', None)
        if was and not busy and rec is not None and not rec.get('_saved'):
            now = time.perf_counter()
            due = getattr(self, '_regression_next_due', None)
            if due is not None:
                rec['_lag_ms'].append(round(max(0, (now - due) * 1000), 2))
            rec['_ui_done_at'] = now
            # ui._poll() will next call the user's original on_done callback.
            self.after(100, lambda: record_finished(self, rec))
        return r

    App.__init__ = new_init
    App._start = new_start
    App._set_busy = new_set_busy


def main():
    parser = argparse.ArgumentParser(description='GUI 真實操作效能與穩定性監測')
    parser.add_argument('--results-dir', type=Path, default=ROOT / 'gui_regression_results')
    parser.add_argument('--heartbeat-ms', type=int, default=100)
    parser.add_argument('--synthetic-test', action='store_true',
                        help='僅測合成資料時啟用專用誤判過濾，不用於真實文件')
    parser.add_argument('--simulate-error-once', action='store_true',
                        help='第一次按開始處理時刻意丟出例外，不會讀取或寫入檔案；測 UI 錯誤恢復')
    args = parser.parse_args()
    if not 20 <= args.heartbeat_ms <= 1000:
        parser.error('--heartbeat-ms 必須介於 20 到 1000')
    if args.synthetic_test:
        os.environ['PII_MEDIA_FP_PROFILE'] = 'synthetic_face_test'
        os.environ['PII_MEDIA_FP_REPORT'] = '1'
    elif os.environ.get('PII_MEDIA_FP_PROFILE'):
        print('[注意] 目前 shell 已設定 PII_MEDIA_FP_PROFILE；一般文件測試前請清除它。', file=sys.stderr)
    args.results_dir.mkdir(parents=True, exist_ok=True)
    import ui
    install_monitor(ui.App, results=args.results_dir.resolve(),
                    interval_ms=args.heartbeat_ms,
                    simulate_error_once=args.simulate_error_once)
    ui.App().mainloop()


if __name__ == '__main__':
    main()
