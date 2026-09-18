"""
PDF OCR Accuracy / PII Detection Test

目的：
1. 建立固定的虛構 PII 測試資料。
2. 測試「文字型 PDF」是否能直接從 PDF 文字層偵測 PII。
3. 建立 5 種掃描型 PDF：clear / small_text / low_resolution / low_contrast / blurred。
4. 掃描型 PDF 直接呼叫 handler_media.py 正式的 multi-zoom PDF OCR 元件，
   不在測試程式內重寫 zoom=2.0 OCR 流程。
5. 統計各類 PII Recall，並檢查轉回 PDF 座標後的 bounding box 是否有效。

重要：
- 全部資料都是虛構資料，不含真實個資。
- 這支測試不修改 handler_media.py。
- 先固定測試條件取得目前 PDF 表現，再決定是否需要調整 PDF OCR。
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import handler_media

try:
    import pymupdf as fitz
except ImportError:
    import fitz


GROUND_TRUTH_LINES = [
    "電話：0912-345-678",
    "Email：test123@example.com",
    "身分證：A123456789",
    "地址：高雄市測試區測試路123號",
    "IP：192.168.1.100",
]

EXPECTED_LABELS = [
    "TW_PHONE",
    "EMAIL",
    "TW_ID",
    "ADDRESS",
    "IP_ADDRESS",
]

PRIMARY_ZOOM = 2.0
FALLBACK_ZOOM = 3.0


def get_test_font(size: int):
    possible_fonts = [
        Path("C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/msjhbd.ttc"),
        Path("C:/Windows/Fonts/mingliu.ttc"),
    ]
    for font_path in possible_fonts:
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size)
    raise RuntimeError("找不到可用的繁體中文字型")


def create_text_image(
    font_size: int = 32,
    text_color=(0, 0, 0),
    background_color=(255, 255, 255),
):
    image = Image.new("RGB", (1000, 420), background_color)
    draw = ImageDraw.Draw(image)
    font = get_test_font(font_size)

    x, y = 50, 40
    line_spacing = int(font_size * 1.7)

    for line in GROUND_TRUTH_LINES:
        draw.text((x, y), line, font=font, fill=text_color)
        y += line_spacing

    return image


def create_cases():
    clear = create_text_image()

    small_text = create_text_image(font_size=18)

    low_resolution = create_text_image().resize(
        (500, 210),
        Image.Resampling.LANCZOS,
    )

    low_contrast = create_text_image(
        text_color=(145, 145, 145),
        background_color=(235, 235, 235),
    )

    blurred = create_text_image().filter(
        ImageFilter.GaussianBlur(radius=1.2)
    )

    return {
        "clear": clear,
        "small_text": small_text,
        "low_resolution": low_resolution,
        "low_contrast": low_contrast,
        "blurred": blurred,
    }


def save_scanned_pdf(image: Image.Image, pdf_path: Path):
    """
    將測試圖片直接嵌入 PDF。
    PDF 本身不建立文字層，因此 handler_media 應視為掃描型 PDF。
    """
    rgb = image.convert("RGB")
    rgb.save(pdf_path, "PDF", resolution=150.0)


def create_text_layer_pdf(pdf_path: Path):
    """
    建立真正有文字層的 PDF。
    使用英數 PII 為主，避免 PyMuPDF 內建字型缺少中文字形影響測試。
    ADDRESS 不列入此文字層測試；ADDRESS 會由掃描型 PDF 測試涵蓋。
    """
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)

    lines = [
        "Phone: 0912-345-678",
        "Email: test123@example.com",
        "TW ID: A123456789",
        "IP: 192.168.1.100",
    ]

    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=14)
        y += 30

    doc.save(str(pdf_path))
    doc.close()


def box_is_valid(box, width: float, height: float):
    x0, y0, x1, y1 = box[:4]
    return (
        0 <= x0 < x1 <= width
        and 0 <= y0 < y1 <= height
    )


def detected_labels(boxes):
    return {box[4] for box in boxes}


def run_scanned_pdf_case(case_name: str, pdf_path: Path):
    """
    直接呼叫 handler_media.py 正式 multi-zoom OCR 元件。

    _collect_pdf_ocr_boxes() 與 _merge_pdf_multizoom_boxes() 就是
    _mask_pdf_scanned_page() 實際使用的 OCR / 座標轉換 / 去重邏輯。
    這樣 benchmark 不再自行重寫一套固定 zoom=2.0 的流程。
    """
    doc = fitz.open(str(pdf_path))
    page = doc[0]

    has_text_layer = handler_media._page_has_text(page)

    primary_boxes, primary_image = handler_media._collect_pdf_ocr_boxes(
        page, PRIMARY_ZOOM
    )
    fallback_boxes, fallback_image = handler_media._collect_pdf_ocr_boxes(
        page, FALLBACK_ZOOM
    )
    boxes = handler_media._merge_pdf_multizoom_boxes(
        primary_boxes, fallback_boxes
    )

    labels = detected_labels(boxes)
    page_rect = page.rect

    valid_count = sum(
        box_is_valid(box, page_rect.width, page_rect.height)
        for box in boxes
    )

    result = {
        "name": case_name,
        "has_text_layer": has_text_layer,
        "primary_render_size": (primary_image.width, primary_image.height),
        "fallback_render_size": (fallback_image.width, fallback_image.height),
        "primary_boxes": primary_boxes,
        "fallback_boxes": fallback_boxes,
        "boxes": boxes,
        "labels": labels,
        "valid_count": valid_count,
        "page_size": (page_rect.width, page_rect.height),
    }

    doc.close()
    return result


def run_text_layer_pdf(pdf_path: Path):
    """
    模擬 handler_media._mask_pdf_text_page 的文字重建與 detect_matches。
    """
    doc = fitz.open(str(pdf_path))
    page = doc[0]

    words = page.get_text("words")
    lines = {}

    for word in words:
        lines.setdefault((word[5], word[6]), []).append(word)

    found = set()

    for line_words in lines.values():
        line_words.sort(key=lambda word: word[7])
        line_text = " ".join(word[4] for word in line_words)

        for _, _, label, _, _ in handler_media.detect_matches(line_text):
            found.add(label)

    result = {
        "has_text_layer": handler_media._page_has_text(page),
        "labels": found,
        "word_count": len(words),
    }

    doc.close()
    return result


def print_scanned_case(result):
    print("\n" + "=" * 88)
    print(f"SCANNED PDF CASE：{result['name']}")
    print("=" * 88)
    print(f"PDF text layer：{'YES' if result['has_text_layer'] else 'NO'}")
    print(f"Primary zoom：{PRIMARY_ZOOM:.1f}")
    print(f"Fallback zoom：{FALLBACK_ZOOM:.1f}")
    print(
        f"Primary render："
        f"{result['primary_render_size'][0]} x {result['primary_render_size'][1]}"
    )
    print(
        f"Fallback render："
        f"{result['fallback_render_size'][0]} x {result['fallback_render_size'][1]}"
    )
    print(f"Primary boxes：{len(result['primary_boxes'])}")
    print(f"Fallback boxes：{len(result['fallback_boxes'])}")
    print(f"Merged boxes：{len(result['boxes'])}")
    print(f"Valid PDF-coordinate boxes：{result['valid_count']}")

    print("\n--- Detection Result ---")
    for label in EXPECTED_LABELS:
        status = "PASS" if label in result["labels"] else "MISS"
        print(f"{label:<18} {status}")

    print("\n--- Merged PDF-coordinate Boxes ---")
    for box in result["boxes"]:
        x0, y0, x1, y1, label, detected_text, score, source = box
        valid = box_is_valid(
            box,
            result["page_size"][0],
            result["page_size"][1],
        )
        print(
            f"{label:<16} text={detected_text!r} "
            f"box=({x0:.1f}, {y0:.1f}, {x1:.1f}, {y1:.1f}) "
            f"valid={valid}"
        )


def print_summary(results):
    print("\n\n" + "=" * 88)
    print("SCANNED PDF MULTI-ZOOM -> PII DETECTION RECALL MATRIX")
    print("=" * 88)

    header = f"{'Case':<20}" + "".join(
        f"{label:>14}"
        for label in EXPECTED_LABELS
    )
    print(header)
    print("-" * 88)

    detected_total = 0
    expected_total = len(results) * len(EXPECTED_LABELS)

    total_boxes = 0
    valid_boxes = 0

    for result in results:
        row = f"{result['name']:<20}"

        for label in EXPECTED_LABELS:
            passed = label in result["labels"]
            detected_total += int(passed)
            row += f"{('PASS' if passed else 'MISS'):>14}"

        print(row)
        total_boxes += len(result["boxes"])
        valid_boxes += result["valid_count"]

    recall = detected_total / expected_total if expected_total else 0.0

    print("-" * 88)
    print(f"Detected：{detected_total} / {expected_total}")
    print(f"PII Recall：{recall * 100:.2f}%")

    print("\n" + "=" * 88)
    print("SCANNED PDF MULTI-ZOOM BOUNDING BOX CHECK")
    print("=" * 88)
    print(f"Total boxes：{total_boxes}")
    print(f"Valid boxes：{valid_boxes}")
    print(
        "Bounding Box Result："
        + ("PASS" if total_boxes == valid_boxes else "FAIL")
    )
    print("=" * 88)


def main():
    sample_dir = Path(__file__).parent / "ocr_pdf_samples"
    sample_dir.mkdir(exist_ok=True)

    print("=" * 88)
    print("PDF OCR / PII DETECTION TEST")
    print("=" * 88)
    print(f"Scanned PDF multi-zoom：{PRIMARY_ZOOM:.1f} + {FALLBACK_ZOOM:.1f}")

    # ---------------------------------------------------------
    # 1. 文字型 PDF
    # ---------------------------------------------------------
    text_pdf_path = sample_dir / "text_layer.pdf"
    create_text_layer_pdf(text_pdf_path)
    text_result = run_text_layer_pdf(text_pdf_path)

    print("\n" + "=" * 88)
    print("TEXT-LAYER PDF CHECK")
    print("=" * 88)
    print(
        "PDF text layer："
        + ("YES" if text_result["has_text_layer"] else "NO")
    )
    print(f"PDF words：{text_result['word_count']}")

    text_expected = ["TW_PHONE", "EMAIL", "TW_ID", "IP_ADDRESS"]
    for label in text_expected:
        print(
            f"{label:<18} "
            + ("PASS" if label in text_result["labels"] else "MISS")
        )

    # ---------------------------------------------------------
    # 2. 掃描型 PDF
    # ---------------------------------------------------------
    results = []

    for case_name, image in create_cases().items():
        pdf_path = sample_dir / f"{case_name}.pdf"
        save_scanned_pdf(image, pdf_path)

        result = run_scanned_pdf_case(case_name, pdf_path)
        results.append(result)
        print_scanned_case(result)

    print_summary(results)


if __name__ == "__main__":
    main()
