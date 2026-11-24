"""Subprocess-only OCR ONNX Runtime provider A/B. No PII enters metrics.

Both CPU baseline and GPU candidate are instrumented with ORT profiling.
Profiles are created exclusively under TemporaryDirectory and deleted after
extracting provider-only aggregate counts.
"""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path
import sys


def _ocr_callsite():
    frame = sys._getframe(1)
    while frame is not None:
        p = frame.f_code.co_filename.replace('\\', '/').lower()
        if 'rapidocr' in p or p.endswith('/ocr_backend.py'):
            return True
        frame = frame.f_back
    return False


def _provider_counts(path):
    counts = Counter()
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
        if not isinstance(data, list):
            return {}
        for entry in data:
            if not isinstance(entry, dict):
                continue
            args = entry.get('args')
            if not isinstance(args, dict):
                continue
            provider = args.get('provider')
            if isinstance(provider, str) and provider.endswith('ExecutionProvider'):
                counts[provider] += 1
    except (OSError, ValueError, TypeError):
        return {}
    return dict(counts)


class ProviderTrial:
    def __init__(self, ort_module, mode, scratch):
        if mode not in ('cpu', 'cuda'):
            raise ValueError('mode must be cpu or cuda')
        self.ort = ort_module
        self.mode = mode
        self.scratch = Path(scratch)
        self.original = None
        self.sessions = []
        self.ocr_calls = 0
        self.backend_ocr_sessions = 0
        self.non_ocr_calls = 0

    def install(self):
        if self.original is not None:
            raise RuntimeError('ALREADY_INSTALLED')
        original = self.ort.InferenceSession
        self.original = original
        def patched(path_or_bytes, sess_options=None, *args, **kwargs):
            if not _ocr_callsite():
                self.non_ocr_calls += 1
                return original(path_or_bytes, sess_options, *args, **kwargs)
            self.ocr_calls += 1
            options = sess_options if sess_options is not None else self.ort.SessionOptions()
            # This is benchmarking-only profiling. Never write profiles to the project.
            options.enable_profiling = True
            options.profile_file_prefix = str(self.scratch / 'ocr')
            if self.mode == 'cuda':
                if 'CUDAExecutionProvider' not in self.ort.get_available_providers():
                    raise RuntimeError('CUDA_EP_UNAVAILABLE')
                forced_providers = ['CUDAExecutionProvider', 'CPUExecutionProvider']
            else:
                # Explicit CPU baseline even if ORT-GPU is installed in the venv.
                forced_providers = ['CPUExecutionProvider']
            if args:
                args = (forced_providers,) + args[1:]
                kwargs.pop('providers', None)
            else:
                kwargs['providers'] = forced_providers
            session = original(path_or_bytes, options, *args, **kwargs)
            providers = session.get_providers()
            self.sessions.append((session, tuple(providers)))
            self.backend_ocr_sessions += 1
            return session
        self.ort.InferenceSession = patched
        return self

    def finish(self):
        if self.original is not None:
            self.ort.InferenceSession = self.original
            self.original = None
        provider_events = Counter()
        session_providers = Counter()
        profiling_files = 0
        for session, providers in self.sessions:
            session_providers['CUDA_enabled' if 'CUDAExecutionProvider' in providers else 'CPU_only'] += 1
            try:
                path = session.end_profiling()
                if path:
                    profiling_files += 1
                    provider_events.update(_provider_counts(path))
            except Exception:
                pass
        return {
            'ocr_session_create_calls': self.ocr_calls,
            'ocr_sessions_created': self.backend_ocr_sessions,
            'other_ort_sessions_bypassed': self.non_ocr_calls,
            'cuda_enabled_sessions': session_providers['CUDA_enabled'],
            'cpu_only_sessions': session_providers['CPU_only'],
            'cuda_kernel_events': provider_events['CUDAExecutionProvider'],
            'cpu_kernel_events': provider_events['CPUExecutionProvider'],
            'profiles_parsed': profiling_files,
            'mode': self.mode,
        }
