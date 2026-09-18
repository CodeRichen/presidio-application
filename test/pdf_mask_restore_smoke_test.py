"""
PDF Mask / Restore Smoke Test

目的：
1. 建立一份不含文字層的虛構 PII 掃描 PDF。
2. 直接呼叫正式 handler_media.mask_pdf()。
3. 檢查：
   - masked PDF 是否成功產生
   - map 是否有資料
   - bbox / crop_b64 是否有效
   - masked PDF 的遮蔽區域是否真的與原 PDF 不同
4. 再呼叫正式 handler_media.restore_pdf()。
5. 檢查 restored PDF 是否成功產生，以及還原區域是否接近原始內容。

注意：
- 全部資料都是虛構資料。
- 這是 smoke test，不修改 handler_media.py。
"""

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

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

EXPECTED_LABELS = {
    "TW_PHONE",
    "EMAIL",
    "TW_ID",
    "ADDRESS",
    "IP_ADDRESS",
}

RENDER_ZOOM = 2.0


def get_test_font(size: int):
    for font_path in [
        Path("C:/Windows/Fonts/msjh.ttc"),
        Path("C:/Windows/Fonts/msjhbd.ttc"),
        Path("C:/Windows/Fonts/mingliu.ttc"),
    ]:
        if font_path.exists():
            return ImageFont.truetype(str(font_path), size)
    raise RuntimeError("找不到可用的繁體中文字型")


def create_source_image():
    image = Image.new("RGB", (1000, 420), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    font = get_test_font(32)

    x, y = 50, 40
    for line in GROUND_TRUTH_LINES:
        draw.text((x, y), line, font=font, fill=(0, 0, 0))
        y += int(32 * 1.7)

    return image


def save_scanned_pdf(image: Image.Image, pdf_path: Path):
    image.convert("RGB").save(pdf_path, "PDF", resolution=150.0)


def render_pdf_page(pdf_path: Path, zoom: float = RENDER_ZOOM):
    doc = fitz.open(str(pdf_path))
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=False)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    page_size = (page.rect.width, page.rect.height)
    doc.close()
    return image, page_size


def load_entries(map_path: Path, masked_pdf_path: Path):
    with map_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get(masked_pdf_path.name, [])


def bbox_valid(entry, page_width, page_height):
    bbox = entry.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4:
        return False

    x0, y0, x1, y1 = bbox
    return (
        0 <= x0 < x1 <= page_width
        and 0 <= y0 < y1 <= page_height
    )


def crop_b64_valid(entry):
    try:
        raw = base64.b64decode(entry["crop_b64"], validate=True)
        image = Image.open(io.BytesIO(raw))
        image.load()
        return image.width > 0 and image.height > 0
    except Exception:
        return False


def pdf_bbox_to_pixel_box(bbox, zoom, image):
    x0, y0, x1, y1 = bbox
    return (
        max(0, int(round(x0 * zoom))),
        max(0, int(round(y0 * zoom))),
        min(image.width, int(round(x1 * zoom))),
        min(image.height, int(round(y1 * zoom))),
    )


def region_difference_score(image_a, image_b, pixel_box):
    """
    回傳兩張圖在指定區域的平均 RGB 絕對差。
    0 表示完全相同；數字越大代表差異越明顯。
    """
    crop_a = image_a.crop(pixel_box)
    crop_b = image_b.crop(pixel_box)

    if crop_a.size != crop_b.size or crop_a.width == 0 or crop_a.height == 0:
        return 0.0

    diff = ImageChops.difference(crop_a, crop_b)
    histogram = diff.histogram()
    total = sum(value * count for value, count in enumerate(histogram))
    channels = 3
    pixels = crop_a.width * crop_a.height
    return total / (pixels * channels) if pixels else 0.0


def main():
    output_dir = Path(__file__).parent / "pdf_mask_restore_smoke_samples"
    output_dir.mkdir(exist_ok=True)

    source_pdf = output_dir / "source.pdf"
    masked_pdf = output_dir / "source_masked.pdf"
    restored_pdf = output_dir / "source_restored.pdf"
    map_file = output_dir / "media_deid_map.json"

    # 每次測試都重新建立，避免舊輸出干擾。
    for path in [source_pdf, masked_pdf, restored_pdf, map_file]:
        if path.exists():
            path.unlink()

    save_scanned_pdf(create_source_image(), source_pdf)

    print("=" * 92)
    print("PDF MASK / RESTORE SMOKE TEST")
    print("=" * 92)
    print(f"Source：{source_pdf}")

    # ---------------------------------------------------------
    # 1. 正式遮蔽流程
    # ---------------------------------------------------------
    entries_returned = handler_media.mask_pdf(
        str(source_pdf),
        str(masked_pdf),
        str(map_file),
    )

    masked_exists = masked_pdf.exists() and masked_pdf.stat().st_size > 0
    map_exists = map_file.exists() and map_file.stat().st_size > 0
    entries = load_entries(map_file, masked_pdf) if map_exists else []

    original_image, page_size = render_pdf_page(source_pdf)
    masked_image, _ = render_pdf_page(masked_pdf)

    page_width, page_height = page_size

    valid_bbox_count = sum(
        bbox_valid(entry, page_width, page_height)
        for entry in entries
    )
    valid_crop_count = sum(crop_b64_valid(entry) for entry in entries)

    labels = {entry.get("label") for entry in entries}
    expected_detected = len(EXPECTED_LABELS & labels)

    changed_regions = 0
    mask_diff_scores = []

    for entry in entries:
        if not bbox_valid(entry, page_width, page_height):
            continue
        pixel_box = pdf_bbox_to_pixel_box(
            entry["bbox"], RENDER_ZOOM, original_image
        )
        score = region_difference_score(
            original_image, masked_image, pixel_box
        )
        mask_diff_scores.append(score)
        if score > 1.0:
            changed_regions += 1

    print("\n--- MASK RESULT ---")
    print(f"mask_pdf() returned entries：{len(entries_returned)}")
    print(f"Masked PDF exists：{'PASS' if masked_exists else 'FAIL'}")
    print(f"Map exists：{'PASS' if map_exists else 'FAIL'}")
    print(f"Map entries：{len(entries)}")
    print(f"Expected PII labels detected：{expected_detected} / {len(EXPECTED_LABELS)}")
    print(f"Valid bbox：{valid_bbox_count} / {len(entries)}")
    print(f"Valid crop_b64：{valid_crop_count} / {len(entries)}")
    print(f"Visibly changed masked regions：{changed_regions} / {len(entries)}")

    # ---------------------------------------------------------
    # 2. 正式還原流程
    # ---------------------------------------------------------
    handler_media.restore_pdf(
        str(masked_pdf),
        str(restored_pdf),
        str(map_file),
    )

    restored_exists = restored_pdf.exists() and restored_pdf.stat().st_size > 0

    restored_regions_better = 0
    restore_scores = []

    if restored_exists:
        restored_image, _ = render_pdf_page(restored_pdf)

        for entry in entries:
            if not bbox_valid(entry, page_width, page_height):
                continue

            pixel_box = pdf_bbox_to_pixel_box(
                entry["bbox"], RENDER_ZOOM, original_image
            )

            masked_diff = region_difference_score(
                original_image, masked_image, pixel_box
            )
            restored_diff = region_difference_score(
                original_image, restored_image, pixel_box
            )
            restore_scores.append(restored_diff)

            # 還原後應比遮蔽後更接近原圖。
            if restored_diff < masked_diff:
                restored_regions_better += 1

    print("\n--- RESTORE RESULT ---")
    print(f"Restored PDF exists：{'PASS' if restored_exists else 'FAIL'}")
    print(
        f"Restored regions closer to original："
        f"{restored_regions_better} / {len(entries)}"
    )

    mask_pass = (
        masked_exists
        and map_exists
        and len(entries) > 0
        and len(entries_returned) == len(entries)
        and valid_bbox_count == len(entries)
        and valid_crop_count == len(entries)
        and changed_regions == len(entries)
    )

    restore_pass = (
        restored_exists
        and len(entries) > 0
        and restored_regions_better == len(entries)
    )

    print("\n" + "=" * 92)
    print("FINAL RESULT")
    print("=" * 92)
    print(f"MASK PIPELINE：{'PASS' if mask_pass else 'FAIL'}")
    print(f"RESTORE PIPELINE：{'PASS' if restore_pass else 'FAIL'}")
    print(
        "OVERALL："
        + ("PASS" if mask_pass and restore_pass else "FAIL")
    )
    print("=" * 92)

    if not (mask_pass and restore_pass):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
