# test/generate_clean_t1.py
#
# 用途：
# 從 presidio/t1.json 的 article 產生乾淨的 OCR 測試圖片。
#
# 輸出：
# test/ocr_samples/t1_clean.png
#
# 不修改 presidio/t1.png，也不修改正式程式。

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]

JSON_PATH = ROOT / "presidio" / "t1.json"

OUTPUT_DIR = ROOT / "test" / "ocr_samples"
OUTPUT_PATH = OUTPUT_DIR / "t1_clean.png"


# ============================================================
# 圖片設定
# ============================================================

WIDTH = 1600

MARGIN_X = 60
MARGIN_Y = 60

FONT_SIZE = 30

LINE_SPACING = 16

# 一行最多寬度
MAX_TEXT_WIDTH = WIDTH - MARGIN_X * 2


def find_chinese_font():
    """
    尋找 Windows 內建中文字型。
    優先使用微軟正黑體。
    """

    candidates = [
        Path(r"C:\Windows\Fonts\msjh.ttc"),
        Path(r"C:\Windows\Fonts\msjhbd.ttc"),
        Path(r"C:\Windows\Fonts\mingliu.ttc"),
    ]

    for path in candidates:
        if path.exists():
            return path

    raise FileNotFoundError(
        "找不到中文字型，請確認 C:\\Windows\\Fonts 中是否有 msjh.ttc"
    )


def wrap_text(draw, text, font):
    """
    按照實際像素寬度自動換行。

    中文可以逐字切；
    英文、Email、ID 等也會依圖片寬度自然換行。
    """

    lines = []

    # 先保留 article 原本的換行
    paragraphs = text.splitlines()

    for paragraph in paragraphs:

        # 原本就是空行
        if not paragraph:
            lines.append("")
            continue

        current = ""

        for char in paragraph:

            candidate = current + char

            bbox = draw.textbbox(
                (0, 0),
                candidate,
                font=font,
            )

            width = bbox[2] - bbox[0]

            if width <= MAX_TEXT_WIDTH:
                current = candidate

            else:
                if current:
                    lines.append(current)

                current = char

        if current:
            lines.append(current)

    return lines


def main():

    # --------------------------------------------------------
    # 讀取 t1.json
    # --------------------------------------------------------

    if not JSON_PATH.exists():
        print(f"[錯誤] 找不到：{JSON_PATH}")
        return

    with open(JSON_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    article = data["article"]

    print("=" * 70)
    print("Generate Clean t1")
    print("=" * 70)

    print("來源：", JSON_PATH)
    print("article 字數：", len(article))

    # --------------------------------------------------------
    # 字型
    # --------------------------------------------------------

    font_path = find_chinese_font()

    print("字型：", font_path)

    font = ImageFont.truetype(
        str(font_path),
        FONT_SIZE,
    )

    # --------------------------------------------------------
    # 先建立暫時畫布，用來計算換行
    # --------------------------------------------------------

    temp = Image.new(
        "RGB",
        (WIDTH, 100),
        "white",
    )

    temp_draw = ImageDraw.Draw(temp)

    lines = wrap_text(
        temp_draw,
        article,
        font,
    )

    # --------------------------------------------------------
    # 計算行高
    # --------------------------------------------------------

    bbox = temp_draw.textbbox(
        (0, 0),
        "測試ABC123",
        font=font,
    )

    font_height = bbox[3] - bbox[1]

    line_height = font_height + LINE_SPACING

    height = (
        MARGIN_Y * 2
        + line_height * len(lines)
    )

    # --------------------------------------------------------
    # 建立正式圖片
    # --------------------------------------------------------

    img = Image.new(
        "RGB",
        (WIDTH, height),
        "white",
    )

    draw = ImageDraw.Draw(img)

    y = MARGIN_Y

    for line in lines:

        if line:
            draw.text(
                (MARGIN_X, y),
                line,
                font=font,
                fill="black",
            )

        y += line_height

    # --------------------------------------------------------
    # 儲存
    # --------------------------------------------------------

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    img.save(
        OUTPUT_PATH,
        "PNG",
    )

    print()
    print("[完成]")
    print("輸出：", OUTPUT_PATH)
    print("圖片尺寸：", img.size)
    print("行數：", len(lines))


if __name__ == "__main__":
    main()