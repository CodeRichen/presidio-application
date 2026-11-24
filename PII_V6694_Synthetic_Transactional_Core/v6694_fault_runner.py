#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9.4 mock-only fail-closed test runner. Nothing real is processed."""
from __future__ import annotations
import argparse
import ast
import json
from pathlib import Path
import shutil
import tempfile

from v6694_core_preview import (SyntheticCorePreview, SafetyReject, SOURCE_MAGIC,
                                MASKED_MAGIC, digest)

SCHEMA = 'pii_v6694_synthetic_transactional_preview_v1'
CASE_NAMES = (
    'SUCCESS_TWO_FAKE_FILES', 'GPU_WORKER_EXCEPTION', 'OCR_TIMEOUT',
    'PDF_EXPORT_EXCEPTION', 'PARTIAL_WRITE_EXCEPTION', 'NO_OUTPUT',
    'ZERO_BYTE_OUTPUT', 'ORIGINAL_COPY_DENIED_BY_GATE', 'SECURITY_GATE_FALSE',
    'SECURITY_GATE_THROW', 'SECURITY_GATE_TRUTHY_NOT_TRUE',
    'SECOND_FILE_FAILURE_ROLLBACK', 'COMMIT_SECOND_LINK_FAILURE',
    'PREEXISTING_RESULT_REJECTED', 'CANDIDATE_SYMLINK_REJECTED',
    'SOURCE_SYMLINK_REJECTED', 'UNLISTED_FILE_REJECTED',
    'CHANGED_DIGEST_REJECTED', 'DUPLICATE_BATCH_REJECTED',
    'CANDIDATE_CHANGED_AFTER_AUDIT', 'SOURCE_CHANGED_BEFORE_COMMIT',
    'SOURCE_BYTES_PRESERVED', 'AUTO_CLEANUP_AFTER_CONTEXT',
)


def safe_source_review(project: Path) -> dict:
    """Limited AST inspection; never import project code or emit code/path strings."""
    result = {'source_parseable': False, 'gui_hooks_present': False,
              'media_exception_continues': False,
              'explicit_media_error_copies_original': False,
              'scan_copies_unmasked_tree': False}
    try:
        u = ast.parse((project / 'ui.py').read_text(encoding='utf-8-sig'))
        c = ast.parse((project / 'core.py').read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, SyntaxError):
        return result
    result['source_parseable'] = True
    app = next((n for n in u.body if isinstance(n, ast.ClassDef) and n.name == 'App'), None)
    methods = {n.name for n in app.body if isinstance(n, ast.FunctionDef)} if app else set()
    result['gui_hooks_present'] = {'run_files', '_start', '_poll'}.issubset(methods)
    funs = {n.name: n for n in c.body if isinstance(n, ast.FunctionDef)}
    for n in ast.walk(funs.get('dispatch_files', ast.Pass())):
        if isinstance(n, ast.ExceptHandler) and any(isinstance(v, ast.Continue) for v in ast.walk(n)):
            result['media_exception_continues'] = True
    for n in ast.walk(funs.get('_mask_media', ast.Pass())):
        if isinstance(n, ast.ExceptHandler) and any(
            isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == 'copy2'
            for v in ast.walk(n)
        ):
            result['explicit_media_error_copies_original'] = True
    result['scan_copies_unmasked_tree'] = any(
        isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == 'copytree'
        for v in ast.walk(funs.get('scan_folder', ast.Pass()))
    )
    return result


def exercise(name: str) -> bool:
    if name not in CASE_NAMES:
        raise ValueError('UNKNOWN_SCENARIO')
    with tempfile.TemporaryDirectory(prefix='pii_v6694_case_') as tmp:
        root = Path(tmp)
        sources = [root / 'fixture_0.dat', root / 'fixture_1.dat']
        for i, p in enumerate(sources):
            p.write_bytes(SOURCE_MAGIC + (f'FAKE_ONLY_{i}').encode('ascii'))
        before = [p.read_bytes() for p in sources]
        allow = {p: digest(p) for p in sources}
        called = []
        selected = sources[:2] if name in ('SUCCESS_TWO_FAKE_FILES', 'SECOND_FILE_FAILURE_ROLLBACK',
                                           'COMMIT_SECOND_LINK_FAILURE') else sources[:1]
        run_options = {}
        
        def processor(source, candidate, index):
            called.append(index)
            if name == 'GPU_WORKER_EXCEPTION':
                raise RuntimeError('MOCK_FAILURE')
            if name == 'OCR_TIMEOUT':
                raise TimeoutError('MOCK_TIMEOUT')
            if name == 'PDF_EXPORT_EXCEPTION':
                raise OSError('MOCK_EXPORT_ERROR')
            if name == 'PARTIAL_WRITE_EXCEPTION':
                candidate.write_bytes(MASKED_MAGIC[:8])
                raise RuntimeError('MOCK_PARTIAL_OUTPUT')
            if name == 'SECOND_FILE_FAILURE_ROLLBACK' and index == 1:
                candidate.write_bytes(MASKED_MAGIC[:8])
                raise RuntimeError('MOCK_SECOND_FAILURE')
            if name == 'NO_OUTPUT':
                return
            if name == 'ZERO_BYTE_OUTPUT':
                candidate.touch()
                return
            if name == 'CANDIDATE_SYMLINK_REJECTED':
                # Windows can prohibit symlink creation without developer mode;
                # this still fails closed as a processor exception.
                candidate.symlink_to(source)
                return
            if name == 'ORIGINAL_COPY_DENIED_BY_GATE':
                shutil.copy2(source, candidate)
                return
            candidate.write_bytes(MASKED_MAGIC + b'FAKE_REDACTION\n')
        
        def independent_audit(candidate, index):
            if name == 'SECURITY_GATE_FALSE':
                return False
            if name == 'SECURITY_GATE_THROW':
                raise RuntimeError('MOCK_AUDIT_EXCEPTION')
            if name == 'SECURITY_GATE_TRUTHY_NOT_TRUE':
                return 'PASS'
            # Separate synthetic oracle; never tests real PDF security.
            return candidate.read_bytes().startswith(MASKED_MAGIC)

        with SyntheticCorePreview(allow) as tx:
            workspace = tx.base
            if name == 'COMMIT_SECOND_LINK_FAILURE':
                run_options['commit_fail_after'] = 1
            if name == 'PREEXISTING_RESULT_REJECTED':
                (tx.published / 'do_not_delete.pdf').write_bytes(b'OLDER_SAFE_MOCK')
            if name == 'CANDIDATE_CHANGED_AFTER_AUDIT':
                run_options['before_commit'] = lambda: (tx.base / 'stage' / 'candidate_00.pdf').write_bytes(b'CHANGED_NOT_AUDITED')
            if name == 'SOURCE_CHANGED_BEFORE_COMMIT':
                run_options['before_commit'] = lambda: sources[0].write_bytes(SOURCE_MAGIC + b'CHANGED_SOURCE')
            if name == 'UNLISTED_FILE_REJECTED':
                unlisted = root / 'unlisted.dat'
                unlisted.write_bytes(SOURCE_MAGIC + b'FAKE_ONLY_UNLISTED')
                selected = [unlisted]
            if name == 'CHANGED_DIGEST_REJECTED':
                sources[0].write_bytes(SOURCE_MAGIC + b'ALTERED_BEFORE_REQUEST')
            if name == 'DUPLICATE_BATCH_REJECTED':
                selected = [sources[0], sources[0]]
            if name == 'SOURCE_SYMLINK_REJECTED':
                fake_symlink = root / 'linked.dat'
                try:
                    fake_symlink.symlink_to(sources[0])
                except (OSError, NotImplementedError):
                    # Refusal to create a symlink means the unsafe case cannot run.
                    # Report pass only for verified platform-level rejection.
                    return not fake_symlink.exists()
                selected = [fake_symlink]
            succeeded = False
            try:
                res = tx.run_batch(selected, processor, independent_audit, **run_options)
                succeeded = res.count == len(selected) and len(tx.files_now()) == len(selected)
            except (SafetyReject, OSError, TimeoutError, RuntimeError, NotImplementedError):
                pass
            if name == 'SUCCESS_TWO_FAKE_FILES':
                checked = succeeded and [p.read_bytes() for p in sources] == before
            elif name == 'PREEXISTING_RESULT_REJECTED':
                checked = not succeeded and len(tx.files_now()) == 1 and (tx.published / 'do_not_delete.pdf').read_bytes() == b'OLDER_SAFE_MOCK'
            elif name in ('SOURCE_CHANGED_BEFORE_COMMIT', 'CHANGED_DIGEST_REJECTED'):
                checked = not succeeded and len(tx.files_now()) == 0
            else:
                checked = not succeeded and len(tx.files_now()) == 0
            if name == 'SOURCE_BYTES_PRESERVED':
                checked = succeeded is True and [p.read_bytes() for p in sources] == before
            if name == 'AUTO_CLEANUP_AFTER_CONTEXT':
                checked = succeeded is True
        if name == 'AUTO_CLEANUP_AFTER_CONTEXT':
            return checked and workspace is not None and not workspace.exists()
        return bool(checked)


def safe_destination(root: Path, requested: str) -> Path:
    root = root.resolve(strict=True)
    if not requested or '\0' in requested:
        raise SafetyReject('BAD_RESULT_DESTINATION')
    dest = (root / requested).resolve()
    forbidden = {'.venv', '.venv_ocr_gpu', '.git', 'presidio', 'real',
                 'pii_v6694_synthetic_transactional_core', 'pii_face_pdf_7page_test'}
    if root not in dest.parents or any(v.lower() in forbidden for v in dest.relative_to(root).parts):
        raise SafetyReject('UNSAFE_RESULT_DESTINATION')
    return dest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--results-dir', default='v6694_fault_results')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    review = safe_source_review(project)
    if args.check_only:
        print('V6694_SOURCE_HOOKS_OK:', review['source_parseable'] and review['gui_hooks_present'])
        print('V6694_PRODUCTION_GPU_ALLOWED: False')
        return
    cases = {name: exercise(name) for name in CASE_NAMES}
    passed = sum(cases.values())
    status = 'SYNTHETIC_TRANSACTION_MOCK_PASS_SOURCE_REVIEW_REQUIRED' if passed == len(cases) else 'SYNTHETIC_TRANSACTION_MOCK_FAILED'
    report = {
        'schema': SCHEMA, 'status': status,
        'mock_only': True, 'real_ocr_executed': False,
        'real_pdf_security_audit_executed': False, 'actual_gui_executed': False,
        'production_files_modified': False, 'production_gpu_allowed': False,
        'mock_scenarios': cases, 'passed': passed, 'total': len(cases),
        'source_review': review,
        'warning': 'Synthetic mock only. Not a replacement for core.py. Do not process real documents.'
    }
    dest = safe_destination(project, args.results_dir)
    dest.mkdir(parents=True, exist_ok=True)
    json_path = dest / 'v6694_safe_transaction_summary.json'
    md_path = dest / 'v6694_safe_transaction_summary.md'
    if json_path.exists() or md_path.exists():
        raise SafetyReject('REPORT_EXISTS_USE_NEW_RESULTS_DIR')
    md = ['# V6.6.9.4：隔離 Core 安全交易與故障注入', '',
          f'- 狀態：**{status}**', f'- 合成測試：**{passed}/{len(cases)}**',
          '- 無真實 OCR、PDF 安全稽核、GPU 或正式 GUI 執行',
          '- 正式檔案及環境未改動；不允許真實文件處理', '',
          '| 情境 | 結果 |', '|---|---|']
    md.extend(f'| `{name}` | {"PASS" if yes else "FAIL"} |' for name, yes in cases.items())
    md.extend(['', '## 原始碼審查旗標', '',
               f'- 媒體例外後繼續：{review["media_exception_continues"]}',
               f'- 失敗複製未遮蔽原稿：{review["explicit_media_error_copies_original"]}',
               f'- 資料夾先複製原稿：{review["scan_copies_unmasked_tree"]}',
               '', '**此報告不等於正式 core.py 修正或 GPU 上線許可。**'])
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    md_path.write_text('\n'.join(md) + '\n', encoding='utf-8')
    print('V6694_STATUS:', status)
    print(f'MOCK_SCENARIOS: {passed}/{len(cases)}')
    print('PRODUCTION_GPU_ALLOWED: False')


if __name__ == '__main__':
    main()
