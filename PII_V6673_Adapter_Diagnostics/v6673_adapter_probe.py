#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.7.3: RAM-only OCR adapter mismatch characterization.

Approved synthetic fixture first page only, both 2x and 3x. Never persist OCR
text, text hashes, boxes, images, raw tracebacks, or private recovery maps.
The worker routing is intentionally left identical to V6672; this is diagnostic
only and does not relax the existing exact-match PDF safety gate.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

PINNED_SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
EXPECTED_KEYS={'text','left','top','width','height','line_id'}


def mismatch_kind(cpu, gpu):
    if not isinstance(cpu,list) or not isinstance(gpu,list):
        return 'INVALID_ADAPTER_TYPE'
    if len(cpu)!=len(gpu):
        return 'WORD_COUNT_DIFFERENCE'
    for a,b in zip(cpu,gpu):
        if not isinstance(a,dict) or not isinstance(b,dict):
            return 'INVALID_WORD_TYPE'
        if set(a)!=EXPECTED_KEYS or set(b)!=EXPECTED_KEYS:
            return 'WORD_SCHEMA_DIFFERENCE'
        if a['text']!=b['text']:
            return 'OCR_TEXT_DIFFERENCE'
        if a['line_id']!=b['line_id']:
            return 'LINE_ID_DIFFERENCE'
        if any(a[k]!=b[k] for k in ('left','top','width','height')):
            return 'BOX_COORDINATE_DIFFERENCE'
    return 'EQUAL' if cpu else 'EMPTY_OCR_OUTPUT'


def _safe_words(words):
    return isinstance(words,list) and all(
        isinstance(w,dict) and
        set(w)==EXPECTED_KEYS and
        isinstance(w['text'],str) and
        isinstance(w['line_id'],int) and not isinstance(w['line_id'],bool) and
        all(isinstance(w[k],(int,float)) and not isinstance(w[k],bool)
            for k in ('left','top','width','height'))
        for w in words
    )


def compact(text):
    """In-memory only; no text or derived hashes are returned to final report."""
    return ''.join(text.split())


def word_statistics(words):
    """Only aggregate numeric counts are returned; raw text is discarded."""
    line_ids = {w['line_id'] for w in words}
    return {
        'word_count': len(words),
        'line_id_groups':len(line_ids),
        'nonempty_word_count':sum(bool(w['text'].strip()) for w in words),
        'text_codepoints_total':sum(len(w['text']) for w in words),
        'text_codepoints_without_whitespace':sum(len(compact(w['text'])) for w in words),
        'words_with_internal_whitespace':sum(any(c.isspace() for c in w['text'].strip()) for w in words),
    }


def compare_without_text(cpu,gpu):
    """Return count and boolean comparison *only*, never content/hashes/boxes.

    Whitespace-compacted equality is a debugging signal, NEVER an acceptable
    substitute for exact adapter equality or the full PDF security gate.
    """
    kind=mismatch_kind(cpu,gpu)
    info={'mismatch_kind':kind,'cpu_schema_ok':_safe_words(cpu),
          'hybrid_schema_ok':_safe_words(gpu)}
    if not info['cpu_schema_ok'] or not info['hybrid_schema_ok']:
        info['hypothesis']='INVALID_OCR_ADAPTER_DATA'
        return info
    info['cpu']=word_statistics(cpu)
    info['hybrid']=word_statistics(gpu)
    a=''.join(compact(w['text']) for w in cpu)
    b=''.join(compact(w['text']) for w in gpu)
    info['ordered_text_equal_after_whitespace_removal']=(a==b)
    # No raw OCR text, OCR hash, diff sample, coordinates, per-word lengths,
    # geometry or token IDs are saved.
    info['exact_adapter_equal']=(kind=='EQUAL')
    if kind=='EQUAL':
        info['hypothesis']='NO_DIFFERENCE_IN_THIS_PAIR'
    elif kind=='WORD_COUNT_DIFFERENCE' and a==b:
        info['hypothesis']='POSSIBLE_SEGMENTATION_OR_LINE_GROUPING_DIFFERENCE'
    elif kind=='WORD_COUNT_DIFFERENCE':
        info['hypothesis']='WORD_COUNT_AND_TEXT_STREAM_DIFFER; CHECK_BACKEND_CONFIG_OR_FILTERING'
    elif a==b:
        info['hypothesis']='TEXT_STREAM_MATCHES_BUT_ADAPTER_FORMAT_DIFFERS'
    else:
        info['hypothesis']='TEXT_STREAM_DIFFERS_OR_REORDERED'
    return info


def provider_gate(nodes):
    n=nodes.get('node_events') or {}
    s=nodes.get('sessions') or {}
    return (s=={'det':1,'cls':1,'rec':1} and nodes.get('profiles_read')==3 and
            all(n.get(k,0)>0 for k in ('det_cuda','cls_cpu','rec_cpu')) and
            all(n.get(k,0)==0 for k in ('cls_cuda','rec_cuda')))


def images(root, media):
    import fitz
    from PIL import Image
    pdf=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    with fitz.open(pdf) as doc:
        if len(doc)!=7: raise RuntimeError('UNAPPROVED_PAGE_COUNT')
        page=doc[0]
        for zoom in (2,3):
            pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),annots=False)
            if pix.n != 3: raise RuntimeError('UNAPPROVED_IMAGE_CHANNELS')
            image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            prepared,scale=media.preprocess_for_ocr(image)
            if float(scale)!=1.0:raise RuntimeError('UNEXPECTED_PREPROCESS_SCALE')
            yield zoom,prepared


def main(argv=None):
    ap=argparse.ArgumentParser()
    for n in ('project-root','gpu-python','tool-dir','out'):
        ap.add_argument('--'+n,required=True)
    a=ap.parse_args(argv)
    root=Path(a.project_root).resolve()
    tool=Path(a.tool_dir).resolve()
    out={'schema':'pii_v6673_adapter_probe_v1','status':'INCOMPLETE',
         'stage':'FIXTURE_CHECK','error_type':None,'pairs_checked':0,
         'pairs_equal':0,'pairs':[],'provider_route_verified':False}
    bridge=None
    try:
        pdf=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        sha=hashlib.sha256()
        with pdf.open('rb') as handle:
            for chunk in iter(lambda:handle.read(1024*1024),b''):
                sha.update(chunk)
        if sha.hexdigest()!=PINNED_SHA:
            out['status']='FIXTURE_NOT_APPROVED';return 2
        out['stage']='APP_IMPORT'
        sys.path[:0]=[str(root),str(tool)]
        import handler_media
        if Path(handler_media.__file__).resolve()!=root/'handler_media.py':
            out['status']='HANDLER_MODULE_MISMATCH';return 2
        from v667_bridge import HybridOCRBridge
        out['stage']='WORKER_START'
        bridge=HybridOCRBridge(Path(a.gpu_python).resolve(),tool/'v667_gpu_worker.py',root)
        for zoom,image in images(root,handler_media):
            out['stage']=f'CPU_OCR_{zoom}X'
            cpu=handler_media.ocr_words(image)
            out['stage']=f'HYBRID_OCR_{zoom}X'
            hybrid=bridge.ocr_words(image)
            out['stage']=f'COMPARE_{zoom}X'
            info=compare_without_text(cpu,hybrid)
            info['zoom']=zoom
            out['pairs'].append(info)
            out['pairs_checked']+=1
            out['pairs_equal']+=int(info['exact_adapter_equal'])
        out['stage']='WORKER_FINISH'
        counts=bridge.finish()
        nodes=counts.get('node_events') or {}
        out['provider_evidence']={
            'sessions':counts.get('sessions',{}),
            'profiles_read':counts.get('profiles_read',0),
            'det_cuda_node_events':nodes.get('det_cuda',0),
            'cls_cpu_node_events':nodes.get('cls_cpu',0),
            'rec_cpu_node_events':nodes.get('rec_cpu',0),
            'cls_cuda_node_events':nodes.get('cls_cuda',0),
            'rec_cuda_node_events':nodes.get('rec_cuda',0),
        }
        out['provider_route_verified']=provider_gate(counts)
        if not out['provider_route_verified']:
            out['status']='PROVIDER_ROUTE_NOT_VERIFIED'
        elif out['pairs_equal'] != 2:
            out['status']='OCR_ADAPTER_MISMATCH_CHARACTERIZED'
        else:
            out['status']='ADAPTER_PREFLIGHT_OK'
        out['stage']='COMPLETE'
        return 0 if out['status']=='ADAPTER_PREFLIGHT_OK' else 2
    except BaseException as exc:
        out['status']='ADAPTER_PROBE_EXCEPTION'
        out['error_type']=type(exc).__name__
        return 2
    finally:
        if bridge is not None:
            try:bridge.close()
            except BaseException:pass
        p=Path(a.out).resolve()
        p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':
    raise SystemExit(main())
