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


def result_to_words(result):
    """Provisional adapter; strict preflight must match real ocr_backend.py exactly."""
    import numpy as np
    texts = getattr(result, 'txts', None)
    boxes = getattr(result, 'boxes', None)
    if texts is None and boxes is None:
        return []
    if texts is None or boxes is None:
        raise RuntimeError('INVALID_RAPIDOCR_OUTPUT')
    if len(texts) != len(boxes):
        raise RuntimeError('RAPIDOCR_BOX_TEXT_COUNT_MISMATCH')
    words=[]
    for i, (text, coords) in enumerate(zip(texts, boxes)):
        text = str(text).strip()
        if not text:
            continue
        polygon=np.asarray(coords, dtype=float)
        if polygon.shape != (4,2) or not np.isfinite(polygon).all():
            raise RuntimeError('INVALID_OCR_BOUNDING_BOX')
        xs, ys = polygon[:,0], polygon[:,1]
        left, top, right, bottom=map(float,(xs.min(), ys.min(), xs.max(), ys.max()))
        words.append({'text':text,'left':left,'top':top,
                      'width':right-left,'height':bottom-top,'line_id':i})
    return words


def worker(protocol):
    import onnxruntime as ort
    if getattr(ort, 'preload_dlls', None):
        ort.preload_dlls(directory='')
    if ort.__version__ != '1.31.1' or 'CUDAExecutionProvider' not in ort.get_available_providers():
        raise RuntimeError('GPU_ENVIRONMENT_NOT_READY')
    with tempfile.TemporaryDirectory(prefix='pii_v667_worker_') as scratch:
        with Meter(ort, Path(scratch)) as meter:
            from rapidocr import RapidOCR
            engine = RapidOCR(params={
                'EngineConfig.onnxruntime.use_cuda': True,
                'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search':'DEFAULT',
            })
            if set(meter.sessions) != {'det','cls','rec'}:
                raise RuntimeError('INCOMPLETE_HYBRID_SESSIONS')
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
                    result=engine(img.convert('RGB'))
                answer = json.dumps({'status':'OK','words':result_to_words(result)},
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
