# -*- coding: utf-8 -*-
"""Synthetic-only OCR diagnostic. Child stderr/stdout MUST be discarded by parent."""
from __future__ import annotations
import argparse
import hashlib
import inspect
import json
import os
from pathlib import Path
import sys
import time
from v61_geometry import describe_added_boxes

SHA = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'


def file_sha(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1048576), b''):
            h.update(block)
    return h.hexdigest()


def perform(root, fixture, expected):
    # The exact synthetic profile used for the preceding V6 A/B test.
    os.environ.update({
        'PII_PDF_EXPORT_V5':'1','PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1','PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1',
    })
    sys.path.insert(0,str(root))
    import handler_media as hm
    import fitz
    src=inspect.getsource(hm._mask_pdf_scanned_page)
    required=('primary_zoom = zoom', 'fallback_zoom = 3.0',
              '_merge_pdf_multizoom_boxes(primary_boxes, fallback_boxes)')
    if not all(p in src for p in required):
        raise RuntimeError('current OCR logic incompatible with V6.1')
    details=json.loads(expected.read_text(encoding='utf-8'))
    if len(details.get('samples',[]))!=7:
        raise RuntimeError('unexpected fixture ground truth')
    with fitz.open(str(fixture)) as doc:
        if len(doc)!=7 or doc.needs_pass:
            raise RuntimeError('unexpected synthetic fixture document')
        page=doc[5]
        if page.rotation:
            raise RuntimeError('unsupported rotated page')
        @hm._pii_media_cache_scope
        def capture():
            t=time.perf_counter()
            primary, primary_img=hm._collect_pdf_ocr_boxes(page,2.0)
            primary_s=time.perf_counter()-t
            t=time.perf_counter()
            fallback, fallback_img=hm._collect_pdf_ocr_boxes(page,3.0)
            fallback_s=time.perf_counter()-t
            merged=hm._merge_pdf_multizoom_boxes(primary,fallback)
            added=merged[len(primary):]
            meta=describe_added_boxes(primary,added,details['samples'][5],page.rect.width,page.rect.height)
            # Drop all image and value-bearing objects on return. Only meta is serialized.
            del primary_img, fallback_img
            return {'primary_boxes':len(primary),'fallback_boxes':len(fallback),
                    'additional_3x_boxes':meta,'additional_count':len(meta),
                    'primary_ocr_s':round(primary_s,3),'fallback_ocr_s':round(fallback_s,3)}
        result=capture()
    result.update({
        'schema':'pii_v61_ocr_extra_v1',
        'status':'MANUAL_REVIEW_REQUIRED',
        'fixture_sha256_verified':True,
        'production_code_changed':False,
        'page':6,
        'expected_previous_v6_additions':1,
        'count_consistent_with_v6':result['additional_count']==1,
        'report_contains_original_ocr_values':False,
        'guidance':'Keep dual 2x+3x OCR until additional regions are reviewed and broader independent Ground Truth is available.',
    })
    return result


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',required=True)
    ap.add_argument('--fixture',required=True)
    ap.add_argument('--expected',required=True)
    ap.add_argument('--report',required=True)
    a=ap.parse_args()
    root=Path(a.root).resolve(); fixture=Path(a.fixture).resolve()
    expected=Path(a.expected).resolve(); report=Path(a.report).resolve()
    try:
        if file_sha(fixture)!=SHA:
            raise RuntimeError('unexpected fixture hash')
        data=perform(root,fixture,expected)
    except BaseException as exc:
        # Fail closed. Do not serialize exception text or traceback: those may contain PII.
        data={'schema':'pii_v61_ocr_extra_v1','status':'ERROR',
              'error_type':type(exc).__name__,'production_code_changed':False}
    report.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if data['status']=='MANUAL_REVIEW_REQUIRED' else 2

if __name__=='__main__':
    raise SystemExit(main())
