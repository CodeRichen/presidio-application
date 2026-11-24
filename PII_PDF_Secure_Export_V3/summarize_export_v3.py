"""Summarize multiple privacy-safe PDF V3 step timing reports."""
import argparse
import json
from collections import defaultdict
from pathlib import Path
import statistics


def summarize(folder: Path):
    entries = []
    for path in sorted(folder.glob('pdf_v3_*.json')):
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            if data.get('schema_version') == 'pdf_secure_export_v3_1':
                entries.append(data)
        except (OSError, ValueError):
            pass
    if not entries:
        raise SystemExit('找不到 pdf_v3_*.json，請先跑 V3 GUI 測試。')
    by_stage = defaultdict(list)
    for run in entries:
        for step in run['steps']:
            by_stage[step['stage']].append(float(step['duration_ms']))
    stages = {}
    for name, vals in sorted(by_stage.items()):
        stages[name] = {'calls': len(vals), 'total_s': round(sum(vals) / 1000, 4),
                        'average_ms': round(statistics.mean(vals), 3),
                        'max_ms': round(max(vals), 3)}
    result = {'runs': len(entries), 'successful':sum(r['status']=='OK' for r in entries),
              'stages':stages,
              'slowest_calls':sorted([{'stage':s['stage'],
                 'duration_ms':s['duration_ms'], **({'page':s['page']} if 'page' in s else {})}
                 for r in entries for s in r['steps']],
                 key=lambda o:o['duration_ms'],reverse=True)[:12],
              'note':'Timing only. Correlation with V2 lag events remains approximate; not proof of GIL contention.'}
    (folder/'pdf_v3_summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# PDF 安全輸出 V3 分段效能診斷','',f'測試次數：{result["runs"]}，成功：{result["successful"]}','','| 步驟 | 呼叫數 | 累積秒數 | 平均 ms/次 | 最慢 ms/次 |','|---|---:|---:|---:|---:|']
    for name,v in stages.items():
        lines.append(f'| {name} | {v["calls"]} | {v["total_s"]:.3f} | {v["average_ms"]:.2f} | {v["max_ms"]:.2f} |')
    lines.extend(['','## 最慢單次呼叫','','| 階段 | 頁數 | 耗時 ms |','|---|---:|---:|'])
    for v in result['slowest_calls']:
        lines.append(f'| {v["stage"]} | {v.get("page", "—")} | {v["duration_ms"]:.2f} |')
    lines.extend(['','**提醒**：這些測量不能證明 GIL 問題。PDF 安全輸出仍需維持完整結構與遮蔽驗證。'])
    (folder/'pdf_v3_summary.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(f'已彙整 {len(entries)} 次輸出；結果位於 {folder.resolve()}')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--results-dir',type=Path,default=Path('gui_v3_export_results'))
    a=parser.parse_args()
    summarize(a.results_dir)
