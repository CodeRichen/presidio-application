#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.6: experimental CPU vs all-CUDA vs hybrid Det(CUDA)+Cls/Rec(CPU).

Only the pinned synthetic 7-page fixture may be processed. No changes to
production venv, models, app, PDFs or private restore maps. Text/bboxes and
ORT profiles live in process RAM or TemporaryDirectory only.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
import json
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time

from v666_support import (require_fixture, verify_environment, render_page,
                          compare_ocr_output, phase_seconds)

REPORT_JSON = 'v666_hybrid_ocr_summary.json'
REPORT_MD = 'v666_hybrid_ocr_summary.md'
MODES = ('cpu', 'cuda', 'hybrid')
ZOOMS = (2.0, 3.0)


def identify_model(model) -> str:
    """Fail closed on ambiguous models, bytes, or changed model naming."""
    if not isinstance(model, (str, Path)):
        raise RuntimeError('MODEL_PATH_NOT_CLASSIFIABLE')
    name = Path(model).name.lower()
    if not name.endswith('.onnx'):
        raise RuntimeError('UNSUPPORTED_MODEL_EXTENSION')
    kinds = [k for k in ('det', 'cls', 'rec')
             if re.search(r'(^|[._-])' + k + r'([._-]|$)', name)]
    if len(kinds) != 1:
        raise RuntimeError('AMBIGUOUS_OCR_MODEL_KIND')
    return kinds[0]


def force_cpu(args, kwargs):
    """Adjust providers, not the model; leave other session configuration intact."""
    args = list(args)
    kwargs = dict(kwargs)
    if args:
        args[0] = ['CPUExecutionProvider']
        if len(args) >= 2:
            args[1] = None
        kwargs.pop('providers', None)
        kwargs.pop('provider_options', None)
    else:
        kwargs['providers'] = ['CPUExecutionProvider']
        kwargs.pop('provider_options', None)
    return tuple(args), kwargs


class TrialSessions:
    def __init__(self, ort, temp):
        self.ort = ort
        self.temp = Path(temp)
        self.mode = None
        self.orig = None
        self.created = []

    def __enter__(self):
        self.orig = self.ort.InferenceSession
        owner = self

        def intercepted(model, sess_options=None, *args, **kwargs):
            if owner.mode not in MODES:
                raise RuntimeError('SESSION_CREATED_WITHOUT_TRIAL_MODE')
            kind = identify_model(model)
            opts = sess_options if sess_options is not None else owner.ort.SessionOptions()
            opts.enable_profiling = True
            opts.profile_file_prefix = str(owner.temp / 'hybrid_ocr_profile')
            if owner.mode == 'cpu' or (owner.mode == 'hybrid' and kind in ('cls', 'rec')):
                args, kwargs = force_cpu(args, kwargs)
            session = owner.orig(model, opts, *args, **kwargs)
            providers = tuple(session.get_providers())
            if owner.mode == 'cpu' and providers != ('CPUExecutionProvider',):
                raise RuntimeError('CPU_SESSION_PROVIDER_MISMATCH')
            if (owner.mode == 'hybrid' and kind in ('cls', 'rec')
                    and providers != ('CPUExecutionProvider',)):
                raise RuntimeError('HYBRID_CPU_STAGE_PROVIDER_MISMATCH')
            if (owner.mode in ('cuda', 'hybrid') and (owner.mode == 'cuda' or kind == 'det')
                    and 'CUDAExecutionProvider' not in providers):
                raise RuntimeError('CUDA_STAGE_PROVIDER_MISMATCH')
            owner.created.append((owner.mode, kind, session, providers))
            return session

        self.ort.InferenceSession = intercepted
        return self

    def __exit__(self, exc_type, exc, tb):
        self.ort.InferenceSession = self.orig
        return False

    def counts(self):
        stage_sessions = defaultdict(int)
        provider_nodes = defaultdict(int)
        profiles_read = 0
        for mode, kind, session, providers in self.created:
            stage_sessions[f'{mode}_{kind}'] += 1
            try:
                profile_path = Path(session.end_profiling())
                events = json.loads(profile_path.read_text(encoding='utf-8'))
                profiles_read += 1
                for event in events:
                    if event.get('cat') != 'Node':
                        continue
                    ep = (event.get('args') or {}).get('provider')
                    if ep == 'CPUExecutionProvider':
                        provider_nodes[f'{mode}_{kind}_cpu'] += 1
                    elif ep == 'CUDAExecutionProvider':
                        provider_nodes[f'{mode}_{kind}_cuda'] += 1
            except (OSError, ValueError, TypeError):
                continue
        return {
            'sessions': dict(sorted(stage_sessions.items())),
            'node_events': dict(sorted(provider_nodes.items())),
            'profiles_read': profiles_read,
        }


def provider_gate(info: dict) -> str | None:
    s, n = info['sessions'], info['node_events']
    for mode in MODES:
        for kind in ('det', 'cls', 'rec'):
            if s.get(f'{mode}_{kind}', 0) != 1:
                return 'SESSION_MODEL_ROUTE_UNVERIFIED'
            desired = 'cuda' if mode == 'cuda' or (mode == 'hybrid' and kind == 'det') else 'cpu'
            undesired = 'cpu' if desired == 'cuda' else 'cuda'
            if n.get(f'{mode}_{kind}_{desired}', 0) <= 0:
                return 'REQUIRED_PROVIDER_KERNEL_NOT_VERIFIED'
            # CUDA models can legitimately use CPU for unsupported ops.
            if desired == 'cpu' and n.get(f'{mode}_{kind}_{undesired}', 0) != 0:
                return 'CPU_ONLY_MODEL_EXECUTED_CUDA'
    return None


def perform(root: Path, repeats: int) -> dict:
    source = require_fixture(root)
    environment = verify_environment()
    import fitz
    import onnxruntime as ort
    preload = getattr(ort, 'preload_dlls', None)
    if callable(preload):
        preload(directory='')

    durations = {mode: [] for mode in MODES}
    by_zoom = {mode: {'2x': [], '3x': []} for mode in MODES}
    stages = {mode: {k: [] for k in ('det', 'cls', 'rec')} for mode in MODES}
    compare = {mode: {'pairs': 0, 'equivalent': 0, 'text_equal': 0,
                      'boxes_within_half_pixel': 0, 'scores_within_0p002': 0,
                      'max_box_delta_px': 0.0} for mode in ('cuda', 'hybrid')}
    warmup = {}
    observed_lines = {mode: 0 for mode in MODES}
    with tempfile.TemporaryDirectory(prefix='pii_v666_') as scratch:
        with TrialSessions(ort, scratch) as meter:
            # Import after interception; RapidOCR may bind InferenceSession eagerly.
            from rapidocr import RapidOCR
            engines = {}
            for mode in MODES:
                meter.mode = mode
                params = {
                    'EngineConfig.onnxruntime.use_cuda': mode != 'cpu',
                    'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'DEFAULT',
                }
                engines[mode] = RapidOCR(params=params)

            with fitz.open(source) as pdf:
                if len(pdf) != 7:
                    raise RuntimeError('UNEXPECTED_PAGE_COUNT')
                first_image = render_page(pdf[0], 2.0)
                for mode in MODES:
                    meter.mode = mode
                    t0 = time.perf_counter()
                    engines[mode](first_image)
                    warmup[mode] = round(time.perf_counter()-t0, 3)
                del first_image
                for round_id in range(repeats):
                    for page_id, page in enumerate(pdf):
                        for zoom in ZOOMS:
                            image = render_page(page, zoom)
                            outputs = {}
                            rotate = (round_id + page_id + int(zoom)) % 3
                            order = MODES[rotate:] + MODES[:rotate]
                            for mode in order:
                                meter.mode = mode
                                t0 = time.perf_counter()
                                outputs[mode] = engines[mode](image)
                                words = getattr(outputs[mode], 'txts', None)
                                observed_lines[mode] += len(words) if words is not None else 0
                                delta = time.perf_counter() - t0
                                durations[mode].append(delta)
                                by_zoom[mode][f'{int(zoom)}x'].append(delta)
                                for k,v in phase_seconds(outputs[mode]).items():
                                    stages[mode][k].append(v)
                            for mode in ('cuda', 'hybrid'):
                                eq = compare_ocr_output(outputs['cpu'], outputs[mode])
                                agg = compare[mode]
                                agg['pairs'] += 1
                                agg['equivalent'] += int(eq['equivalent_for_screening'])
                                agg['text_equal'] += int(eq['exact_text_equal'])
                                agg['boxes_within_half_pixel'] += int(eq['boxes_within_half_pixel'])
                                agg['scores_within_0p002'] += int(eq['scores_within_0p002'])
                                if eq['max_box_delta_px'] is not None:
                                    agg['max_box_delta_px'] = max(agg['max_box_delta_px'], eq['max_box_delta_px'])
                            del outputs, image
                        print(f'V6.6.6 {round_id+1}/{repeats}: page {page_id+1}/7 completed (2x+3x)', flush=True)
            providers = meter.counts()

    expected = 14*repeats
    status = provider_gate(providers)
    if status is None and (observed_lines['cpu'] <= 0 or any(v != observed_lines['cpu'] for v in observed_lines.values())):
        status = 'OCR_LINE_COUNTS_INVALID'
    if status is None and any(x['pairs'] != expected or x['equivalent'] != expected for x in compare.values()):
        status = 'OCR_OUTPUT_DIFFERENCE_NEEDS_REVIEW'
    if status is None:
        status = 'HYBRID_OCR_EQUIVALENT_SYNTHETIC_ONLY'
    totals = {mode: round(sum(durations[mode]), 3) for mode in MODES}
    def improvement(mode):
        return round(100*(sum(durations['cpu'])-sum(durations[mode]))/sum(durations['cpu']), 2)
    return {
        'schema':'pii_v666_hybrid_ocr_v1',
        'status':status, 'only_synthetic_documents':True,
        'fixture_sha256_verified':True, 'production_code_modified':False,
        'production_environment_changed':False,
        'full_pdf_security_gate_executed':False, 'dual_zoom_kept':True,
        'environment':environment, 'repeats':repeats,
        'routed_models':{'cpu':'all CPU', 'cuda':'all CUDA with possible ORT CPU fallback',
                         'hybrid':'det CUDA, cls and rec CPU'},
        'provider_evidence':providers,
        'output_comparisons':compare,
        'ocr_line_counts':observed_lines,
        'measurements':{
            'warmup_s':warmup, 'ocr_total_s':totals,
            'improvement_pct_vs_cpu':{k:improvement(k) for k in ('cuda','hybrid')},
            'by_zoom_s':{k:{z:round(sum(v),3) for z,v in item.items()} for k,item in by_zoom.items()},
            'stage_mean_s':{mode:{k:(round(statistics.mean(v),4) if v else None)
                                  for k,v in phase.items()} for mode,phase in stages.items()},
        },
        'warning': 'OCR-only ONNX profiling overhead and model warmup excluded from OCR totals. '
                   'GPU det + CPU rec/cls is an experimental session interception, not a supported RapidOCR config.'
    }


def write_reports(out: Path, report: dict):
    out.mkdir(parents=True, exist_ok=True)
    (out/REPORT_JSON).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    times=report.get('measurements', {})
    comps=report.get('output_comparisons', {})
    lines=[
        '# V6.6.6：RapidOCR CPU／全 CUDA／混合 GPU 偵測 A/B', '',
        f"- 狀態：**{report['status']}**",
        '- 僅固定 SHA-256 七頁合成 PDF，完整 2×+3× OCR。',
        '- 混合模式：det 使用 CUDA；cls、rec 強制 CPU，逐模型檢查 Provider Node events。',
        '- 透過隔離程序攔截 InferenceSession 達成實驗路由，非 RapidOCR 官方獨立開關。',
        '- 只比較 OCR 文字、字框與分數，不執行完整 PII／人臉／PDF 安全回歸。',
        '- 不更改正式 .venv 或專案檔案；不保存原文、裁圖、字框、私有 map、profile 或影像。',
        '', '| OCR 模式 | 累積秒數（不含預熱） | 相較 CPU |',
        '|---|---:|---:|',
    ]
    for mode in MODES:
        seconds=times.get('ocr_total_s',{}).get(mode,'—')
        improv=times.get('improvement_pct_vs_cpu',{}).get(mode,'—') if mode!='cpu' else '—'
        lines.append(f'| {mode} | {seconds} | {improv}% |')
    lines.extend(['','| 與 CPU 比對 | OCR 配對 | 等價 | 文字一致 |',
                  '|---|---:|---:|---:|'])
    for mode in ('cuda','hybrid'):
        c = comps.get(mode,{})
        lines.append(f"| {mode} | {c.get('pairs','—')} | {c.get('equivalent','—')} | {c.get('text_equal','—')} |")
    lines.extend(['',
        '- OCR 分數為 ±0.002 容許值，文字為完全相同；框座標容許 0.5 輸入像素。',
        '- CUDA 子模型可能有部分 CPU fallback；只強制 cls／rec 完全 CPU。',
        '- 未比較正式 GUI、冷啟動或 PDF 遮蔽結果；本版不能用於部署。',
        '- 強制終止時，需檢查 %TEMP% 中 pii_v666_ 開頭的暫存目錄。',
        ''])
    (out/REPORT_MD).write_text('\n'.join(lines),encoding='utf-8')


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v666_hybrid_results')
    p.add_argument('--check-only',action='store_true')
    p.add_argument('--repeats',type=int,choices=(1,2),default=1)
    args=p.parse_args(argv)
    root=Path(args.project_root).resolve()
    tool=Path(__file__).resolve().parent
    if tool.parent!=root or tool.name!='PII_V666_Hybrid_OCR':
        print('INSTALL_TOOL_IN_PROJECT_ROOT')
        return 2
    if Path(sys.prefix).resolve() != (root/'.venv_ocr_gpu').resolve():
        print('MUST_RUN_IN_ISOLATED_GPU_VENV')
        return 2
    try:
        require_fixture(root)
        verify_environment()
        if args.check_only:
            print('CHECK_OK: synthetic PDF, CUDA EP, RapidOCR 3.10.0, isolated GPU environment')
            return 0
        report=perform(root,args.repeats)
    except Exception as exc:
        # Never persist exception string: it could contain OCR/source content.
        report={'schema':'pii_v666_hybrid_ocr_v1','status':'RUNTIME_ERROR',
                'error_type':type(exc).__name__, 'only_synthetic_documents':True,
                'production_code_modified':False, 'full_pdf_security_gate_executed':False}
    write_reports((root/args.results_dir).resolve(),report)
    print('V6.6.6 STATUS:',report['status'])
    return 0 if report['status']=='HYBRID_OCR_EQUIVALENT_SYNTHETIC_ONLY' else 2

if __name__=='__main__':
    raise SystemExit(main())
