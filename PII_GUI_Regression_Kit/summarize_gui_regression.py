"""Aggregate minimal GUI timing records without writing private file content."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import statistics


def aggregate(records):
    ok = [r for r in records if r.get('status') == 'OK']
    def summary(key):
        values = [float(r[key]) for r in ok if isinstance(r.get(key), (int,float))]
        if not values: return {'average': None, 'min': None, 'max': None}
        return { 'average': round(statistics.mean(values), 3),
                 'min': round(min(values), 3), 'max': round(max(values), 3)}
    return {'runs': len(records), 'successful_runs': len(ok),
            'error_runs': sum(r.get('status') == 'ERROR' for r in records),
            'error_recovery_simulations': sum(bool(r.get('simulated_error')) for r in records),
            'click_to_ui_s': summary('click_to_ui_s'),
            'worker_s': summary('worker_s'),
            'ui_lag_max_ms': summary('ui_lag_max_ms'),
            'ui_lag_p95_ms': summary('ui_lag_p95_ms'),
            'rss_peak_mb': summary('rss_peak_mb')}


def main():
    p = argparse.ArgumentParser(description='彙整多次 GUI 效能測試')
    p.add_argument('--results-dir', type=Path, default=Path('gui_regression_results'))
    a = p.parse_args()
    files = sorted(a.results_dir.glob('run_*.json'))
    recs = [json.loads(f.read_text(encoding='utf-8')) for f in files]
    if not recs:
        raise SystemExit(f'尚未有 run_*.json：{a.results_dir}')
    summ = aggregate(recs)
    report_path = a.results_dir / 'gui_timing_summary.md'
    lines = ['# GUI 效能與穩定性回歸測試','',
             '本報告只記錄測試器的測量結果；UI 排程延遲僅為響應能力的代理指標。', '',
             f"- 執行次數：{summ['runs']}",
             f"- 成功：{summ['successful_runs']}",
             f"- 失敗：{summ['error_runs']}（模擬錯誤：{summ['error_recovery_simulations']}）", '',
             '| 指標 | 平均 | 最小 | 最大 |', '|---|---:|---:|---:|']
    for key, label in [('click_to_ui_s', '點擊至完成秒數'), ('worker_s', 'Worker 秒數'),
                       ('ui_lag_max_ms', '每次最長 UI 排程延遲（毫秒）'),
                       ('ui_lag_p95_ms', '每次 p95 UI 排程延遲（毫秒）'),
                       ('rss_peak_mb', '整體程序 RSS 峰值（MB）')]:
        x=summ[key]
        disp=lambda v: f'{v:.3f}' if v is not None else 'N/A'
        lines.append(f"| {label} | {disp(x['average'])} | {disp(x['min'])} | {disp(x['max'])} |")
    lines += ['', '## 分次紀錄', '', '| 次數 | 輸入 | 輸出 | 狀態 | 點擊至完成(s) | Worker(s) | UI lag max(ms) | RSS peak(MB) |',
              '|---|---:|---:|---|---:|---:|---:|---:|']
    for i,r in enumerate(recs,1):
        lines.append(f"| {i} | {r.get('input_count',0)} | {r.get('output_count',0)} | {r.get('status','?')} | "
                     f"{r.get('click_to_ui_s','-')} | {r.get('worker_s','-')} | "
                     f"{r.get('ui_lag_max_ms','-')} | {r.get('rss_peak_mb','-')} |")
    lines += ['', '## 解讀提醒', '',
              '- UI heartbeat lag 大於 500 ms 的次數可用來找卡頓，但不能等同直接測量使用者點擊回應時間。',
              '- 連續執行的記憶體峰值上升可能是模型快取，不單獨證明洩漏。',
              '- 本報告不驗證 PII 遮蔽是否完整；需要搭配原本 Ground Truth 與 PDF 安全回歸測試。',
              '- 模擬錯誤是預期錯誤，不等於正式處理失敗。']
    report_path.write_text('\n'.join(lines)+'\n',encoding='utf-8')
    (a.results_dir / 'gui_timing_summary.json').write_text(json.dumps(summ,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'已產生：{report_path}')
    print(json.dumps(summ, ensure_ascii=False, indent=2))


if __name__=='__main__':main()
