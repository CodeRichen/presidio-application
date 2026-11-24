"""V5: Render only already-redacted visible pages, and create/save PDF in child process.

No source PDF, original XREFs, restore map or unmasked image bytes are passed
across the process boundary. Parent alone validates and atomically replaces the
output. Disabled unless the caller explicitly selects V5.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from typing import Callable

try:
    import pymupdf as fitz
except ImportError:
    import fitz

# Defensive limits (not related to PII classification). Fail closed for oversized frames.
_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_PAGES = 2000
_CHILD_TIMEOUT_SECONDS = 900
_WORKER_NAME = "pdf_secure_v5_worker.py"


def _same_file(a: str, b: str) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _worker_script() -> Path:
    return Path(__file__).resolve().with_name(_WORKER_NAME)


def _validate_document(masked_doc, output_path: str, dpi: int) -> list[tuple[float, float]]:
    if not getattr(masked_doc, 'is_pdf', False) or masked_doc.is_closed:
        raise ValueError('masked_doc 必須是已開啟、已完成遮蔽的 PDF')
    if masked_doc.needs_pass:
        raise ValueError('PDF 需要密碼，無法安全輸出')
    if not isinstance(dpi, int) or isinstance(dpi, bool) or not (72 <= dpi <= 600):
        raise ValueError('dpi 必須介於 72 與 600 之間')
    if not isinstance(output_path, (str, os.PathLike)) or not str(output_path).lower().endswith('.pdf'):
        raise ValueError('輸出路徑必須為 .pdf')
    if len(masked_doc) == 0 or len(masked_doc) > _MAX_PAGES:
        raise ValueError('PDF 頁數為 0 或超過 V5 預設頁數上限')
    src = getattr(masked_doc, 'name', None)
    if src and _same_file(str(src), str(output_path)):
        raise ValueError('安全輸出不得覆寫來源 PDF')
    sizes = []
    for page in masked_doc:
        rect = page.rect
        w, h = float(rect.width), float(rect.height)
        if not (0 < w < 50000 and 0 < h < 50000):
            raise ValueError('PDF 頁面尺寸不合法')
        sizes.append((w, h))
    return sizes


def save_secure_raster_pdf_v5(
    masked_doc,
    output_path: str,
    *,
    dpi: int = 200,
    verify_fn: Callable[[str, list[tuple[float, float]]], None],
) -> None:
    """Stream redacted RGB page pixels through stdin; child handles PNG encoding and saving.

    The child never sees the source PDF or restoration map. The parent controls
    the temporary path, validates structural safety, then atomically replaces.
    Must be called by the existing GUI's background worker, not Tk main thread.
    """
    sizes = _validate_document(masked_doc, output_path, dpi)
    if not callable(verify_fn):
        raise TypeError('verify_fn 必須為既有安全驗證函式')

    script = _worker_script()
    if not script.is_file():
        raise RuntimeError('找不到 V5 PDF 子程序檔案，安全中止')
    if getattr(sys, 'frozen', False):
        # sys.executable would be the GUI EXE, not a Python interpreter.
        raise RuntimeError('V5 目前僅支援以 Python 啟動的 GUI；封裝 EXE 請使用原本儲存流程')

    output_path = os.path.abspath(os.fspath(output_path))
    out_dir = os.path.dirname(output_path)
    os.makedirs(out_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix='.pii_v5_secure_', suffix='.pdf', dir=out_dir)
    os.close(fd)
    os.unlink(tmp_path)  # PyMuPDF must save to a fresh name.

    proc = None
    try:
        # Isolated child has no Tk runtime, main-thread GIL, source PDF, or model state.
        proc = subprocess.Popen(
            [sys.executable, '-I', str(script)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            cwd=str(script.parent),
        )
        if proc.stdin is None:
            raise RuntimeError('V5 子程序 stdin 不可用')
        header = {
            'protocol': 'pii-secure-pdf-v5-2',
            'tmp_path': tmp_path,
            'page_sizes': sizes,
        }
        proc.stdin.write(json.dumps(header, ensure_ascii=False).encode('utf-8') + b'\n')
        for page in masked_doc:
            pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
            try:
                width, height = pix.width, pix.height
                raw_pixels = pix.samples  # Already-redacted visible RGB pixels only.
            finally:
                del pix
            if not 0 < len(raw_pixels) <= _MAX_IMAGE_BYTES:
                raise RuntimeError('V5 輸出頁面影像過大，安全中止')
            if len(raw_pixels) != width * height * 3:
                raise RuntimeError('V5 RGB 頁面像素長度異常，安全中止')
            proc.stdin.write(struct.pack('>IIQ', width, height, len(raw_pixels)))
            proc.stdin.write(raw_pixels)
            del raw_pixels
        proc.stdin.flush()
        proc.stdin.close()
        proc.stdin = None  # communicate() must not flush an already closed pipe.
        try:
            stdout, stderr = proc.communicate(timeout=_CHILD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise RuntimeError('V5 PDF 子程序逾時，安全中止') from None
        if proc.returncode != 0 or stdout.strip() != b'V5_OK':
            # Avoid emitting potentially sensitive paths/traceback to GUI logs.
            raise RuntimeError(f'V5 PDF 子程序執行失敗 (exit={proc.returncode})；原輸出未覆蓋')
        if not os.path.isfile(tmp_path) or os.path.getsize(tmp_path) == 0:
            raise RuntimeError('V5 PDF 子程序未產生有效輸出')
        verify_fn(tmp_path, sizes)
        os.replace(tmp_path, output_path)
    finally:
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
                proc.stdin = None
            if proc.poll() is None:
                proc.kill()
            proc.communicate()
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
