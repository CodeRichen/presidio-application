#!/usr/bin/env python3
# coding: utf-8
"""Internal synthetic-fixture worker. Logs must NOT be persisted by caller."""
from __future__ import annotations
import argparse
import inspect
import json
import os
from pathlib import Path
import sys
import time


def install_observer(handler, mode: str) -> dict:
    """Wrap only existing per-page calls; never modify production module on disk."""
    info = {'mode': mode, 'primary_calls': 0, 'fallback_calls': 0,
            'fallback_skipped': 0, 'extra_boxes_per_page': [], 'merge_calls': 0}
    original_collect = handler._collect_pdf_ocr_boxes
    original_merge = handler._merge_pdf_multizoom_boxes

    if mode == 'zoom2_only':
        # Fail closed when current local project has changed the 2x/3x function.
        src = inspect.getsource(handler._mask_pdf_scanned_page)
        if 'fallback_zoom = 3.0' not in src or 'primary_zoom = zoom' not in src:
            raise RuntimeError('本機版本的多解析度 OCR 結構不同，拒絕跳過 3x')
        if src.count('fallback_img') != 1 or '_merge_pdf_multizoom_boxes(primary_boxes, fallback_boxes)' not in src:
            raise RuntimeError('fallback 影像或合併邏輯已改動，拒絕跳過 3x')

    def wrapped_collect(page, zoom, *args, **kwargs):
        if abs(float(zoom) - 3.0) < 1e-9:
            info['fallback_calls'] += 1
            if mode == 'zoom2_only':
                info['fallback_skipped'] += 1
                return [], None  # Verified current implementation never uses fallback_img.
        else:
            info['primary_calls'] += 1
        return original_collect(page, zoom, *args, **kwargs)

    def wrapped_merge(primary, fallback, *args, **kwargs):
        merged = original_merge(primary, fallback, *args, **kwargs)
        info['merge_calls'] += 1
        # Counts only: never export coordinates, labels, values, or image crops.
        info['extra_boxes_per_page'].append(max(0, len(merged) - len(primary)))
        return merged

    handler._collect_pdf_ocr_boxes = wrapped_collect
    handler._merge_pdf_multizoom_boxes = wrapped_merge
    return info


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--toolkit', required=True)
    parser.add_argument('--source', required=True)
    parser.add_argument('--expected', required=True)
    parser.add_argument('--outdir', required=True)
    parser.add_argument('--mode', choices=('dual_zoom','zoom2_only'), required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    toolkit = Path(args.toolkit).resolve()
    outdir = Path(args.outdir).resolve()
    os.environ.update({
        'PII_PDF_EXPORT_V5': '1', 'PII_PDF_EXPORT_V3': '0',
        'PII_OCR_BACKEND': 'rapid', 'PII_NER_BACKEND': 'ac',
        'PII_FACE_ENABLED': '1', 'PII_MEDIA_FP_PROFILE': 'synthetic_face_test',
        'PII_MEDIA_FP_REPORT': '0', 'PII_MEDIA_CACHE': '1', 'PII_ZH_SINGLE_PASS': '1',
    })
    sys.path.insert(0, str(toolkit))
    sys.path.insert(0, str(root))
    import handler_media
    stats = install_observer(handler_media, args.mode)
    from run_7page_pdf_test import run
    start = time.perf_counter()
    try:
        run(args.source, args.expected, outdir)
    finally:
        stats['wall_s'] = round(time.perf_counter() - start, 3)
        # This file contains counts and durations only, no PII values.
        (outdir / 'v6_stage_counts.json').write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
