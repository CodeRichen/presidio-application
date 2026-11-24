"""Offline before/after PII-detection benchmark on the existing 36 synthetic images.

Loads RapidOCR only ONCE per image; measures *only* PII processing.
Does not modify handler_media.py, unless separately installed using install_pii_cache.py.
Does not write raw OCR text, PII spans, or reversible crops into reports.
"""
from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
from pathlib import Path
import statistics
import sys
import time
from PIL import Image

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
BENCH_DIR = PROJECT / 'ocr_ab_benchmark_36'


def median(values):
    return statistics.median(values) if values else None


def run(argv=None):
    ap = argparse.ArgumentParser(description='RapidOCR + PII no-cache vs scoped-cache A/B')
    ap.add_argument('--limit', type=int, default=4, help='前 N 張圖片；0=全部36張')
    ap.add_argument('--repeat', type=int, default=1, help='每張圖片執行幾次 A/B（預設1）')
    ap.add_argument('--output', type=Path, default=HERE / 'cache_results')
    ap.add_argument('--show-logs', action='store_true', help='顯示 PII 模型原始日誌（可能含合成姓名）')
    args = ap.parse_args(argv)
    if args.limit < 0 or args.repeat < 1:
        ap.error('limit 必須 >=0，repeat 必須 >=1')
    if not (PROJECT / 'handler_media.py').exists():
        ap.error(f'找不到 {PROJECT / "handler_media.py"}')
    labels_path = BENCH_DIR / 'data' / 'labels.json'
    if not labels_path.is_file():
        ap.error('找不到 OCR Benchmark 測試資料，請先解壓縮 ocr_ab_benchmark_36 到專案根目錄')

    with labels_path.open(encoding='utf-8') as fh:
        cases = json.load(fh)['cases']
    if args.limit:
        cases = cases[:args.limit]
    args.output.mkdir(parents=True, exist_ok=True)

    sys.path.insert(0, str(PROJECT))
    sys.path.insert(0, str(BENCH_DIR))
    os.environ['PII_OCR_BACKEND'] = 'rapid'
    import handler_media
    from ocr_backend import rapid_ocr_words
    from benchmark_lib import score_one_case

    if not hasattr(handler_media, '_pii_media_cache_var'):
        ap.error('尚未安裝 PII cache 補丁。先執行 install_pii_cache.py --project . --apply')

    data_dir = BENCH_DIR / 'data'
    padding = handler_media.MASK_PADDING
    out_rows = []
    mismatch_rows = []
    class _Discard:
        def write(self, message):
            return len(message)
        def flush(self):
            pass
    log_context = contextlib.nullcontext() if args.show_logs else contextlib.redirect_stdout(_Discard())
    old_setting = os.environ.get('PII_MEDIA_CACHE')
    old_stats = os.environ.get('PII_MEDIA_CACHE_STATS')
    os.environ['PII_MEDIA_CACHE_STATS'] = '0'
    try:
        # Warm-up both branches after importing model, not counted.
        if cases:
            first = Image.open(data_dir / cases[0]['image']).convert('RGB')
            proc, scale = handler_media.preprocess_for_ocr(first)
            words = rapid_ocr_words(proc)
            for mode in ('0', '1'):
                os.environ['PII_MEDIA_CACHE'] = mode
                with log_context:
                    handler_media.find_sensitive_boxes_in_words(words, scale=scale)

        for ci, case in enumerate(cases):
            case_id = case['id']
            image = Image.open(data_dir / case['image']).convert('RGB')
            proc, scale = handler_media.preprocess_for_ocr(image)
            words = rapid_ocr_words(proc)
            mode_data = {'baseline': [], 'cached': []}
            for repeat in range(args.repeat):
                modes = [('baseline', '0'), ('cached', '1')]
                if (ci + repeat) % 2:
                    modes.reverse()  # alternate to limit order bias
                for label, mode in modes:
                    os.environ['PII_MEDIA_CACHE'] = mode
                    started = time.perf_counter()
                    with log_context:
                        boxes = handler_media.find_sensitive_boxes_in_words(words, scale=scale)
                    elapsed = time.perf_counter() - started
                    scored = score_one_case(image, case, data_dir, words, boxes, padding, 0.98)
                    flags = {target['id']: bool(target['fully_covered']) for target in scored['targets']}
                    record = {
                        'elapsed': elapsed,
                        'boxes': boxes,
                        'flags': flags,
                        'safe_fraction': scored['safe_ink_covered_fraction'],
                    }
                    mode_data[label].append(record)

            base = mode_data['baseline'][0]
            cached = mode_data['cached'][0]
            boxes_identical = all(r['boxes'] == base['boxes'] for lst in mode_data.values() for r in lst)
            flag_identical = all(r['flags'] == base['flags'] for lst in mode_data.values() for r in lst)
            safe_identical = all(r['safe_fraction'] == base['safe_fraction'] for lst in mode_data.values() for r in lst)
            base_time = median([r['elapsed'] for r in mode_data['baseline']])
            cache_time = median([r['elapsed'] for r in mode_data['cached']])
            row = {
                'case': case_id, 'difficulty': case['condition'],
                'baseline_pii_seconds': round(base_time, 4),
                'cached_pii_seconds': round(cache_time, 4),
                'speedup': round(base_time / cache_time, 3) if cache_time > 0 else None,
                'boxes_identical': boxes_identical,
                'target_flags_identical': flag_identical,
                'safe_loss_identical': safe_identical,
                'target_covered': sum(base['flags'].values()),
                'target_count': len(base['flags']),
            }
            out_rows.append(row)
            if not (boxes_identical and flag_identical and safe_identical):
                mismatch_rows.append(case_id)
            print(
                f"{ci+1:02d}/{len(cases):02d} {case_id:<22} "
                f"PII {base_time:.3f}s → {cache_time:.3f}s "
                f"{row['speedup']:.2f}×  "
                f"{'PASS' if row['boxes_identical'] and row['target_flags_identical'] and row['safe_loss_identical'] else 'FAIL'}"
            )
    finally:
        if old_setting is None:
            os.environ.pop('PII_MEDIA_CACHE', None)
        else:
            os.environ['PII_MEDIA_CACHE'] = old_setting
        if old_stats is None:
            os.environ.pop('PII_MEDIA_CACHE_STATS', None)
        else:
            os.environ['PII_MEDIA_CACHE_STATS'] = old_stats

    if not out_rows:
        ap.error('沒有測試案例')
    csv_path = args.output / 'per_case.csv'
    with csv_path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(out_rows[0]))
        writer.writeheader()
        writer.writerows(out_rows)
    base_m = median([r['baseline_pii_seconds'] for r in out_rows])
    cached_m = median([r['cached_pii_seconds'] for r in out_rows])
    covered = sum(r['target_covered'] for r in out_rows)
    total = sum(r['target_count'] for r in out_rows)
    by_cond = {}
    for r in out_rows:
        by_cond.setdefault(r['difficulty'], []).append(r)
    lines = [
        '# handler_media.py PII 快取 A/B 結果', '',
        f'圖片：{len(out_rows)}；同一圖片 OCR 只執行一次、A/B 共用辨識詞和座標。',
        f'重複測量：每張每模式 {args.repeat} 次（每張交替 A/B 順序）。',
        '', '| 指標 | 無快取 baseline | 單張/單頁快取 |',
        '|---|---:|---:|',
        f'| PII 部分中位耗時 | {base_m:.3f}s | {cached_m:.3f}s |',
        f'| PII 部分加速比（中位耗時比） | 1.00× | {base_m / cached_m:.2f}× |',
        f'| 標註字形遮蔽（兩模式相同） | {covered}/{total} | {covered}/{total} |',
        f'| 全部 bbox／label／值／score／source 完全一致圖片 | {len(out_rows) - len(mismatch_rows)}/{len(out_rows)} | {len(out_rows) - len(mismatch_rows)}/{len(out_rows)} |',
        '', '## 各難度 PII 偵測耗時（秒，中位數）', '',
        '| 難度 | Baseline | Cached | 加速比 |', '|---|---:|---:|---:|',
    ]
    for cond, items in sorted(by_cond.items()):
        b = median([r['baseline_pii_seconds'] for r in items])
        c = median([r['cached_pii_seconds'] for r in items])
        lines.append(f'| {cond} | {b:.3f} | {c:.3f} | {b / c:.2f}× |')
    lines += [
        '', '## 安全門檻', '',
        '**PASS**：每張 baseline/cached 的原始遮蔽框清單完全一致（包含座標、標籤、文字、score、來源），且每筆標註遮蔽狀態與非敏感文字遮蔽率一致。',
        '此比較不保證真實文件安全；若遮蔽框不同，即使兩者都是144/144也視為不通過。',
        '', f'不一致案例：{", ".join(mismatch_rows) if mismatch_rows else "無"}',
        '', '報告只存案例 ID 與統計值，不存 OCR 辨識原文或可還原裁切。',
    ]
    md_path = args.output / 'pii_cache_summary.md'
    md_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('\n結論:', 'PASS — 所有框完全一致' if not mismatch_rows else 'FAIL — 有框或覆蓋差異')
    print('PII 中位耗時:', f'{base_m:.3f}s → {cached_m:.3f}s ({base_m/cached_m:.2f}×)')
    print('報告:', md_path)
    if mismatch_rows:
        raise SystemExit(2)

if __name__ == '__main__':
    run()
