#!/usr/bin/env python3
"""Benchmark PyMuPDF save parameters on an ALREADY MASKED, raster-only PDF.

Never modifies the input or the project's pdf_secure_output.py. New PDF objects
contain only renderings of the input's visible pages. Temporary outputs are
verified before being removed. No page text/pixels/PII included in the report.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
from statistics import mean, median
import tempfile
import time

try:
    import pymupdf as fitz
except ImportError:
    import fitz

MODES = {
    "baseline": dict(garbage=4, deflate=True, clean=True, incremental=False),
    "no_clean": dict(garbage=4, deflate=True, clean=False, incremental=False),
    "no_gc_clean": dict(garbage=0, deflate=True, clean=False, incremental=False),
    "no_deflate": dict(garbage=0, deflate=False, clean=False, incremental=False),
}


def verify_flat_pdf(path, expected_sizes, expected_hashes=None):
    """Verify PDF structure and visual pixel hashes at a consistent dpi."""
    hashes = []
    with fitz.open(str(path)) as doc:
        if doc.needs_pass or len(doc) != len(expected_sizes):
            raise RuntimeError("Security check: unexpected encryption/page count")
        if doc.embfile_count() or doc.is_form_pdf:
            raise RuntimeError("Security check: attachment/form")
        for i, (p, (w, h)) in enumerate(zip(doc, expected_sizes), 1):
            if abs(p.rect.width - w) > .1 or abs(p.rect.height - h) > .1:
                raise RuntimeError(f"Security check: page {i} size differs")
            if p.get_text("text").strip() or p.get_links():
                raise RuntimeError(f"Security check: page {i} has text/links")
            if next(iter(p.annots() or ()), None) is not None:
                raise RuntimeError(f"Security check: page {i} has annotations")
            if next(iter(p.widgets() or ()), None) is not None:
                raise RuntimeError(f"Security check: page {i} has widgets")
            if len(p.get_images(full=True)) != 1:
                raise RuntimeError(f"Security check: page {i} is not one raster image")
            pix = p.get_pixmap(dpi=96, colorspace=fitz.csRGB, alpha=False, annots=False)
            hashes.append(hashlib.sha256(pix.samples).hexdigest())
    if expected_hashes is not None and hashes != expected_hashes:
        raise RuntimeError("Visual parity check: rendered output pixels differ")
    return hashes


def make_raster_copy(input_path, dpi):
    """Make a new PDF from already masked visible page pixels only, in memory."""
    flat = fitz.open()
    sizes = []
    try:
        with fitz.open(str(input_path)) as src:
            if src.needs_pass or not len(src):
                raise ValueError("Masked input PDF must be unencrypted and nonempty")
            # Refuse to benchmark potentially unsanitized input; protect users
            # from treating this harness as a privacy-redaction tool.
            if src.embfile_count() or src.is_form_pdf:
                raise ValueError("Input still has attachments or forms; choose an already masked PDF")
            for p in src:
                if p.get_text("text").strip() or p.get_links():
                    raise ValueError("Input has searchable text/links; use a verified masked PDF")
                if next(iter(p.annots() or ()), None) is not None:
                    raise ValueError("Input has annotations; use a verified masked PDF")
                if next(iter(p.widgets() or ()), None) is not None:
                    raise ValueError("Input has widgets; use a verified masked PDF")
                sizes.append((p.rect.width, p.rect.height))
                pix = p.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
                png = pix.tobytes("png")
                page = flat.new_page(width=p.rect.width, height=p.rect.height)
                page.insert_image(page.rect, stream=png, keep_proportion=False)
                del png, pix
        return flat, sizes
    except Exception:
        flat.close()
        raise


def main():
    ap = argparse.ArgumentParser(description="V4 PDF save() options benchmark; no modifications to project")
    ap.add_argument("--masked", type=Path, required=True, help="Previously masked and structurally verified PDF")
    ap.add_argument("--results-dir", type=Path, default=Path("pdf_save_v4_results"))
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--repeats", type=int, default=3)
    args = ap.parse_args()
    if not 72 <= args.dpi <= 600 or not 1 <= args.repeats <= 10:
        ap.error("dpi must be 72..600, repeats 1..10")
    if not args.masked.is_file():
        ap.error("--masked file not found")
    args.results_dir.mkdir(parents=True, exist_ok=True)
    records = []
    with make_raster_copy(args.masked, args.dpi)[0] as flat:
        sizes = [(p.rect.width, p.rect.height) for p in flat]
        # Alternate order to reduce filesystem cache/order effects.
        keys = list(MODES)
        for repetition in range(args.repeats):
            ordered = keys if repetition % 2 == 0 else list(reversed(keys))
            for mode in ordered:
                with tempfile.TemporaryDirectory(prefix=".v4_", dir=str(args.results_dir)) as td:
                    out = Path(td) / "candidate.pdf"
                    t0 = time.perf_counter()
                    flat.save(str(out), **MODES[mode])
                    save_s = time.perf_counter() - t0
                    size_bytes = out.stat().st_size
                    # Independent security + visual parity tests for each mode.
                    if mode == "baseline" and repetition == 0:
                        hashes = verify_flat_pdf(out, sizes)
                        expected_hashes = hashes
                    else:
                        verify_flat_pdf(out, sizes, expected_hashes)
                    records.append({"repeat": repetition + 1, "mode": mode,
                                    "save_s": round(save_s, 5), "size_bytes": size_bytes,
                                    "security_and_pixels": "PASS"})
                    print(f"[{mode:12}] run {repetition+1}: {save_s:.3f}s, {size_bytes/1048576:.2f} MiB, PASS")
    summary = {}
    for mode in MODES:
        rows = [r for r in records if r["mode"] == mode]
        summary[mode] = {"average_save_s": round(mean(r["save_s"] for r in rows), 5),
                         "median_save_s": round(median(r["save_s"] for r in rows), 5),
                         "average_size_mb": round(mean(r["size_bytes"] for r in rows) / 1048576, 3),
                         "passed": len(rows)}
    report = {"schema": "masked_pdf_save_v4_ab_1", "page_count": len(sizes), "dpi": args.dpi,
              "repeats": args.repeats, "mode_options": MODES, "summary": summary,
              "runs": records,
              "note": "Experiment on an already masked PDF only; does not benchmark OCR or GUI latency. Verified outputs are deleted. Saving changes require full PII and security regression before deployment."}
    output = args.results_dir / "pdf_save_v4_ab_summary.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[PASS] Wrote report: {output}")
    print("[NOTE] This benchmark does not modify project files; all temporary PDFs removed.")

if __name__ == "__main__":
    main()
