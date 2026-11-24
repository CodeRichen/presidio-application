# -*- coding: utf-8 -*-
"""Offline YuNet-vs-manual face Ground Truth evaluator for original PDFs.

Place beside face_detector.py in your PII project. The project configuration and
model files are used as-is. No downloads, PII map reads, or source modification.

python evaluate_yunet_face_gt.py --pdf PATH_TO_ORIGINAL.pdf --gt PATH_TO_FACE_GT.json
python evaluate_yunet_face_gt.py --pdf PATH_TO_ORIGINAL.pdf --gt PATH_TO_FACE_GT.json --output face_eval.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from pathlib import Path

ZOOM = 2.0  # Matches handler_media.py scanned-PDF face_zoom/primary_zoom.
DEFAULT_IOU = 0.50


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def iou(a, b):
    """Intersection/union for PDF-point [x0,y0,x1,y1] boxes."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x1-x0)*max(0.0, y1-y0)
    if not inter:
        return 0.0
    area_a = (a[2]-a[0])*(a[3]-a[1])
    area_b = (b[2]-b[0])*(b[3]-b[1])
    return inter/(area_a + area_b - inter)


def match_one_to_one(gt, predicted, threshold):
    """Maximum-cardinality bipartite matching; high IoU candidates searched first.

    Unlike simply summing overlaps, a prediction can never count as multiple TP.
    """
    candidate = []
    for g in gt:
        edges = [(j, iou(g, p["bbox_pt"])) for j, p in enumerate(predicted)]
        candidate.append(sorted([(j, s) for j, s in edges if s >= threshold],
                                key=lambda pair: (-pair[1], pair[0])))
    matched_pred_to_gt = {}

    def augment(gi, visited):
        for pj, _ in candidate[gi]:
            if pj in visited:
                continue
            visited.add(pj)
            if pj not in matched_pred_to_gt or augment(matched_pred_to_gt[pj], visited):
                matched_pred_to_gt[pj] = gi
                return True
        return False

    # Most constrained GTs first reduces ambiguity; maximum cardinality is maintained.
    for gi in sorted(range(len(gt)), key=lambda k: (len(candidate[k]), k)):
        augment(gi, set())
    matches = [{"gt_index": gi + 1, "prediction_index": pj + 1,
                "iou": round(iou(gt[gi], predicted[pj]["bbox_pt"]), 6)}
               for pj, gi in sorted(matched_pred_to_gt.items(), key=lambda x: x[1])]
    used_g = set(matched_pred_to_gt.values())
    used_p = set(matched_pred_to_gt.keys())
    return matches, [i+1 for i in range(len(gt)) if i not in used_g], \
        [j+1 for j in range(len(predicted)) if j not in used_p]


def validate_inputs(pdf_path: Path, gt_path: Path, doc):
    data = json.loads(gt_path.read_text(encoding="utf-8-sig"))
    if data.get("schema") != "face_gt_pdf_points_v1":
        raise ValueError("Ground Truth schema 不符，請使用 face_gt_annotator.py 建立的 JSON")
    source = data.get("source_pdf", {})
    if source.get("sha256") != digest(pdf_path):
        raise ValueError("來源 PDF SHA256 與標註 JSON 不一致；請使用建立 Ground Truth 時的原始 PDF")
    pages = data.get("pages")
    if (not isinstance(pages, list) or len(pages) != len(doc)
            or source.get("page_count") != len(doc)):
        raise ValueError("Ground Truth 與 PDF 頁數不一致")
    if doc.needs_pass:
        raise ValueError("PDF 加密，不能進行本次評估")
    for k, (record, page) in enumerate(zip(pages, doc), 1):
        if page.rotation != 0:
            raise ValueError(f"第 {k} 頁旋轉角度不是 0，與本專題 PDF 偵測設定不相容")
        w, h = page.rect.width, page.rect.height
        if (record.get("page") != k or abs(record.get("width_pt", -1)-w) > .05
                or abs(record.get("height_pt", -1)-h) > .05):
            raise ValueError(f"第 {k} 頁尺寸或頁碼與原始 PDF 不一致")
        if not isinstance(record.get("faces"), list):
            raise ValueError(f"第 {k} 頁 faces 欄位不是清單")
        for rect in record["faces"]:
            if (not isinstance(rect, list) or len(rect) != 4
                    or any(not isinstance(n, (float, int)) or not math.isfinite(n) for n in rect)
                    or not (0 <= rect[0] < rect[2] <= w and 0 <= rect[1] < rect[3] <= h)):
                raise ValueError(f"第 {k} 頁 Ground Truth 座標不合法")
    return data


def evaluate(pdf_path: Path, gt_path: Path, threshold=DEFAULT_IOU):
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz
    from PIL import Image
    # Import project's real detector rather than implementing a new YuNet pipeline.
    from face_detector import detect_faces, padded_face_box, face_enabled

    if not face_enabled():
        raise RuntimeError("PII_FACE_ENABLED=0 或停用人臉偵測；請使用與正式遮蔽相同的有效設定")

    with fitz.open(str(pdf_path)) as doc:
        gt = validate_inputs(pdf_path, gt_path, doc)
        results = []
        tp = fp = fn = 0
        for idx, page in enumerate(doc):
            record = gt["pages"][idx]
            pix = page.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM),
                                  colorspace=fitz.csRGB, alpha=False, annots=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            # Match handler_media.py scanned-PDF path: primary_img at zoom=2.0.
            raw_faces = detect_faces(img)
            predictions = []
            for face in raw_faces:
                # Delegate the face tuple interpretation to project's own function.
                # padding=0 measures raw YuNet detection boxes, not enlarged mask boxes.
                px = padded_face_box(face, img.size, padding=0.0)
                if len(px) != 4:
                    raise ValueError("padded_face_box 輸出格式不符")
                bbox = [max(0., min(float(pix_x)/ZOOM, float(limit)))
                        for pix_x, limit in zip(px, (page.rect.width, page.rect.height,
                                                     page.rect.width, page.rect.height))]
                if bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    continue
                predictions.append({"bbox_pt": [round(v, 3) for v in bbox],
                                    "confidence": round(float(face[4]), 6)})
            matches, misses, extras = match_one_to_one(record["faces"], predictions, threshold)
            ptp, pfp, pfn = len(matches), len(extras), len(misses)
            tp += ptp; fp += pfp; fn += pfn
            results.append({"page": idx+1,
                            "gt_count": len(record["faces"]),
                            "predicted_count": len(predictions),
                            "TP": ptp, "FP": pfp, "FN": pfn,
                            "matches": matches,
                            "missed_gt_indices": misses,
                            "false_positive_prediction_indices": extras,
                            "predictions": predictions})
            print(f"第 {idx+1} 頁｜GT {len(record['faces'])}｜YuNet {len(predictions)}｜TP {ptp} FP {pfp} FN {pfn}", flush=True)

    precision = tp/(tp+fp) if (tp+fp) else 0.0
    recall = tp/(tp+fn) if (tp+fn) else 0.0
    f1 = 2*precision*recall/(precision+recall) if (precision+recall) else 0.0
    return {
        "description": "Offline YuNet raw-detection evaluation against independent PDF-point GT",
        "original_pdf_filename": pdf_path.name,
        "original_pdf_sha256": digest(pdf_path),
        "ground_truth_filename": gt_path.name,
        "evaluation": {"iou_threshold": threshold, "render_zoom": ZOOM,
                       "raw_boxes_without_mask_padding": True,
                       "pairing": "maximum-cardinality one-to-one IoU>=threshold",
                       "detector": "project face_detector.detect_faces default settings",
                       "PII_FACE_MODEL_environment": os.environ.get("PII_FACE_MODEL", "(project default)")},
        "totals": {"ground_truth_faces": tp+fn, "detected_boxes": tp+fp,
                   "TP": tp, "FP": fp, "FN": fn,
                   "precision": round(precision, 6), "recall": round(recall, 6),
                   "f1": round(f1, 6)},
        "pages": results,
        "limits": [
            "Measures raw YuNet face detection, not PII redaction visual coverage.",
            "Results apply only to this manually annotated seven-page dataset.",
            "Ground Truth must be independently labeled and visually reviewed.",
            "Original PDF was not modified; no map file or face crop was saved.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description="Compare real YuNet detections with PDF face Ground Truth")
    parser.add_argument("--pdf", required=True, help="Original, unredacted PDF used during manual labeling")
    parser.add_argument("--gt", required=True, help="face_ground_truth.json from face_gt_annotator.py")
    parser.add_argument("--output", help="Result JSON (default: next to GT, face_yunet_evaluation.json)")
    parser.add_argument("--iou", type=float, default=DEFAULT_IOU, help="IoU match threshold (default 0.50)")
    args = parser.parse_args()
    if not (0 < args.iou <= 1):
        parser.error("--iou 必須介於 0 和 1 之間（不含 0）")
    pdf_path = Path(args.pdf).resolve()
    gt_path = Path(args.gt).resolve()
    output = Path(args.output).resolve() if args.output else gt_path.with_name("face_yunet_evaluation.json")
    if not pdf_path.is_file() or not gt_path.is_file():
        parser.error("找不到原始 PDF 或 Ground Truth JSON")
    if output == pdf_path or output == gt_path or output.exists():
        parser.error("輸出檔案已存在或與輸入相同，為避免覆寫請指定新的 --output 路徑")
    try:
        results = evaluate(pdf_path, gt_path, threshold=args.iou)
        # Atomic no-clobber creation (exclusive); never write a partial report on failure.
        with output.open("x", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
            f.write("\n")
        totals = results["totals"]
        print("\n=== YuNet Ground Truth 統計 ===")
        print(f"GT={totals['ground_truth_faces']}，預測框={totals['detected_boxes']}，"
              f"TP={totals['TP']}，FP={totals['FP']}，FN={totals['FN']}")
        print(f"Precision={totals['precision']:.4f}，Recall={totals['recall']:.4f}，F1={totals['f1']:.4f}")
        print(f"報告已儲存：{output}")
    except Exception as exc:
        print(f"[評估失敗] {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)

if __name__ == "__main__":
    main()
