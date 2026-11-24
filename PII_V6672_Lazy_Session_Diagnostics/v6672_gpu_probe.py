#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GPU-only synthetic probe: never reads app files, images, PII or PDFs.

V667.1 accidentally required sessions right after RapidOCR construction.
RapidOCR is permitted to construct lazily, so inspect sessions only after a
synthetic inference. Probe accepts only actual det CUDA node events; full route
must be verified independently by adapter probe on approved PDF.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import tempfile


def session_flags(meter):
    return {k: int(k in meter.sessions) for k in ('det','cls','rec')}


def probe(ort, scratch, make_image):
    from v667_gpu_worker import Meter
    out = {'schema':'pii_v6672_gpu_probe_v1', 'status':'INCOMPLETE',
           'stage':'RAPIDOCR_IMPORT', 'error_type':None,
           'sessions_after_construct':{}, 'sessions_after_synthetic':{},
           'det_cuda_node_events':0, 'profiles_read':0,
           'provider_verified':False}
    try:
      with Meter(ort, scratch) as meter:
        # Critical import order: RapidOCR can bind InferenceSession on import.
        # Interception MUST be installed before importing the library.
        from rapidocr import RapidOCR
        out['stage'] = 'RAPIDOCR_CONSTRUCT'
        engine = RapidOCR(params={
            'EngineConfig.onnxruntime.use_cuda':True,
            'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search':'DEFAULT',
        })
        out['sessions_after_construct'] = session_flags(meter)
        out['stage'] = 'SYNTHETIC_OCR'
        # OCR calls, rather than constructor, are what may cause model sessions
        # to be created. A few scales ensure the test actually invokes det.
        for width,height in ((800,256),(1024,384)):
            engine(make_image(width,height))
        out['sessions_after_synthetic'] = session_flags(meter)
        out['stage'] = 'ORT_PROFILING'
        counts = meter.counts()
        out['profiles_read'] = counts.get('profiles_read',0)
        out['det_cuda_node_events'] = counts.get('node_events',{}).get('det_cuda',0)
        out['provider_verified'] = bool(out['det_cuda_node_events']>0 and
                                        out['sessions_after_synthetic'].get('det')==1 and
                                        out['profiles_read']>=1)
        out['status'] = ('GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY' if out['provider_verified']
                         else 'GPU_DET_CUDA_NOT_VERIFIED')
        return out
    except BaseException as exc:
        out['status']='GPU_PROBE_EXCEPTION'
        out['error_type']=type(exc).__name__
        return out


def synthetic_image(width,height):
    from PIL import Image, ImageDraw, ImageFont
    img=Image.new('RGB',(width,height),'white')
    d=ImageDraw.Draw(img)
    font=ImageFont.load_default()
    for i, line in enumerate(('SYNTHETIC TEST ONLY 12345', 'EXAMPLE ABC DEF 98765',
                               'NO PRIVATE INFORMATION')):
        d.text((24,36+i*64),line,font=font,fill='black',stroke_width=1)
    return img


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--out',required=True)
    ap.add_argument('--tool-dir',required=True)
    a=ap.parse_args(argv)
    destination=Path(a.out).resolve()
    tool=Path(a.tool_dir).resolve()
    out={'schema':'pii_v6672_gpu_probe_v1','status':'INCOMPLETE',
         'stage':'ORT_IMPORT','error_type':None,'provider_verified':False}
    try:
        sys.path.insert(0,str(tool))
        import onnxruntime as ort
        out['stage']='ORT_DLL_PRELOAD'
        preload=getattr(ort,'preload_dlls',None)
        if callable(preload):
            preload(directory='')
        out['stage']='ORT_PROVIDER'
        if ort.__version__!='1.31.1' or 'CUDAExecutionProvider' not in ort.get_available_providers():
            out['status']='ORT_GPU_ENVIRONMENT_NOT_READY'
            return 2
        out['stage']='RAPIDOCR_IMPORT'
        with tempfile.TemporaryDirectory(prefix='pii_v6672_gpu_profile_') as scratch:
            out=probe(ort,Path(scratch),synthetic_image)
        return 0 if out['provider_verified'] else 2
    except BaseException as exc:
        out['status']='GPU_PROBE_EXCEPTION'
        out['error_type']=type(exc).__name__
        return 2
    finally:
        destination.parent.mkdir(parents=True,exist_ok=True)
        destination.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    raise SystemExit(main())
