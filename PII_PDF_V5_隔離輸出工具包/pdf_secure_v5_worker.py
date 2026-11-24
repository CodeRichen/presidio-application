"""V5 child worker: consumes only post-redaction RGB page pixels from stdin.

Deliberately has no access to the source PDF or restore map.
No original PDF objects are copied to the new PDF.
"""
from __future__ import annotations

import json
import math
import os
import struct
import sys

try:
    import pymupdf as fitz
except ImportError:
    import fitz

_MAX_IMAGE_BYTES = 512 * 1024 * 1024
_MAX_PAGES = 2000


def _read_exact(f, n: int) -> bytes:
    chunks = []
    while n:
        chunk = f.read(min(n, 16 * 1024 * 1024))
        if not chunk:
            raise EOFError('子程序資料不完整')
        chunks.append(chunk)
        n -= len(chunk)
    return b''.join(chunks)


def main() -> int:
    source = sys.stdin.buffer
    line = source.readline(64 * 1024)
    if not line.endswith(b'\n'):
        raise ValueError('無效的 V5 協定標頭')
    data = json.loads(line)
    if data.get('protocol') != 'pii-secure-pdf-v5-2':
        raise ValueError('V5 協定版本不符')
    sizes = data.get('page_sizes')
    tmp_path = data.get('tmp_path')
    if not isinstance(sizes, list) or not 0 < len(sizes) <= _MAX_PAGES:
        raise ValueError('頁數不合法')
    if not isinstance(tmp_path, str) or not os.path.isabs(tmp_path) or not tmp_path.lower().endswith('.pdf'):
        raise ValueError('輸出暫存路徑不合法')
    with fitz.open() as fresh:
        for pair in sizes:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError('頁面尺寸缺失')
            w, h = pair
            if (not isinstance(w, (int, float)) or not isinstance(h, (int, float))
                or not math.isfinite(w) or not math.isfinite(h) or not (0 < w < 50000 and 0 < h < 50000)):
                raise ValueError('頁面尺寸不合法')
            width, height, n = struct.unpack('>IIQ', _read_exact(source, 16))
            if not (0 < width <= 100000 and 0 < height <= 100000):
                raise ValueError('頁面像素尺寸不合法')
            if n != width * height * 3 or not 0 < n <= _MAX_IMAGE_BYTES:
                raise ValueError('RGB 像素數量不合法')
            raw_pixels = _read_exact(source, n)
            pix = fitz.Pixmap(fitz.csRGB, width, height, raw_pixels, False)
            png = pix.tobytes('png')  # CPU-heavy encoding happens in child process.
            target = fresh.new_page(width=w, height=h)
            target.insert_image(target.rect, stream=png, keep_proportion=False)
            del png, pix, raw_pixels
        if source.read(1):
            raise ValueError('頁面資料後仍有多餘內容')
        # Exactly the current safe-export settings, in an isolated interpreter.
        fresh.save(tmp_path, garbage=4, deflate=True, clean=True, incremental=False)
    sys.stdout.buffer.write(b'V5_OK\n')
    sys.stdout.buffer.flush()
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception:
        # Parent reports a generic fail-closed error; never send source content to stdout.
        sys.exit(1)
