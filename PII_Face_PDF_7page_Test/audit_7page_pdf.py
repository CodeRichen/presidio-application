#!/usr/bin/env python3
# coding: utf-8
"""Audit visual text coverage, face-map boxes, and structure of a redacted PDF.
No OCR is performed here. This is a best-effort regression audit, not a security proof.
"""
import argparse
import json
from pathlib import Path
import fitz
from PIL import Image
import numpy as np


def render_to_size(page, width, height):
    pix = page.get_pixmap(matrix=fitz.Matrix(width/page.rect.width, height/page.rect.height),
                          colorspace=fitz.csRGB, alpha=False, annots=False)
    img = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
    if img.size != (width, height):
        img = img.resize((width, height), Image.Resampling.BILINEAR)
    return np.asarray(img)


def bbox_crop(a, coords):
    h, w = a.shape[:2]
    x0,y0,x1,y1 = coords
    x0,y0 = max(0,int(x0)),max(0,int(y0))
    x1,y1 = min(w,int(x1)),min(h,int(y1))
    return a[y0:y1,x0:x1]


def is_black(pix, threshold=50):
    return (pix[:,:,0]<=threshold) & (pix[:,:,1]<=threshold) & (pix[:,:,2]<=threshold)


def main(source, output, expected, report, map_file=None):
    source, output, expected, report = map(Path, (source, output, expected, report))
    if not source.exists(): raise FileNotFoundError(f'Source PDF not found: {source}')
    if not output.exists(): raise FileNotFoundError(f'Redacted PDF not found: {output}')
    fixture = json.loads(expected.read_text(encoding='utf-8'))
    samples = fixture['samples']
    mappings = None
    if map_file:
        mf = Path(map_file)
        if mf.exists():
            mappings = json.loads(mf.read_text(encoding='utf-8')).get(output.name, [])
            if not mappings:
                print(f'WARNING: no entries for output {output.name} in mapping file; only visual text + PDF checks available')
        else:
            print('WARNING: map-file not found; FACE-map validation will be omitted')
    result = {'source_file':source.name,'output_file':output.name,'scenarios':[],
              'note':'Face expected counts are visual estimates; face-map boxes alone cannot prove missed faces are absent.'}
    with fitz.open(source) as src, fitz.open(output) as dst:
        result['structure']={
            'source_pages':len(src),'output_pages':len(dst),
            'source_attachments':src.embfile_count(),'output_attachments':dst.embfile_count(),
            'output_text_chars':sum(len(p.get_text()) for p in dst),
            'output_annotations':sum(len(list(p.annots() or [])) for p in dst),
            'output_eof_markers':output.read_bytes().count(b'%%EOF'),
        }
        if len(src)!=len(dst) or len(dst)!=len(samples):
            result['status']='FAIL'
            result['error']='Page count differs from expected 7'
            report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            raise ValueError('Incorrect page count')
        all_text = []
        all_faces = []
        for i, sample in enumerate(samples):
            w,h=sample['size_px']
            srcimg=render_to_size(src[i],w,h)
            outimg=render_to_size(dst[i],w,h)
            text=[]
            for e in sample['expected_text']:
                region=bbox_crop(outimg,e['bbox_px'])
                coverage=float(is_black(region).mean()) if region.size else 0.0
                text.append({'label':e['label'],'coverage':round(coverage,5),'pass_98pct':coverage>=0.98})
                all_text.append(coverage>=0.98)
            # Approximate extra blackened pixels in the right-hand synthetic data panel.
            # Does not identify the *cause* (padding may be counted).
            black_before=is_black(srcimg); black_after=is_black(outimg)
            changed=(~black_before) & black_after
            panel_start=int(w*0.59)
            target_mask=np.zeros((h,w),dtype=bool)
            for e in sample['expected_text']:
                x0,y0,x1,y1=e['bbox_px']
                target_mask[max(y0,0):min(y1,h),max(x0,0):min(x1,w)]=True
            extra=int((changed[:,panel_start:] & ~target_mask[:,panel_start:]).sum())
            face_entries=[]
            face_coverages=[]
            if mappings is not None:
                face_entries=[e for e in mappings if e.get('label')=='FACE' and e.get('page')==i]
                for e in face_entries:
                    # PDF-space rect -> reference raster pixel coordinates
                    bb=e['bbox']; p=dst[i]
                    box=[int(round(bb[0]*w/p.rect.width)),int(round(bb[1]*h/p.rect.height)),
                         int(round(bb[2]*w/p.rect.width)),int(round(bb[3]*h/p.rect.height))]
                    crop=bbox_crop(outimg,box)
                    face_coverages.append(float(is_black(crop).mean()) if crop.size else 0.0)
                    all_faces.append(face_coverages[-1]>=0.98)
            row={'page':i+1,'scenario':sample['scenario'],'expected_text_count':len(text),
                 'text':text,'text_pass_98pct':sum(t['pass_98pct'] for t in text),
                 'additional_black_pixels_panel':extra,
                 'expected_faces_visual_estimate':sample['expected_faces_manual_estimate'],
                 'detected_faces_from_map':len(face_entries) if mappings is not None else None,
                 'face_map_boxes_black_coverage':[round(x,5) for x in face_coverages] if mappings is not None else None}
            result['scenarios'].append(row)
            cdesc=(f'{len(face_entries)} FACE entries' if mappings is not None else 'FACE map not checked')
            print(f"Page {i+1}: text {row['text_pass_98pct']}/{len(text)} >=98%, {cdesc}; extra black panel pixels={extra}")
        result['summary']={'text_regions':len(all_text),'text_regions_pass_98pct':sum(all_text),
                           'face_map_regions_checked':len(all_faces),'face_map_regions_pass_98pct':sum(all_faces),
                           'face_counts_needing_review':[r['page'] for r in result['scenarios'] if r['detected_faces_from_map'] is None or r['detected_faces_from_map'] < r['expected_faces_visual_estimate']]}

        structural_ok=(result['structure']['output_attachments']==0 and
                       result['structure']['output_text_chars']==0 and
                       result['structure']['output_annotations']==0 and
                       result['structure']['output_eof_markers']==1)
        # Face count is intentionally *not* an automatic PASS gate, since estimates are not verified ground truth.
        result['automated_structure_and_text_checks']='PASS' if structural_ok and all(all_text) else 'FAIL'
        result['status']='REVIEW' if (not structural_ok or not all(all_text) or not all(all_faces)
             or mappings is None or result['summary']['face_counts_needing_review']) else 'PASS_AUTOMATED'
        # A PASS_AUTOMATED result still requires human review of missed/occluded faces.

    report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print('STATUS:',result['status'])
    print('SUMMARY:',result['summary'])
    print('STRUCTURE:',result['structure'])
    print('REPORT:',report)
    print('SECURITY NOTE: PASS only indicates this suite passed; visually review every face, including small/occluded faces.')
    return result

if __name__ == '__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--source', default='pii_face_7page_scan_source.pdf')
    ap.add_argument('--output', required=True)
    ap.add_argument('--expected',default='expected_text_and_faces.json')
    ap.add_argument('--report',default='pdf_face_7page_audit.json')
    ap.add_argument('--map-file',default=None,help='Local PRIVATE restore map file; do not upload this file')
    a=ap.parse_args()
    main(a.source,a.output,a.expected,a.report,a.map_file)
