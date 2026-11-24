#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate Canva-ready comparison from two TC-STR OCR benchmark JSON reports."""
import argparse
import json
from pathlib import Path
import sys


def load(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


def main():
    p = argparse.ArgumentParser(description='TC-STR two-engine result comparison')
    p.add_argument('--rapid', required=True)
    p.add_argument('--windows', required=True)
    p.add_argument('--output', required=True)
    a = p.parse_args()
    r, w = load(a.rapid), load(a.windows)
    for key in ['dataset', 'split', 'sample_size', 'labels_sha256',
                'sample_selection_sha256', 'preprocess', 'text_order', 'primary_normalization']:
        if r.get(key) != w.get(key):
            raise ValueError(f'兩份測試條件不一致，不可比較：{key}')
    if r['engine_requested'] != 'rapid' or w['engine_requested'] != 'windows':
        raise ValueError('報告引擎標籤不符合 --rapid 與 --windows')
    fmt_percent = lambda x: f'{x * 100:.2f}%'
    rr, ww = r['strict'], w['strict']
    lines = [
        '# TC-STR OCR Benchmark（可貼入 Canva）',
        '',
        f"測試資料：TC-STR 7k-word / test；{r['sample_size']} 張相同圖片；seed={r['sample_seed']}。",
        '',
        '| 指標 | RapidOCR | Windows OCR |',
        '|---|---:|---:|',
        f"| Exact Match | {fmt_percent(rr['exact_match_rate'])} | {fmt_percent(ww['exact_match_rate'])} |",
        f"| CER（越低越好） | {fmt_percent(rr['cer'])} | {fmt_percent(ww['cer'])} |",
        f"| 完整正確張數 | {rr['exact_count']}/{r['sample_size']} | {ww['exact_count']}/{w['sample_size']} |",
        f"| 空辨識張數 | {r['empty_predictions']} | {w['empty_predictions']} |",
        f"| 單張處理時間中位數 | {r['latency_seconds']['median_per_image']:.3f} 秒 | {w['latency_seconds']['median_per_image']:.3f} 秒 |",
        '',
        '評估方法：使用專題的 handler_media OCR 流程，按相同圖片與相同字元比對政策計算。',
        'CER 以整批字元編輯距離加總／Ground Truth 字元數加總計算。',
        '報告的處理時間包含影像前處理與 OCR 推論；不包含資料下載或 GUI 操作。',
        '注意：TC-STR 為裁切文字辨識測試，不等於整頁 PDF 的 PII 遮蔽安全性。',
    ]
    out = Path(a.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('x', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    print('\n'.join(lines))
    print(f'\n已建立：{out}')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'[FAILED] {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(2)
