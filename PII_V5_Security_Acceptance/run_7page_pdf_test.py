#!/usr/bin/env python3
# coding: utf-8
"""Run a local end-to-end mask_pdf test using the *existing* project modules.
Run from the user's project's root directory. Do not upload the private restore map.
"""
import argparse
import os
from pathlib import Path
import sys
import time
from audit_7page_pdf import main as audit


def run(source, expected, results_dir, allow_overwrite=False):
    source=Path(source).resolve()
    expected=Path(expected).resolve()
    results=Path(results_dir).resolve()
    results.mkdir(parents=True,exist_ok=True)
    output=results/'pii_face_7page_masked.pdf'
    private_map=results/'PRIVATE_restore_map_DO_NOT_SHARE.json'
    if not source.exists(): raise FileNotFoundError(source)
    if output.exists() and not allow_overwrite:
        raise FileExistsError(f'{output} already exists. Pass --overwrite if you intend to replace test output.')
    # The fixture-specific filter must be enabled for this synthetic test only.
    # Apply BEFORE importing handler_media.
    if os.environ.get('PII_FACE_ENABLED', '1').strip().lower() in {'0','false','off','no'}:
        raise RuntimeError('PII_FACE_ENABLED is disabled. Set PII_FACE_ENABLED=1 to test faces.')
    os.environ['PII_MEDIA_FP_PROFILE']='synthetic_face_test'
    os.environ.setdefault('PII_MEDIA_FP_REPORT','1')
    os.environ.setdefault('PII_OCR_BACKEND','rapid')
    os.environ.setdefault('PII_MEDIA_CACHE','1')
    os.environ.setdefault('PII_ZH_SINGLE_PASS','1')
    sys.path.insert(0,str(Path.cwd()))
    import handler_media
    print('HANDLER:',Path(handler_media.__file__).resolve())
    print('SYNTHETIC_FP_PROFILE:',os.environ['PII_MEDIA_FP_PROFILE'])
    start=time.perf_counter()
    entries=handler_media.mask_pdf(str(source),str(output),map_file=str(private_map))
    sec=time.perf_counter()-start
    print(f'PROCESSING_TIME_SECONDS: {sec:.3f}')
    if output.exists():
        result=audit(source,output,expected,results/'pdf_face_7page_audit.json', private_map)
        result['processing_seconds']=round(sec,3)
        (results/'pdf_face_7page_audit.json').write_text(__import__('json').dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('REDACTION_ENTRIES_TOTAL:',len(entries))
    print('PRIVATE MAP FILE (DO NOT UPLOAD):', private_map)
    print('TEST REPORT (safe to share; contains no unmasked pixel crops):', results/'pdf_face_7page_audit.json')

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--source',required=True)
    ap.add_argument('--expected',required=True)
    ap.add_argument('--results-dir',default='pdf_face_7page_results')
    ap.add_argument('--overwrite',action='store_true')
    a=ap.parse_args()
    run(a.source,a.expected,a.results_dir,a.overwrite)
