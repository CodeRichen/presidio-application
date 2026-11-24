#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate deliberately INSECURE overlay PDF vs proper redaction using known synthetic target boxes."""
import argparse
import json
from pathlib import Path
try:
    import pymupdf as fitz
except ImportError:
    import fitz


def main():
    ap=argparse.ArgumentParser()
    base=Path(__file__).resolve().parent
    ap.add_argument('--source',type=Path,default=base/'fixtures/synthetic_scanned_pii_3pages.pdf')
    ap.add_argument('--ground-truth',type=Path,default=base/'fixtures/synthetic_scanned_pii_ground_truth.json')
    ap.add_argument('--output-dir',type=Path,default=base/'results/examples')
    args=ap.parse_args()
    args.output_dir.mkdir(parents=True,exist_ok=True)
    gt=json.loads(args.ground_truth.read_text(encoding='utf-8'))
    bad=args.output_dir/'BAD_visual_overlay_only.pdf'
    good=args.output_dir/'GOOD_known_boxes_redaction.pdf'

    with fitz.open(args.source) as doc:
        for i,p in enumerate(doc):
            for e in gt['pages'][i]['expected']:
                rect=fitz.Rect(e['bbox_pt'])
                p.draw_rect(rect,color=(0,0,0),fill=(0,0,0),overlay=True)
        doc.save(bad)
    with fitz.open(args.source) as doc:
        for i,p in enumerate(doc):
            for e in gt['pages'][i]['expected']:
                rect=fitz.Rect(e['bbox_pt'])
                p.add_redact_annot(rect,fill=(0,0,0))
            p.apply_redactions(images=2,graphics=1,text=0)
        doc.save(good,garbage=4,deflate=True)
    print('INSECURE visual overlay:',bad)
    print('Known-box redaction  :',good)
    print('使用 audit_pdf_security.py 分別檢查；GOOD 只代表此合成案例的目標區域。')

if __name__=='__main__':main()
