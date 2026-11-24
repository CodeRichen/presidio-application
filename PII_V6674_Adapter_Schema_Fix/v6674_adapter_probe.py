#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.7.4 -- safe OCR adapter schema diagnostic on SHA-pinned synthetic PDF.

This tool deliberately does not adapt, normalize, truncate, or omit OCR output.
Original OCR text and image bytes are used only in RAM for strict comparison.
Only aggregate *allowlisted* field-presence/type counters leave the process.
"""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

PINNED_SHA = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
FIELDS = ('text', 'left', 'top', 'width', 'height', 'line_id')
EXPECTED_KEYS = set(FIELDS)


def valid_type(field, value):
    if field == 'text':
        return isinstance(value, str)
    if field == 'line_id':
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def schema_summary(words):
    """Only privacy-safe aggregate counts; never preserve unknown field names.

    For missing fields and wrong field types, keys come exclusively from FIELDS.
    Values, unexpected key names, text lengths per item and coordinates NEVER
    leave memory or appear in the report.
    """
    out = {
        'word_count': len(words) if isinstance(words, list) else None,
        'dict_rows': 0, 'non_dict_rows': 0,
        'unexpected_field_rows': 0, 'unexpected_field_total': 0,
        'required_missing': {k: 0 for k in FIELDS},
        'required_wrong_type': {k: 0 for k in FIELDS},
        'text_string_rows': 0, 'line_id_valid_rows': 0,
        'line_id_groups': None, 'nonempty_word_count': None,
        'text_codepoints_total': None,
        'text_codepoints_without_whitespace': None,
        'words_with_internal_whitespace': None,
        'schema_ok': False,
    }
    if not isinstance(words, list):
        return out
    valid_line_ids = set()
    nonempty = 0
    char_count = 0
    nonspace_count = 0
    whitespace_words = 0
    all_text_valid = True
    all_lines_valid = True
    all_schema_valid = True
    for item in words:
        if not isinstance(item, dict):
            out['non_dict_rows'] += 1
            all_schema_valid = all_lines_valid = all_text_valid = False
            continue
        out['dict_rows'] += 1
        extra = len(item.keys() - EXPECTED_KEYS)
        out['unexpected_field_total'] += extra
        out['unexpected_field_rows'] += int(extra > 0)
        if extra:
            all_schema_valid = False
        for f in FIELDS:
            if f not in item:
                out['required_missing'][f] += 1
                all_schema_valid = False
                if f == 'text': all_text_valid = False
                if f == 'line_id': all_lines_valid = False
            elif not valid_type(f, item[f]):
                out['required_wrong_type'][f] += 1
                all_schema_valid = False
                if f == 'text': all_text_valid = False
                if f == 'line_id': all_lines_valid = False
        if isinstance(item.get('text'), str):
            out['text_string_rows'] += 1
            txt = item['text']
            nonempty += bool(txt.strip())
            char_count += len(txt)
            nonspace_count += sum(not c.isspace() for c in txt)
            whitespace_words += int(any(c.isspace() for c in txt.strip()))
        if valid_type('line_id', item.get('line_id')):
            out['line_id_valid_rows'] += 1
            valid_line_ids.add(item['line_id'])
    out['schema_ok'] = all_schema_valid
    if all_lines_valid:
        out['line_id_groups'] = len(valid_line_ids)
    if all_text_valid:
        out['nonempty_word_count'] = nonempty
        out['text_codepoints_total'] = char_count
        out['text_codepoints_without_whitespace'] = nonspace_count
        out['words_with_internal_whitespace'] = whitespace_words
    return out


def mismatch_kind(a, b):
    if not isinstance(a, list) or not isinstance(b, list):
        return 'INVALID_ADAPTER_TYPE'
    if len(a) != len(b):
        return 'WORD_COUNT_DIFFERENCE'
    if not a:
        return 'EMPTY_OCR_OUTPUT'
    for x, y in zip(a, b):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return 'INVALID_WORD_TYPE'
        if set(x) != EXPECTED_KEYS or set(y) != EXPECTED_KEYS:
            return 'WORD_SCHEMA_DIFFERENCE'
        if not valid_type('text', x.get('text')) or not valid_type('text', y.get('text')):
            return 'WORD_TYPE_DIFFERENCE'
        if x['text'] != y['text']:
            return 'OCR_TEXT_DIFFERENCE'
        if x['line_id'] != y['line_id']:
            return 'LINE_ID_DIFFERENCE'
        if any(x[k] != y[k] for k in ('left','top','width','height')):
            return 'BOX_COORDINATE_DIFFERENCE'
    return 'EQUAL'


def compare_without_text(cpu, hybrid):
    """Return aggregate statistics even if schema invalid; never raise KeyError.

    IMPORTANT: schema mismatch MUST NOT be treated as an OCR-equivalent pass.
    """
    left = schema_summary(cpu)
    right = schema_summary(hybrid)
    info = {
        'mismatch_kind': mismatch_kind(cpu, hybrid),
        'cpu_schema_ok': left['schema_ok'],
        'hybrid_schema_ok': right['schema_ok'],
        'cpu': {k:v for k,v in left.items() if k!='schema_ok'},
        'hybrid': {k:v for k,v in right.items() if k!='schema_ok'},
        'ordered_text_equal_after_whitespace_removal': None,
        'exact_adapter_equal': False,
        'hypothesis': 'UNKNOWN',
    }
    if not left['schema_ok'] or not right['schema_ok']:
        if not left['schema_ok'] and not right['schema_ok']:
            info['hypothesis'] = 'BOTH_ADAPTER_SCHEMAS_INVALID'
        elif not left['schema_ok']:
            info['hypothesis'] = 'CPU_ADAPTER_SCHEMA_INVALID'
        else:
            info['hypothesis'] = 'HYBRID_ADAPTER_SCHEMA_INVALID'
        return info
    # Only compare full OCR text in local RAM, and discard immediately.
    cpu_stream = ''.join(''.join(w['text'].split()) for w in cpu)
    hybrid_stream = ''.join(''.join(w['text'].split()) for w in hybrid)
    same_stream = cpu_stream == hybrid_stream
    info['ordered_text_equal_after_whitespace_removal'] = same_stream
    info['exact_adapter_equal'] = info['mismatch_kind'] == 'EQUAL'
    if info['exact_adapter_equal']:
        info['hypothesis'] = 'EXACT_ADAPTER_EQUAL'
    elif info['mismatch_kind'] == 'WORD_COUNT_DIFFERENCE' and same_stream:
        info['hypothesis'] = 'POSSIBLE_SEGMENTATION_DIFFERENCE_NOT_SAFE_TO_ADOPT'
    elif info['mismatch_kind'] == 'WORD_COUNT_DIFFERENCE':
        info['hypothesis'] = 'COUNT_AND_TEXT_STREAM_DIFFER'
    elif same_stream:
        info['hypothesis'] = 'TEXT_STREAM_SAME_BUT_STRUCTURE_DIFFERS'
    else:
        info['hypothesis'] = 'TEXT_STREAM_DIFFERS_OR_REORDERED'
    return info


def provider_gate(evidence):
    n = evidence.get('node_events') or {}
    s = evidence.get('sessions') or {}
    return (
        s == {'det':1, 'cls':1, 'rec':1}
        and evidence.get('profiles_read') == 3
        and all(n.get(k,0)>0 for k in ('det_cuda','cls_cpu','rec_cpu'))
        and all(n.get(k,0)==0 for k in ('cls_cuda','rec_cuda'))
    )


def images(root, media):
    import fitz
    from PIL import Image
    pdf = root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    with fitz.open(pdf) as doc:
        if len(doc)!=7: raise RuntimeError('UNAPPROVED_PAGE_COUNT')
        page=doc[0]
        for zoom in (2,3):
            pix=page.get_pixmap(matrix=fitz.Matrix(zoom,zoom),annots=False)
            if pix.n != 3: raise RuntimeError('UNAPPROVED_IMAGE_CHANNELS')
            img=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            processed,scale=media.preprocess_for_ocr(img)
            if float(scale)!=1.0: raise RuntimeError('UNEXPECTED_PREPROCESS_SCALE')
            yield zoom,processed


def main(argv=None):
    ap=argparse.ArgumentParser()
    for key in ('project-root','gpu-python','tool-dir','out'):
        ap.add_argument('--'+key,required=True)
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve()
    tool=Path(args.tool_dir).resolve()
    report={
        'schema':'pii_v6674_adapter_probe_v1',
        'status':'INCOMPLETE', 'stage':'FIXTURE_CHECK', 'error_type':None,
        'pairs_checked':0, 'pairs_equal':0, 'pairs':[],
        'provider_route_verified':False,
    }
    bridge=None
    try:
        pdf=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        h=hashlib.sha256()
        with pdf.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024),b''):
                h.update(chunk)
        if h.hexdigest()!=PINNED_SHA:
            report['status']='FIXTURE_NOT_APPROVED';return 2
        report['stage']='APP_IMPORT'
        sys.path[:0]=[str(root),str(tool)]
        import handler_media
        if Path(handler_media.__file__).resolve()!=root/'handler_media.py':
            report['status']='HANDLER_MODULE_MISMATCH';return 2
        from v667_bridge import HybridOCRBridge
        report['stage']='WORKER_START'
        bridge=HybridOCRBridge(Path(args.gpu_python).resolve(), tool/'v667_gpu_worker.py',root)
        for zoom,img in images(root,handler_media):
            report['stage']=f'CPU_OCR_{zoom}X'
            cpu=handler_media.ocr_words(img)
            report['stage']=f'HYBRID_OCR_{zoom}X'
            hybrid=bridge.ocr_words(img)
            report['stage']=f'COMPARE_{zoom}X'
            info=compare_without_text(cpu,hybrid)
            info['zoom']=zoom
            report['pairs'].append(info)
            report['pairs_checked']+=1
            report['pairs_equal']+=int(info['exact_adapter_equal'])
        report['stage']='WORKER_FINISH'
        evidence=bridge.finish()
        n=evidence.get('node_events') or {}
        report['provider_evidence']={
            'sessions':evidence.get('sessions') or {},
            'profiles_read':evidence.get('profiles_read',0),
            'det_cuda_node_events':n.get('det_cuda',0),
            'cls_cpu_node_events':n.get('cls_cpu',0),
            'rec_cpu_node_events':n.get('rec_cpu',0),
            'cls_cuda_node_events':n.get('cls_cuda',0),
            'rec_cuda_node_events':n.get('rec_cuda',0),
        }
        report['provider_route_verified']=provider_gate(evidence)
        if not report['provider_route_verified']:
            report['status']='PROVIDER_ROUTE_NOT_VERIFIED'
        elif any(not p['cpu_schema_ok'] or not p['hybrid_schema_ok'] for p in report['pairs']):
            report['status']='OCR_ADAPTER_SCHEMA_MISMATCH'
        elif report['pairs_equal']!=2:
            report['status']='OCR_ADAPTER_MISMATCH_CHARACTERIZED'
        else:
            report['status']='ADAPTER_PREFLIGHT_OK'
        report['stage']='COMPLETE'
        return 0 if report['status']=='ADAPTER_PREFLIGHT_OK' else 2
    except BaseException as exc:
        report['status']='ADAPTER_PROBE_EXCEPTION'
        report['error_type']=type(exc).__name__
        return 2
    finally:
        if bridge is not None:
            try: bridge.close()
            except BaseException: pass
        out=Path(args.out).resolve()
        out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


if __name__=='__main__':
    raise SystemExit(main())
