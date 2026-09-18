"""
OCR Accuracy Test

目的：
1. 自動建立多種不同難度的假 PII 測試圖片
2. 全部使用目前 handler_media.py 的 OCR 流程
3. 計算每個測試情境的 CER（Character Error Rate）
4. 固定測試條件，供 OCR 修改前後比較

重要：
- 所有測試資料都是虛構資料，不含真實個資
- 此程式不修改 handler_media.py
- 為了維持修改前後可比較性，不應任意修改測試條件
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# =========================================================
# 專案路徑
# =========================================================

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import handler_media


# =========================================================
# Ground Truth
# =========================================================

GROUND_TRUTH_LINES = [
    "姓名：王小明",
    "電話：0912-345-678",
    "Email：test123@example.com",
    "身分證：A123456789",
    "地址：高雄市測試區測試路123號",
    "IP：192.168.1.100",
]

GROUND_TRUTH_TEXT = "\n".join(GROUND_TRUTH_LINES)


# =========================================================
# Levenshtein Distance
# =========================================================

def levenshtein_distance(a: str, b: str) -> int:
    """
    計算兩個字串之間的 Levenshtein Distance。

    插入、刪除、替換一個字元都算一次錯誤。
    """
    previous_row = list(range(len(b) + 1))

    for i, char_a in enumerate(a, start=1):
        current_row = [i]

        for j, char_b in enumerate(b, start=1):
            insert_cost = current_row[j - 1] + 1
            delete_cost = previous_row[j] + 1

            replace_cost = previous_row[j - 1]
            if char_a != char_b:
                replace_cost += 1

            current_row.append(
                min(
                    insert_cost,
                    delete_cost,
                    replace_cost,
                )
            )

        previous_row = current_row

    return previous_row[-1]


# =========================================================
# CER
# =========================================================

def calculate_cer(reference: str, hypothesis: str) -> float:
    """
    Character Error Rate

    CER = Edit Distance / Ground Truth 字元數

    CER 越低越好。
    """
    if not reference:
        return 0.0 if not hypothesis else 1.0

    distance = levenshtein_distance(reference, hypothesis)

    return distance / len(reference)


def normalize_for_cer(text: str) -> str:
    """
    CER 評估前正規化。

    Windows OCR 會把中文字拆成不同 word。
    測試程式重新組合 word 時會產生額外空白，
    因此 CER 計算時忽略空白。

    全形冒號與半形冒號視為相同。
    """

    text = text.replace(" ", "")
    text = text.replace("\t", "")
    text = text.replace("：", ":")

    return text


# =========================================================
# 字型
# =========================================================

def get_test_font(size: int):
    """
    尋找 Windows 內建、支援繁體中文的字型。
    """

    possible_fonts = [
        Path("C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/msjhbd.ttc"),
        Path("C:/Windows/Fonts/mingliu.ttc"),
    ]

    for font_path in possible_fonts:
        if font_path.exists():
            return ImageFont.truetype(
                str(font_path),
                size,
            )

    raise RuntimeError(
        "找不到可用的繁體中文字型"
    )


# =========================================================
# 基礎圖片產生器
# =========================================================

def create_text_image(
    font_size: int = 32,
    text_color=(0, 0, 0),
    background_color=(255, 255, 255),
):
    """
    建立含有固定 Ground Truth 的圖片。
    """

    width = 1000
    height = 420

    image = Image.new(
        "RGB",
        (width, height),
        background_color,
    )

    draw = ImageDraw.Draw(image)
    font = get_test_font(font_size)

    x = 50
    y = 40

    # 行距跟字型大小一起調整
    line_spacing = int(font_size * 1.7)

    for line in GROUND_TRUTH_LINES:
        draw.text(
            (x, y),
            line,
            font=font,
            fill=text_color,
        )

        y += line_spacing

    return image


# =========================================================
# Case 1：Clear
# =========================================================

def create_clear_image():
    """
    正常、清晰、高對比文字。
    """

    return create_text_image(
        font_size=32,
        text_color=(0, 0, 0),
        background_color=(255, 255, 255),
    )


# =========================================================
# Case 2：Small Text
# =========================================================

def create_small_text_image():
    """
    模擬較小的文字。
    """

    return create_text_image(
        font_size=18,
        text_color=(0, 0, 0),
        background_color=(255, 255, 255),
    )


# =========================================================
# Case 3：Low Resolution
# =========================================================

def create_low_resolution_image():
    """
    先建立正常圖片，再縮小，
    模擬低解析度來源。
    """

    image = create_text_image(
        font_size=32,
        text_color=(0, 0, 0),
        background_color=(255, 255, 255),
    )

    image = image.resize(
        (500, 210),
        Image.Resampling.LANCZOS,
    )

    return image


# =========================================================
# Case 4：Low Contrast
# =========================================================

def create_low_contrast_image():
    """
    模擬灰色文字 + 淺灰背景。
    """

    return create_text_image(
        font_size=32,
        text_color=(145, 145, 145),
        background_color=(235, 235, 235),
    )


# =========================================================
# Case 5：Blurred
# =========================================================

def create_blurred_image():
    """
    模擬輕微失焦或掃描模糊。
    """

    image = create_text_image(
        font_size=32,
        text_color=(0, 0, 0),
        background_color=(255, 255, 255),
    )

    image = image.filter(
        ImageFilter.GaussianBlur(radius=1.2)
    )

    return image


# =========================================================
# OCR words → text
# =========================================================

def words_to_text(words: list[dict]) -> str:
    """
    使用 line_id 將 OCR words 重建成多行文字。
    """

    lines = {}

    for word in words:
        line_id = word.get("line_id", 0)

        if line_id not in lines:
            lines[line_id] = []

        lines[line_id].append(word)

    output_lines = []

    for line_id in sorted(lines.keys()):

        # 確保同一行按照 X 座標排列
        sorted_words = sorted(
            lines[line_id],
            key=lambda word: word["left"],
        )

        line_text = " ".join(
            word["text"]
            for word in sorted_words
        )

        output_lines.append(line_text)

    return "\n".join(output_lines)


# =========================================================
# 執行單一測試
# =========================================================

def run_test_case(
    case_name: str,
    image: Image.Image,
    sample_dir: Path,
):
    """
    對單一圖片執行目前專案的 OCR，
    並回傳 CER 等測試結果。
    """

    image_path = sample_dir / f"{case_name}.png"

    image.save(image_path)

    # -----------------------------------------------------
    # 使用目前 handler_media.py 的 preprocessing
    # -----------------------------------------------------

    processed_image, scale = (
        handler_media.preprocess_for_ocr(image)
    )

    # -----------------------------------------------------
    # 使用目前 handler_media.py 的 Windows OCR
    # -----------------------------------------------------

    words = handler_media.ocr_words(
        processed_image
    )

    ocr_text = words_to_text(words)

    # -----------------------------------------------------
    # CER
    # -----------------------------------------------------

    normalized_reference = normalize_for_cer(
        GROUND_TRUTH_TEXT
    )

    normalized_ocr = normalize_for_cer(
        ocr_text
    )

    distance = levenshtein_distance(
        normalized_reference,
        normalized_ocr,
    )

    cer = calculate_cer(
        normalized_reference,
        normalized_ocr,
    )

    return {
        "name": case_name,
        "path": image_path,
        "scale": scale,
        "ocr_text": ocr_text,
        "distance": distance,
        "cer": cer,
        "word_count": len(words),
    }


# =========================================================
# 顯示單一測試結果
# =========================================================

def print_test_result(result: dict):
    print("\n" + "=" * 60)

    print(
        f"TEST CASE：{result['name']}"
    )

    print("=" * 60)

    print(
        f"Image：{result['path']}"
    )

    print(
        f"OCR scale：{result['scale']:.2f}"
    )

    print("\n--- OCR Result ---")

    print(result["ocr_text"])

    print("\n--- Metrics ---")

    print(
        f"Edit Distance : "
        f"{result['distance']}"
    )

    print(
        f"CER           : "
        f"{result['cer']:.4f}"
    )

    print(
        f"CER (%)       : "
        f"{result['cer'] * 100:.2f}%"
    )

    print(
        f"OCR words     : "
        f"{result['word_count']}"
    )


# =========================================================
# Summary
# =========================================================

def print_summary(results: list[dict]):
    print("\n")
    print("=" * 60)
    print("OCR ACCURACY TEST SUMMARY")
    print("=" * 60)

    print(
        f"{'Test Case':<22}"
        f"{'CER':>12}"
        f"{'CER (%)':>14}"
    )

    print("-" * 48)

    total_cer = 0.0

    for result in results:
        total_cer += result["cer"]

        print(
            f"{result['name']:<22}"
            f"{result['cer']:>12.4f}"
            f"{result['cer'] * 100:>13.2f}%"
        )

    average_cer = total_cer / len(results)

    print("-" * 48)

    print(
        f"{'AVERAGE':<22}"
        f"{average_cer:>12.4f}"
        f"{average_cer * 100:>13.2f}%"
    )

    print("=" * 60)


# =========================================================
# Main
# =========================================================

def main():

    sample_dir = (
        Path(__file__).parent
        / "ocr_samples"
    )

    sample_dir.mkdir(
        exist_ok=True
    )

    print("=" * 60)
    print("OCR ACCURACY TEST")
    print("=" * 60)

    print("\nGround Truth：")
    print(GROUND_TRUTH_TEXT)

    # -----------------------------------------------------
    # 固定測試案例
    # -----------------------------------------------------

    test_cases = [
        (
            "clear",
            create_clear_image(),
        ),
        (
            "small_text",
            create_small_text_image(),
        ),
        (
            "low_resolution",
            create_low_resolution_image(),
        ),
        (
            "low_contrast",
            create_low_contrast_image(),
        ),
        (
            "blurred",
            create_blurred_image(),
        ),
    ]

    results = []

    # -----------------------------------------------------
    # 執行所有測試
    # -----------------------------------------------------

    for case_name, image in test_cases:

        result = run_test_case(
            case_name,
            image,
            sample_dir,
        )

        results.append(result)

        print_test_result(
            result
        )

    # -----------------------------------------------------
    # Summary
    # -----------------------------------------------------

    print_summary(
        results
    )


if __name__ == "__main__":
    main()