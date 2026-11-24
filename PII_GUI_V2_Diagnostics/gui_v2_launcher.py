"""Offline, read-only GUI performance probes; no edits to ui.py or mask handlers.

Run from project root using virtual environment; instrument only file-tab batches.
No OCR strings, PII values, file names, file paths or restoration maps in diagnostics.
"""
from __future__ import annotations
import argparse
from collections import Counter, deque
import datetime as dt
import functools
import importlib
import json
import os
from pathlib import Path
import statistics
import sys
import threading
import time
import uuid

KIT = Path(__file__).resolve().parent
ROOT = KIT.parent


def iso_now():
    return dt.datetime.now().astimezone().isoformat(timespec='seconds')


def save_json(path, obj):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.part')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(tmp, path)


def _pctl(values, pct):
    if not values:
        return 0.0
    data = sorted(values)
    return data[min(len(data) - 1, int((len(data) - 1) * pct))]


class Probe:
    """Nested duration instrumentation with thread-scoped active phase.

    Inclusive durations of a parent INCLUDE nested stages. Exclusive durations
    deduct nested probes and should be used when attributing total time.
    """
    def __init__(self, max_events=35000):
        self.lock = threading.Lock()
        self.local = threading.local()
        self.max_events = max_events
        self.active = None
        self.stacks = {}
        self.events = []
        self.dropped = 0
        self._patches = []
        self.installed = []

    def start(self, record):
        with self.lock:
            self.events = []
            self.dropped = 0
            self.stacks = {}
            self.active = record

    def stop(self, record):
        with self.lock:
            if self.active is record:
                self.active = None
            out = list(self.events)
            dropped = self.dropped
        return out, dropped

    def top(self, thread_id=None):
        with self.lock:
            if thread_id is not None:
                stack = self.stacks.get(thread_id, [])
                return stack[-1] if stack else 'worker.uninstrumented'
            for stack in self.stacks.values():
                if stack:
                    return stack[-1]
            return 'worker.uninstrumented'

    def wrap(self, target, attr, label, *, get_extra=None):
        original = getattr(target, attr, None)
        if not callable(original):
            return False
        # Skip wrappers already created by this probe.
        if getattr(original, '__gui_v2_probe__', False):
            return False

        @functools.wraps(original)
        def wrapped(*args, **kwargs):
            with self.lock:
                rec = self.active
            if rec is None:
                return original(*args, **kwargs)
            start = time.perf_counter()
            tid = threading.get_ident()
            st = getattr(self.local, 'frames', None)
            if st is None:
                st = []
                self.local.frames = st
            frame = {'name': label, 'start': start, 'child_s': 0.0}
            st.append(frame)
            with self.lock:
                self.stacks[tid] = [x['name'] for x in st]
            failed = False
            try:
                return original(*args, **kwargs)
            except BaseException:
                failed = True
                raise
            finally:
                end = time.perf_counter()
                elapsed = max(0.0, end - start)
                st.pop()
                if st:
                    st[-1]['child_s'] += elapsed
                with self.lock:
                    self.stacks[tid] = [x['name'] for x in st]
                    if self.active is rec:
                        if len(self.events) < self.max_events:
                            extra = None
                            if get_extra:
                                try:
                                    extra = get_extra(args, kwargs)
                                except Exception:
                                    pass
                            e = {
                                'stage': label,
                                'at_s': round(start - rec['t0'], 4),
                                'duration_s': round(elapsed, 5),
                                'exclusive_s': round(max(0.0, elapsed - frame['child_s']), 5),
                                'error': failed,
                            }
                            if isinstance(extra, dict):
                                # get_extra must contain only safe numeric or categorical data.
                                e.update(extra)
                            self.events.append(e)
                        else:
                            self.dropped += 1
        wrapped.__gui_v2_probe__ = True
        setattr(target, attr, wrapped)
        self._patches.append((target, attr, original))
        self.installed.append(label)
        return True

    def restore(self):
        for target, attr, old in reversed(self._patches):
            setattr(target, attr, old)
        self._patches.clear()


def _page_info(args, kwargs):
    page = args[0] if args else kwargs.get('page')
    value = getattr(page, 'number', None)
    return {'page_index': int(value)} if isinstance(value, int) else {}


def _zoom_info(args, kwargs):
    info = _page_info(args, kwargs)
    zoom = args[1] if len(args) >= 2 else kwargs.get('zoom')
    if isinstance(zoom, (int, float)):
        info['zoom'] = round(float(zoom), 2)
    return info


def install_processing_probes(probe, core_module):
    """Patch only existing symbols in memory, preserving original args/return/raises."""
    probe.wrap(core_module, 'dispatch_files', 'batch.dispatch')
    media = getattr(core_module, 'handler_media', None)
    if media is None:
        try:
            media = importlib.import_module('handler_media')
        except ImportError:
            return

    specs = {
        'mask_file': ('media.file', None),
        'mask_pdf': ('pdf.mask', None),
        'mask_image': ('image.mask', None),
        '_mask_pdf_scanned_page': ('pdf.page.scanned', _page_info),
        '_mask_pdf_text_page': ('pdf.page.text', _page_info),
        '_collect_pdf_ocr_boxes': ('pdf.zoom_ocr', _zoom_info),
        '_merge_pdf_multizoom_boxes': ('pdf.zoom_merge', None),
        '_page_has_text': ('pdf.page_text_check', _page_info),
        'preprocess_for_ocr': ('ocr.preprocess', None),
        'ocr_words': ('ocr.recognize', None),
        'find_sensitive_boxes_in_words': ('pii.boxes', None),
        'detect_matches': ('pii.detect', None),
        'save_secure_raster_pdf': ('pdf.secure_export', None),
        '_save_map_entry': ('map.save', None),
        # Alias present in some versions of handler_media.
        'detect_faces': ('face.detect', None),
        'mask_faces': ('face.mask', None),
    }
    for name, (label, extra) in specs.items():
        probe.wrap(media, name, label, get_extra=extra)

    # Extra coverage for versions where face_detector is accessed dynamically.
    # Avoid double-wrapping the SAME direct import alias; resolve lazily at import.
    for module_name, attrs in (
        ('face_detector', {'detect_faces': 'face.model_detect', 'padded_face_box': 'face.padding'}),
        ('ocr_backend', {'ocr_words_with_backend': 'ocr.backend'}),
    ):
        try:
            mod = importlib.import_module(module_name)
        except ImportError:
            continue
        for name, label in attrs.items():
            probe.wrap(mod, name, label)

    # For versions where the direct import is used at the module level,
    # instrumenting that local alias captures the actual invocation.
    for name in ('handler_text', 'handler_code'):
        mod = getattr(core_module, name, None)
        if mod is not None:
            probe.wrap(mod, 'presidio_matches', 'pii.presidio')
            if name == 'handler_code':
                probe.wrap(mod, 'detect', 'pii.code_detect')


def install_gui_probes(probe, App):
    for name, label in (
        ('_poll', 'ui.poll'),
        ('_append_log_batch', 'ui.log_batch'),
        ('refresh_map_view', 'ui.refresh_map'),
        ('refresh_lists', 'ui.refresh_lists'),
    ):
        probe.wrap(App, name, label)


def summarize_events(events):
    by = {}
    for e in events:
        item = by.setdefault(e['stage'], {'calls': 0, 'inclusive_s': 0.0,
                                         'exclusive_s': 0.0, 'max_call_s': 0.0, 'errors': 0})
        item['calls'] += 1
        item['inclusive_s'] += e['duration_s']
        item['exclusive_s'] += e['exclusive_s']
        item['max_call_s'] = max(item['max_call_s'], e['duration_s'])
        item['errors'] += int(e['error'])
    for item in by.values():
        for key in ('inclusive_s', 'exclusive_s', 'max_call_s'):
            item[key] = round(item[key], 3)
    return dict(sorted(by.items(), key=lambda kv: kv[1]['exclusive_s'], reverse=True))


def gap_summary(due, now, samples, top_count=5):
    subset = [(t, phase) for t, phase, _rss in samples if due <= t <= now]
    counts = Counter(phase for _t, phase in subset)
    max_gap = max((b[0] - a[0] for a, b in zip(subset, subset[1:])), default=0.0)
    return {
        'phase_samples': dict(counts.most_common(top_count)),
        'sample_count': len(subset),
        'max_sample_gap_ms': round(max_gap * 1000, 2),
    }


def install_gui_monitor(App, probe, *, results_dir, heartbeat_ms=100,
                        sample_ms=50, slow_threshold_ms=500):
    """Instrument _start with result capture and nonblocking process sampling.

    No patch to Tk internals, and no access to underlying document contents.
    """
    try:
        import psutil
    except ImportError as exc:
        raise SystemExit('缺少 psutil：請在虛擬環境安裝 pip install psutil') from exc

    proc = psutil.Process(os.getpid())
    original_init = App.__init__
    original_start = App._start
    original_set_busy = App._set_busy

    def now_mb():
        try:
            return round(proc.memory_info().rss / (1024 * 1024), 2)
        except (psutil.Error, OSError):
            return None

    def heartbeat(self):
        rec = getattr(self, '_v2_current', None)
        now = time.perf_counter()
        due = getattr(self, '_v2_next_due', None)
        if rec is not None and due is not None and not rec['finished']:
            lag = max(0., (now - due) * 1000)
            rec['lag_values'].append(round(lag, 2))
            if lag >= slow_threshold_ms:
                with rec['sample_lock']:
                    samples = list(rec['samples'])
                detail = gap_summary(due, now, samples)
                rec['slow_gaps'].append({
                    'at_s': round(now - rec['t0'], 3),
                    'delay_ms': round(lag, 2),
                    **detail,
                })
        # 'after' may be delayed by Tk activity or native/Python contention.
        self._v2_next_due = now + heartbeat_ms / 1000
        try:
            self.after(heartbeat_ms, lambda: heartbeat(self))
        except Exception:
            pass

    def new_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._v2_current = None
        self._v2_next_due = time.perf_counter() + heartbeat_ms / 1000
        self.after(heartbeat_ms, lambda: heartbeat(self))
        print('[GUI-V2] 監測已啟動：只儲存階段名稱、耗時和記憶體，不保存敏感內容。')

    def finish(self, rec):
        if rec['finished']:
            return
        rec['finished'] = True
        rec['stop'].set()
        finished = time.perf_counter()
        events, dropped = probe.stop(rec)
        with rec['sample_lock']:
            snapshots = list(rec['samples'])
        phase_summary = summarize_events(events)
        # Attribute slow gaps to overlapping *measured* spans as another clue.
        # Long overlapping spans do not establish they blocked Tk's main thread.
        for gap in rec['slow_gaps']:
            lo = gap['at_s'] - gap['delay_ms'] / 1000
            hi = gap['at_s']
            overlaps = {}
            for e in events:
                start = e['at_s']
                stop = start + e['duration_s']
                shared = max(0.0, min(stop, hi) - max(start, lo))
                if shared > 0.01:
                    overlaps[e['stage']] = max(overlaps.get(e['stage'], 0.0), shared)
            gap['overlapping_stages_s'] = {
                stage: round(span, 3)
                for stage, span in sorted(overlaps.items(), key=lambda x: x[1], reverse=True)[:8]
            }
        lags = rec['lag_values']
        result = {
            'schema_version': 'gui_v2_diagnostic_1',
            'timestamp': rec['started_at'],
            'run_id': rec['run_id'],
            'status': 'ERROR' if rec['error_type'] else 'OK',
            'exception_type': rec['error_type'],
            'input_extensions': dict(rec['inputs']),
            'output_count': rec['output_count'],
            'click_to_ui_s': round(finished - rec['t0'], 4),
            'worker_s': round(rec['worker_end'] - rec['worker_start'], 4)
                if rec['worker_end'] is not None else None,
            'ui_lag_p95_ms': round(_pctl(lags, 0.95), 2),
            'ui_lag_max_ms': round(max(lags, default=0), 2),
            'ui_lag_over_threshold': sum(x >= slow_threshold_ms for x in lags),
            'ui_lag_threshold_ms': slow_threshold_ms,
            'rss_start_mb': rec['rss_start_mb'],
            'rss_peak_mb': rec['rss_peak_mb'],
            'rss_end_mb': now_mb(),
            'phase_summary': phase_summary,
            'instrumented_stages': list(dict.fromkeys(probe.installed)),
            'events_truncated': dropped,
            'slow_ui_gaps': rec['slow_gaps'],
            'monitor_sampler_samples': len(snapshots),
            'monitor_sampler_max_gap_ms': round(max((b[0] - a[0]) * 1000 for a, b in zip(snapshots, snapshots[1:])), 2) if len(snapshots) > 1 else 0,
            'slow_gap_correlation_is_approximate': True,
            'note': 'A timer lag is not proof of GIL contention. Phase samples are approximate. No PII values, names, paths, page text or crops recorded.',
        }
        target = Path(results_dir) / f'v2_{rec["run_id"]}.json'
        try:
            save_json(target, result)
            print(f'[GUI-V2] {result["status"]} | worker={result["worker_s"]}s | '
                  f'lag_max={result["ui_lag_max_ms"]}ms | peak={result["rss_peak_mb"]}MB')
            print(f'[GUI-V2] 報告：{target}')
        except Exception as e:
            print(f'[GUI-V2] 報告輸出失敗：{type(e).__name__}')
        finally:
            if getattr(self, '_v2_current', None) is rec:
                self._v2_current = None

    def new_start(self, func, msg, on_done):
        if getattr(self, 'busy', False) or '處理檔案' not in str(msg):
            return original_start(self, func, msg, on_done)
        rec = {
            'run_id': f'{dt.datetime.now().strftime("%Y%m%d_%H%M%S")}_{os.getpid()}_{uuid.uuid4().hex[:6]}',
            'started_at': iso_now(),
            't0': time.perf_counter(),
            'worker_start': None,
            'worker_end': None,
            'worker_tid': None,
            'finished': False,
            'error_type': None,
            'stop': threading.Event(),
            'sample_lock': threading.Lock(),
            'samples': [],
            'lag_values': [],
            'slow_gaps': [],
            'rss_start_mb': now_mb(),
            'rss_peak_mb': now_mb(),
            'inputs': Counter(Path(p).suffix.lower() for p in getattr(self, 'files', [])),
            'output_count': 0,
        }
        self._v2_current = rec
        probe.start(rec)

        def background_sampler():
            while not rec['stop'].wait(sample_ms / 1000):
                now = time.perf_counter()
                phase = probe.top(thread_id=rec.get('worker_tid')) if rec.get('worker_tid') else 'worker.not_started'
                rss = now_mb()
                if rss is not None:
                    rec['rss_peak_mb'] = max(rec['rss_peak_mb'] or 0, rss)
                with rec['sample_lock']:
                    rec['samples'].append((now, phase, rss))
        threading.Thread(target=background_sampler, daemon=True).start()

        def measured_func():
            rec['worker_tid'] = threading.get_ident()
            rec['worker_start'] = time.perf_counter()
            try:
                return func()
            except BaseException as e:
                rec['error_type'] = type(e).__name__
                raise
            finally:
                rec['worker_end'] = time.perf_counter()

        def measured_done(outs):
            rec['output_count'] = len(outs or [])
            return on_done(outs)

        try:
            return original_start(self, measured_func, msg, measured_done)
        except BaseException:
            rec['stop'].set()
            probe.stop(rec)
            self._v2_current = None
            raise

    def new_set_busy(self, busy, msg=''):
        was_busy = getattr(self, 'busy', False)
        result = original_set_busy(self, busy, msg)
        rec = getattr(self, '_v2_current', None)
        if was_busy and not busy and rec is not None and not rec['finished']:
            # Let original on_done update outputs before saving report.
            self.after(50, lambda r=rec: finish(self, r))
        return result

    App.__init__ = new_init
    App._start = new_start
    App._set_busy = new_set_busy


def main(argv=None):
    p = argparse.ArgumentParser(description='UI V2 phase + lag correlation, offline')
    p.add_argument('--results-dir', default=str(ROOT / 'gui_v2_results'))
    p.add_argument('--heartbeat-ms', type=int, default=100)
    p.add_argument('--sample-ms', type=int, default=50)
    p.add_argument('--slow-ms', type=int, default=500)
    p.add_argument('--synthetic-test', action='store_true',
                   help='Only for known synthetic data; never use this policy on real PII docs')
    args = p.parse_args(argv)
    if not 25 <= args.heartbeat_ms <= 500:
        p.error('--heartbeat-ms expected 25..500')
    if not 20 <= args.sample_ms <= 500:
        p.error('--sample-ms expected 20..500')
    if not 100 <= args.slow_ms <= 10000:
        p.error('--slow-ms expected 100..10000')
    if args.synthetic_test:
        os.environ['PII_MEDIA_FP_PROFILE'] = 'synthetic_face_test'
        os.environ['PII_MEDIA_FP_REPORT'] = '1'
    elif os.environ.get('PII_MEDIA_FP_PROFILE'):
        print('[GUI-V2] 警告：shell 已設定 synthetic face-test profile，請勿對真實文件使用！', file=sys.stderr)
    sys.path.insert(0, str(ROOT))
    # Both import and model warmup happen before per-job timing, as in V1.
    import ui
    probe = Probe()
    install_processing_probes(probe, ui.core)
    install_gui_probes(probe, ui.App)
    install_gui_monitor(ui.App, probe,
                        results_dir=Path(args.results_dir).resolve(),
                        heartbeat_ms=args.heartbeat_ms,
                        sample_ms=args.sample_ms,
                        slow_threshold_ms=args.slow_ms)
    print('[GUI-V2] 已安裝診斷階段：', ', '.join(dict.fromkeys(probe.installed)), file=sys.stderr)
    ui.App().mainloop()


if __name__ == '__main__':
    main()
