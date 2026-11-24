"""V6.6.2: CPU vs CUDA ORT provider experiment on fixed synthetic PDF.

Never modifies production files, never processes real documents. Only aggregated
numeric metrics survive TemporaryDirectory; all restored crop maps, PDFs and
ORT profile traces are ephemeral. No installation or package change occurs.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

from v662_child import checksum, FIXTURE_SHA256
KIT='PII_V5_Security_Acceptance'

def setup(root, source):
    kit=root/KIT
    if not (kit/'v5_acceptance.py').is_file(): raise RuntimeError('MISSING_V5_KIT')
    if not source.is_file() or checksum(source)!=FIXTURE_SHA256: raise RuntimeError('FIXTURE_MISSING_OR_CHANGED')
    sys.path.insert(0,str(kit))
    import v5_acceptance as gate
    gate.check_setup(root,source,kit)
    expected=json.loads((kit/'expected_text_and_faces.json').read_text(encoding='utf-8'))
    if len(expected['samples']) != 7 or sum(len(x['expected_text']) for x in expected['samples']) !=27:
        raise RuntimeError('FIXTURE_GT_INVALID')
    return gate,kit

def ort_status():
    try:
        import onnxruntime as ort
        providers=ort.get_available_providers()
        return {'ort_import_ok':True, 'cuda_ep_available':'CUDAExecutionProvider' in providers,
                'cpu_ep_available':'CPUExecutionProvider' in providers}
    except ImportError:
        return {'ort_import_ok':False,'cuda_ep_available':False,'cpu_ep_available':False}

def child_env():
    env=os.environ.copy()
    env.update({'PII_PDF_EXPORT_V5':'1','PII_PDF_EXPORT_V3':'0',
                'PII_OCR_BACKEND':'rapid','PII_NER_BACKEND':'ac',
                'PII_FACE_ENABLED':'1','PII_MEDIA_FP_PROFILE':'synthetic_face_test',
                'PII_MEDIA_FP_REPORT':'0','PII_MEDIA_CACHE':'1',
                'PII_ZH_SINGLE_PASS':'1'})
    return env

def perform(root, source, kit, tool, scratch, mode, idx, gate, timeout):
    folder=scratch/f'{idx:02d}_{mode}'
    folder.mkdir()
    cmd=[sys.executable,str(tool/'v662_child.py'),
         '--project-root',str(root),'--source',str(source),
         '--expected',str(kit/'expected_text_and_faces.json'),
         '--runner',str(kit/'run_7page_pdf_test.py'),
         '--results-dir',str(folder),'--mode',mode]
    start=time.perf_counter()
    try:
        p=subprocess.run(cmd,cwd=root,env=child_env(),
                         stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                         timeout=timeout,check=False)
        rc=p.returncode
    except subprocess.TimeoutExpired:
        rc=None
    result={'mode':mode,'elapsed_s':round(time.perf_counter()-start,3),
            'execution_ok':rc==0,'safe':False,'reason':None}
    pdf=folder/'pii_face_7page_masked.pdf'
    audit=folder/'pdf_face_7page_audit.json'
    stats=folder/'v662_provider_counts.json'
    if rc is None: result['reason']='TIMEOUT';return result,pdf
    if rc!=0: result['reason']='CHILD_FAILED';return result,pdf
    if not all(f.is_file() for f in (pdf,audit,stats)):
        result['reason']='MISSING_OUTPUT';return result,pdf
    try:
        counts=json.loads(stats.read_text(encoding='utf-8'))
        gt=gate.audit_status(audit)
        pdf_gate=gate.check_pdf(pdf)
        result.update({k:int(counts[k]) for k in (
            'ocr_session_create_calls','ocr_sessions_created','other_ort_sessions_bypassed',
            'cuda_enabled_sessions','cpu_only_sessions','cuda_kernel_events',
            'cpu_kernel_events','profiles_parsed')})
        result['safe']=bool(gt.get('automated_pass') and pdf_gate.get('pass') and
                           gt.get('text_regions_pass')==27 and gt.get('face_map_boxes_pass')==17)
        result['text_pass']=gt.get('text_regions_pass')
        result['face_pass']=gt.get('face_map_boxes_pass')
        result['pdf_structure_pass']=pdf_gate.get('pass')
        if not result['safe']: result['reason']='SECURITY_GATE_FAILED'
        elif counts['ocr_sessions_created']==0: result['reason']='OCR_SESSION_NOT_INTERCEPTED'
        elif mode=='cuda' and (counts['cuda_enabled_sessions']==0 or counts['cuda_kernel_events']==0):
            result['reason']='CUDA_EXECUTION_UNVERIFIED'
        elif mode=='cpu' and (counts['cpu_only_sessions']!=counts['ocr_sessions_created'] or counts['cpu_kernel_events']==0):
            result['reason']='CPU_EXECUTION_UNVERIFIED'
    except (OSError,KeyError,TypeError,ValueError):
        result['safe']=False
        result['reason']='REPORT_VALIDATION_FAILED'
    return result,pdf

def compare(gate, left, right):
    raw=gate.compare_page_pixels(left,right,dpi=150)
    return {'pages_compared':raw['pages_compared'],
            'identical_pages':raw['identical_pages'],
            'all_identical': bool(raw['all_identical'] and raw['pages_compared']==7 and raw['identical_pages']==7)}

def classify(baseline,candidate,pixels):
    if not baseline or len(baseline)!=len(candidate) or len(pixels)!=len(baseline):return {'status':'INCOMPLETE'}
    if any(not x['safe'] for x in baseline+candidate):return {'status':'SECURITY_GATE_FAILED'}
    for x in baseline+candidate:
        if x.get('reason'):return {'status':x['reason']}
    if any(not x['all_identical'] for x in pixels):return {'status':'PIXEL_DIFFERENCE'}
    b=statistics.median(x['elapsed_s'] for x in baseline)
    c=statistics.median(x['elapsed_s'] for x in candidate)
    pct=100*(b-c)/b
    return {'status':'PROMISING_AB_ONLY' if pct>=10 else 'NO_CONFIRMED_GAIN',
            'baseline_median_s':round(b,3),'candidate_median_s':round(c,3),
            'improvement_pct':round(pct,2),'pairs':len(baseline)}

def write_report(d, dest):
    dest.mkdir(parents=True, exist_ok=True)
    (dest/'v662_gpu_ab_summary.json').write_text(json.dumps(d,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# V6.6.2：RapidOCR CPU / CUDA ONNX Runtime 安全 A/B', '',
           f"**狀態：{d['status']}**", '',
           '- 只針對已驗證 SHA-256 的七頁合成 PDF；不修改正式專案、模型或安裝環境。',
           '- 2×+3× OCR、文字 PII、人臉遮蔽與 V5 安全重建均保留。',
           '- ORT 執行緒未調整；僅測 CPU 與 CUDA 執行提供者。',
           '- GPU 工作確認採用 ORT 暫存 profiling 中的 provider kernel 計數，而非只檢查 CUDA 是否列在可用清單。',
           '- 每組均執行完整遮蔽與安全稽核；相同配對逐頁比較 PDF 像素。',
           '- 本工具不儲存 OCR 原文、個資、影像、PDF、還原 map 或原始 ORT profile。',
           '- 本測試可能因 CUDA DLL／版本不相容而失敗；不會自動安裝任何套件。',
           '', '| 組次 | CPU 秒數 | CUDA 秒數 | CPU kernel events | CUDA kernel events | PDF 像素一致 |',
           '|---|---:|---:|---:|---:|---|']
    for i,(b,c) in enumerate(zip(d['baseline'],d['candidate']),1):
        px=d['pixel_checks'][i-1] if i-1<len(d['pixel_checks']) else {}
        lines.append(f"| {i} | {b.get('elapsed_s','—')} | {c.get('elapsed_s','—')} | {b.get('cpu_kernel_events','—')} | {c.get('cuda_kernel_events','—')} | {px.get('all_identical','—')} |")
    s=d.get('comparison',{})
    lines+=['',f"- CPU 中位數：{s.get('baseline_median_s','—')} 秒",
            f"- CUDA 中位數：{s.get('candidate_median_s','—')} 秒",
            f"- 加速幅度：{s.get('improvement_pct','—')}%",
            '- PROMISING_AB_ONLY 只代表固定合成文件有加速訊號，並不代表能部署於正式系統。',
            '- 固定 GT 遮蔽通過不代表對所有文件零漏判；GUI 響應未測。',
            '- 若程式被強制結束，請檢查 %TEMP% 內 pii_v662_ 前綴目錄並清除。','']
    (dest/'v662_gpu_ab_summary.md').write_text('\n'.join(lines),encoding='utf-8')

def main(argv=None):
    p=argparse.ArgumentParser()
    p.add_argument('--project-root',default='.')
    p.add_argument('--source',default='PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    p.add_argument('--results-dir',default='v662_gpu_results')
    p.add_argument('--repeats',type=int,choices=range(1,6),default=2)
    p.add_argument('--timeout',type=int,default=1200)
    p.add_argument('--check-only',action='store_true')
    a=p.parse_args(argv)
    root=Path(a.project_root).resolve()
    source=(root/a.source).resolve()
    tool=Path(__file__).resolve().parent
    if tool.parent!=root:raise RuntimeError('KEEP_TOOL_IN_PROJECT_ROOT')
    gate,kit=setup(root,source)
    providers=ort_status()
    if a.check_only:
        print('FIXTURE_OK: seven-page synthetic PDF & V5 safety gate verified')
        print('ORT_IMPORT_OK:',providers['ort_import_ok'])
        print('CUDA_EP_AVAILABLE:',providers['cuda_ep_available'])
        print('CPU_EP_AVAILABLE:',providers['cpu_ep_available'])
        print('NO_PACKAGE_INSTALL_PERFORMED')
        return 0
    report={'schema':'pii_v662_gpu_ab_1','status':'INCOMPLETE',
            'production_code_changed':False,'only_synthetic_documents':True,
            'dual_zoom_kept':True,'fixture_sha256_verified':True,
            'available':providers,'repeats':a.repeats,
            'baseline':[],'candidate':[],'pixel_checks':[],'comparison':{}}
    if not providers['cuda_ep_available']:
        report['status']='CUDA_EP_NOT_AVAILABLE'
        report['comparison']={'status':report['status']}
        write_report(report,(root/a.results_dir).resolve())
        print('CUDA_EP_NOT_AVAILABLE: skipped benchmark; no package changes')
        return 0
    with tempfile.TemporaryDirectory(prefix='pii_v662_') as td:
        scratch=Path(td)
        for idx in range(a.repeats):
            # Alternating ordering AB/BA to reduce systematic warmup bias.
            order=('cpu','cuda') if idx%2==0 else ('cuda','cpu')
            paths={}
            for mode in order:
                print(f'V6.6.2 pair {idx+1}/{a.repeats}: {mode} (private subprocess)',flush=True)
                r, pdf=perform(root,source,kit,tool,scratch,mode,2*idx+(mode=='cuda'),gate,a.timeout)
                report['baseline' if mode=='cpu' else 'candidate'].append(r)
                paths[mode]=pdf
                if not r.get('safe') or r.get('reason'):
                    report['status']=r.get('reason') or 'SECURITY_GATE_FAILED'
                    break
            if report['status']!='INCOMPLETE':break
            px=compare(gate,paths['cpu'],paths['cuda'])
            report['pixel_checks'].append(px)
            if not px['all_identical']:
                report['status']='PIXEL_DIFFERENCE'
                break
        if report['status']=='INCOMPLETE':
            report['comparison']=classify(report['baseline'],report['candidate'],report['pixel_checks'])
            report['status']=report['comparison']['status']
        else:
            report['comparison']={'status':report['status']}
    write_report(report,(root/a.results_dir).resolve())
    print('STATUS:',report['status'])
    return 0 if report['status'] in ('PROMISING_AB_ONLY','NO_CONFIRMED_GAIN') else 2

if __name__=='__main__':
    raise SystemExit(main())
