#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Runs only inside separate GPU venv, never reads project files/documents.
Only exception *types* and fixed stage/status tokens are persisted.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

STAGES = ('ORT_IMPORT', 'ORT_DLL_PRELOAD', 'ORT_PROVIDER', 'RAPIDOCR_IMPORT',
          'RAPIDOCR_CONSTRUCT', 'RAPIDOCR_SYNTHETIC_CALL', 'ORT_PROFILING')

def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--out', required=True)
    p.add_argument('--v667-dir', required=True)
    a = p.parse_args(argv)
    destination = Path(a.out).resolve()
    v667 = Path(a.v667_dir).resolve()
    info = {'schema':'pii_v6671_gpu_probe_v1', 'status':'INCOMPLETE',
            'stage':'ORT_IMPORT','error_type':None, 'provider_verified':False}
    # Never allow library prints to reach reports or terminal. The orchestrator
    # also directs all child stdout/stderr to DEVNULL.
    try:
        sys.path.insert(0, str(v667))
        from v667_gpu_worker import Meter
        import onnxruntime as ort
        info['stage'] = 'ORT_DLL_PRELOAD'
        if callable(getattr(ort,'preload_dlls',None)):
            ort.preload_dlls(directory='')
        info['stage'] = 'ORT_PROVIDER'
        if 'CUDAExecutionProvider' not in ort.get_available_providers():
            info['status'] = 'CUDA_PROVIDER_MISSING'
            return 2
        info['stage'] = 'RAPIDOCR_IMPORT'
        from rapidocr import RapidOCR
        info['stage'] = 'RAPIDOCR_CONSTRUCT'
        with tempfile.TemporaryDirectory(prefix='pii_v6671_gpu_probe_') as td:
            with Meter(ort, Path(td)) as meter:
                engine = RapidOCR(params={
                    'EngineConfig.onnxruntime.use_cuda': True,
                    'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'DEFAULT',
                })
                if set(meter.sessions) != {'det','cls','rec'}:
                    info['status'] = 'INCOMPLETE_MODEL_SESSIONS'
                    return 2
                info['stage'] = 'RAPIDOCR_SYNTHETIC_CALL'
                from PIL import Image, ImageDraw
                image = Image.new('RGB',(640,192),'white')
                ImageDraw.Draw(image).text((36,45),'SYNTHETIC ABC 12345',fill='black')
                engine(image)
                info['stage'] = 'ORT_PROFILING'
                evidence = meter.counts()
                info['sessions'] = evidence.get('sessions',{})
                info['det_cuda_node_events'] = evidence.get('node_events',{}).get('det_cuda',0)
                info['profiles_read'] = evidence.get('profiles_read',0)
                info['provider_verified'] = bool(info['det_cuda_node_events']>0 and
                                                 info['profiles_read']==3)
                info['status'] = ('GPU_PROBE_OK' if info['provider_verified']
                                  else 'GPU_PROVIDER_NOT_VERIFIED')
                return 0 if info['provider_verified'] else 2
    except BaseException as e:
        info['status']='GPU_PROBE_EXCEPTION'
        info['error_type']=type(e).__name__
        return 2
    finally:
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__ == '__main__':
    raise SystemExit(main())
