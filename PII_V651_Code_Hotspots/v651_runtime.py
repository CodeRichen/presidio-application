"""Ephemeral cProfile around handler_code.detect; never store text, args or hashes."""
from __future__ import annotations
import cProfile
from functools import wraps
from pathlib import Path
import pstats
import time


def safe_file_label(filename):
    """Remove all directories, including user names and drive prefixes."""
    if filename.startswith('~'):
        return '<builtin>'
    return filename.replace('\\', '/').rsplit('/', 1)[-1][:100]


def safe_func_label(name):
    """Compiled Python function names should not contain arguments/PII."""
    return ''.join(c if c.isascii() and (c.isalnum() or c in '._<>-') else '_' for c in name)[:120]


class CodeHotspots:
    def __init__(self, code_module, every=4, max_profiled_calls=80):
        if not isinstance(every, int) or every < 1 or not isinstance(max_profiled_calls, int) or max_profiled_calls < 1:
            raise ValueError('every/max_profiled_calls must be positive integers')
        self.module = code_module
        self.every = every
        self.max_profiled_calls = max_profiled_calls
        self.profiler = cProfile.Profile()
        self.calls = 0
        self.profiled_calls = 0
        self.elapsed_s = 0.0
        self.profiled_elapsed_s = 0.0
        self._original = None
        self._wrapped = None

    def install(self):
        if self._original is not None:
            raise RuntimeError('already installed')
        original = getattr(self.module, 'detect', None)
        if not callable(original):
            raise RuntimeError('handler_code.detect is not callable')
        self._original = original

        @wraps(original)
        def wrapped(*args, **kwargs):
            self.calls += 1
            sampled = (self.calls - 1) % self.every == 0 and self.profiled_calls < self.max_profiled_calls
            started = time.perf_counter()
            if sampled:
                self.profiler.enable()
            try:
                return original(*args, **kwargs)
            finally:
                if sampled:
                    self.profiler.disable()
                    self.profiled_calls += 1
                elapsed = time.perf_counter() - started
                self.elapsed_s += elapsed
                if sampled:
                    self.profiled_elapsed_s += elapsed

        self._wrapped = wrapped
        setattr(self.module, 'detect', wrapped)
        return self

    def restore(self):
        if self._original is not None:
            setattr(self.module, 'detect', self._original)
            self._original = None
        self._wrapped = None

    def safe_summary(self, top_n=25):
        # cProfile stats contain (file, line, function name), NOT call arguments.
        stats = pstats.Stats(self.profiler)
        rows = []
        for (file, line, name), (primitive, total, exclusive, cumulative, _callers) in stats.stats.items():
            rows.append({
                'module_file': safe_file_label(str(file)),
                'function': safe_func_label(str(name)),
                'line': int(line) if isinstance(line, int) and line >= 0 else 0,
                'primitive_calls': int(primitive),
                'total_calls': int(total),
                'self_s': round(float(exclusive), 5),
                'cumulative_s': round(float(cumulative), 5),
            })
        by_self = sorted(rows, key=lambda x: x['self_s'], reverse=True)[:top_n]
        by_cum = sorted(rows, key=lambda x: x['cumulative_s'], reverse=True)[:top_n]
        return {
            'schema': 'v651_code_hotspots_1',
            'function': 'handler_code.detect',
            'total_calls': self.calls,
            'profiled_calls': self.profiled_calls,
            'every_n_calls': self.every,
            'max_profiled_calls': self.max_profiled_calls,
            'all_calls_wall_s_instrumented': round(self.elapsed_s, 4),
            'sampled_calls_wall_s_instrumented': round(self.profiled_elapsed_s, 4),
            'top_self_time': by_self,
            'top_cumulative_time': by_cum,
            'note': ('Only sampled calls are attributed by cProfile. Times are measured with profiler overhead, '
                     'and cumulative times overlap. No text, arguments, OCR crops, or hashes are stored.'),
        }
