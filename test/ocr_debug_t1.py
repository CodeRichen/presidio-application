# test/ocr_debug_t1.py
# 用途：
# 檢查 t1.png 經過目前正式 OCR 前處理後，
# Windows OCR 實際辨識出哪些 word / line。
#
# 不修改正式程式，只做除錯。

import sys
from pathlib import Path

from PIL import Image

# 讓 test/ 底下的程式可以 import 專案根目錄模組
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 上先載入 handler_text，再載入 handler_media，
# 避免先前遇過的 torch DLL import-order 問題。
import handler_text  # noqa: E402,F401
import handler_media  # noqa: E402


IMAGE_PATH = ROOT / "presidio" / "t1.png"


def main():
    print("=" * 70)
    print("OCR Debug")
    print("圖片：", IMAGE_PATH)
    print("=" * 70)

    if not IMAGE_PATH.exists():
        print(f"[錯誤] 找不到圖片：{IMAGE_PATH}")
        return

    # 完全沿用正式圖片流程的 OCR 前處理
    img = Image.open(IMAGE_PATH).convert("RGB")
    processed, scale = handler_media.preprocess_for_ocr(img)

    # Windows OCR 原始 word 結果
    words = handler_media.ocr_words(processed)

    print(f"\nOCR word 數量：{len(words)}")
    print(f"scale：{scale}")

    # =========================================================
    # 1. 每一行 OCR 結果
    # =========================================================
    print("\n")
    print("=" * 70)
    print("【每行 OCR 結果】")
    print("=" * 70)

    lines = handler_media._group_lines(words)

    for i, line_words in enumerate(lines):
        spaced = " ".join(w["text"] for w in line_words)
        compact = "".join(w["text"] for w in line_words)

        print(f"\nLine {i}")
        print(f"  spaced : {spaced}")
        print(f"  compact: {compact}")

    # =========================================================
    # 2. 每個 OCR word + 座標
    # =========================================================
    print("\n")
    print("=" * 70)
    print("【每個 OCR word】")
    print("=" * 70)

    for w in words:
        print(
            f"line={w['line_id']:>2} "
            f"text={w['text']!r:<25} "
            f"x={w['left']:.1f} "
            f"y={w['top']:.1f} "
            f"w={w['width']:.1f} "
            f"h={w['height']:.1f}"
        )

    # =========================================================
    # 3. 專門搜尋我們現在有問題的內容
    # =========================================================
    print("\n")
    print("=" * 70)
    print("【問題 PII 所在行】")
    print("=" * 70)

    keywords = [
        "TWMD",
        "CGH",
        "POL",
        "EMP",
        "CLM",
        "D287",
        "wang",
        "203",
        "03.74",
    ]

    found_any = False

    for i, line_words in enumerate(lines):
        spaced = " ".join(w["text"] for w in line_words)
        compact = "".join(w["text"] for w in line_words)

        if any(
            keyword.lower() in compact.lower()
            or keyword.lower() in spaced.lower()
            for keyword in keywords
        ):
            found_any = True

            print(f"\nLine {i}")
            print(f"  spaced : {spaced}")
            print(f"  compact: {compact}")
            print("  words:")

            for w in line_words:
                print(
                    f"    {w['text']!r} "
                    f"({w['left']:.1f}, {w['top']:.1f}, "
                    f"{w['width']:.1f}, {w['height']:.1f})"
                )

    if not found_any:
        print("沒有找到指定關鍵字。")


if __name__ == "__main__":
    main()