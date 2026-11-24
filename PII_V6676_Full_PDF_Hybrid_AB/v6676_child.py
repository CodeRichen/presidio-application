#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Run existing V5 pinned synthetic PDF runner, possibly with a RAM-only GPU OCR bridge.
Executable ONLY from the V6676 benchmark orchestrator (CLI is not production use).
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from v667_bridge import HybridOCRBridge, strict_words_equal


def preview_images(source):
    import fitz
    from PIL import Image
    import handler_media
    with fitz.open(source) as pdf:
        if len(pdf) != 7:
            raise RuntimeError('UNEXPECTED_PDF_PAGE_COUNT')
        page=pdf[0]
        for zoom in (2.0, 3.0):
            pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),annots=False)
            if pix.n != 3:
                raise RuntimeError('UNEXPECTED_PIXMAP_CHANNELS')
            img=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            processed,scale=handler_media.preprocess_for_ocr(img)
            if float(scale)!=1.0:
                raise RuntimeError('UNEXPECTED_OCR_PREPROCESS_SCALE')
            yield processed


def run(root, source, runner, expected, output, mode, gpu_python, worker):
    sys.path.insert(0,str(root))
    sys.path.insert(0,str(runner.parent))
    # Import app modules only after synthetic-only environment is applied by parent.
    import handler_media
    import run_7page_pdf_test
    if Path(handler_media.__file__).resolve() != (root/'handler_media.py').resolve():
        raise RuntimeError('UNEXPECTED_HANDLER_MODULE')
    bridge=None
    result={'schema':'pii_v6676_child_v1', 'mode':mode,'status':'INCOMPLETE',
            'preflight_pairs':0,'preflight_equal_pairs':0,'worker_ocr_calls':0,
            'worker_ocr_wall_s':0,'provider_evidence':{},'preflight_s':None}
    original_ocr=handler_media.ocr_words
    result['stage']='BEGIN'
    try:
        if mode == 'hybrid':
            result['stage']='GPU_WORKER_START'
            bridge=HybridOCRBridge(gpu_python, worker, root)
            result['stage']='PREFLIGHT_2X_3X'
            started=time.perf_counter()
            for image in preview_images(source):
                cpu_words=original_ocr(image)
                hybrid_words=bridge.ocr_words(image)
                result['preflight_pairs']+=1
                if strict_words_equal(cpu_words,hybrid_words):
                    result['preflight_equal_pairs']+=1
                else:
                    from v6675_adapter_probe import mismatch_kind
                    result['status']='OCR_ADAPTER_MISMATCH'
                    result['preflight_mismatch_kind']=mismatch_kind(cpu_words, hybrid_words)
                    return result
            result['preflight_s']=round(time.perf_counter()-started,3)
            # Do not use the bridge until exact adapter agreement was checked.
            handler_media.ocr_words=bridge.ocr_words

        result['stage']='RUN_FULL_PDF'
        outdir=output/'pdf_result'
        outdir.mkdir(parents=True,exist_ok=False)
        started=time.perf_counter()
        run_7page_pdf_test.run(source,expected,outdir)
        result['run_and_audit_s']=round(time.perf_counter()-started,3)
        audit=json.loads((outdir/'pdf_face_7page_audit.json').read_text(encoding='utf-8'))
        result['processing_s']=float(audit['processing_seconds'])
        if bridge:
            result['stage']='WORKER_PROVIDER_PROFILE'
            result['worker_ocr_calls']=bridge.ocr_calls-result['preflight_pairs']
            result['worker_ocr_wall_s']=round(bridge.ocr_wall_s,3)
            result['provider_evidence']=bridge.finish()
        result['stage']='COMPLETE'
        result['status']='COMPLETE'
        return result
    except BaseException as exc:
        result['status']='CHILD_EXCEPTION'
        result['error_type']=type(exc).__name__ if type(exc).__name__ in {'RuntimeError','TypeError','ValueError','KeyError','OSError','TimeoutError','AssertionError','AttributeError','ImportError','ModuleNotFoundError','FileNotFoundError','PermissionError','IndexError'} else 'OTHER_EXCEPTION'
        return result
    finally:
        handler_media.ocr_words=original_ocr
        if bridge:
            bridge.close()


def main(argv=None):
    p=argparse.ArgumentParser()
    for name in ('project-root','source','runner','expected','outdir','mode','gpu-python','worker'):
        p.add_argument('--'+name,required=True)
    a=p.parse_args(argv)
    root, source, runner, expected, output, gpu_python, worker=[Path(v).resolve() for v in (
        a.project_root, a.source,a.runner,a.expected,a.outdir,a.gpu_python,a.worker)]
    mode=a.mode
    if mode not in ('cpu','hybrid') or not (root in source.parents and root in runner.parents):
        raise RuntimeError('UNAUTHORIZED_TEST_CONFIGURATION')
    approved = root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    if source != approved or not source.is_file():
        raise RuntimeError('ONLY_APPROVED_SYNTHETIC_FIXTURE_ALLOWED')
    h=hashlib.sha256()
    with source.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    if h.hexdigest()!='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49':
        raise RuntimeError('FIXTURE_SHA256_MISMATCH')
    output.mkdir(parents=True,exist_ok=False)
    try:
        stats=run(root,source,runner,expected,output,mode,gpu_python,worker)
        (output/'child_stats.json').write_text(json.dumps(stats,ensure_ascii=False,indent=2),encoding='utf-8')
        return 0 if stats['status']=='COMPLETE' else 4
    except BaseException:
        (output/'child_stats.json').write_text(json.dumps({
            'schema':'pii_v6676_child_v1','mode':mode,'status':'CHILD_FAILED'}),encoding='utf-8')
        return 3

if __name__=='__main__':
    raise SystemExit(main())
