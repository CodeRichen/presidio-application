#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.7.4: diagnostic-only OCR adapter counts without any persisted OCR text.

This runs a short, fixed first-page 2x/3x preflight in a child. It NEVER runs
full PDF masking, trains models, modifies dependencies or application code.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
JSON_NAME='v6674_adapter_diagnosis_summary.json'
MD_NAME='v6674_adapter_diagnosis_summary.md'


def validate(root, tool):
    if tool != root/'PII_V6674_Adapter_Schema_Fix':
        raise RuntimeError('TOOL_MUST_BE_IN_PROJECT_ROOT')
    pdf=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    files=[pdf,root/'handler_media.py',root/'ocr_backend.py',
           root/'.venv'/'Scripts'/'python.exe',root/'.venv_ocr_gpu'/'Scripts'/'python.exe',
           *[tool/f for f in ('v6674_adapter_probe.py','v667_bridge.py','v667_gpu_worker.py')]]
    if any(not f.is_file() for f in files):
        raise RuntimeError('REQUIRED_PROJECT_COMPONENT_MISSING')
    h=hashlib.sha256()
    with pdf.open('rb') as handle:
        for chunk in iter(lambda:handle.read(1024*1024),b''):
            h.update(chunk)
    if h.hexdigest()!=SHA:
        raise RuntimeError('UNAPPROVED_SYNTHETIC_FIXTURE')
    return root/'.venv'/'Scripts'/'python.exe',root/'.venv_ocr_gpu'/'Scripts'/'python.exe'


def only_allowlisted_child_data(raw):
    if raw.get('schema')!='pii_v6674_adapter_probe_v1':
        return {'status':'INVALID_CHILD_SCHEMA'}
    fields=('status','stage','error_type','pairs_checked','pairs_equal','provider_route_verified')
    safe={f:raw.get(f) for f in fields}
    pairs=[]
    for p in raw.get('pairs',[]):
        if not isinstance(p,dict):continue
        keep=('zoom','mismatch_kind','cpu_schema_ok','hybrid_schema_ok',
              'ordered_text_equal_after_whitespace_removal','exact_adapter_equal','hypothesis')
        item={k:p.get(k) for k in keep}
        for side in ('cpu','hybrid'):
            if isinstance(p.get(side),dict):
                counts=('word_count','line_id_groups','nonempty_word_count',
                    'text_codepoints_total','text_codepoints_without_whitespace',
                    'words_with_internal_whitespace','dict_rows','non_dict_rows',
                    'unexpected_field_rows','unexpected_field_total',
                    'text_string_rows','line_id_valid_rows')
                item[side]={k:p[side].get(k) if type(p[side].get(k)) is int else None
                            for k in counts}
                for field in ('required_missing','required_wrong_type'):
                    values=p[side].get(field) or {}
                    item[side][field]={k:values.get(k,0) if type(values.get(k,0)) is int else 0
                        for k in ('text','left','top','width','height','line_id')}
        pairs.append(item)
    safe['pairs']=pairs
    if isinstance(raw.get('provider_evidence'),dict):
        ev=raw['provider_evidence']
        safe['provider_evidence']={k:ev.get(k) for k in (
            'sessions','profiles_read','det_cuda_node_events','cls_cpu_node_events',
            'rec_cpu_node_events','cls_cuda_node_events','rec_cuda_node_events')}
    return safe


def run_child(python,tool,root,gpu,out,timeout_s):
    args=[str(python),str(tool/'v6674_adapter_probe.py'),
          '--project-root',str(root),'--gpu-python',str(gpu),
          '--tool-dir',str(tool),'--out',str(out)]
    env=os.environ.copy()
    env.update({'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
                'PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1','PII_MEDIA_FP_REPORT':'0'})
    env.pop('PII_MEDIA_FP_PROFILE',None)
    start=time.perf_counter()
    try:
        result=subprocess.run(args,cwd=str(root),env=env,stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                              timeout=timeout_s,check=False)
        code=result.returncode
    except subprocess.TimeoutExpired:
        code=None
    except (OSError,ValueError):
        code='SPAWN_ERROR'
    if out.is_file():
        try:
            safe=only_allowlisted_child_data(json.loads(out.read_text(encoding='utf-8')))
        except (OSError,ValueError,TypeError):
            safe={'status':'INVALID_CHILD_REPORT'}
    else:
        safe={'status':'PROBE_TIMEOUT' if code is None else 'PROBE_NO_REPORT'}
    return {'return_code':code,'elapsed_s':round(time.perf_counter()-start,3),**safe}


def status(probe):
    if probe.get('status')=='ADAPTER_PREFLIGHT_OK' and probe.get('return_code')==0 and probe.get('pairs_equal')==2 and probe.get('provider_route_verified'):
        return 'ADAPTER_PREFLIGHT_OK'
    if probe.get('status')=='OCR_ADAPTER_SCHEMA_MISMATCH':
        return 'OCR_ADAPTER_SCHEMA_MISMATCH'
    if probe.get('status')=='OCR_ADAPTER_MISMATCH_CHARACTERIZED':
        return 'OCR_ADAPTER_MISMATCH_CHARACTERIZED'
    if probe.get('status')=='PROVIDER_ROUTE_NOT_VERIFIED':
        return 'PROVIDER_ROUTE_NOT_VERIFIED'
    return 'DIAGNOSTIC_FAILED'


def write_reports(out,report):
    out.mkdir(parents=True,exist_ok=True)
    (out/JSON_NAME).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    probe=report['probe']
    lines=['# V6.6.7.4：OCR Adapter 欄位結構診斷','',
        f"- 狀態：**{report['status']}**",
        '- 固定 SHA-256 的七頁合成 PDF，只使用第 1 頁的 2×／3× OCR。',
        '- 正式程式與兩套虛擬環境不修改；不執行完整 PII、人臉、PDF 安全回歸。',
        '- 僅保存欄位存在數量、欄位型別異常數量及匯總筆數，不保存 OCR 內容、未知欄位名稱、雜湊、座標、PDF 或私有 map。',
        '- 先完整檢查兩邊六個欄位的結構，再考慮比較文字；欄位不符時禁止視為相容。','',
        '| 解析度 | 正式版筆數 | 混合版筆數 | 正式版行群組 | 混合版行群組 | 去空白串接相同 | 差異類型 |',
        '|---|---:|---:|---:|---:|---|---|']
    for p in probe.get('pairs',[]):
        a=p.get('cpu') or {};b=p.get('hybrid') or {}
        same=p.get('ordered_text_equal_after_whitespace_removal')
        lines.append(f"| {p.get('zoom','?')}× | {a.get('word_count','—')} | {b.get('word_count','—')} | {a.get('line_id_groups','—')} | {b.get('line_id_groups','—')} | {same if same is not None else '—'} | {p.get('mismatch_kind','—')} |")
        lines.append('')
        lines.append(f"- {p.get('zoom','?')}× 診斷線索：`{p.get('hypothesis','UNKNOWN')}`")
        for label, s in (('正式版',a),('混合版',b)):
            miss=s.get('required_missing') or {}; wrong=s.get('required_wrong_type') or {}
            miss_s=', '.join(f'{k}={miss.get(k,0)}' for k in ('text','left','top','width','height','line_id') if miss.get(k,0)) or '無'
            wrong_s=', '.join(f'{k}={wrong.get(k,0)}' for k in ('text','left','top','width','height','line_id') if wrong.get(k,0)) or '無'
            lines.append(f"  - {label}：dict={s.get('dict_rows','—')}，非 dict={s.get('non_dict_rows','—')}，多餘欄位筆數={s.get('unexpected_field_rows','—')}，缺少欄位：{miss_s}，欄位型別異常：{wrong_s}")
    lines.extend(['',f"- 比對成功：{probe.get('pairs_equal',0)} / {probe.get('pairs_checked',0)}",
        f"- 模型路由已確認：{probe.get('provider_route_verified',False)}",
        f"- 失敗位置：{probe.get('stage','—')}，例外類型：{probe.get('error_type') or '無'}",
        '- 即使狀態是 ADAPTER_PREFLIGHT_OK，仍需完整七頁 PDF 的 PII／人臉／結構／像素安全驗收。',''])
    (out/MD_NAME).write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--results-dir',default='v6674_adapter_results')
    ap.add_argument('--check-only',action='store_true')
    ap.add_argument('--timeout',type=int,default=240)
    a=ap.parse_args(argv)
    if a.timeout<20 or a.timeout>1200:
        raise ValueError('TIMEOUT_OUT_OF_RANGE')
    root=Path(a.project_root).resolve()
    tool=Path(__file__).resolve().parent
    app,gpu=validate(root,tool)
    if a.check_only:
        print('CHECK_OK: approved synthetic fixture, app adapter and two separate Python venvs found')
        return 0
    dest=(root/a.results_dir).resolve()
    if dest==root or root not in dest.parents:
        raise RuntimeError('RESULTS_MUST_BE_UNDER_PROJECT_ROOT')
    with tempfile.TemporaryDirectory(prefix='pii_v6674_') as tmp:
        probe=run_child(app,tool,root,gpu,Path(tmp)/'child.json',a.timeout)
    result={'schema':'pii_v6674_adapter_diagnosis_v1',
        'status':status(probe),'synthetic_fixture_sha256_verified':True,
        'only_synthetic_documents':True,'production_code_modified':False,
        'production_environment_modified':False,'full_pdf_security_gate_executed':False,
        'probe':probe,
        'warning':'Diagnostic only. No model, PII, face, final PDF or GUI speed acceptance.'}
    write_reports(dest,result)
    print('V6.6.7.4 STATUS:',result['status'])
    print('Compared pairs:',probe.get('pairs_checked',0))
    print('Report:',dest/MD_NAME)
    return 0 if result['status']=='ADAPTER_PREFLIGHT_OK' else 2

if __name__=='__main__':
    raise SystemExit(main())
