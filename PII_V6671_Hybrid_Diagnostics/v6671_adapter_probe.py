#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Synthetic fixture-only OCR adapter preflight, executed in the app's .venv.
Does not modify app code; records no OCR strings, coordinates or hashes.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

SHA = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def mismatch_kind(cpu,gpu):
    if not isinstance(cpu,list) or not isinstance(gpu,list): return 'INVALID_ADAPTER_TYPE'
    if len(cpu)!=len(gpu): return 'WORD_COUNT_DIFFERENCE'
    keys={'text','left','top','width','height','line_id'}
    for a,b in zip(cpu,gpu):
        if not isinstance(a,dict) or not isinstance(b,dict): return 'INVALID_WORD_TYPE'
        if set(a)!=keys or set(b)!=keys: return 'WORD_SCHEMA_DIFFERENCE'
        if a['text']!=b['text']: return 'OCR_TEXT_DIFFERENCE'
        if a['line_id']!=b['line_id']: return 'LINE_ID_DIFFERENCE'
        if any(a[k]!=b[k] for k in ('left','top','width','height')):
            return 'BOX_COORDINATE_DIFFERENCE'
    if not cpu: return 'EMPTY_OCR_OUTPUT'
    return 'EQUAL'

def main(argv=None):
    ap=argparse.ArgumentParser()
    for n in ('project-root','gpu-python','worker','v667-dir','out'):
        ap.add_argument('--'+n,required=True)
    a=ap.parse_args(argv)
    root=Path(a.project_root).resolve()
    info={'schema':'pii_v6671_adapter_probe_v1','status':'INCOMPLETE',
          'stage':'FIXTURE_CHECK', 'error_type':None,'pairs_checked':0,
          'pairs_equal':0,'mismatch_kind':None}
    bridge=None
    try:
        src=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not src.is_file() or hashlib.sha256(src.read_bytes()).hexdigest()!=SHA:
            info['status']='FIXTURE_NOT_APPROVED';return 2
        info['stage']='APP_IMPORT'
        sys.path.insert(0,str(root))
        sys.path.insert(0,str(Path(a.v667_dir).resolve()))
        import handler_media
        if Path(handler_media.__file__).resolve()!=root/'handler_media.py':
            info['status']='HANDLER_MODULE_MISMATCH';return 2
        from v667_child import preview_images
        from v667_bridge import HybridOCRBridge
        info['stage']='WORKER_START'
        bridge=HybridOCRBridge(Path(a.gpu_python).resolve(),Path(a.worker).resolve(),root)
        for zoom_index,img in enumerate(preview_images(src)):
            info['stage']=f'CPU_OCR_{zoom_index+1}'
            cpu_words=handler_media.ocr_words(img)
            info['stage']=f'GPU_OCR_{zoom_index+1}'
            gpu_words=bridge.ocr_words(img)
            info['pairs_checked']+=1
            info['stage']=f'COMPARE_{zoom_index+1}'
            comparison=mismatch_kind(cpu_words,gpu_words)
            if comparison!='EQUAL':
                info['status']='OCR_ADAPTER_MISMATCH'
                info['mismatch_kind']=comparison
                return 2
            info['pairs_equal']+=1
        info['stage']='WORKER_FINISH'
        nodes=bridge.finish()
        info['provider_evidence']={
            'sessions':nodes.get('sessions',{}),
            'det_cuda_node_events':nodes.get('node_events',{}).get('det_cuda',0),
            'cls_cpu_node_events':nodes.get('node_events',{}).get('cls_cpu',0),
            'rec_cpu_node_events':nodes.get('node_events',{}).get('rec_cpu',0),
            'profiles_read':nodes.get('profiles_read',0)}
        n=nodes.get('node_events',{})
        if not(nodes.get('profiles_read')==3 and
               nodes.get('sessions',{})=={'det':1,'cls':1,'rec':1} and
               all(n.get(k,0)>0 for k in ('det_cuda','cls_cpu','rec_cpu')) and
               all(n.get(k,0)==0 for k in ('cls_cuda','rec_cuda'))):
            info['status']='PROVIDER_ROUTE_NOT_VERIFIED';return 2
        info['status']='ADAPTER_PREFLIGHT_OK'
        info['stage']='COMPLETE'
        return 0
    except BaseException as e:
        info['status']='ADAPTER_PROBE_EXCEPTION'
        info['error_type']=type(e).__name__
        return 2
    finally:
        if bridge is not None:
            try:bridge.close()
            except BaseException:pass
        path=Path(a.out).resolve()
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(info,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':
    raise SystemExit(main())
