"""Runtime-only PII detection profiler. NO PII strings or hashes in persisted output.

Patch only already-imported handler_media functions in a disposable subprocess.
No production files are changed. Raw strings exist temporarily only in process RAM.
"""
from __future__ import annotations
from collections import Counter
from contextvars import ContextVar
from functools import wraps
import math
import statistics
import time

_PHASE = ContextVar('v65_phase', default='outside_zoom')


def _percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    return s[lo] * (hi - k) + s[hi] * (k - lo) if lo != hi else s[lo]


class Metric:
    def __init__(self):
        self.calls = 0
        self.samples = []
        self.elapsed_s = 0.0
        self.input_chars = 0
        self.length_buckets = Counter()
        self.phase_calls = Counter()
        self.repeated_input_calls = 0
        self._seen_inputs = set()  # Ephemeral sensitive strings; NEVER serialized.

    def add(self, text, secs, phase):
        self.calls += 1
        self.elapsed_s += secs
        self.samples.append(secs)
        if isinstance(text, str):
            n = len(text)
            self.input_chars += n
            bucket = '0' if not n else '1-16' if n <= 16 else '17-64' if n <= 64 else '65-256' if n <= 256 else '>256'
            self.length_buckets[bucket] += 1
            if text in self._seen_inputs:
                self.repeated_input_calls += 1
            else:
                self._seen_inputs.add(text)
        self.phase_calls[phase] += 1

    def safe_summary(self):
        return {
            'calls': self.calls,
            'inclusive_s': round(self.elapsed_s, 4),
            'mean_ms': round(self.elapsed_s / self.calls * 1000, 3) if self.calls else 0.0,
            'median_ms': round(statistics.median(self.samples) * 1000, 3) if self.samples else 0.0,
            'p95_ms': round(_percentile(self.samples, 95) * 1000, 3),
            'max_ms': round(max(self.samples) * 1000, 3) if self.samples else 0.0,
            'input_chars_total': self.input_chars,
            'repeated_input_calls': self.repeated_input_calls,
            'repeated_input_ratio_pct': round(100 * self.repeated_input_calls / self.calls, 2) if self.calls else 0.0,
            'length_buckets': dict(self.length_buckets),
            'phase_calls': dict(self.phase_calls),
        }

    def discard_sensitive(self):
        self._seen_inputs.clear()


class Profiler:
    def __init__(self, handler_media):
        self.media = handler_media
        self.code = handler_media.handler_code
        self.text_module = self.code.handler_text
        self.metric = {name: Metric() for name in ('media_detect_matches', 'code_detect', 'presidio_matches')}
        self.zoom_calls = Counter()
        self.cache_hits_at_call = 0
        self.cache_misses_at_call = 0
        self.cache_unknown_at_call = 0
        self._original = []

    def _replace(self, obj, name, replacement):
        original = getattr(obj, name)
        setattr(obj, name, replacement)
        self._original.append((obj, name, original))

    def _wrap_timed(self, original, metric_name, inspect_cache=False):
        @wraps(original)
        def wrapped(*args, **kwargs):
            if inspect_cache:
                cache_var = getattr(self.media, '_pii_media_cache_var', None)
                c = cache_var.get() if cache_var is not None else None
                arg = args[0] if args else kwargs.get('text')
                if isinstance(c, dict) and isinstance(arg, str) and not kwargs and len(args) == 1:
                    if arg in c:
                        self.cache_hits_at_call += 1
                    else:
                        self.cache_misses_at_call += 1
                else:
                    self.cache_unknown_at_call += 1
            t0 = time.perf_counter()
            try:
                return original(*args, **kwargs)
            finally:
                text = args[0] if args else kwargs.get('text')
                self.metric[metric_name].add(text, time.perf_counter() - t0, _PHASE.get())
        return wrapped

    def install(self):
        if self._original:
            raise RuntimeError('profiler already installed')
        for obj, name, metric_name, check_cache in [
            (self.media, 'detect_matches', 'media_detect_matches', True),
            (self.code, 'detect', 'code_detect', False),
            (self.text_module, 'presidio_matches', 'presidio_matches', False),
        ]:
            original = getattr(obj, name)
            if not callable(original):
                raise TypeError(f'{name} is not callable')
            self._replace(obj, name, self._wrap_timed(original, metric_name, check_cache))
        orig_collect = getattr(self.media, '_collect_pdf_ocr_boxes')
        @wraps(orig_collect)
        def collect(page, zoom, *args, **kwargs):
            z = float(zoom)
            phase = 'zoom2' if abs(z - 2.0) < 1e-7 else 'zoom3' if abs(z - 3.0) < 1e-7 else 'other_zoom'
            self.zoom_calls[phase] += 1
            token = _PHASE.set(phase)
            try:
                return orig_collect(page, zoom, *args, **kwargs)
            finally:
                _PHASE.reset(token)
        self._replace(self.media, '_collect_pdf_ocr_boxes', collect)
        return self

    def restore(self):
        for obj, name, original in reversed(self._original):
            setattr(obj, name, original)
        self._original.clear()

    def safe_summary(self):
        # No OCR texts, no hashes, no file paths, no images or bounding boxes.
        return {
            'schema': 'pii_v65_profile_v1',
            'functions': {k: m.safe_summary() for k, m in self.metric.items()},
            'zoom_calls': dict(self.zoom_calls),
            'media_cache_preexisting_hits': self.cache_hits_at_call,
            'media_cache_preexisting_misses': self.cache_misses_at_call,
            'media_cache_unknown_calls': self.cache_unknown_at_call,
            'note': 'Inclusive times overlap between functions and must NOT be summed. Repeated inputs counted in RAM only; no OCR text or hashes persisted.',
        }

    def discard_sensitive(self):
        for m in self.metric.values():
            m.discard_sensitive()
