#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU worker for pinned synthetic-only V667 run.
Isolated .venv_ocr_gpu; uses RAM-only anonymous byte pipes for OCR payload.
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile

from v667_bridge import read_frame, send_frame, MAX_REQ


def identify_model(model):
    if not isinstance(model, (str, Path)):
        raise RuntimeError('UNRECOGNIZED_OCR_MODEL')
    name = Path(model).name.lower()
    if not name.endswith('.onnx'):
        raise RuntimeError('UNSUPPORTED_OCR_MODEL')
    found = [x for x in ('det', 'cls', 'rec') if re.search(r'(^|[._-])' + x + r'([._-]|$)', name)]
    if len(found) != 1:
        raise RuntimeError('AMBIGUOUS_OCR_MODEL')
    return found[0]


def force_cpu(args, kwargs):
    args = list(args)
    kwargs = dict(kwargs)
    if args:
        args[0] = ['CPUExecutionProvider']
        if len(args) > 1:
            args[1] = None
        kwargs.pop('providers', None)
        kwargs.pop('provider_options', None)
    else:
        kwargs['providers'] = ['CPUExecutionProvider']
        kwargs.pop('provider_options', None)
    return tuple(args), kwargs


class Meter:
    def __init__(self, ort, scratch):
        self.ort, self.scratch = ort, scratch
        self.original = None
        self.sessions = {}

    def __enter__(self):
        self.original = self.ort.InferenceSession
        owner = self
        def intercept(model, sess_options=None, *args, **kwargs):
            kind = identify_model(model)
            if kind in owner.sessions:
                raise RuntimeError('MULTIPLE_SESSIONS_SAME_MODEL_KIND')
            opts = sess_options or owner.ort.SessionOptions()
            opts.enable_profiling = True
            opts.profile_file_prefix = str(owner.scratch / f'profile_{kind}')
            if kind in ('cls', 'rec'):
                args, kwargs = force_cpu(args, kwargs)
            sess = owner.original(model, opts, *args, **kwargs)
            providers = tuple(sess.get_providers())
            if kind == 'det' and 'CUDAExecutionProvider' not in providers:
                raise RuntimeError('DET_CUDA_SESSION_MISSING')
            if kind in ('cls', 'rec') and providers != ('CPUExecutionProvider',):
                raise RuntimeError('CLS_REC_NOT_CPU_ONLY')
            owner.sessions[kind] = sess
            return sess
        self.ort.InferenceSession = intercept
        return self

    def __exit__(self, et, ex, tb):
        self.ort.InferenceSession = self.original
        return False

    def counts(self):
        result = {'sessions': {k: int(k in self.sessions) for k in ('det', 'cls', 'rec')},
                  'node_events': {}, 'profiles_read': 0}
        for kind, sess in self.sessions.items():
            path = Path(sess.end_profiling())
            with path.open('r', encoding='utf-8') as f:
                raw = json.load(f)
            result['profiles_read'] += 1
            for event in raw:
                if event.get('cat') == 'Node':
                    provider = (event.get('args') or {}).get('provider')
                    ep = 'cuda' if provider == 'CUDAExecutionProvider' else ('cpu' if provider == 'CPUExecutionProvider' else None)
                    if ep:
                        key = f'{kind}_{ep}'
                        result['node_events'][key] = result['node_events'].get(key, 0) + 1
        return result


def app_adapter_words(result, width, height):
    """Invoke the project's actual RapidOCR output adapter; no approximated boxes.

    Does not call _rapid_engine(), which would create an unrelated CPU engine.
    """
    from ocr_backend import rapid_result_to_words
    return rapid_result_to_words(result, width, height)


def run_official_ocr(engine, rgb):
    """Apply the same OCR option and source adapter as formal ocr_backend.py."""
    result = engine(rgb, return_word_box=True)
    return app_adapter_words(result, rgb.width, rgb.height)


def worker(protocol):
    import onnxruntime as ort
    if getattr(ort, 'preload_dlls', None):
        ort.preload_dlls(directory='')
    if ort.__version__ != '1.31.1' or 'CUDAExecutionProvider' not in ort.get_available_providers():
        raise RuntimeError('GPU_ENVIRONMENT_NOT_READY')
    # The tested project root is the worker's cwd, set by HybridOCRBridge.
    # Import only that source; not a stale or separately installed adapter.
    root = Path.cwd().resolve()
    sys.path.insert(0, str(root))
    import ocr_backend
    if Path(ocr_backend.__file__).resolve() != (root / 'ocr_backend.py').resolve():
        raise RuntimeError('UNEXPECTED_PROJECT_OCR_BACKEND')
    if not hasattr(ocr_backend, 'rapid_result_to_words'):
        raise RuntimeError('OFFICIAL_RAPID_ADAPTER_MISSING')
    with tempfile.TemporaryDirectory(prefix='pii_v6675_worker_') as scratch:
        with Meter(ort, Path(scratch)) as meter:
            from rapidocr import RapidOCR
            thread_count = int(os.environ.get('PII_RAPID_CPU_THREADS', '4'))
            if thread_count not in (0,2,4,6,8):
                raise RuntimeError('INVALID_THREAD_CONFIGURATION')
            cfg = {
                'EngineConfig.onnxruntime.use_cuda': True,
                'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'DEFAULT',
            }
            if thread_count:
                cfg['EngineConfig.onnxruntime.intra_op_num_threads'] = thread_count
            engine = RapidOCR(params=cfg)
            # RapidOCR may initialize ONNX sessions lazily on the first OCR
            # call. READY only means the worker can accept the approved test
            # input; verified provider evidence is checked after OCR at DONE.
            send_frame(protocol,json.dumps({'status':'READY','mode':'hybrid'}).encode('utf-8'))
            from PIL import Image
            requests = 0
            while True:
                raw=read_frame(sys.stdin.buffer, MAX_REQ)
                if raw == b'!DONE':
                    nodes = meter.counts()
                    send_frame(protocol,json.dumps({'status':'SUMMARY','provider_evidence':nodes}).encode('utf-8'))
                    return
                if len(raw)<16 or not raw.startswith(b'\x89PNG\r\n\x1a\n'):
                    raise RuntimeError('INVALID_IMAGE_TRANSPORT')
                with Image.open(io.BytesIO(raw)) as img:
                    if img.format != 'PNG':
                        raise RuntimeError('NOT_PNG_INPUT')
                    rgb=img.convert('RGB')
                    words=run_official_ocr(engine,rgb)
                answer = json.dumps({'status':'OK','words':words},
                                    ensure_ascii=False, separators=(',',':')).encode('utf-8')
                send_frame(protocol,answer)
                requests += 1
                if requests > 50:
                    raise RuntimeError('TOO_MANY_OCR_REQUESTS')


def main():
    # Preserve the inherited stdout pipe only for binary protocol; redirect any
    # library print()/native stdout output to discarded stderr before imports.
    protocol = os.fdopen(os.dup(1), 'wb', buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    try:
        worker(protocol)
        return 0
    except BaseException:
        # No exceptions with private image/text arguments are ever printed.
        return 2
    finally:
        protocol.close()


if __name__ == '__main__':
    raise SystemExit(main())
