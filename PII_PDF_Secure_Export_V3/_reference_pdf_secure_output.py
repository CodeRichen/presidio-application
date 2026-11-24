"""Secure output for a PDF after all text/image PII redactions are applied.

This module deliberately never copies source PDF objects into the output.
It renders only the *visible, already-masked* pages into a fresh raster-only PDF.

Privacy properties:
- Embedded attachments / annotations / actions / form fields / text layers
  from the source PDF are not copied.
- Source images are not copied as PDF objects; only flattened rendered pixels
  are inserted into newly-created PDF pages.
- Output is written to a temporary PDF, verified, and atomically replaced.

IMPORTANT:
This is a structural safeguard, NOT an OCR/PII detection guarantee.
The caller MUST mask all sensitive content before invoking this function.
Existing restore maps can still contain sensitive original image crops.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

try:
    import pymupdf as fitz  # pip install pymupdf
except ImportError:
    import fitz


def _is_same_file(path1: str, path2: str) -> bool:
    """Compare destination with source even when destination doesn't exist yet."""
    return os.path.normcase(os.path.realpath(path1)) == os.path.normcase(os.path.realpath(path2))


def _verify_secure_raster_pdf(path: str, expected_sizes: list[tuple[float, float]]) -> None:
    """Fail-closed on unexpected structural contents; not an OCR proof."""
    with fitz.open(path) as check:
        if check.needs_pass:
            raise RuntimeError("安全 PDF 驗證失敗：輸出意外要求密碼")
        if len(check) != len(expected_sizes):
            raise RuntimeError("安全 PDF 驗證失敗：頁數不一致")
        if check.embfile_count():
            raise RuntimeError("安全 PDF 驗證失敗：仍有嵌入附件")
        if check.is_form_pdf:
            raise RuntimeError("安全 PDF 驗證失敗：意外包含 PDF 表單")
        for i, (p, (w, h)) in enumerate(zip(check, expected_sizes), 1):
            if abs(p.rect.width - w) > 0.1 or abs(p.rect.height - h) > 0.1:
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁尺寸變更")
            if p.get_text("text").strip():
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁仍有可提取文字")
            if p.get_links():
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁仍有連結")
            if next(iter(p.annots() or ()), None) is not None:
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁仍有註解")
            if next(iter(p.widgets() or ()), None) is not None:
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁仍有表單元件")
            if len(p.get_images(full=True)) != 1:
                raise RuntimeError(f"安全 PDF 驗證失敗：第 {i} 頁不是單一平面影像")


def save_secure_raster_pdf(masked_doc: fitz.Document, output_path: str, *, dpi: int = 200) -> None:
    """Save already-masked pages in a new, raster-only PDF, then verify.

    Args:
        masked_doc: Open PyMuPDF PDF document with redactions ALREADY applied.
        output_path: Target path, different from the source document.
        dpi: Render resolution, typically 150-200. 200 keeps small text sharp.

    Note: Rotated or cropped original pages become unrotated visible page images.
    If a separate restore-map uses original unrotated PDF coordinates, remap its
    rectangles before using them on the output of rotated/cropped PDFs.
    """
    if not masked_doc.is_pdf or masked_doc.is_closed:
        raise ValueError("masked_doc 必須是已開啟的 PDF 文件")
    if masked_doc.needs_pass:
        raise ValueError("masked_doc 需要密碼，無法安全輸出")
    if not (72 <= dpi <= 600):
        raise ValueError("dpi 必須介於 72 與 600 之間")
    if len(masked_doc) == 0:
        raise ValueError("PDF 不可以是空文件")
    if not output_path.lower().endswith(".pdf"):
        raise ValueError("output_path 必須以 .pdf 結尾")
    if getattr(masked_doc, "name", None) and _is_same_file(masked_doc.name, output_path):
        raise ValueError("輸出路徑不得覆寫原始 PDF")

    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)

    fd, temporary_path = tempfile.mkstemp(prefix=".pii_secure_", suffix=".pdf", dir=output_dir)
    os.close(fd)
    os.unlink(temporary_path)  # Document.save requires a fresh pathname.

    page_sizes = []
    try:
        with fitz.open() as fresh:
            for source_page in masked_doc:
                visible = source_page.rect
                if visible.width <= 0 or visible.height <= 0:
                    raise ValueError("PDF 頁面尺寸不合法")
                page_sizes.append((visible.width, visible.height))

                # Render only the post-redaction visible pixels. Never use the
                # original image XREFs or any unredacted source file bytes.
                pix = source_page.get_pixmap(
                    dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False
                )
                target = fresh.new_page(width=visible.width, height=visible.height)
                target.insert_image(target.rect, stream=pix.tobytes("png"), keep_proportion=False)
                del pix

            # Newly created PDF has no inherited source attachments, metadata,
            # links, forms, JavaScript or invisible text layer.
            fresh.save(temporary_path, garbage=4, deflate=True, clean=True, incremental=False)

        _verify_secure_raster_pdf(temporary_path, page_sizes)
        os.replace(temporary_path, output_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
