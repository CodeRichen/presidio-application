"""Local OCR A/B test. Does NOT call a cloud API.

Usage:
  python compare_ocr.py samples/ocr_clear.png --expect 林家宇 --expect 0912-345-678

Outputs are printed only to the terminal; do not use real private data
unless you are comfortable displaying it in terminal history/logs.
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from PIL import Image
from ocr_backend import rapid_ocr_words


def run(name, fn, img):
    started = time.perf_counter()
    try:
        words = fn(img)
    except Exception as exc:
        return name, None, time.perf_counter() - started, str(exc)
    # Recreate lines without adding extra spaces in Chinese names/phone strings.
    by_line = {}
    for w in words:
        by_line.setdefault(w["line_id"], []).append(w)
    lines = ["".join(x["text"] for x in by_line[i]) for i in sorted(by_line)]
    return name, lines, time.perf_counter() - started, None


def main():
    ap = argparse.ArgumentParser(description="Windows OCR vs offline RapidOCR")
    ap.add_argument("image", type=Path)
    ap.add_argument("--expect", action="append", default=[], help="預期出現的文字；可重複指定")
    args = ap.parse_args()
    if not args.image.is_file():
        ap.error(f"找不到圖片：{args.image}")
    img = Image.open(args.image).convert("RGB")
    import handler_media
    windows_fn = getattr(handler_media, "_windows_ocr_words", handler_media.ocr_words)
    print("測試圖片：", args.image, "大小：", img.size)
    outcomes = [run("Windows OCR", windows_fn, img),run("RapidOCR", rapid_ocr_words, img)]
    for name, lines, seconds, error in outcomes:
        print(f"\n[{name}] 耗時 {seconds:.3f} 秒")
        if error:
            print("無法執行：", error)
            continue
        for line in lines:
            print("  ", line)
        if args.expect:
            fulltext="".join(lines).replace(" ", "")
            matched=sum(s.replace(" ", "") in fulltext for s in args.expect)
            print(f"預期字串命中：{matched}/{len(args.expect)} （僅測 OCR 文字，非 PII Recall）")
    print("\n提醒：正式個資遮蔽還需要測試 PII 偵測及座標是否完整覆蓋；OCR 文字命中不等於遮蔽成功。")


if __name__ == "__main__":
    main()
