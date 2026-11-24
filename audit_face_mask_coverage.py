# -*- coding: utf-8 -*-
"""Independent face-redaction coverage audit for a rasterized, masked PDF.

Inputs: original PDF, independently labeled face_ground_truth.json (PDF points),
masked PDF. This intentionally does NOT use YuNet predictions, FACE map records,
or reversible crops to decide coverage.

Example:
python audit_face_mask_coverage.py --original original.pdf --gt face_ground_truth.json \
    --masked masked.pdf --output face_mask_coverage_20261123.json

Requires: PyMuPDF (import pymupdf or fitz), numpy.
No writes to source files. Refuses to overwrite the report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def validate_gt(gt: dict, original: Path, orig_doc, masked_doc) -> None:
    require(gt.get("schema") == "face_gt_pdf_points_v1", "GT schema 不符：需使用 face_gt_annotator.py 的 JSON")
    src = gt.get("source_pdf")
    require(isinstance(src, dict), "GT 缺少 source_pdf")
    require(src.get("sha256") == sha256(original), "原始 PDF SHA256 不符合 GT，拒絕混用不同檔案")
    records = gt.get("pages")
    require(isinstance(records, list) and len(records) == len(orig_doc), "GT 與原始 PDF 頁數不符")
    require(src.get("page_count") == len(orig_doc), "GT 記錄的原始 PDF 頁數不符")
    require(len(orig_doc) == len(masked_doc), "遮蔽 PDF 與原始 PDF 頁數不符")
    require(not orig_doc.needs_pass and not masked_doc.needs_pass, "不支援加密 PDF")
    for i, (rec, src_page, mask_page) in enumerate(zip(records, orig_doc, masked_doc), 1):
        require(rec.get("page") == i, f"第 {i} 頁 GT 編號不符")
        require(src_page.rotation == mask_page.rotation == 0, f"第 {i} 頁 PDF 旋轉角度不是 0")
        w, h = src_page.rect.width, src_page.rect.height
        require(abs(rec.get("width_pt", -999) - w) <= .05 and abs(rec.get("height_pt", -999) - h) <= .05,
                f"第 {i} 頁 GT 尺寸不符")
        require(abs(mask_page.rect.width - w) <= .05 and abs(mask_page.rect.height - h) <= .05,
                f"第 {i} 頁遮蔽 PDF 頁面尺寸不符，不能直接比較座標")
        faces = rec.get("faces")
        require(isinstance(faces, list), f"第 {i} 頁 GT faces 不是清單")
        for j, rect in enumerate(faces, 1):
            require(isinstance(rect, list) and len(rect) == 4 and
                    all(type(x) in (int, float) and math.isfinite(x) for x in rect) and
                    0 <= rect[0] < rect[2] <= w and 0 <= rect[1] < rect[3] <= h,
                    f"第 {i} 頁第 {j} 張人臉座標不合法")


def run(original: Path, gt_path: Path, masked: Path, *, output: Path | None,
        zoom: float, black_max: int, min_black_coverage: float) -> dict:
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    import numpy as np

    for p, title in ((original, '原始 PDF'), (gt_path, 'Ground Truth JSON'), (masked, '遮蔽 PDF')):
        require(p.is_file(), f"找不到{title}：{p}")
    require(original.resolve() != masked.resolve(), "原始 PDF 與遮蔽 PDF 不能是同一個檔案")
    require(0.5 <= zoom <= 4.0, "--zoom 須介於 0.5 到 4.0")
    require(0 <= black_max <= 255, "--black-max 須介於 0 到 255")
    require(0 < min_black_coverage <= 1, "--min-coverage 須介於 0 到 1")
    if output is not None:
        require(output.suffix.lower() == '.json', "輸出檔案必須是 .json")
        require(not output.exists(), "輸出 JSON 已存在，為避免覆寫請使用新檔名")
        require(output.resolve() not in (original.resolve(), gt_path.resolve(), masked.resolve()), "輸出不能覆蓋輸入檔案")

    gt = json.loads(gt_path.read_text(encoding='utf-8-sig'))
    rows = []
    page_records = []
    total = 0
    passed = 0
    with fitz.open(str(original)) as d1, fitz.open(str(masked)) as d2:
        validate_gt(gt, original, d1, d2)
        for page_no, (orig_page, mask_page, rec) in enumerate(zip(d1, d2, gt['pages']), 1):
            page_status = {'page': page_no, 'gt_faces': len(rec['faces']), 'passed': 0, 'faces': []}
            if not rec['faces']:
                page_records.append(page_status)
                continue
            # Render both files in the *same RGB coordinate space*.
            src_pix = orig_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB,
                                          alpha=False, annots=False)
            out_pix = mask_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB,
                                          alpha=False, annots=False)
            require(src_pix.width == out_pix.width and src_pix.height == out_pix.height,
                    f"第 {page_no} 頁兩份 PDF 的渲染尺寸不同，拒絕比較")
            src_arr = np.frombuffer(src_pix.samples, dtype=np.uint8).reshape(src_pix.height, src_pix.width, 3)
            mask_arr = np.frombuffer(out_pix.samples, dtype=np.uint8).reshape(out_pix.height, out_pix.width, 3)
            for face_no, bbox in enumerate(rec['faces'], 1):
                x0 = max(0, int(math.ceil(bbox[0]*zoom)))
                y0 = max(0, int(math.ceil(bbox[1]*zoom)))
                x1 = min(out_pix.width, int(math.floor(bbox[2]*zoom)))
                y1 = min(out_pix.height, int(math.floor(bbox[3]*zoom)))
                # Excluding subpixel perimeter avoids false failures from antialiasing.
                require(x1 > x0 and y1 > y0,
                        f"第 {page_no} 頁第 {face_no} 個 GT 框太小，無法在目前 zoom 檢測")
                s = src_arr[y0:y1, x0:x1]
                o = mask_arr[y0:y1, x0:x1]
                is_black = np.max(o, axis=2) <= black_max
                src_black = np.max(s, axis=2) <= black_max
                fraction = float(is_black.mean())
                # Original content was not already black: informative secondary comparison.
                originally_visible = ~src_black
                visible_count = int(originally_visible.sum())
                new_black_frac = (float(is_black[originally_visible].mean())
                                  if visible_count > 0 else None)
                ok = fraction >= min_black_coverage
                entry = {
                    'face_index': face_no,
                    'bbox_pt': [round(float(v), 3) for v in bbox],
                    'black_pixel_coverage': round(fraction, 6),
                    'new_black_coverage_of_original_nonblack_pixels':
                        round(new_black_frac, 6) if new_black_frac is not None else None,
                    'pixel_count': int(is_black.size),
                    'passed': ok
                }
                total += 1
                passed += int(ok)
                page_status['passed'] += int(ok)
                page_status['faces'].append(entry)
                rows.append((page_no, face_no, fraction, ok))
            page_records.append(page_status)
            print(f"第 {page_no} 頁｜GT {len(rec['faces'])} 張｜黑框覆蓋通過 {page_status['passed']} 張", flush=True)

    report = {
        'description': 'Independent manual-GT face-region black-pixel coverage audit',
        'original_pdf_filename': original.name,
        'original_pdf_sha256': sha256(original),
        'masked_pdf_filename': masked.name,
        'masked_pdf_sha256': sha256(masked),
        'ground_truth_filename': gt_path.name,
        'settings': {'render_zoom': zoom, 'black_pixel_threshold_rgb_max': black_max,
                     'minimum_black_coverage': min_black_coverage,
                     'coordinate_system': 'PDF points; top-left origin; [x0,y0,x1,y1]',
                     'edge_policy': 'pixel centers within annotated rectangle; outside-edge pixels ignored'},
        'totals': {'ground_truth_faces': total, 'faces_passed': passed, 'faces_failed': total-passed,
                   'pass_rate': round(passed/total, 6) if total else None,
                   'lowest_face_black_pixel_coverage': round(min((r[2] for r in rows)), 6) if rows else None,
                   'overall_pass': total > 0 and passed == total},
        'pages': page_records,
        'limits': [
            'Measures black pixels within independent annotated face rectangles, not detector Precision/Recall.',
            'A high black fraction does not guarantee removal of every identifying facial feature; visually review all 7 pages.',
            'Naturally dark pixels can satisfy black-pixel tests; secondary comparison against original nonblack pixels is reported.',
            'This does not inspect recoverable source content in hidden PDF objects, metadata, or the separate media de-identification map.',
            'Results apply only to this particular manually annotated PDF and masked output.'
        ]
    }
    print('\n=== 人臉遮蔽覆蓋驗收 ===')
    print(f"人工 GT={total}, 通過={passed}, 不通過={total-passed}")
    print(f"最低單臉黑色像素覆蓋率={report['totals']['lowest_face_black_pixel_coverage']}")
    print(f"門檻={min_black_coverage:.1%}；整體={'PASS' if report['totals']['overall_pass'] else 'REVIEW / NOT PASS'}")
    if output is not None:
        # Exclusive create: no inadvertent overwrite or restoration leaks.
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open('x', encoding='utf-8', newline='\n') as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
            f.write('\n')
        print(f"報告：{output}")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--original', type=Path, required=True, help='人工標註時的原始 PDF')
    ap.add_argument('--gt', type=Path, required=True, help='face_gt_annotator.py 的 face_ground_truth.json')
    ap.add_argument('--masked', type=Path, required=True, help='與原始 PDF 對應的遮蔽後 PDF')
    ap.add_argument('--output', type=Path, help='新增的 JSON 報告位置（不覆寫）')
    ap.add_argument('--zoom', type=float, default=2.0)
    ap.add_argument('--black-max', type=int, default=55)
    ap.add_argument('--min-coverage', type=float, default=0.98)
    args = ap.parse_args()
    try:
        result = run(args.original, args.gt, args.masked, output=args.output, zoom=args.zoom,
                     black_max=args.black_max, min_black_coverage=args.min_coverage)
    except (ValueError, OSError, RuntimeError, ImportError, json.JSONDecodeError) as exc:
        print(f"驗收無法執行：{exc}", file=sys.stderr)
        return 2
    return 0 if result['totals']['overall_pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
