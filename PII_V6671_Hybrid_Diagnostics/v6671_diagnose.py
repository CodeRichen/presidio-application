#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V667.1: fail-closed stage diagnosis of original V667 worker + OCR adapter.
Never runs full PDF anonymization; never saves text, screenshots or restoration maps.
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
JSON_NAME='v6671_hybrid_diagnosis_summary.json'
MD_NAME='v6671_hybrid_diagnosis_summary.md'


def setup(root, tool):
    if tool != root/'PII_V6671_Hybrid_Diagnostics':
        raise RuntimeError('TOOL_MUST_BE_AT_PROJECT_ROOT')
    v667=root/'PII_V667_Full_PDF_Hybrid_AB'
    source=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    needed=[v667/'v667_gpu_worker.py',v667/'v667_bridge.py',v667/'v667_child.py',
            root/'handler_media.py',root/'ocr_backend.py',source,
            root/'.venv'/'Scripts'/'python.exe', root/'.venv_ocr_gpu'/'Scripts'/'python.exe']
    missing=[p for p in needed if not p.is_file()]
    if missing:
        # Never include absolute file paths, user names or any private data in reports.
        raise RuntimeError('REQUIRED_PROJECT_COMPONENT_MISSING')
    h=hashlib.sha256()
    with source.open('rb') as f:
        for part in iter(lambda:f.read(1024*1024),b''):
            h.update(part)
    if h.hexdigest()!=PINNED_SHA:
        raise RuntimeError('SYNTHETIC_SHA256_MISMATCH')
    return {'source':source,'v667':v667,
            'app_python':root/'.venv'/'Scripts'/'python.exe',
            'gpu_python':root/'.venv_ocr_gpu'/'Scripts'/'python.exe'}


def safe_child(cmd, cwd, env, report_path, timeout_s):
    started=time.perf_counter()
    try:
        p=subprocess.run([str(c) for c in cmd],cwd=str(cwd), env=env,
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,timeout=timeout_s,check=False)
        rc=p.returncode
    except subprocess.TimeoutExpired:
        rc=None
    except (OSError,ValueError):
        rc='SPAWN_ERROR'
    result={'return_code':rc,'elapsed_s':round(time.perf_counter()-started,3)}
    if report_path.exists():
        try:
            obj=json.loads(report_path.read_text(encoding='utf-8'))
            if obj.get('schema') in ('pii_v6671_gpu_probe_v1','pii_v6671_adapter_probe_v1'):
                # Only known-safe fields from this tool, not arbitrary exception messages.
                allowed=('status','stage','error_type','provider_verified','sessions',
                         'det_cuda_node_events','profiles_read','pairs_checked',
                         'pairs_equal','mismatch_kind','provider_evidence')
                for field in allowed:
                    if field in obj:
                        result[field]=obj[field]
        except (ValueError,OSError,TypeError):
            result['status']='INVALID_CHILD_REPORT'
    elif rc is None:
        result['status']='PROBE_TIMEOUT'
    else:
        result['status']='PROBE_NO_REPORT'
    return result


def derive_status(gpu,adapter):
    if gpu.get('status')!='GPU_PROBE_OK':
        return 'GPU_WORKER_INITIALIZATION_NEEDS_DIAGNOSIS'
    if adapter.get('status')!='ADAPTER_PREFLIGHT_OK':
        if adapter.get('status')=='OCR_ADAPTER_MISMATCH':
            return 'OCR_ADAPTER_MISMATCH'
        if adapter.get('status')=='PROVIDER_ROUTE_NOT_VERIFIED':
            return 'PROVIDER_ROUTE_NOT_VERIFIED'
        return 'WORKER_OR_ADAPTER_EXCEPTION'
    return 'WORKER_AND_ADAPTER_PREFLIGHT_OK'


def write_report(dest,report):
    dest.mkdir(parents=True,exist_ok=True)
    (dest/JSON_NAME).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    gpu=report.get('gpu_probe',{})
    adapter=report.get('adapter_probe',{})
    rows=['# V6.6.7.1：混合 OCR 失敗階段診斷（僅合成測試）','',
          f"- 狀態：**{report['status']}**",'',
          '| 檢查 | 結果 | 階段 | 例外類型 |','|---|---|---|---|',
          f"| 獨立 GPU 引擎 | {gpu.get('status','NOT_RUN')} | {gpu.get('stage','—')} | {gpu.get('error_type') or '—'} |",
          f"| 原始 OCR Adapter 預檢 | {adapter.get('status','NOT_RUN')} | {adapter.get('stage','—')} | {adapter.get('error_type') or '—'} |",
          '',f"- 預檢通過：{adapter.get('pairs_equal',0)} / {adapter.get('pairs_checked',0)}。",
          f"- 不同類型：{adapter.get('mismatch_kind') or '無紀錄'}。",
          '- 不執行完整 PDF 脫敏，不比較 PDF 像素，也不計算加速百分比。',
          '- 原始 V6.6.7、正式程式、兩個虛擬環境均未修改。',
          '- 僅使用固定 SHA-256 合成 PDF；不記錄 OCR 文字、框、影像、map、異常訊息或 traceback。',
          '- 只有 GPU 和 Adapter 兩項都通過，才考慮重新進行完整 PDF 安全 A/B。','']
    (dest/MD_NAME).write_text('\n'.join(rows),encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v6671_hybrid_diagnostics_results')
    p.add_argument('--check-only',action='store_true')
    p.add_argument('--timeout',type=int,default=240)
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    tool=Path(__file__).resolve().parent
    paths=setup(root,tool)
    if a.check_only:
        print('CHECK_OK: approved synthetic fixture, original V667 files and both venvs located')
        return 0
    out=(root/a.results_dir).resolve()
    if out==root or root not in out.parents:
        raise RuntimeError('RESULTS_MUST_BE_UNDER_PROJECT_ROOT')
    report={'schema':'pii_v6671_diagnosis_v1','status':'INCOMPLETE',
            'synthetic_fixture_sha256_verified':True,'full_pdf_security_gate_executed':False,
            'production_environment_modified':False,'production_code_modified':False,
            'gpu_probe':{},'adapter_probe':{},
            'warning':'Diagnostic only. Never use with real documents; no acceleration claim.'}
    with tempfile.TemporaryDirectory(prefix='pii_v6671_') as tmp:
        scratch=Path(tmp)
        gpu_file=scratch/'gpu_probe.json'
        gpu_env=os.environ.copy()
        gpu_cmd=[paths['gpu_python'], tool/'v6671_gpu_probe.py',
                 '--out',gpu_file,'--v667-dir',paths['v667']]
        report['gpu_probe']=safe_child(gpu_cmd,root,gpu_env,gpu_file,a.timeout)
        if report['gpu_probe'].get('status')=='GPU_PROBE_OK':
            app_file=scratch/'adapter_probe.json'
            app_env=os.environ.copy()
            app_env.update({'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
                            'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1',
                            'PII_ZH_SINGLE_PASS':'1', 'PII_MEDIA_FP_PROFILE':'synthetic_face_test'})
            app_cmd=[paths['app_python'],tool/'v6671_adapter_probe.py',
                     '--project-root',root,'--gpu-python',paths['gpu_python'],
                     '--worker',paths['v667']/'v667_gpu_worker.py',
                     '--v667-dir',paths['v667'],'--out',app_file]
            report['adapter_probe']=safe_child(app_cmd,root,app_env,app_file,a.timeout)
    report['status']=derive_status(report['gpu_probe'],report['adapter_probe'])
    write_report(out,report)
    print('V667.1 STATUS:',report['status'])
    print('GPU stage:',report['gpu_probe'].get('stage','—'))
    print('Adapter stage:',report['adapter_probe'].get('stage','—'))
    print('Report:',out/MD_NAME)
    return 0 if report['status']=='WORKER_AND_ADAPTER_PREFLIGHT_OK' else 2

if __name__=='__main__':
    raise SystemExit(main())
