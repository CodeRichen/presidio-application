#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.5 — isolated CPU / CUDA RapidOCR screening on a SHA-pinned synthetic PDF.

No real documents, OCR text, OCR hashes, positions or profiler JSON are persisted.
This is NOT a full PII / face / PDF security A/B and must not be deployed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import contextlib
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

EXPECTED_SHA = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
EXPECTED_PAGES = 7
ZOOMS = (2.0, 3.0)
REPORT_JSON = 'v665_rapidocr_gpu_summary.json'
REPORT_MD = 'v665_rapidocr_gpu_summary.md'


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def require_fixture(project: Path) -> Path:
    source = project / 'PII_Face_PDF_7page_Test' / 'pii_face_7page_scan_source.pdf'
    if not source.is_file() or sha256(source) != EXPECTED_SHA:
        raise RuntimeError('FIXTURE_MISSING_OR_SHA_MISMATCH')
    return source


def verify_environment() -> dict:
    import onnxruntime as ort
    versions = {
        'onnxruntime': ort.__version__,
        'onnxruntime-gpu': importlib.metadata.version('onnxruntime-gpu'),
        'rapidocr': importlib.metadata.version('rapidocr'),
    }
    providers = ort.get_available_providers()
    if versions['onnxruntime'] != '1.31.1' or versions['onnxruntime-gpu'] != '1.31.1':
        raise RuntimeError('ORT_GPU_VERSION_MISMATCH')
    if versions['rapidocr'] != '3.10.0':
        raise RuntimeError('RAPIDOCR_VERSION_MISMATCH')
    if 'CUDAExecutionProvider' not in providers or 'CPUExecutionProvider' not in providers:
        raise RuntimeError('CUDA_OR_CPU_PROVIDER_UNAVAILABLE')
    return {'versions': versions, 'cuda_available': True, 'cpu_available': True}


def preprocess(img):
    """Copy the current handler_media.py OCR preprocessing, without importing the app."""
    from PIL import ImageEnhance, ImageFilter
    return (ImageEnhance.Contrast(img.convert('L')).enhance(1.8)
            .filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2))
            .convert('RGB'))


def render_page(page, zoom: float):
    import fitz
    from PIL import Image
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), annots=False)
    if pix.n != 3:
        raise RuntimeError('UNEXPECTED_PDF_PIXEL_FORMAT')
    return preprocess(Image.frombytes('RGB', (pix.width, pix.height), pix.samples))


def status_for(checked: int, cpu_sessions: int, gpu_sessions: int,
               cpu_nodes: int, gpu_nodes: int, mismatches: int, expected: int = 14) -> str:
    if checked != expected:
        return 'INCOMPLETE_DUAL_ZOOM'
    if cpu_sessions <= 0 or cpu_nodes <= 0:
        return 'CPU_PROVIDER_NOT_VERIFIED'
    if gpu_sessions <= 0 or gpu_nodes <= 0:
        return 'NO_CONFIRMED_CUDA_OCR_KERNEL'
    if mismatches:
        return 'OCR_OUTPUT_DIFFERENCE_NEEDS_REVIEW'
    return 'OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY'


def compare_ocr_output(cpu, gpu) -> dict:
    """Compare in RAM; never return text/boxes/hashes to the caller."""
    import numpy as np
    raw_a = getattr(cpu, 'txts', None)
    raw_b = getattr(gpu, 'txts', None)
    a = tuple(raw_a if raw_a is not None else ())
    b = tuple(raw_b if raw_b is not None else ())
    # Equality must be exact for text; a near match can change downstream PII.
    text_equal = (a == b)
    a_box = getattr(cpu, 'boxes', None)
    b_box = getattr(gpu, 'boxes', None)
    a_box = np.asarray(a_box if a_box is not None else [], dtype=np.float64)
    b_box = np.asarray(b_box if b_box is not None else [], dtype=np.float64)
    shape_equal = a_box.shape == b_box.shape
    box_delta = (float(np.max(np.abs(a_box - b_box)))
                 if shape_equal and a_box.size else (0.0 if shape_equal else None))
    # OCR-only screening: <=0.5 input-image pixel deviation, not PDF pixel equality.
    boxes_equal = bool(shape_equal and box_delta is not None and box_delta <= 0.5)
    raw_as = getattr(cpu, 'scores', None)
    raw_bs = getattr(gpu, 'scores', None)
    a_scores = np.asarray(raw_as if raw_as is not None else [], dtype=np.float64)
    b_scores = np.asarray(raw_bs if raw_bs is not None else [], dtype=np.float64)
    scores_equal = bool(a_scores.shape == b_scores.shape and
                        np.allclose(a_scores, b_scores, atol=0.002, rtol=0))
    return {
        'cpu_lines': len(a), 'gpu_lines': len(b),
        'exact_text_equal': bool(text_equal),
        'boxes_within_half_pixel': boxes_equal,
        'max_box_delta_px': round(box_delta, 4) if box_delta is not None else None,
        'scores_within_0p002': scores_equal,
        'equivalent_for_screening': bool(text_equal and boxes_equal and scores_equal),
    }


def phase_seconds(ocr_output) -> dict:
    phases = getattr(ocr_output, 'elapse_list', None)
    if not isinstance(phases, (list, tuple)) or len(phases) != 3:
        return {}
    try:
        return {k: round(float(v), 4) for k, v in zip(('det', 'cls', 'rec'), phases)}
    except (TypeError, ValueError):
        return {}


class SessionMeter:
    """Enable per-session profiling in temp; CPU explicitly locked; GPU not forced.

    Keeping GPU providers as requested by RapidOCR means a successful smoke also
    tests RapidOCR's own `use_cuda` configuration, not just a monkeypatch.
    """
    def __init__(self, ort, temp: Path):
        self.ort, self.temp = ort, Path(temp)
        self.orig = None
        self.mode = None
        self.sessions: list[tuple[str, object, tuple[str, ...]]] = []

    def __enter__(self):
        self.orig = self.ort.InferenceSession
        meter = self
        def wrapped(model, sess_options=None, *args, **kwargs):
            if meter.mode not in ('cpu', 'cuda'):
                raise RuntimeError('ORT_SESSION_CREATED_WITHOUT_TEST_MODE')
            options = sess_options if sess_options is not None else meter.ort.SessionOptions()
            options.enable_profiling = True
            options.profile_file_prefix = str(meter.temp / 'ocr_profile')
            if meter.mode == 'cpu':
                # Make a real CPU-only control inside the GPU-capable ORT build.
                if args:
                    # Provider options must match the replacement CPU-only list.
                    args = (['CPUExecutionProvider'],) + ((None,) if len(args) > 1 else ()) + args[2:]
                    kwargs.pop('providers', None)
                else:
                    kwargs['providers'] = ['CPUExecutionProvider']
                kwargs.pop('provider_options', None)
            session = meter.orig(model, options, *args, **kwargs)
            meter.sessions.append((meter.mode, session, tuple(session.get_providers())))
            return session
        self.ort.InferenceSession = wrapped
        return self

    def __exit__(self, ex_type, ex, tb):
        self.ort.InferenceSession = self.orig
        self.orig = None
        return False

    def counts(self) -> dict:
        output = {'cpu_sessions': 0, 'gpu_sessions_with_cuda': 0,
                  'gpu_sessions_cpu_fallback_only': 0,
                  'cpu_node_events': 0, 'cuda_node_events': 0,
                  'gpu_cpu_fallback_node_events': 0,
                  'profile_files_read': 0}
        for mode, sess, providers in self.sessions:
            if mode == 'cpu':
                if providers != ('CPUExecutionProvider',):
                    continue
                output['cpu_sessions'] += 1
            elif 'CUDAExecutionProvider' in providers:
                output['gpu_sessions_with_cuda'] += 1
            else:
                output['gpu_sessions_cpu_fallback_only'] += 1
            try:
                filepath = Path(sess.end_profiling())
                raw = json.loads(filepath.read_text(encoding='utf-8'))
                output['profile_files_read'] += 1
                for event in raw:
                    if event.get('cat') != 'Node':
                        continue
                    p = (event.get('args') or {}).get('provider')
                    if mode == 'cpu' and p == 'CPUExecutionProvider':
                        output['cpu_node_events'] += 1
                    elif mode == 'cuda' and p == 'CUDAExecutionProvider':
                        output['cuda_node_events'] += 1
                    elif mode == 'cuda' and p == 'CPUExecutionProvider':
                        output['gpu_cpu_fallback_node_events'] += 1
            except (OSError, ValueError, TypeError):
                continue
        return output


def write_reports(target: Path, report: dict) -> None:
    target.mkdir(parents=True, exist_ok=True)
    (target / REPORT_JSON).write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    timings = report.get('measurements', {})
    checks = report.get('checks', {})
    nodes = report.get('provider_events', {})
    lines = [
        '# V6.6.5 RapidOCR CPU / CUDA OCR-only A/B（固定合成 PDF）', '',
        f"- 狀態：**{report['status']}**",
        '- 只使用 SHA-256 固定的七頁合成 PDF，2×+3× 共 14 個頁面／解析度配對。',
        '- 同一獨立 .venv_ocr_gpu；CPU 及 GPU 皆使用 RapidOCR 3.10.0 與 ORT GPU 1.31.1。',
        '- GPU 使用 CUDA 的 cudnn_conv_algo_search=DEFAULT，避免 EXHAUSTIVE 重複搜尋造成延遲。',
        '- 此測試不是完整 PDF 遮蔽／PII／人臉／輸出安全回歸，正式程式沒有修改。',
        '- 報告不儲存 OCR 文字、個資字串、文字雜湊、字框座標、圖片或原始 ONNX profile。', '',
        '| 指標 | 結果 |', '|---|---:|',
        f"| OCR 配對數 | {checks.get('pairs_completed', 0)} / 14 |",
        f"| 輸出等價配對 | {checks.get('equivalent_pairs', 0)} |",
        f"| 文字完全一致配對 | {checks.get('text_equal_pairs', 0)} |",
        f"| 字框偏差 ≤0.5 px 配對 | {checks.get('boxes_equal_pairs', 0)} |",
        f"| CUDA Node 事件 | {nodes.get('cuda_node_events', 0)} |",
        f"| CPU Node 事件 | {nodes.get('cpu_node_events', 0)} |",
        f"| CPU OCR 累積處理秒數 | {timings.get('cpu_total_s', '—')} |",
        f"| GPU OCR 累積處理秒數 | {timings.get('gpu_total_s', '—')} |",
        f"| 純 OCR 差異 | {timings.get('improvement_pct', '—')}% |", '',
        '本數據不含正式 APP PII／人臉／V5 PDF 輸出，也不代表可部署加速。',
        '如結果存在 OCR 文字、框或信心分數差異，禁止直接替換正式引擎。',
        '首次模型載入不列入比對計時，不能拿本數據當完整 GUI 效能。',
        '若強制中止，請檢查 %TEMP% 的 pii_v665_ 前綴暫存目錄。',
    ]
    (target / REPORT_MD).write_text('\n'.join(lines) + '\n', encoding='utf-8')


def run_trial(project: Path, repeats: int) -> dict:
    source = require_fixture(project)
    env = verify_environment()
    import fitz
    import onnxruntime as ort
    import numpy as np
    if hasattr(ort, 'preload_dlls'):
        ort.preload_dlls(directory='')
    outcomes = []
    times = {'cpu': [], 'cuda': []}
    per_zoom = {'2x': {'cpu': [], 'cuda': []}, '3x': {'cpu': [], 'cuda': []}}
    per_stage = {mode: {'det': [], 'cls': [], 'rec': []} for mode in ('cpu','cuda')}
    warmup = {}
    with tempfile.TemporaryDirectory(prefix='pii_v665_') as scratch:
        with SessionMeter(ort, Path(scratch)) as meter:
            # Import RapidOCR *after* patching ORT, as internal modules may
            # bind InferenceSession on import and otherwise escape profiling.
            from rapidocr import RapidOCR
            meter.mode = 'cpu'
            cpu = RapidOCR(params={'EngineConfig.onnxruntime.use_cuda': False})
            meter.mode = 'cuda'
            gpu = RapidOCR(params={
                'EngineConfig.onnxruntime.use_cuda': True,
                'EngineConfig.onnxruntime.cuda_ep_cfg.cudnn_conv_algo_search': 'DEFAULT',
            })
            with fitz.open(source) as pdf:
                if len(pdf) != EXPECTED_PAGES:
                    raise RuntimeError('UNEXPECTED_PDF_PAGE_COUNT')
                # Warm up both models using the SAME synthetic input before timing.
                image = render_page(pdf[0], 2.0)
                for mode, engine in (('cpu', cpu), ('cuda', gpu)):
                    meter.mode = mode
                    started = time.perf_counter()
                    engine(image)
                    warmup[mode] = round(time.perf_counter() - started, 3)
                del image
                for r in range(repeats):
                    for page_i, page in enumerate(pdf):
                        for zoom in ZOOMS:
                            image = render_page(page, zoom)
                            answers = {}
                            # AB/BA reversal per item; comparison and timings in RAM only.
                            order = (('cpu','cuda') if (r + page_i + int(zoom)) % 2 == 0
                                     else ('cuda','cpu'))
                            for mode in order:
                                engine = cpu if mode == 'cpu' else gpu
                                meter.mode = mode
                                t0 = time.perf_counter()
                                answers[mode] = engine(image)
                                elapsed = time.perf_counter() - t0
                                times[mode].append(elapsed)
                                per_zoom[f'{int(zoom)}x'][mode].append(elapsed)
                                for k, val in phase_seconds(answers[mode]).items():
                                    per_stage[mode][k].append(val)
                            comparison = compare_ocr_output(answers['cpu'], answers['cuda'])
                            comparison['zoom'] = f'{int(zoom)}x'
                            outcomes.append(comparison)
                            del answers, image
            # Must collect profiles while sessions still alive; raw profiles stay temp.
            node_counts = meter.counts()
    checked = len(outcomes)
    mismatches = sum(not p['equivalent_for_screening'] for p in outcomes)
    cpu_total = sum(times['cpu'])
    cuda_total = sum(times['cuda'])
    status = status_for(checked, node_counts['cpu_sessions'],
                        node_counts['gpu_sessions_with_cuda'],
                        node_counts['cpu_node_events'], node_counts['cuda_node_events'], mismatches,
                        EXPECTED_PAGES * len(ZOOMS) * repeats)
    if node_counts['gpu_sessions_cpu_fallback_only']:
        status = 'GPU_SESSION_FELL_BACK_TO_CPU'
    if not outcomes or not any(p['cpu_lines'] or p['gpu_lines'] for p in outcomes):
        status = 'NO_TEXT_DETECTED_UNSUITABLE_FIXTURE'
    summary = {
        'schema':'pii_v665_rapidocr_gpu_screen_v1',
        'status':status, 'synthetic_only':True,
        'production_environment_changed':False,
        'production_code_modified':False,
        'full_pdf_security_gate_executed':False,
        'fixture_sha256_verified':True,
        'pdf_pages':EXPECTED_PAGES, 'zoom_values':[2,3], 'repeats':repeats,
        'environment':env, 'cuda_conv_search':'DEFAULT',
        'checks':{
            'pairs_completed':checked,
            'equivalent_pairs': checked-mismatches,
            'text_equal_pairs':sum(p['exact_text_equal'] for p in outcomes),
            'boxes_equal_pairs':sum(p['boxes_within_half_pixel'] for p in outcomes),
            'scores_equal_pairs':sum(p['scores_within_0p002'] for p in outcomes),
            'mismatched_pairs':mismatches,
            'cpu_lines_total':sum(p['cpu_lines'] for p in outcomes),
            'gpu_lines_total':sum(p['gpu_lines'] for p in outcomes),
            'max_box_delta_px':round(max((p['max_box_delta_px'] for p in outcomes
                                         if p['max_box_delta_px'] is not None),default=0),4),
            'by_zoom': {z: {
                'pairs': sum(p['zoom'] == z for p in outcomes),
                'equivalent': sum(p['zoom'] == z and p['equivalent_for_screening'] for p in outcomes),
                'text_equal': sum(p['zoom'] == z and p['exact_text_equal'] for p in outcomes),
            } for z in ('2x','3x')},
        },
        'provider_events':node_counts,
        'measurements':{
            'cpu_warmup_s':warmup['cpu'], 'gpu_warmup_s':warmup['cuda'],
            'cpu_total_s':round(cpu_total,3),'gpu_total_s':round(cuda_total,3),
            'improvement_pct':round(100*(cpu_total-cuda_total)/cpu_total,2) if cpu_total else None,
            'by_zoom_s':{z:{mode:round(sum(values),3) for mode,values in v.items()}
                         for z,v in per_zoom.items()},
            'stage_means_s':{mode:{k:(round(statistics.mean(v),4) if v else None)
                                   for k,v in stages.items()} for mode,stages in per_stage.items()},
        },
        'warning':'OCR-only screening; no PII/face/PDF security acceptance and no GUI measurement.'
    }
    return summary


def main(argv=None) -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--project-root',default='.')
    p.add_argument('--results-dir',default='v665_rapidocr_gpu_results')
    p.add_argument('--check-only',action='store_true')
    p.add_argument('--repeats',type=int,choices=(1,2),default=1)
    args=p.parse_args(argv)
    project=Path(args.project_root).resolve()
    report_dir=(project/args.results_dir).resolve()
    tool=Path(__file__).resolve().parent
    if tool.name != 'PII_V665_RapidOCR_GPU_Trial' or tool.parent != project:
        print('INSTALL_V665_IN_PROJECT_ROOT')
        return 2
    # Reject accidental execution with the official production venv.
    if Path(sys.prefix).resolve() != (project / '.venv_ocr_gpu').resolve():
        print('MUST_RUN_WITH_ISOLATED_DOT_VENV_OCR_GPU')
        return 2
    try:
        require_fixture(project)
        env=verify_environment()
        if args.check_only:
            print('CHECK_OK: SHA-pinned synthetic fixture, CUDA+CPU EP, ORT GPU 1.31.1, RapidOCR 3.10.0')
            return 0
        report=run_trial(project, args.repeats)
    except Exception as exc:
        # No exception strings or tracebacks: they may contain OCR text or paths.
        report={'schema':'pii_v665_rapidocr_gpu_screen_v1','status':'RUNTIME_ERROR',
                'error_kind':type(exc).__name__,
                'synthetic_only':True,'production_environment_changed':False,
                'production_code_modified':False,'full_pdf_security_gate_executed':False}
    write_reports(report_dir,report)
    print('V6.6.5 status:',report['status'])
    print('Report files:',REPORT_JSON,REPORT_MD)
    return 0 if report['status']=='OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY' else 2


if __name__ == '__main__':
    sys.exit(main())
