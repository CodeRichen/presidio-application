#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Original app OCR adapter vs isolated hybrid worker on pinned synthetic page.

Runs only from V6672 orchestrator inside formal application's .venv;
never persists OCR text, boxes, images, hashes of OCR text or private maps.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

PINNED_SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
KEYS={'text','left','top','width','height','line_id'}


def mismatch_kind(cpu,gpu):
    if not isinstance(cpu,list) or not isinstance(gpu,list):return 'INVALID_ADAPTER_TYPE'
    if len(cpu)!=len(gpu):return 'WORD_COUNT_DIFFERENCE'
    for a,b in zip(cpu,gpu):
        if not isinstance(a,dict) or not isinstance(b,dict):return 'INVALID_WORD_TYPE'
        if set(a)!=KEYS or set(b)!=KEYS:return 'WORD_SCHEMA_DIFFERENCE'
        if a['text']!=b['text']:return 'OCR_TEXT_DIFFERENCE'
        if a['line_id']!=b['line_id']:return 'LINE_ID_DIFFERENCE'
        if any(a[k]!=b[k] for k in ('left','top','width','height')):
            return 'BOX_COORDINATE_DIFFERENCE'
    if not cpu:return 'EMPTY_OCR_OUTPUT'
    return 'EQUAL'


def provider_gate(nodes):
    n=nodes.get('node_events') or {}
    s=nodes.get('sessions') or {}
    return (s=={'det':1,'cls':1,'rec':1} and nodes.get('profiles_read')==3 and
            all(n.get(k,0)>0 for k in ('det_cuda','cls_cpu','rec_cpu')) and
            all(n.get(k,0)==0 for k in ('cls_cuda','rec_cuda')))


def preview_images(source,handler_media):
    import fitz
    from PIL import Image
    with fitz.open(source) as doc:
        if len(doc)!=7:raise RuntimeError('INVALID_APPROVED_PAGE_COUNT')
        page=doc[0]
        for zoom in (2.0,3.0):
            pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),annots=False)
            if pix.n !=3:raise RuntimeError('INVALID_APPROVED_PIXEL_CHANNELS')
            image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            prepared,scale=handler_media.preprocess_for_ocr(image)
            if float(scale)!=1.0:raise RuntimeError('UNEXPECTED_OCR_PREPROCESS_SCALE')
            yield prepared


def main(argv=None):
    p=argparse.ArgumentParser()
    for n in ('project-root','gpu-python','tool-dir','out'):
        p.add_argument('--'+n,required=True)
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    tool=Path(a.tool_dir).resolve()
    out={'schema':'pii_v6672_adapter_probe_v1','status':'INCOMPLETE','stage':'FIXTURE_CHECK',
         'error_type':None,'pairs_checked':0,'pairs_equal':0,'mismatch_kind':None}
    bridge=None
    try:
        source=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if (not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest()!=PINNED_SHA):
            out['status']='FIXTURE_NOT_APPROVED';return 2
        out['stage']='APP_IMPORT'
        sys.path.insert(0,str(root))
        sys.path.insert(0,str(tool))
        import handler_media
        if Path(handler_media.__file__).resolve()!=root/'handler_media.py':
            out['status']='HANDLER_MODULE_MISMATCH';return 2
        from v667_bridge import HybridOCRBridge
        out['stage']='WORKER_START'
        bridge=HybridOCRBridge(Path(a.gpu_python).resolve(),tool/'v667_gpu_worker.py',root)
        for i,image in enumerate(preview_images(source,handler_media),start=1):
            out['stage']=f'CPU_OCR_{i}'
            cpu=handler_media.ocr_words(image)
            out['stage']=f'GPU_OCR_{i}'
            gpu=bridge.ocr_words(image)
            out['pairs_checked']+=1
            out['stage']=f'COMPARE_{i}'
            kind=mismatch_kind(cpu,gpu)
            if kind!='EQUAL':
                out['status']='OCR_ADAPTER_MISMATCH'
                out['mismatch_kind']=kind
                return 2
            out['pairs_equal']+=1
        out['stage']='WORKER_FINISH'
        nodes=bridge.finish()
        n=nodes.get('node_events') or {}
        out['provider_evidence']={
            'sessions':nodes.get('sessions',{}),
            'det_cuda_node_events':n.get('det_cuda',0),
            'cls_cpu_node_events':n.get('cls_cpu',0),
            'rec_cpu_node_events':n.get('rec_cpu',0),
            'cls_cuda_node_events':n.get('cls_cuda',0),
            'rec_cuda_node_events':n.get('rec_cuda',0),
            'profiles_read':nodes.get('profiles_read',0)}
        if not provider_gate(nodes):
            out['status']='PROVIDER_ROUTE_NOT_VERIFIED'
            return 2
        out['stage']='COMPLETE'
        out['status']='ADAPTER_PREFLIGHT_OK'
        return 0
    except BaseException as exc:
        out['status']='ADAPTER_PROBE_EXCEPTION'
        out['error_type']=type(exc).__name__
        return 2
    finally:
        if bridge is not None:
            try:bridge.close()
            except BaseException:pass
        path=Path(a.out).resolve()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    raise SystemExit(main())
