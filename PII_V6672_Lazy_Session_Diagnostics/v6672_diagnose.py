#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.7.2: correct premature RapidOCR session checks; diagnostic only.

No full PDF anonymization; only pinned synthetic fixture page 1 is compared.
No modifications to either venv, application, or older experimental packages.
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

PINNED_SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
REPORT_JSON='v6672_lazy_session_summary.json'
REPORT_MD='v6672_lazy_session_summary.md'


def setup(root,tool):
    if tool!=root/'PII_V6672_Lazy_Session_Diagnostics':
        raise RuntimeError('TOOL_MUST_BE_IN_PROJECT_ROOT')
    source=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    needed=[source,root/'handler_media.py',root/'ocr_backend.py',
            root/'.venv'/'Scripts'/'python.exe',root/'.venv_ocr_gpu'/'Scripts'/'python.exe',
            *[tool/x for x in ('v6672_gpu_probe.py','v6672_adapter_probe.py',
                               'v667_gpu_worker.py','v667_bridge.py')]]
    if any(not p.is_file() for p in needed):
        raise RuntimeError('REQUIRED_PROJECT_COMPONENT_MISSING')
    h=hashlib.sha256()
    with source.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):
            h.update(block)
    if h.hexdigest()!=PINNED_SHA:
        raise RuntimeError('ONLY_APPROVED_SYNTHETIC_FIXTURE_ALLOWED')
    return {'app_python':root/'.venv'/'Scripts'/'python.exe',
            'gpu_python':root/'.venv_ocr_gpu'/'Scripts'/'python.exe'}


def safe_child(command,cwd,env,out,timeout_s,expected_schema):
    start=time.perf_counter()
    try:
        child=subprocess.run([str(x) for x in command],cwd=str(cwd),env=env,
                             stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL,timeout=timeout_s,check=False)
        code=child.returncode
    except subprocess.TimeoutExpired:
        code=None
    except (OSError,ValueError):
        code='SPAWN_ERROR'
    result={'return_code':code,'elapsed_s':round(time.perf_counter()-start,3)}
    if out.is_file():
        try:
            raw=json.loads(out.read_text(encoding='utf-8'))
            if raw.get('schema')!=expected_schema:
                result['status']='INVALID_CHILD_REPORT'
            else:
                allowed=('status','stage','error_type','sessions_after_construct',
                         'sessions_after_synthetic','provider_verified',
                         'det_cuda_node_events','profiles_read','pairs_checked','pairs_equal',
                         'mismatch_kind','provider_evidence')
                for name in allowed:
                    if name in raw:result[name]=raw[name]
        except (OSError,ValueError,TypeError):
            result['status']='INVALID_CHILD_REPORT'
    else:
        result['status']=('PROBE_TIMEOUT' if code is None else 'PROBE_NO_REPORT')
    return result


def derive(gpu,adapter):
    if (gpu.get('return_code') != 0 or
            gpu.get('status')!='GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY'):
        return 'GPU_DET_NOT_VERIFIED'
    if adapter.get('status')=='OCR_ADAPTER_MISMATCH':
        return 'OCR_ADAPTER_MISMATCH'
    if adapter.get('status')=='PROVIDER_ROUTE_NOT_VERIFIED':
        return 'PROVIDER_ROUTE_NOT_VERIFIED'
    if (adapter.get('return_code') == 0 and
            adapter.get('status')=='ADAPTER_PREFLIGHT_OK' and
            adapter.get('pairs_equal')==2):
        return 'WORKER_AND_ADAPTER_PREFLIGHT_OK'
    return 'WORKER_OR_ADAPTER_EXCEPTION'


def write_reports(folder,report):
    folder.mkdir(parents=True,exist_ok=True)
    (folder/REPORT_JSON).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    gpu=report.get('gpu_probe') or {}
    app=report.get('adapter_probe') or {}
    lines=['# V6.6.7.2：RapidOCR 延遲 Session 初始化安全診斷','',
           f"- 狀態：**{report['status']}**",
           '- 這是診斷工具，非完整 PDF 速度／遮蔽安全 A/B。',
           '- CUDA 偵測證據需來自實際 Node events，而不是 Provider 清單。',
           '- 原版程式、正式虛擬環境與舊工具不更動；使用獨立實驗 worker。','',
           '| 階段 | 結果 | 位置 | 例外類型 |','|---|---|---|---|',
           f"| GPU 合成推論 | {gpu.get('status','NOT_RUN')} | {gpu.get('stage','—')} | {gpu.get('error_type') or '—'} |",
           f"| 固定第 1 頁 OCR 預檢 | {app.get('status','NOT_RUN')} | {app.get('stage','—')} | {app.get('error_type') or '—'} |",
           '',f"- 建立 RapidOCR 後觀測 Session：{gpu.get('sessions_after_construct',{})}",
           f"- 合成 OCR 後觀測 Session：{gpu.get('sessions_after_synthetic',{})}",
           f"- GPU det CUDA Node：{gpu.get('det_cuda_node_events',0)}",
           f"- OCR adapter 完全一致：{app.get('pairs_equal',0)} / {app.get('pairs_checked',0)}",
           f"- 差異類型：{app.get('mismatch_kind') or '無'}",
           '', '- adapter 要求文字、字框、line_id 等欄位精確一致；若不一致即停止。',
           '- 沒有寫出 OCR 內容、圖片、字框、原文摘要、個資、PDF、原始 profiler 或還原表。',
           '- 目前固定合成文件的結果不代表真實文件的辨識安全性。','']
    (folder/REPORT_MD).write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--results-dir',default='v6672_lazy_session_results')
    ap.add_argument('--check-only',action='store_true')
    ap.add_argument('--timeout',type=int,default=240)
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve()
    tool=Path(__file__).resolve().parent
    paths=setup(root,tool)
    if args.check_only:
        print('CHECK_OK: pinned synthetic fixture, adapter, separate GPU venv found')
        return 0
    output=(root/args.results_dir).resolve()
    if output==root or root not in output.parents:
        raise RuntimeError('RESULTS_MUST_BE_UNDER_PROJECT_ROOT')
    report={'schema':'pii_v6672_lazy_session_diagnosis_v1',
            'status':'INCOMPLETE','synthetic_fixture_sha256_verified':True,
            'production_environment_modified':False,'production_code_modified':False,
            'full_pdf_security_gate_executed':False,'only_synthetic_documents':True,
            'gpu_probe':{},'adapter_probe':{},
            'warning':'This is only preflight, no PDF safety or GUI performance result.'}
    with tempfile.TemporaryDirectory(prefix='pii_v6672_') as tmp:
        scratch=Path(tmp)
        gpu_file=scratch/'gpu.json'
        gpu_command=[paths['gpu_python'],tool/'v6672_gpu_probe.py',
                     '--out',gpu_file,'--tool-dir',tool]
        report['gpu_probe']=safe_child(gpu_command,root,os.environ.copy(),gpu_file,
                                       args.timeout,'pii_v6672_gpu_probe_v1')
        if report['gpu_probe'].get('status')=='GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY':
            adapter_file=scratch/'adapter.json'
            env=os.environ.copy()
            env.update({'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
                        'PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1',
                        'PII_MEDIA_FP_PROFILE':'synthetic_face_test',
                        'PII_MEDIA_FP_REPORT':'0'})
            adapter_command=[paths['app_python'],tool/'v6672_adapter_probe.py',
                             '--project-root',root,'--gpu-python',paths['gpu_python'],
                             '--tool-dir',tool,'--out',adapter_file]
            report['adapter_probe']=safe_child(adapter_command,root,env,adapter_file,
                                               args.timeout,'pii_v6672_adapter_probe_v1')
    report['status']=derive(report['gpu_probe'],report['adapter_probe'])
    write_reports(output,report)
    print('V6.6.7.2 STATUS:',report['status'])
    print('GPU stage:',report['gpu_probe'].get('stage','—'))
    print('Adapter stage:',report['adapter_probe'].get('stage','—'))
    print('Report:',output/REPORT_MD)
    return 0 if report['status']=='WORKER_AND_ADAPTER_PREFLIGHT_OK' else 2


if __name__=='__main__':
    raise SystemExit(main())
