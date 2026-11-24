"""Compare before/after V2 GUI timing results without inspecting PII or source docs."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import statistics


def load_runs(directory):
    directory=Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f'找不到資料夾: {directory}')
    runs=[]
    for p in sorted(directory.glob('v2_*.json')):
        try:
            d=json.loads(p.read_text(encoding='utf-8'))
        except (ValueError,UnicodeDecodeError):
            continue
        if d.get('schema_version')!='gui_v2_diagnostic_1':
            continue
        if d.get('status')!='OK':
            continue
        runs.append(d)
    if not runs:
        raise ValueError(f'{directory} 沒有成功的 v2_*.json，無法比較')
    return runs


def summarize(rs):
    def mean(k):
        vals=[float(x[k]) for x in rs if isinstance(x.get(k),(float,int))]
        return round(statistics.mean(vals),3) if vals else None
    return {
        'successful_runs':len(rs),
        'avg_click_to_ui_s':mean('click_to_ui_s'),
        'avg_worker_s':mean('worker_s'),
        'avg_max_ui_lag_ms':mean('ui_lag_max_ms'),
        'worst_ui_lag_ms':max(round(float(x.get('ui_lag_max_ms',0)),3) for x in rs),
        'avg_p95_ui_lag_ms':mean('ui_lag_p95_ms'),
        'total_ui_lag_over_500ms':sum(int(x.get('ui_lag_over_threshold',0)) for x in rs),
        'avg_peak_rss_mb':mean('rss_peak_mb'),
    }


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--before-dir',required=True)
    p.add_argument('--after-dir',required=True)
    a=p.parse_args()
    before=summarize(load_runs(a.before_dir))
    after=summarize(load_runs(a.after_dir))
    report={'before':before,'after':after,
            'improvement_worst_ui_lag_pct':round(100*(before['worst_ui_lag_ms']-after['worst_ui_lag_ms'])/before['worst_ui_lag_ms'],2) if before['worst_ui_lag_ms'] else None,
            'note':'Timing data only. V5 UI latency must be tested on the target Windows GUI.'}
    folder=Path(a.after_dir); out=folder/'gui_v5_comparison.json'
    out.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print('GUI 最嚴重排程延遲：',before['worst_ui_lag_ms'],'→',after['worst_ui_lag_ms'],'ms')
    print('GUI p95 延遲平均：',before['avg_p95_ui_lag_ms'],'→',after['avg_p95_ui_lag_ms'],'ms')
    print('工作完成平均秒數：',before['avg_click_to_ui_s'],'→',after['avg_click_to_ui_s'])
    print('[PASS] 報告：',out)

if __name__=='__main__':
    main()
