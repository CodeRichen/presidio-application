# test/ocr_scale_test.py
#
# 測試目的：
# 比較同一張 t1.png 在不同放大倍率下，
# Windows OCR 是否能辨識回原本漏掉的敏感資訊。
#
# 注意：
# 這支程式只做 OCR 測試，不修改正式程式。

import sys
from pathlib import Path

from PIL import Image

# 讓 test/ 底下的程式可以 import 專案根目錄模組
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import handler_media


# ============================================================
# 測試設定
# ============================================================

IMAGE_PATH = ROOT / "test" / "ocr_samples" / "t1_clean.png"

# 要比較的圖片放大倍率
SCALES = [1.0, 1.5, 2.0, 3.0]

# 我們特別關心的 PII
TARGETS = [
    "D287654321",
    "chihming.wang@example.com",
    "TWMD-0098234",
    "CGH-20260214-0071",
    "POL-TW-4471982",
    "EMP-05821",
    "CLM-2026-0034127",
    "203.74.12.88",
]


def resize_image(img: Image.Image, scale: float) -> Image.Image:
    """
    依照指定倍率放大圖片。

    使用 LANCZOS，避免單純拉伸造成太嚴重的鋸齒。
    """
    if scale == 1.0:
        return img.copy()

    width = round(img.width * scale)
    height = round(img.height * scale)

    return img.resize(
        (width, height),
        Image.Resampling.LANCZOS,
    )


def get_ocr_text(img: Image.Image) -> str:
    """
    呼叫目前專案 handler_media.py 的 Windows OCR。

    ocr_words() 回傳每個 OCR word，
    這裡把它們按照 line_id 重新組成文字。
    """

    words = handler_media.ocr_words(img)

    lines = {}

    for word in words:
        line_id = word["line_id"]

        if line_id not in lines:
            lines[line_id] = []

        lines[line_id].append(word["text"])

    result = []

    for line_id in sorted(lines):
        # compact 版本：
        # 不加入空白，方便檢查 Email、ID、案件編號等。
        line_text = "".join(lines[line_id])
        result.append(line_text)

    return "\n".join(result)


def target_status(text: str, target: str) -> str:
    """
    檢查完整 Ground Truth 是否出現在 OCR 結果中。
    """

    if target.lower() in text.lower():
        return "PASS"

    return "MISS"


def main():

    if not IMAGE_PATH.exists():
        print(f"找不到圖片：{IMAGE_PATH}")
        return

    original = Image.open(IMAGE_PATH).convert("RGB")

    print("=" * 80)
    print("OCR Scale Test")
    print("圖片：", IMAGE_PATH)
    print("原始尺寸：", original.size)
    print("=" * 80)

    summary = {}

    for scale in SCALES:

        print()
        print("=" * 80)
        print(f"Scale = {scale}x")
        print("=" * 80)

        resized = resize_image(original, scale)

        print("圖片尺寸：", resized.size)

        # 使用 Task 13 現在正式程式的 OCR 前處理
        processed, _ = handler_media.preprocess_for_ocr(resized)

        text = get_ocr_text(processed)

        print()
        print("【OCR 全文】")
        print(text)

        print()
        print("【目標 PII】")

        scale_result = {}

        for target in TARGETS:

            status = target_status(text, target)

            scale_result[target] = status

            mark = "✓" if status == "PASS" else "✗"

            print(
                f"{mark} {target:<30} "
                f"{status}"
            )

        summary[scale] = scale_result

    # ========================================================
    # 最後比較表
    # ========================================================

    print()
    print("=" * 80)
    print("最終比較")
    print("=" * 80)

    header = f"{'Target':<30}"

    for scale in SCALES:
        header += f"{scale:>8}x"

    print(header)
    print("-" * len(header))

    for target in TARGETS:

        row = f"{target:<30}"

        for scale in SCALES:

            status = summary[scale][target]

            if status == "PASS":
                symbol = "PASS"
            else:
                symbol = "MISS"

            row += f"{symbol:>9}"

        print(row)

    print()

    for scale in SCALES:

        passed = sum(
            1
            for result in summary[scale].values()
            if result == "PASS"
        )

        total = len(TARGETS)

        print(
            f"{scale}x："
            f"{passed}/{total} "
            f"({passed / total * 100:.1f}%)"
        )


if __name__ == "__main__":
    main()