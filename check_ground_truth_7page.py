# -*- coding: utf-8 -*-
"""Compare an already-masked 7-page PDF against expected_text_and_faces.json.

Read-only. Does not run OCR, load NER, modify any PDF, or expose crop data.
Dependencies: PyMuPDF (fitz), numpy. Optional --media-map is local-only and sensitive.
"""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys

import fitz
import numpy as np


def black_fraction(arr, rect, *, threshold=55):
    height, width = arr.shape[:2]
    x0, y0, x1, y1 = rect
    l = max(0, min(width, math.floor(x0)))
    t = max(0, min(height, math.floor(y0)))
    r = max(0, min(width, math.ceil(x1)))
    b = max(0, min(height, math.ceil(y1)))
    if l >= r or t >= b:
        return 0.0
    cut = arr[t:b, l:r, :3]
    return float(np.mean(np.max(cut, axis=2) <= threshold))


def main():
    parser = argparse.ArgumentParser(description="Check known text regions against ground truth in a previously masked PDF")
    parser.add_argument("--pdf", required=True, type=Path, help="Masked 7-page PDF")
    parser.add_argument("--truth", required=True, type=Path, help="expected_text_and_faces.json")
    parser.add_argument("--media-map", type=Path, help="Optional local private media_deid_map.json; never upload/share")
    parser.add_argument("--json-out", type=Path, help="Optional aggregate report (does not contain source PII or crops)")
    parser.add_argument("--min-coverage", type=float, default=0.98)
    args = parser.parse_args()
    if not (0 <= args.min_coverage <= 1):
        parser.error("--min-coverage must be in [0,1]")

    with args.truth.open("r", encoding="utf-8-sig") as f:
        gt = json.load(f)
    samples = gt.get("samples")
    if not isinstance(samples, list) or not samples:
        parser.error("ground truth has no samples")

    pages_summary = []
    passed_total = 0
    total = 0
    label_counts = Counter()
    face_estimate = sum(int(sample.get("expected_faces_manual_estimate", 0)) for sample in samples)
    with fitz.open(args.pdf) as doc:
        if len(doc) != len(samples):
            raise ValueError(f"page mismatch: PDF={len(doc)} ground truth={len(samples)}")
        for idx, sample in enumerate(samples):
            page = doc[idx]
            # 2x render provides enough pixels for the known synthetic text regions.
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csRGB, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
            source_w, source_h = sample["size_px"]
            x_scale, y_scale = pix.width / source_w, pix.height / source_h
            checks = []
            for item in sample.get("expected_text", []):
                x0, y0, x1, y1 = item["bbox_px"]
                rect = (x0 * x_scale, y0 * y_scale, x1 * x_scale, y1 * y_scale)
                coverage = black_fraction(arr, rect)
                passed = coverage >= args.min_coverage
                checks.append({"label": item["label"], "black_coverage": round(coverage, 5), "pass": passed})
                total += 1
                passed_total += int(passed)
                label_counts[item["label"]] += 1
            good = sum(int(c["pass"]) for c in checks)
            pages_summary.append({"page": idx + 1, "passed": good, "expected": len(checks), "items": checks})
            print(f"Page {idx+1}: {good}/{len(checks)} text regions >= {args.min_coverage:.0%} black coverage")

        structure = {
            "page_count": len(doc),
            "no_extractable_text": all(not page.get_text().strip() for page in doc),
            "one_image_per_page": all(len(page.get_images(full=True)) == 1 for page in doc),
            "no_embedded_files": doc.embfile_count() == 0,
        }

        faces = {"expected_visual_estimate": face_estimate, "status": "not_verified_without_face_bboxes"}
        if args.media_map:
            with args.media_map.open("r", encoding="utf-8-sig") as f:
                data = json.load(f)
            entries = data.get(args.pdf.name) if isinstance(data, dict) else None
            if not isinstance(entries, list):
                faces["status"] = "no_matching_filename_in_private_map"
            else:
                face_entries = [e for e in entries if isinstance(e, dict) and str(e.get("label", "")).upper() == "FACE"]
                face_per_page = Counter()
                samples_check = []
                # BBoxes in handler_media's PDF FACE entries are in PDF page coordinates (points).
                for entry in face_entries:
                    try:
                        page_num = int(entry["page"])
                        bbox = [float(z) for z in entry["bbox"]]
                        if not (0 <= page_num < len(doc)) or len(bbox) != 4:
                            raise ValueError()
                        page = doc[page_num]
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), colorspace=fitz.csRGB, alpha=False)
                        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
                        scale_x, scale_y = pix.width / page.rect.width, pix.height / page.rect.height
                        region = [bbox[0]*scale_x, bbox[1]*scale_y, bbox[2]*scale_x, bbox[3]*scale_y]
                        coverage = black_fraction(arr, region)
                        face_per_page[page_num+1] += 1
                        samples_check.append(coverage)
                    except (KeyError, TypeError, ValueError, OverflowError):
                        samples_check.append(0)
                faces.update({
                    "status": "map_reported_boxes_checked_not_independent_ground_truth",
                    "map_face_count": len(face_entries),
                    "map_face_per_page": {str(i): face_per_page[i] for i in range(1, len(doc)+1)},
                    "box_coverage_passed": sum(c >= args.min_coverage for c in samples_check),
                    "box_coverage_total": len(samples_check),
                    "face_count_matches_visual_estimate": len(face_entries) == face_estimate,
                })

    report = {
        "scope": "synthetic seven-page PDF, known text bounding-box coverage only",
        "black_pixel_threshold_rgb_max": 55,
        "minimum_black_coverage": args.min_coverage,
        "text_total": total,
        "text_passed": passed_total,
        "text_pass_rate": round(passed_total / total, 6) if total else None,
        "label_counts": dict(label_counts),
        "pages": pages_summary,
        "structure": structure,
        "faces": faces,
        "limits": [
            "Black coverage of annotated boxes does not establish NER/OCR precision or recall on unannotated PII.",
            "Ground truth contains only visually estimated face counts, not independent face bounding boxes.",
            "Checking FACE entries from the program's own map is not independent face detection accuracy verification.",
        ],
    }
    print(f"\nTEXT GROUND TRUTH: {passed_total}/{total} pass")
    print(f"PDF STRUCTURE: {structure}")
    print(f"FACE GROUND TRUTH: {face_estimate} visually estimated faces; {faces['status']}")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"REPORT: {args.json_out}")
    return 0 if total > 0 and passed_total == total and all(structure.values()) else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as err:
        print(f"Validation error: {type(err).__name__}: {err}", file=sys.stderr)
        sys.exit(2)
