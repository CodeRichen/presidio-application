#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize actual UI button-to-list completion measurements (no raw PII)."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def main(argv=None):
    ap=argparse.ArgumentParser()
    base=Path(__file__).resolve().parent
    ap.add_argument('--input',type=Path,default=base/'results/ui_timing.jsonl')
    ap.add_argument('--output',type=Path,default=base/'results/ui_timing_summary.md')
    args=ap.parse_args(argv)
    if not args.input.is_file():ap.error('找不到 UI 測速報告；先執行 launch_ui_timed.py 並使用「開始處理」')
    batches=defaultdict(list)
    for line in args.input.read_text(encoding='utf-8').splitlines():
        if not line.strip():continue
        item=json.loads(line)
        if item.get('status')!='OK':continue
        file_types=item.get('file_types',{})
        label=', '.join(f'{k or "(none)"}×{v}' for k,v in sorted(file_types.items()))
        batches[label].append(item)
    rows=[
        '# 真正 UI 端到端測速（按鈕到輸出清單完成）',
        '',
        '以下數據取自使用者實際操作，不包含 UI 啟動與模型初次載入時間。',
        '',
        '| 檔案組合 | 次數 | UI 中位秒數 | 背景工作中位秒數 | OCR 中位累積秒數 | PII 中位累積秒數 |',
        '|---|---:|---:|---:|---:|---:|',
    ]
    for key,arr in sorted(batches.items()):
        med=lambda getter:statistics.median(getter(x) for x in arr)
        rows.append(f'| {key} | {len(arr)} | {med(lambda x:x["click_to_ui_s"]):.3f} | '
                    f'{med(lambda x:x["worker_s"]):.3f} | '
                    f'{med(lambda x:x["phases"]["ocr"]["seconds"]):.3f} | '
                    f'{med(lambda x:x["phases"]["pii"]["seconds"]):.3f} |')
    rows += ['', '注意：OCR、PII、mask_pdf 等計時存在巢狀呼叫，不可將所有階段數字直接相加。',
             '模型預熱、硬碟快取、不同尺寸、背景工作都會改變速度。',
             '正式比較建議對同一個合成圖片／PDF 分別操作至少 3 次。']
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text('\n'.join(rows)+'\n',encoding='utf-8')
    print('\n'.join(rows))
    print('存檔：',args.output)

if __name__=='__main__':main()
