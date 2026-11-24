#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.7.6: Full synthetic PDF CPU-vs-hybrid OCR experiment.
Uses installed production .venv for PII/face/V5 and separate .venv_ocr_gpu
for OCR det CUDA / cls+rec CPU; does not modify either environment.

No raw OCR text, crops, PDFs, profiles or private restoration maps are persisted
outside TemporaryDirectory. Results contain counters, statuses and timing only.
"""
from __future__ import annotations
import argparse
import json
import os
import signal
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

EXPECTED_SHA='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
REPORT_JSON='v6676_full_pdf_hybrid_summary.json'
REPORT_MD='v6676_full_pdf_hybrid_summary.md'


def require_setup(root, source, tool):
    if tool.parent!=root or tool.name!='PII_V6676_Full_PDF_Hybrid_AB':
        raise RuntimeError('V6676_TOOL_MUST_BE_IN_PROJECT_ROOT')
    gate_dir=root/'PII_V5_Security_Acceptance'
    sys.path.insert(0,str(gate_dir))
    import v5_acceptance as gate
    gate.check_setup(root,source,gate_dir)
    if gate.sha256_file(source)!=EXPECTED_SHA:
        raise RuntimeError('UNAPPROVED_SYNTHETIC_FIXTURE')
    if not (root/'ocr_backend.py').is_file():
        raise RuntimeError('PROJECT_OCR_BACKEND_PY_MISSING')
    expected=gate_dir/'expected_text_and_faces.json'
    if not expected.is_file():
        raise RuntimeError('V5_EXPECTED_GROUND_TRUTH_MISSING')
    if not (gate_dir/'run_7page_pdf_test.py').is_file():
        raise RuntimeError('V5_PDF_RUNNER_MISSING')
    data=json.loads(expected.read_text(encoding='utf-8'))
    if len(data['samples'])!=7 or sum(len(x['expected_text']) for x in data['samples'])!=27:
        raise RuntimeError('UNEXPECTED_GROUND_TRUTH')
    app_python=root/'.venv'/'Scripts'/'python.exe'
    gpu_python=root/'.venv_ocr_gpu'/'Scripts'/'python.exe'
    if not app_python.is_file() or not gpu_python.is_file():
        raise RuntimeError('MISSING_WINDOWS_VENV_PYTHON')
    return gate,gate_dir,expected,app_python,gpu_python


def child_env():
    env=os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5':'1','PII_PDF_EXPORT_V3':'0',
        'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
        'PII_FACE_ENABLED':'1','PII_MEDIA_FP_PROFILE':'synthetic_face_test',
        'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1','PII_ZH_SINGLE_PASS':'1',
    })
    return env


def provider_gate(info):
    s=info.get('sessions') or {}
    n=info.get('node_events') or {}
    if any(s.get(k)!=1 for k in ('det','cls','rec')):
        return False
    return (n.get('det_cuda',0)>0 and n.get('cls_cpu',0)>0
            and n.get('rec_cpu',0)>0 and n.get('cls_cuda',0)==0
            and n.get('rec_cuda',0)==0 and info.get('profiles_read')==3)


def run_trial(root,tool,gate,gate_dir,expected,app_python,gpu_python,source,mode,target,timeout):
    args=[str(app_python),str(tool/'v6676_child.py'),
          '--project-root',str(root),'--source',str(source),
          '--runner',str(gate_dir/'run_7page_pdf_test.py'),
          '--expected',str(expected),'--outdir',str(target),
          '--mode',mode,'--gpu-python',str(gpu_python),
          '--worker',str(tool/'v667_gpu_worker.py')]
    started=time.perf_counter()
    # A Windows child may launch the isolated GPU OCR worker: terminate the
    # process TREE on timeout, otherwise a worker could outlive the test and
    # retain temporary OCR payloads in RAM or hold native profile files open.
    flags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0) if os.name=='nt' else 0
    child=None
    spawn_error=False
    try:
        child=subprocess.Popen(args,cwd=str(root),env=child_env(),
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,creationflags=flags,
                               start_new_session=(os.name!='nt'))
        child.wait(timeout=timeout)
        rc=child.returncode
    except OSError:
        rc='SPAWN_ERROR'
        spawn_error=True
    except subprocess.TimeoutExpired:
        rc=None
        if os.name=='nt':
            try:
                subprocess.run(['taskkill','/PID',str(child.pid),'/T','/F'],
                               stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,timeout=15,check=False)
            except (OSError,subprocess.TimeoutExpired):
                pass
        else:
            try:os.killpg(child.pid,signal.SIGKILL)
            except (ProcessLookupError,PermissionError):pass
        if child.poll() is None:
            child.kill()
        try:child.wait(timeout=10)
        except subprocess.TimeoutExpired:pass
    record={'mode':mode,'return_code':rc,'child_elapsed_s':round(time.perf_counter()-started,3),
            'safe':False,'reason':None}
    info_path=target/'child_stats.json'
    if info_path.is_file():
        try:
            stats=json.loads(info_path.read_text(encoding='utf-8'))
            if stats.get('schema')=='pii_v6676_child_v1' and stats.get('mode')==mode:
                record['child_status']=stats.get('status')
                record['child_stage']=stats.get('stage') if stats.get('stage') in ('BEGIN','GPU_WORKER_START','PREFLIGHT_2X_3X','RUN_FULL_PDF','WORKER_PROVIDER_PROFILE','COMPLETE','MAIN') else 'UNKNOWN'
                if stats.get('error_type') in ('RuntimeError','TypeError','ValueError','KeyError','OSError','TimeoutError','AssertionError','AttributeError','ImportError','ModuleNotFoundError','FileNotFoundError','PermissionError','IndexError','OTHER_EXCEPTION'):
                    record['error_type']=stats['error_type']
                if stats.get('preflight_mismatch_kind') in ('WORD_COUNT_DIFFERENCE','WORD_SCHEMA_DIFFERENCE','WORD_TYPE_DIFFERENCE','OCR_TEXT_DIFFERENCE','LINE_ID_DIFFERENCE','BOX_COORDINATE_DIFFERENCE','CONF_FIELD_DIFFERENCE','INVALID_CONF_VALUE','CONF_TOLERANCE_EXCEEDED','EMPTY_OCR_OUTPUT','INVALID_ADAPTER_TYPE','INVALID_WORD_TYPE'):
                    record['preflight_mismatch_kind']=stats['preflight_mismatch_kind']
                record['preflight_pairs']=stats.get('preflight_pairs',0)
                record['preflight_equal_pairs']=stats.get('preflight_equal_pairs',0)
                for metric in ('processing_s','run_and_audit_s','preflight_s',
                               'worker_ocr_calls','worker_ocr_wall_s'):
                    if metric in stats:
                        record[metric]=stats[metric]
                if mode=='hybrid':
                    evidence=stats.get('provider_evidence') or {}
                    counts=evidence.get('node_events') or {}
                    session_counts=evidence.get('sessions') or {}
                    record['provider_evidence']={
                        'sessions':{k:int(session_counts.get(k,0)) for k in ('det','cls','rec')},
                        'node_events':{k:int(counts.get(k,0)) for k in ('det_cuda','det_cpu','cls_cpu','cls_cuda','rec_cpu','rec_cuda')},
                        'profiles_read':int(evidence.get('profiles_read',0)),
                    }
        except (ValueError,OSError,TypeError):
            pass
    if spawn_error:
        record['reason']='SPAWN_ERROR'
        return record
    if rc is None:
        record['reason']='TIMEOUT'
        return record
    if rc!=0:
        record['reason']=record.get('child_status') or 'CHILD_FAILED'
        return record
    if record.get('child_status')!='COMPLETE':
        record['reason']='INCOMPLETE_CHILD'
        return record
    if mode=='hybrid':
        if record.get('preflight_pairs')!=2 or record.get('preflight_equal_pairs')!=2:
            record['reason']='OCR_ADAPTER_MISMATCH'
            return record
        if record.get('worker_ocr_calls')!=14:
            record['reason']='INCOMPLETE_DUAL_ZOOM_OCR'
            return record
        if not provider_gate(record.get('provider_evidence',{})):
            record['reason']='HYBRID_PROVIDER_NOT_VERIFIED'
            return record
    pdf=target/'pdf_result'/'pii_face_7page_masked.pdf'
    audit=target/'pdf_result'/'pdf_face_7page_audit.json'
    if not pdf.is_file() or not audit.is_file():
        record['reason']='NO_PDF_OR_AUDIT'
        return record
    try:
        gt=gate.audit_status(audit)
        structure=gate.check_pdf(pdf)
        record['text_regions_pass']=gt['text_regions_pass']
        record['face_map_boxes_pass']=gt['face_map_boxes_pass']
        record['pdf_structure_pass']=structure['pass']
        if not(gt['automated_pass'] and structure['pass'] and
               gt['text_regions_pass']==27 and gt['face_map_boxes_pass']==17):
            record['reason']='SECURITY_GATE_FAILED'
            return record
        record['processing_s']=float(record['processing_s'])
        record['safe']=True
    except (ValueError,TypeError,KeyError,OSError):
        record['reason']='SECURITY_GATE_ERROR'
    return record


def strictly_identical_pages(gate,a,b):
    result=gate.compare_page_pixels(a,b,dpi=150)
    return bool(result.get('all_identical') and result.get('pages_compared')==7
                and result.get('identical_pages')==7)


def summarize(report):
    pairs=report.get('pairs',[])
    if not pairs or any(not(p.get('cpu',{}).get('safe') and p.get('hybrid',{}).get('safe'))
                        for p in pairs):
        return {'status':'SECURITY_OR_EXECUTION_FAILED'}
    if any(not p.get('pixel_identical') for p in pairs):
        return {'status':'PDF_PIXEL_DIFFERENCE'}
    base=[float(p['cpu']['processing_s']) for p in pairs]
    trial=[float(p['hybrid']['processing_s']) for p in pairs]
    b,c=statistics.median(base),statistics.median(trial)
    if b<=0:
        return {'status':'INVALID_TIMINGS'}
    delta=100*(b-c)/b
    paired=[round(100*(x-y)/x,2) for x,y in zip(base,trial)]
    status=('INSUFFICIENT_REPEATS' if len(pairs)<3 else
            'PROMISING_FULL_PDF_AB_ONLY' if delta>=10 and
            sum(v>0 for v in paired)>=len(paired)-1 else 'NO_CONFIRMED_FULL_PDF_GAIN')
    return {'status':status,'pairs':len(pairs),
            'cpu_processing_median_s':round(b,3),
            'hybrid_processing_median_s':round(c,3),
            'improvement_pct':round(delta,2),
            'paired_improvement_pct':paired,
            'positive_pairs':sum(v>0 for v in paired)}


def write_report(path, report):
    path.mkdir(parents=True,exist_ok=True)
    (path/REPORT_JSON).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    cmp=report.get('comparison',{})
    lines=['# V6.6.7.6：混合 OCR 完整 PDF 安全 A/B（固定合成檔）','',
           f"- 狀態：**{report['status']}**",
           '- 比較正式 CPU OCR 與隔離 GPU worker（det CUDA、cls/rec CPU）。',
           '- 正式 `.venv` 不變，GPU worker 使用獨立 `.venv_ocr_gpu`。',
           '- 固定七頁合成 PDF、保留 2×+3×、PII/人臉、V5 PDF 輸出。',
           '- 混合模式在完整執行前需通過兩組原始 OCR adapter 完全一致的預檢。',
           '- PDF 安全驗收、逐頁像素一致，否則禁止採用。',
           '- A/B 的 processing_s 不含獨立子程序啟動和混合模式預檢，不能當成 GUI 端到端時間。',
           '- **不得用於真實文件，不會變更正式程式與模型。**','',
           '| 指標 | 結果 |','|---|---:|',
           f"| CPU 純處理中位數 | {cmp.get('cpu_processing_median_s','—')} 秒 |",
           f"| 混合 OCR 純處理中位數 | {cmp.get('hybrid_processing_median_s','—')} 秒 |",
           f"| 改善幅度 | {cmp.get('improvement_pct','—')}% |",
           f"| 安全 PDF 像素一致配對 | {sum(bool(x.get('pixel_identical')) for x in report.get('pairs',[]))} / {len(report.get('pairs',[]))} |",
           '', '## 逐組結果','','| 組次 | 次序 | CPU 秒 | 混合秒 | 像素一致 |',
           '|---|---|---:|---:|---|']
    for pair in report.get('pairs',[]):
        lines.append(f"| {pair['pair']} | {pair['order']} | {pair.get('cpu',{}).get('processing_s','—')} | {pair.get('hybrid',{}).get('processing_s','—')} | {pair.get('pixel_identical') if pair.get('pixel_identical') is not None else '尚未比較'} |")
        for mode in ('cpu','hybrid'):
            t=pair.get(mode,{})
            if t and not t.get('safe'):
                lines.append(f"- {mode} 未通過：reason={t.get('reason') or 'UNKNOWN'}，stage={t.get('child_stage') or 'UNKNOWN'}，error_type={t.get('error_type') or 'none'}，adapter={t.get('preflight_mismatch_kind') or 'none'}")
    lines += ['', '- 所有時間為測試資料；包含的 CPU OCR 與 GPU worker 通訊成本需另行評估。',
              '- 27/27 是固定文字 GT；17/17 是已記錄人臉 map 框，不是所有真實人臉召回率。',
              '- 輸出僅含統計，不保存 OCR 文字、圖片、PDF、私有 map 或 ORT profile。',
              '- 強制結束可能留下 %TEMP% 內 pii_v6676_ 前綴目錄，請手動確認清除。','']
    (path/REPORT_MD).write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--project-root',default='.')
    ap.add_argument('--results-dir',default='v6676_full_pdf_results')
    ap.add_argument('--repeats',type=int,choices=(1,2,3),default=1)
    ap.add_argument('--timeout',type=int,default=1800)
    ap.add_argument('--check-only',action='store_true')
    a=ap.parse_args(argv)
    root=Path(a.project_root).resolve()
    tool=Path(__file__).resolve().parent
    source=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
    result=(root/a.results_dir).resolve()
    if result==root or root not in result.parents:
        raise RuntimeError('RESULTS_DIR_MUST_BE_WITHIN_PROJECT')
    gate,gate_dir,expected,app_python,gpu_python=require_setup(root,source,tool)
    if a.check_only:
        print('CHECK_OK: synthetic fixture, V5 toolkit, project OCR adapter and both Windows venvs found')
        print('NOTE: adapter equivalence and CUDA kernels are verified only during actual test')
        return 0
    report={'schema':'pii_v6676_full_pdf_ab_v1','status':'INCOMPLETE',
            'fixture_sha256_verified':True,'only_synthetic_documents':True,
            'production_environment_changed':False,'production_code_modified':False,
            'full_pdf_security_gate_executed':False,'dual_zoom_kept':True,
            'separate_gpu_worker':True,'repeats':a.repeats,'pairs':[],'comparison':{},
            'warning':'Preflight and model initialization excluded from processing_s. GUI untested.'}
    try:
        with tempfile.TemporaryDirectory(prefix='pii_v6676_') as temp:
            scratch=Path(temp)
            for i in range(a.repeats):
                # Fast fail: one-pair smoke starts with hybrid; alternate order in subsequent repeats.
                order=('hybrid','cpu') if i%2==0 else ('cpu','hybrid')
                pair={'pair':i+1,'order':'BA' if i%2==0 else 'AB',
                      'cpu':{},'hybrid':{},'pixel_identical':None}
                paths={}
                for idx,mode in enumerate(order):
                    print(f'[V6.6.7.6] Pair {i+1}/{a.repeats} {mode}: full synthetic PDF',flush=True)
                    folder=scratch/f'pair{i+1:02d}_{idx}_{mode}'
                    trial=run_trial(root,tool,gate,gate_dir,expected,app_python,gpu_python,
                                    source,mode,folder,a.timeout)
                    pair[mode]=trial
                    paths[mode]=folder
                    if not trial['safe']:
                        report['status']=trial.get('reason') or 'SECURITY_OR_EXECUTION_FAILED'
                        break
                if pair['cpu'].get('safe') and pair['hybrid'].get('safe'):
                    report['full_pdf_security_gate_executed']=True
                    pa=paths['cpu']/'pdf_result'/'pii_face_7page_masked.pdf'
                    pb=paths['hybrid']/'pdf_result'/'pii_face_7page_masked.pdf'
                    try:
                        pair['pixel_identical']=strictly_identical_pages(gate,pa,pb)
                    except (OSError,ValueError,TypeError,KeyError,RuntimeError):
                        report['status']='PDF_PIXEL_GATE_ERROR'
                    else:
                        if not pair['pixel_identical']:
                            report['status']='PDF_PIXEL_DIFFERENCE'
                report['pairs'].append(pair)
                if report['status']!='INCOMPLETE':
                    break
            if report['status']=='INCOMPLETE':
                report['comparison']=summarize(report)
                report['status']=report['comparison']['status']
            else:
                report['comparison']={'status':report['status']}
    finally:
        write_report(result,report)
    print('[V6.6.7.6] Status:',report['status'])
    print('[V6.6.7.6] Report:',result/REPORT_MD)
    return 0 if report['status'] in ('INSUFFICIENT_REPEATS','PROMISING_FULL_PDF_AB_ONLY','NO_CONFIRMED_FULL_PDF_GAIN') else 2

if __name__=='__main__':
    raise SystemExit(main())
