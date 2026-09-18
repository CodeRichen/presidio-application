"""
OCR -> PII Detection Regression Test

目的：
驗證圖片經過正式 OCR preprocessing 後，
PII 是否能成功偵測並產生有效 bounding box。

find_sensitive_boxes_in_words() 回傳格式：

(
    x0,
    y0,
    x1,
    y1,
    label,
    text,
    score,
    source,
)

目前純文字 detector 可辨識：
- TW_PHONE
- EMAIL
- TW_ID
- ADDRESS
- IP_ADDRESS

PERSON 目前依賴中文 NER，
因此不納入本次 OCR PII Recall。
"""

import sys
from pathlib import Path

from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import handler_media


# =========================================================
# 測試設定
# =========================================================

SAMPLE_DIR = PROJECT_ROOT / "test" / "ocr_samples"

TEST_CASES = [
    "clear",
    "small_text",
    "low_resolution",
    "low_contrast",
    "blurred",
]

EXPECTED_LABELS = {
    "TW_PHONE",
    "EMAIL",
    "TW_ID",
    "ADDRESS",
    "IP_ADDRESS",
}


# =========================================================
# Bounding Box Helper
# =========================================================

def unpack_box(box):
    """
    handler_media.find_sensitive_boxes_in_words()
    回傳：

    x0, y0, x1, y1,
    label, text, score, source
    """

    (
        x0,
        y0,
        x1,
        y1,
        label,
        text,
        score,
        source,
    ) = box

    return {
        "x0": x0,
        "y0": y0,
        "x1": x1,
        "y1": y1,
        "label": label,
        "text": text,
        "score": score,
        "source": source,
    }


def has_valid_coordinates(box):
    """
    確認 bounding box 座標有效。

    有效條件：
    x1 > x0
    y1 > y0
    """

    data = unpack_box(box)

    return (
        data["x1"] > data["x0"]
        and data["y1"] > data["y0"]
    )


# =========================================================
# 單一測試案例
# =========================================================

def run_case(case_name: str):

    image_path = (
        SAMPLE_DIR
        / f"{case_name}.png"
    )

    if not image_path.exists():
        raise FileNotFoundError(
            f"找不到測試圖片：{image_path}\n"
            "請先執行 ocr_accuracy_test.py。"
        )

    image = Image.open(
        image_path
    ).convert("RGB")

    # 使用正式 preprocessing
    processed, scale = (
        handler_media.preprocess_for_ocr(
            image
        )
    )

    # Windows OCR
    words = handler_media.ocr_words(
        processed
    )

    # 正式 PII + bounding box pipeline
    boxes = (
        handler_media.find_sensitive_boxes_in_words(
            words,
            scale=scale,
        )
    )

    detected_labels = set()

    valid_box_count = 0

    unpacked_boxes = []

    for box in boxes:

        data = unpack_box(
            box
        )

        unpacked_boxes.append(
            data
        )

        detected_labels.add(
            data["label"]
        )

        if has_valid_coordinates(
            box
        ):
            valid_box_count += 1

    found = (
        EXPECTED_LABELS
        & detected_labels
    )

    missing = (
        EXPECTED_LABELS
        - detected_labels
    )

    return {
        "case": case_name,
        "scale": scale,
        "word_count": len(words),
        "boxes": boxes,
        "unpacked_boxes": unpacked_boxes,
        "box_count": len(boxes),
        "valid_box_count": valid_box_count,
        "detected_labels": detected_labels,
        "found": found,
        "missing": missing,
    }


# =========================================================
# Main
# =========================================================

def main():

    print("=" * 85)
    print("OCR -> PII DETECTION REGRESSION TEST")
    print("=" * 85)

    all_results = []

    total_expected = 0
    total_found = 0

    for case_name in TEST_CASES:

        result = run_case(
            case_name
        )

        all_results.append(
            result
        )

        expected_count = len(
            EXPECTED_LABELS
        )

        found_count = len(
            result["found"]
        )

        total_expected += (
            expected_count
        )

        total_found += (
            found_count
        )

        print("\n")
        print("=" * 85)
        print(
            f"CASE：{case_name}"
        )
        print("=" * 85)

        print(
            f"OCR scale："
            f"{result['scale']:.2f}"
        )

        print(
            f"OCR words："
            f"{result['word_count']}"
        )

        print(
            f"PII boxes："
            f"{result['box_count']}"
        )

        print(
            f"Valid boxes："
            f"{result['valid_box_count']}"
        )

        # -------------------------------------------------
        # Detection
        # -------------------------------------------------

        print("\n--- Detection Result ---")

        for label in sorted(
            EXPECTED_LABELS
        ):

            if label in result["found"]:
                status = "PASS"
            else:
                status = "MISS"

            print(
                f"{label:<20}"
                f"{status}"
            )

        # -------------------------------------------------
        # Bounding boxes
        # -------------------------------------------------

        print("\n--- Detected Boxes ---")

        if not result["unpacked_boxes"]:

            print(
                "(no boxes)"
            )

        else:

            for box in (
                result["unpacked_boxes"]
            ):

                valid = (
                    box["x1"] > box["x0"]
                    and box["y1"] > box["y0"]
                )

                print(
                    f"{box['label']:<15} "
                    f"text={box['text']!r} "
                    f"box=("
                    f"{box['x0']:.1f}, "
                    f"{box['y0']:.1f}, "
                    f"{box['x1']:.1f}, "
                    f"{box['y1']:.1f}) "
                    f"valid={valid}"
                )

    # =====================================================
    # Recall Matrix
    # =====================================================

    print("\n")
    print("=" * 85)
    print("PII DETECTION RECALL MATRIX")
    print("=" * 85)

    labels = [
        "TW_PHONE",
        "EMAIL",
        "TW_ID",
        "ADDRESS",
        "IP_ADDRESS",
    ]

    header = (
        f"{'Case':<18}"
    )

    for label in labels:

        header += (
            f"{label:>13}"
        )

    print(header)

    print("-" * 85)

    for result in all_results:

        row = (
            f"{result['case']:<18}"
        )

        for label in labels:

            if label in result["found"]:
                mark = "PASS"
            else:
                mark = "MISS"

            row += (
                f"{mark:>13}"
            )

        print(row)

    print("-" * 85)

    recall = (
        total_found
        / total_expected
        if total_expected
        else 0.0
    )

    print(
        f"Detected："
        f"{total_found} / "
        f"{total_expected}"
    )

    print(
        f"PII Recall："
        f"{recall * 100:.2f}%"
    )

    # =====================================================
    # Bounding Box Summary
    # =====================================================

    total_boxes = sum(
        result["box_count"]
        for result in all_results
    )

    total_valid_boxes = sum(
        result["valid_box_count"]
        for result in all_results
    )

    print("\n")
    print("=" * 85)
    print("BOUNDING BOX CHECK")
    print("=" * 85)

    print(
        f"Total boxes："
        f"{total_boxes}"
    )

    print(
        f"Valid boxes："
        f"{total_valid_boxes}"
    )

    if total_boxes == 0:

        print(
            "Bounding Box Result："
            "FAIL（沒有產生 bounding box）"
        )

    elif (
        total_boxes
        == total_valid_boxes
    ):

        print(
            "Bounding Box Result：PASS"
        )

    else:

        print(
            "Bounding Box Result："
            "FAIL（存在無效 bounding box）"
        )

    print("=" * 85)


if __name__ == "__main__":
    main()