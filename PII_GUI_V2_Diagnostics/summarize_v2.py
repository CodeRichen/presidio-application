"""Summarize local GUI V2 phase traces. No external services or PII."""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import statistics


def summary_dir(results_dir):
    folder = Path(results_dir)
    paths = sorted(folder.glob('v2_*.json'))
    if not paths:
        raise SystemExit(f'找不到 v2_*.json：{folder}（先執行 GUI V2 測試）')
    records = []
    for path in paths:
        try:
            v = json.loads(path.read_text(encoding='utf-8'))
            if v.get('schema_version') == 'gui_v2_diagnostic_1':
                records.append(v)
        except (OSError, ValueError):
            print(f'[略過] 無法解析 {path.name}')
    if not records:
        raise SystemExit('沒有符合 V2 格式的紀錄')
    good = [r for r in records if r.get('status') == 'OK']
    metrics = ['click_to_ui_s', 'worker_s', 'ui_lag_max_ms', 'ui_lag_p95_ms',
               'rss_start_mb', 'rss_peak_mb', 'rss_end_mb']
    stats = {}
    for key in metrics:
        valid = [r[key] for r in good if isinstance(r.get(key), (int, float))]
        stats[key] = {'mean': round(statistics.mean(valid), 3),
                      'min': round(min(valid), 3), 'max': round(max(valid), 3)} if valid else None
    stage_data = defaultdict(lambda: {'calls': 0, 'inclusive_s': 0.0, 'exclusive_s': 0.0,
                                      'max_call_s': 0.0, 'errors': 0})
    for r in good:
        for stage, vals in r.get('phase_summary', {}).items():
            s = stage_data[stage]
            for k in ('calls', 'inclusive_s', 'exclusive_s', 'errors'):
                s[k] += vals.get(k, 0)
            s['max_call_s'] = max(s['max_call_s'], vals.get('max_call_s', 0))
    for vals in stage_data.values():
        for key in ('inclusive_s', 'exclusive_s', 'max_call_s'):
            vals[key] = round(vals[key], 3)
    stages = dict(sorted(stage_data.items(), key=lambda x: x[1]['exclusive_s'], reverse=True))
    slow = []
    for r in records:
        for g in r.get('slow_ui_gaps', []):
            slow.append({'run_id': r['run_id'], **g})
    slow = sorted(slow, key=lambda x: x['delay_ms'], reverse=True)
    out = {
        'runs': len(records), 'success': len(good), 'errors': len(records) - len(good),
        'success_metrics': stats, 'phase_totals_across_successful_runs': stages,
        'slow_gaps_count': len(slow), 'worst_slow_gaps': slow[:15],
        'guidance': 'Inclusive totals nest; use exclusive when attributing time. Slow gap overlap is correlation, not GIL proof.'
    }
    (folder / 'gui_v2_summary.json').write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# GUI V2 效能瓶頸診斷', '',
             f'- 共 {len(records)} 次：成功 {len(good)}、失敗 {len(records) - len(good)}',
             f'- 偵測到 >= 500ms 等級的 UI 排程延遲事件：{len(slow)}（門檻以執行時設定為準）',
             '', '## 主要指標（僅成功執行）', '', '| 指標 | 平均 | 最小 | 最大 |', '|---|---:|---:|---:|']
    for k in metrics:
        v = stats[k]
        if v:
            lines.append(f'| `{k}` | {v["mean"]} | {v["min"]} | {v["max"]} |')
    lines += ['', '## 階段耗時（成功執行的累積值）', '',
              '| 階段 | 呼叫數 | inclusive 秒 | exclusive 秒 | 最慢單次 秒 |',
              '|---|---:|---:|---:|---:|']
    for k, v in list(stages.items())[:35]:
        lines.append(f'| `{k}` | {v["calls"]} | {v["inclusive_s"]} | {v["exclusive_s"]} | {v["max_call_s"]} |')
    lines += ['', '## 最嚴重的排程延遲', '',
              '| 延遲 ms | 時間點 s | 觀測到的 worker 階段 | 重疊測量階段 | sampler 最大間隔 ms |',
              '|---:|---:|---|---|---:|']
    for g in slow[:15]:
        sampled = ', '.join(f'{n}×{cnt}' for n, cnt in list(g.get('phase_samples', {}).items())[:3]) or '無樣本'
        overlap = ', '.join(f'{n}({sec}s)' for n, sec in list(g.get('overlapping_stages_s', {}).items())[:3]) or '未觀察到'
        lines.append(f'| {g["delay_ms"]} | {g["at_s"]} | {sampled} | {overlap} | {g["max_sample_gap_ms"]} |')
    lines += ['', '## 判讀提醒', '',
              '- **Inclusive** 包含子階段，不可直接相加；**exclusive** 扣除巢狀階段，可用於找耗時來源。',
              '- `ui_lag_max_ms` 是 Tk `after()` 排程延遲，**不是**實際點擊延遲。',
              '- 遇到沒有相符 phase 的卡頓，應檢查未被儀表化的本機函式、GIL、模型初始化或系統排程。',
              '- 同一事件的「重疊階段」只是時間相關，不能單憑此值宣稱 GIL 造成卡頓。',
              '- 不蒐集檔名、檔案路徑、OCR 文字、PII 原值、臉部框座標或還原對照表。', '']
    (folder / 'gui_v2_summary.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'完成：{folder / "gui_v2_summary.md"}')
    print(f'完成：{folder / "gui_v2_summary.json"}')
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--results-dir', type=Path, default=Path('gui_v2_results'))
    a = p.parse_args()
    summary_dir(a.results_dir)


if __name__ == '__main__':
    main()
