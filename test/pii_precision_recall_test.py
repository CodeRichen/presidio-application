"""
t1.json 的 PII Precision / Recall / F1 評估工具。

用途：
1. 讀取 presidio/t1.json 的 article + answer 作為 Ground Truth。
2. 呼叫目前專案的 handler_media.detect_matches()。
3. 用「字元範圍有重疊」配對預測與標準答案。
4. 同時計算：
   - Span 指標：只要敏感範圍抓對就算 TP，不要求類別完全一致。
   - Strict 指標：範圍重疊且類別相容才算 TP。
5. 印出 FP / FN，方便定位誤報與漏報。

執行位置：專案根目錄
    python test/pii_precision_recall_test.py
"""

import json
import sys
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# IMPORTANT:
# 與 main.py 保持相同的載入順序：先載入 handler_text（會初始化 Torch /
# Transformers 中文 NER），再載入 handler_media（會載入 Pillow / OCR 相關模組）。
# 在目前 Windows 環境中反過來載入，可能造成 torch c10.dll 的 WinError 1114。
import handler_text   # noqa: E402,F401
import handler_media  # noqa: E402


JSON_PATH = ROOT / "presidio" / "t1.json"

# Ground Truth 與目前偵測器的名稱不完全相同，因此只做必要的同義類別映射。
LABEL_ALIASES = {
    "PERSON_NAME": {"PERSON"},
    "PHONE_NUMBER": {"TW_PHONE", "PHONE_NUMBER", "US_PHONE"},
    "EMAIL": {"EMAIL", "EMAIL_ADDRESS"},
    "TW_ID": {"TW_ID"},
    "ADDRESS": {"ADDRESS", "LOCATION"},
    "IP_ADDRESS": {"IP_ADDRESS"},
    "BANK_ACCOUNT": {"BANK_ACCOUNT"},
    "DATE_OF_BIRTH": {"DATE_OF_BIRTH", "DATE_TIME"},
    "DATE": {"DATE_TIME", "DATE_OF_BIRTH"},
}

# 這些 Ground Truth 類別目前沒有明確的一對一偵測 label。
# Strict 評估仍會列為 FN；Span 評估則可看「至少有沒有遮到」。
# 不在這裡硬把它們映射到 CREDENTIAL_LIKE，避免把錯誤分類美化成正確。
UNMAPPED_GT_LABELS = {
    "LICENSE_NUMBER",
    "MEDICAL_RECORD_NUMBER",
    "INSURANCE_POLICY_NUMBER",
    "BANK_NAME",
    "EMPLOYEE_ID",
    "CASE_NUMBER",
}


def find_all_occurrences(text: str, value: str):
    """回傳 value 在 text 中所有出現位置。"""
    out = []
    start = 0
    while True:
        idx = text.find(value, start)
        if idx < 0:
            break
        out.append((idx, idx + len(value)))
        start = idx + 1
    return out


def build_ground_truth(article: str, answers):
    """
    將 t1.json answer 轉成帶 start/end 的 Ground Truth。
    同一文字若出現多次（例如王志明），依 answer 出現次數依序配置。
    """
    occurrence_cache = {}
    used_count = Counter()
    gt = []

    for value, label in answers:
        if value not in occurrence_cache:
            occurrence_cache[value] = find_all_occurrences(article, value)

        occurrences = occurrence_cache[value]
        nth = used_count[value]

        if nth >= len(occurrences):
            print(f"[警告] Ground Truth 找不到第 {nth + 1} 次出現：{value!r} ({label})")
            continue

        start, end = occurrences[nth]
        used_count[value] += 1
        gt.append({
            "start": start,
            "end": end,
            "label": label,
            "text": value,
        })

    return gt


def overlap_len(a, b):
    return max(0, min(a["end"], b["end"]) - max(a["start"], b["start"]))


def labels_compatible(gt_label: str, pred_label: str):
    if gt_label == pred_label:
        return True
    return pred_label in LABEL_ALIASES.get(gt_label, set())


def make_predictions(article: str):
    raw = handler_media.detect_matches(article)
    preds = []
    for start, end, label, score, source in raw:
        preds.append({
            "start": start,
            "end": end,
            "label": label,
            "score": score,
            "source": source,
            "text": article[start:end],
        })
    return preds


def greedy_match(gt, preds, strict: bool):
    """
    一個 GT 最多配一個 prediction，一個 prediction 也最多配一個 GT。
    優先配對重疊字元數最多者。
    strict=False：只看 span overlap。
    strict=True：還要求 label 相容。
    """
    candidates = []

    for gi, g in enumerate(gt):
        for pi, p in enumerate(preds):
            ov = overlap_len(g, p)
            if ov <= 0:
                continue
            if strict and not labels_compatible(g["label"], p["label"]):
                continue

            # 第二排序依據：重疊比例越高越優先
            union = max(g["end"], p["end"]) - min(g["start"], p["start"])
            iou = ov / union if union else 0.0
            candidates.append((ov, iou, gi, pi))

    candidates.sort(reverse=True)

    matched_g = set()
    matched_p = set()
    pairs = []

    for ov, iou, gi, pi in candidates:
        if gi in matched_g or pi in matched_p:
            continue
        matched_g.add(gi)
        matched_p.add(pi)
        pairs.append((gi, pi, ov, iou))

    tp = len(pairs)
    fp = len(preds) - tp
    fn = len(gt) - tp

    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )

    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "pairs": pairs,
        "matched_g": matched_g,
        "matched_p": matched_p,
    }


def print_metrics(title, result):
    print(f"\n===== {title} =====")
    print(f"TP        : {result['tp']}")
    print(f"FP        : {result['fp']}")
    print(f"FN        : {result['fn']}")
    print(f"Precision : {result['precision']:.2%}")
    print(f"Recall    : {result['recall']:.2%}")
    print(f"F1-score  : {result['f1']:.2%}")


def main():
    with JSON_PATH.open("r", encoding="utf-8") as f:
        data = json.load(f)

    article = data["article"]
    answers = data["answer"]

    gt = build_ground_truth(article, answers)
    preds = make_predictions(article)

    print(f"Ground Truth 數量：{len(gt)}")
    print(f"系統預測數量      ：{len(preds)}")

    span_result = greedy_match(gt, preds, strict=False)
    strict_result = greedy_match(gt, preds, strict=True)

    print_metrics("Span 評估（有抓到敏感範圍即可）", span_result)
    print_metrics("Strict 評估（範圍 + 類別都要相容）", strict_result)

    print("\n===== Strict False Positive（誤報 / 類別錯誤）=====")
    unmatched_preds = [
        (i, p) for i, p in enumerate(preds)
        if i not in strict_result["matched_p"]
    ]
    if not unmatched_preds:
        print("無")
    else:
        for _, p in unmatched_preds:
            score = "規則" if p["score"] is None else f"{p['score']:.2f}"
            print(
                f"FP  {p['label']:<22} "
                f"{p['text']!r} "
                f"[{p['start']}:{p['end']}] "
                f"來源={p['source']} score={score}"
            )

    print("\n===== Strict False Negative（漏報 / 類別錯誤）=====")
    unmatched_gt = [
        (i, g) for i, g in enumerate(gt)
        if i not in strict_result["matched_g"]
    ]
    if not unmatched_gt:
        print("無")
    else:
        for _, g in unmatched_gt:
            note = " [目前無類別映射]" if g["label"] in UNMAPPED_GT_LABELS else ""
            print(
                f"FN  {g['label']:<24} "
                f"{g['text']!r} "
                f"[{g['start']}:{g['end']}]{note}"
            )

    print("\n===== 系統所有預測 =====")
    for p in preds:
        score = "規則" if p["score"] is None else f"{p['score']:.2f}"
        print(
            f"{p['label']:<22} "
            f"{p['text']!r} "
            f"[{p['start']}:{p['end']}] "
            f"來源={p['source']} score={score}"
        )

    print("\n注意：")
    print("1. 這支測試直接評估 t1.json 的原始文字，不經 OCR。")
    print("2. 因此它主要量 PII detector / 規則 / NER 的 Precision、Recall。")
    print("3. 圖片 OCR 的錯字、掉字、Bounding Box 問題要另外用 OCR 測試衡量。")
    print("4. Strict 指標比 Span 指標嚴格；兩者差距大通常代表『有抓到，但類別判錯』。")


if __name__ == "__main__":
    main()
