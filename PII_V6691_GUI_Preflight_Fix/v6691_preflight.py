#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9 source-only GUI integration readiness audit.

No project code is imported or executed; no OCR, user file, private map, GUI,
subprocess or model is started. The report is a strict allowlist of booleans,
status strings and safe counts. This is NOT production GUI validation.
"""
from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

KIT = 'PII_V6691_GUI_Preflight_Fix'
SCHEMA = 'pii_v6691_gui_static_preflight_v1'
FILE_NAMES = ('ui.py', 'core.py', 'handler_media.py', 'ocr_backend.py')
GUARD_DIRS = {'.git', '.venv', '.venv_ocr_gpu', 'presidio',
              'PII_V5_Security_Acceptance', 'PII_V6676_Full_PDF_Hybrid_AB', KIT}
KEYS = (
    'ui_app_class', 'ui_run_files', 'ui_background_start', 'ui_poll',
    'ui_dispatch_files_call', 'ui_background_thread', 'ui_hotkey_lock',
    'ui_done_queue', 'core_dispatch_files', 'core_media_entry_call',
    'core_media_exception_continues', 'core_explicit_failure_copies_original', 'core_scan_copies_source_tree', 'handler_media_mask_pdf',
    'handler_media_mask_image', 'handler_media_mask_file',
    'handler_media_ocr_words', 'handler_media_backend_wrapper',
    'ocr_backend_rapid_words', 'ocr_backend_wordbox_requested',
    'ocr_backend_adapter_converter', 'v6676_benchmark_present',
    'v6676_gpu_worker_present', 'v5_acceptance_present',
    'synthetic_fixture_present', 'windows_env_present',
    'windows_gpu_env_present',
)
REQUIRED = (
    'ui_app_class', 'ui_run_files', 'ui_background_start', 'ui_poll',
    'ui_dispatch_files_call', 'ui_background_thread', 'ui_done_queue',
    'core_dispatch_files', 'core_media_entry_call',
    'handler_media_mask_pdf', 'handler_media_mask_image',
    'handler_media_mask_file', 'handler_media_ocr_words',
    'handler_media_backend_wrapper', 'ocr_backend_rapid_words',
    'ocr_backend_wordbox_requested', 'ocr_backend_adapter_converter',
    'v6676_benchmark_present', 'v6676_gpu_worker_present',
    'v5_acceptance_present', 'synthetic_fixture_present',
)


def _name(n: ast.AST) -> str:
    if isinstance(n, ast.Name):
        return n.id
    if isinstance(n, ast.Attribute):
        return _name(n.value) + '.' + n.attr
    return ''


def _function(tree: ast.AST, name: str):
    for n in getattr(tree, 'body', ()):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _class(tree: ast.AST, name: str):
    return next((n for n in getattr(tree, 'body', ())
                 if isinstance(n, ast.ClassDef) and n.name == name), None)


def _has_call(tree: ast.AST | None, full_name: str) -> bool:
    return bool(tree and any(isinstance(n, ast.Call) and _name(n.func) == full_name
                             for n in ast.walk(tree)))


def _has_literal(tree: ast.AST | None, value: str) -> bool:
    return bool(tree and any(isinstance(n, ast.Constant) and n.value == value
                             for n in ast.walk(tree)))


def _has_word_box(tree: ast.AST | None) -> bool:
    for n in ast.walk(tree) if tree else ():
        if isinstance(n, ast.Call):
            for kw in n.keywords:
                if kw.arg == 'return_word_box' and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    return True
    return False


def _has_except_continue(tree: ast.AST | None) -> bool:
    # Do not infer safety from whether an exception is handled; this is a review flag.
    return bool(tree and any(isinstance(n, ast.ExceptHandler) and
                 any(isinstance(c, ast.Continue) for c in ast.walk(n))
                 for n in ast.walk(tree)))


def _media_entry_route(core: ast.AST | None, dispatch: ast.AST | None) -> bool:
    """Recognize either direct handler_media.mask_file OR an actual helper route.

    Important: A mere reference to _mask_media does not establish a valid route.
    The helper must route to mask_pdf or mask_image and invoke that callable.
    Static structural evidence only, not execution or safety verification.
    """
    if _has_call(dispatch, 'handler_media.mask_file'):
        return True
    helper = _function(core, '_mask_media') if core else None
    if not (_has_call(dispatch, '_mask_media') and helper):
        return False
    if _has_call(helper, 'handler_media.mask_file'):
        return True
    names = {_name(n) for n in ast.walk(helper) if isinstance(n, ast.Attribute)}
    has_both = {'handler_media.mask_pdf', 'handler_media.mask_image'} <= names
    # Original project uses "func = ...; entries = list(func(...))"
    has_callable = _has_call(helper, 'func')
    return bool(has_both and has_callable)


def _explicit_error_copies_original(core: ast.AST | None) -> bool:
    helper = _function(core, '_mask_media') if core else None
    return bool(helper and any(
        isinstance(n, ast.ExceptHandler) and _has_call(n, 'shutil.copy2')
        for n in ast.walk(helper)
    ))

def scan_trees(trees: dict[str, ast.AST], existence: dict[str, bool]) -> dict[str, bool]:
    result = {key: False for key in KEYS}
    ui = trees.get('ui.py')
    app = _class(ui, 'App') if ui else None
    run_files = _function(app, 'run_files') if app else None
    start = _function(app, '_start') if app else None
    poll = _function(app, '_poll') if app else None
    result.update({
        'ui_app_class': app is not None,
        'ui_run_files': run_files is not None,
        'ui_background_start': start is not None,
        'ui_poll': poll is not None,
        'ui_dispatch_files_call': _has_call(run_files, 'core.dispatch_files'),
        'ui_background_thread': _has_call(start, 'threading.Thread'),
        'ui_hotkey_lock': bool(start and any(_name(n) == 'hotkey_service.WORK_LOCK' for n in ast.walk(start))),
        'ui_done_queue': _has_literal(start, 'done') and _has_literal(poll, 'done'),
    })
    core = trees.get('core.py')
    dispatch = _function(core, 'dispatch_files') if core else None
    result.update({
        'core_dispatch_files': dispatch is not None,
        'core_media_entry_call': _media_entry_route(core, dispatch),
        'core_media_exception_continues': _has_except_continue(dispatch),
        'core_explicit_failure_copies_original': _explicit_error_copies_original(core),
        'core_scan_copies_source_tree': _has_call(_function(core, 'scan_folder'), 'shutil.copytree') if core else False,
    })
    media = trees.get('handler_media.py')
    result.update({
        'handler_media_mask_pdf': _function(media, 'mask_pdf') is not None if media else False,
        'handler_media_mask_image': _function(media, 'mask_image') is not None if media else False,
        'handler_media_mask_file': _function(media, 'mask_file') is not None if media else False,
        'handler_media_ocr_words': _function(media, 'ocr_words') is not None if media else False,
        'handler_media_backend_wrapper': _has_call(_function(media, 'ocr_words'), 'ocr_words_with_backend') if media else False,
    })
    backend = trees.get('ocr_backend.py')
    rapid = _function(backend, 'rapid_ocr_words') if backend else None
    result.update({
        'ocr_backend_rapid_words': rapid is not None,
        'ocr_backend_wordbox_requested': _has_word_box(rapid),
        'ocr_backend_adapter_converter': _has_call(rapid, 'rapid_result_to_words'),
    })
    for key in ('v6676_benchmark_present', 'v6676_gpu_worker_present',
                'v5_acceptance_present', 'synthetic_fixture_present',
                'windows_env_present', 'windows_gpu_env_present'):
        result[key] = bool(existence.get(key))
    return result


def source_trees(root: Path):
    trees, issues = {}, []
    for name in FILE_NAMES:
        file = root / name
        if not file.is_file():
            issues.append('MISSING_' + name.upper().replace('.', '_'))
            continue
        try:
            # Reject absurd or non-Python content without printing source or path.
            if file.stat().st_size > 3_000_000:
                issues.append('OVERSIZE_' + name.upper().replace('.', '_'))
                continue
            trees[name] = ast.parse(file.read_text(encoding='utf-8-sig'), filename=name)
        except (OSError, UnicodeError, SyntaxError, ValueError):
            issues.append('UNREADABLE_' + name.upper().replace('.', '_'))
    return trees, issues


def status_of(checks, issues):
    if issues:
        return 'SOURCE_FILES_MISSING_OR_UNREADABLE'
    if all(checks[k] for k in REQUIRED):
        return 'STATIC_HOOKS_READY_FOR_REVIEW'
    return 'SOURCE_SIGNATURE_CHANGED_REVIEW_REQUIRED'


def generate_report(root: Path):
    trees, issues = source_trees(root)
    existence = {
        'v6676_benchmark_present': (root/'PII_V6676_Full_PDF_Hybrid_AB'/'v6676_benchmark.py').is_file(),
        'v6676_gpu_worker_present': (root/'PII_V6676_Full_PDF_Hybrid_AB'/'v667_gpu_worker.py').is_file(),
        'v5_acceptance_present': (root/'PII_V5_Security_Acceptance'/'v5_acceptance.py').is_file(),
        'synthetic_fixture_present': (root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf').is_file(),
        'windows_env_present': (root/'.venv'/'Scripts'/'python.exe').is_file(),
        'windows_gpu_env_present': (root/'.venv_ocr_gpu'/'Scripts'/'python.exe').is_file(),
    }
    checks = scan_trees(trees, existence)
    status = status_of(checks, issues)
    return {
        'schema': SCHEMA, 'status': status,
        'mode': 'STATIC_SOURCE_INSPECTION_ONLY',
        'production_code_modified': False, 'production_environment_modified': False,
        'project_code_imported': False, 'gui_started': False,
        'ocr_executed': False, 'pdf_processed': False,
        'actual_gui_integration_complete': False,
        'only_allowlisted_check_results_saved': True,
        'source_issues': issues,
        'checks': checks,
        'failed_required_check_count': sum(not checks[k] for k in REQUIRED),
        'review_flags': {
            'core_may_suppress_media_exceptions': checks['core_media_exception_continues'],
            'core_media_explicit_error_may_copy_original': checks['core_explicit_failure_copies_original'],
            'core_scan_folder_copies_originals': checks['core_scan_copies_source_tree'],
            'ui_uses_hotkey_work_lock': checks['ui_hotkey_lock'],
            'gui_version_not_verified_against_prior_upload': True,
        },
        'warning': 'Static preflight only. Do not enable GPU for real documents. '
                   'No full PDF safety, no actual ui.py timing, no file masking is executed.',
    }


def validate_destination(root: Path, out: Path, tool: Path) -> Path:
    root, out, tool = root.resolve(), out.resolve(), tool.resolve()
    if out == root or root not in out.parents or out == tool or tool in out.parents:
        raise ValueError('INVALID_RESULTS_DIR')
    if any(part.lower() in {x.lower() for x in GUARD_DIRS} for part in out.relative_to(root).parts):
        raise ValueError('PROTECTED_RESULTS_DIR')
    return out


def write_report(out: Path, report: dict[str, Any]):
    if out.exists() and any(out.iterdir()):
        raise FileExistsError('RESULTS_DIR_NOT_EMPTY')
    out.mkdir(parents=True, exist_ok=True)
    (out/'v669_source_preflight.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    lines = ['# V6.6.9.1：GUI 正式碼掛接點檢查器修正', '',
             f"- 靜態狀態：**{report['status']}**",
             '- 沒有執行 GUI、OCR、PDF、模型或正式應用程式。',
             '- 不修改任何正式程式、模型、虛擬環境、剪貼簿或還原對照表。',
             '- 原始碼內容、路徑、PII 與 private map 均不會寫進報告。',
             '', '| 檢查代號 | 結果 |', '|---|---|']
    for key, value in report['checks'].items():
        lines.append(f'| `{key}` | {"PASS" if value else "NOT_FOUND"} |')
    lines.extend(['', '## 待確認項目', '',
                  f"- 必要掛接點未確認：{report['failed_required_check_count']} 個。",
                  f"- core 是否可能在媒體處理錯誤後 `continue`：{report['review_flags']['core_may_suppress_media_exceptions']}。",
                  f"- 指定輸出路徑遇到錯誤時是否可能複製原檔：{report['review_flags'].get('core_media_explicit_error_may_copy_original', False)}。",
                  f"- 資料夾掃描是否先複製原檔：{report['review_flags'].get('core_scan_folder_copies_originals', False)}。",
                  '- `STATIC_HOOKS_READY_FOR_REVIEW` 不是 GPU 上線許可：仍需確認最新 `ui.py` / `core.py` 並製作隔離整合副本。',
                  '- 注意：靜態掛接點 PASS 不是安全性 PASS；禁止直接處理真實文件。',
                  '- 真正安全處理的驗收仍需重新進行 27/27、17/17、PDF 結構與 CPU/GPU 像素比較。', ''])
    (out/'v669_source_preflight.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None):
    ap = argparse.ArgumentParser(description='V6.6.9.1 corrected read-only source preflight')
    ap.add_argument('--project-root', default='.')
    ap.add_argument('--results-dir', default='v669_source_preflight_results')
    ap.add_argument('--check-only', action='store_true')
    a = ap.parse_args(argv)
    root = Path(a.project_root).resolve()
    tool = Path(__file__).resolve().parent
    if tool.parent != root or tool.name != KIT:
        print('ERROR: TOOL_MUST_BE_IN_PROJECT_ROOT')
        return 2
    report = generate_report(root)
    print('SOURCE_PREFLIGHT_STATUS:', report['status'])
    print('FAILED_REQUIRED_HOOKS:', report['failed_required_check_count'])
    if a.check_only:
        return 0 if report['status']=='STATIC_HOOKS_READY_FOR_REVIEW' else 1
    try:
        out = validate_destination(root, root/a.results_dir, tool)
        write_report(out, report)
    except (OSError, ValueError) as exc:
        print('REPORT_ERROR:', str(exc) if str(exc) in ('INVALID_RESULTS_DIR',
              'PROTECTED_RESULTS_DIR', 'RESULTS_DIR_NOT_EMPTY') else 'WRITE_FAILED')
        return 2
    print('REPORT_CREATED: v669_source_preflight.json + v669_source_preflight.md')
    return 0 if report['status']=='STATIC_HOOKS_READY_FOR_REVIEW' else 1


if __name__ == '__main__':
    raise SystemExit(main())
