#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TC-STR test split benchmark for the project's handler_media OCR backend.

No network, no model training, no changes to project files. Use separate
processes for each OCR engine; results contain aggregate metrics only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import re
import statistics
import sys
import time
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Item:
    relative_path: str
    label: str
    full_path: Path


def locate_dataset(p: Path) -> Path:
    p = p.resolve()
    if (p / 'test_labels.txt').is_file():
        return p
    if (p / 'TC-STR' / 'test_labels.txt').is_file():
        return p / 'TC-STR'
    raise ValueError(f'找不到 test_labels.txt：請將 --dataset 指到 TC-STR 解壓後的資料夾（目前：{p}）')


def split_label(line: str, number: int) -> tuple[str, str]:
    raw = line.rstrip('\r\n').lstrip('\ufeff')
    if '\t' in raw:
        rel, label = raw.split('\t', 1)
    else:
        # Some copies of the official label file use spaces, not tabs.
        # The greedy image extension match tolerates whitespace in paths.
        m = re.match(r'^(.+\.(?:png|jpe?g|bmp|webp))\s+(.+)$', raw, re.IGNORECASE)
        if not m:
            raise ValueError(f'test_labels.txt 第 {number} 行格式無法識別')
        rel, label = m.group(1), m.group(2)
    rel = rel.strip().replace('\\', '/')
    label = label.strip()
    if not rel or not label:
        raise ValueError(f'test_labels.txt 第 {number} 行缺少路徑或標註文字')
    return rel, label


def read_items(root: Path) -> list[Item]:
    items: list[Item] = []
    label_file = root / 'test_labels.txt'
    for i, raw in enumerate(label_file.read_text(encoding='utf-8-sig').splitlines(), 1):
        if not raw.strip():
            continue
        rel, label = split_label(raw, i)
        target = (root / rel).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError(f'資料集標註包含不允許的外部路徑：第 {i} 行')
        if not target.is_file():
            raise FileNotFoundError(f'缺少測試圖片（第 {i} 行）：{rel}')
        items.append(Item(rel, label, target))
    if not items:
        raise ValueError('找不到任何 test_labels.txt 測試項目')
    return items


def norm(text: str, *, compact: bool = False) -> str:
    text = unicodedata.normalize('NFC', text).strip()
    if compact:
        text = ''.join(text.split())
    return text


def edit_distance(a: str, b: str) -> int:
    if len(a) > len(b):
        a, b = b, a
    row = list(range(len(a) + 1))
    for i, cb in enumerate(b, 1):
        next_row = [i]
        for j, ca in enumerate(a, 1):
            next_row.append(min(row[j] + 1, next_row[j-1] + 1,
                                row[j-1] + (ca != cb)))
        row = next_row
    return row[-1]


def join_ocr_words(words: list[dict]) -> str:
    """TC-STR crops usually contain one word/short line. Preserve word order,
    joining split tokens without invented whitespace, which is suitable for
    Chinese cropped-word recognition. Both engines use the same rule.
    """
    if not isinstance(words, list):
        raise TypeError('ocr_words() 應回傳 list[dict]')
    for w in words:
        if not isinstance(w, dict) or 'text' not in w:
            raise TypeError('OCR 回傳格式不是具 text 欄位的 dict')
    ordered = sorted(enumerate(words),
                     key=lambda iw: (str(iw[1].get('line_id', 0)),
                                     float(iw[1].get('top', 0)),
                                     float(iw[1].get('left', 0)), iw[0]))
    return ''.join(str(w.get('text') or '') for _, w in ordered)


def percentile_p95(values: list[float]) -> float:
    return sorted(values)[min(len(values)-1, int(0.95 * (len(values)-1) + .999999))]


def run(args: argparse.Namespace) -> int:
    root = locate_dataset(Path(args.dataset))
    items = read_items(root)
    labels_sha = hashlib.sha256((root / 'test_labels.txt').read_bytes()).hexdigest()
    if args.limit < 0:
        raise ValueError('--limit 必須為 0（完整測試）或正整數')
    k = len(items) if args.limit == 0 else min(args.limit, len(items))
    indices = sorted(random.Random(args.seed).sample(range(len(items)), k))
    subset_digest = hashlib.sha256('\n'.join(
        f'{i}\t{items[i].relative_path}\t{items[i].label}' for i in indices
    ).encode('utf-8')).hexdigest()
    print(f'[TC-STR] test_labels={len(items)} 筆；此次取樣={k} 筆；seed={args.seed}')
    print(f'[TC-STR] 選樣 SHA256={subset_digest}')
    print(f'[TC-STR] 資料夾：{root}')
    if args.check_only:
        print('[CHECK_PASS] 測試圖片與標註檔存在；尚未呼叫 OCR、未建立報告')
        return 0
    if not args.engine:
        raise ValueError('正式測試必須指定 --engine rapid 或 --engine windows')
    out = Path(args.output).resolve()
    if out.exists():
        raise FileExistsError(f'報告已存在，拒絕覆寫：{out}')
    if out == (root / 'test_labels.txt').resolve():
        raise ValueError('輸出路徑與標註檔衝突')
    out.parent.mkdir(parents=True, exist_ok=True)
    # Import after selecting the engine. This uses the project's exact OCR
    # adapter, not an independently configured RapidOCR model.
    os.environ['PII_OCR_BACKEND'] = args.engine
    try:
        from PIL import Image
        from handler_media import ocr_words, preprocess_for_ocr
    except Exception as exc:
        raise RuntimeError('無法載入專題的 handler_media OCR 流程。請在 presidio_app_allen 根目錄執行，並確認 .venv 套件已安裝') from exc

    total_ref_chars = 0
    total_edits = 0
    exact = 0
    compact_chars = 0
    compact_edits = 0
    compact_exact = 0
    empty_predictions = 0
    durations: list[float] = []
    started = time.perf_counter()
    for n, idx in enumerate(indices, 1):
        item = items[idx]
        try:
            with Image.open(item.full_path) as image:
                img = image.convert('RGB')
            t0 = time.perf_counter()
            if not args.no_preprocess:
                img, _ = preprocess_for_ocr(img)
            recognized = join_ocr_words(ocr_words(img))
            durations.append(time.perf_counter() - t0)
        except Exception as exc:
            raise RuntimeError(f'第 {n}/{k} 筆 OCR 發生例外（原始標註檔第 {idx+1} 項）：{type(exc).__name__}: {exc}') from exc
        reference, prediction = norm(item.label), norm(recognized)
        total_ref_chars += len(reference)
        total_edits += edit_distance(reference, prediction)
        exact += int(reference == prediction)
        reference_c, prediction_c = norm(item.label, compact=True), norm(recognized, compact=True)
        compact_chars += len(reference_c)
        compact_edits += edit_distance(reference_c, prediction_c)
        compact_exact += int(reference_c == prediction_c)
        empty_predictions += int(not prediction)
        if n == 1 or n % 25 == 0 or n == k:
            print(f'[進度] {n}/{k}，正確 {exact}，目前 CER={total_edits / max(total_ref_chars,1):.4f}')

    results = {
        'description': 'TC-STR 7k-word held-out test OCR benchmark (project pipeline)',
        'dataset': 'TC-STR 7k-word',
        'split': 'test',
        'sample_size': k,
        'test_set_total': len(items),
        'sample_seed': args.seed,
        'labels_sha256': labels_sha,
        'sample_selection_sha256': subset_digest,
        'engine_requested': args.engine,
        'engine_selector': 'PII_OCR_BACKEND',
        'pipeline': 'handler_media.preprocess_for_ocr + handler_media.ocr_words',
        'preprocess': not args.no_preprocess,
        'text_order': 'line_id, top, left; concatenate tokens without inserted spaces',
        'primary_normalization': 'Unicode NFC + outer whitespace trim; no Traditional/Simplified conversion',
        'strict': {
            'exact_count': exact,
            'exact_match_rate': exact / k,
            'character_edit_operations': total_edits,
            'reference_characters': total_ref_chars,
            'cer': total_edits / max(total_ref_chars, 1),
        },
        'supplementary_whitespace_ignored': {
            'exact_count': compact_exact,
            'exact_match_rate': compact_exact / k,
            'character_edit_operations': compact_edits,
            'reference_characters': compact_chars,
            'cer': compact_edits / max(compact_chars, 1),
        },
        'empty_predictions': empty_predictions,
        'latency_seconds': {
            'median_per_image': statistics.median(durations),
            'mean_per_image': statistics.mean(durations),
            'p95_per_image': percentile_p95(durations),
            'total_timed': sum(durations),
            'total_wall_including_import': time.perf_counter()-started,
            'first_call_may_include_model_initialization': True,
        },
        'limitations': [
            'TC-STR images are cropped text, not whole-page OCR nor a PII detection benchmark.',
            'Metrics evaluate OCR localization+recognition as executed by project handler_media, not an isolated recognition network.',
            'The same fixed sample selection and evaluation policy are needed for cross-engine comparison.',
            'These test images may not represent scanned PDFs or sensitive field detection performance.',
        ],
    }
    # x mode prevents overwrites, including races from another local process.
    with out.open('x', encoding='utf-8') as handle:
        json.dump(results, handle, ensure_ascii=False, indent=2)
    print('\n=== TC-STR OCR 測試結果 ===')
    print(f'引擎: {args.engine}; 圖片: {k}')
    print(f'Exact Match: {exact}/{k} = {exact/k:.2%}')
    print(f'CER: {total_edits / max(total_ref_chars,1):.2%}')
    print(f'忽略空白 Exact: {compact_exact}/{k} = {compact_exact/k:.2%}')
    print(f'空辨識: {empty_predictions}; 每張耗時中位數: {statistics.median(durations):.3f}s')
    print(f'報告：{out}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description='TC-STR test split：專題 OCR 基準測試（不訓練模型）')
    parser.add_argument('--dataset', required=True, help='TC-STR 根目錄，內含 test_labels.txt')
    parser.add_argument('--engine', choices=['rapid', 'windows'], help='使用 PII_OCR_BACKEND 切換引擎')
    parser.add_argument('--limit', type=int, default=100, help='抽測張數；0 = 完整測試集（預設 100）')
    parser.add_argument('--seed', type=int, default=42, help='可重現隨機樣本種子（預設 42）')
    parser.add_argument('--output', help='JSON 報告輸出路徑（拒絕覆寫）')
    parser.add_argument('--no-preprocess', action='store_true', help='不使用專題影像前處理；兩引擎需保持一致')
    parser.add_argument('--check-only', action='store_true', help='只檢查資料集，不呼叫 OCR')
    args = parser.parse_args()
    try:
        if not args.check_only and not args.output:
            parser.error('正式評估必須指定 --output，避免覆寫既有報告')
        return run(args)
    except Exception as exc:
        print(f'[FAILED] {type(exc).__name__}: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
