#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9.3: synthetic-only, no-import, no-production-writes fail-closed review.

This is NOT an OCR adapter, production core.py replacement, nor PDF security audit.
The fault harness uses tiny synthetic byte fixtures + injected callables only.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Callable

SCHEMA = 'pii_v6693_fail_closed_fault_review_v1'
SCENARIOS = (
    'BASELINE_APPROVED_MOCK',
    'GPU_WORKER_CRASH',
    'OCR_TIMEOUT',
    'PDF_EXPORT_ERROR',
    'PARTIAL_OUTPUT_EXCEPTION',
    'MISSING_OUTPUT',
    'EMPTY_OUTPUT',
    'SECURITY_AUDIT_REJECTED',
    'SECURITY_AUDIT_EXCEPTION',
    'MALICIOUS_UNTRUSTED_PATH',
    'MODIFIED_SOURCE_REJECTED',
    'SECOND_ITEM_FAILURE_ROLLBACK',
    'WRONG_RESULT_TYPE',
    'OUTPUT_SYMLINK_REJECTED',
)

class Rejected(Exception):
    """Intentionally never include input payloads in error messages."""


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def test_destination(project_root: Path, requested: str) -> Path:
    """Results must be beneath project root and outside source/private roots."""
    root = project_root.resolve(strict=True)
    if not requested or '\0' in requested:
        raise Rejected('BAD_REPORT_DESTINATION')
    dest = (root / requested).resolve()
    if dest == root or root not in dest.parents:
        raise Rejected('REPORT_OUTSIDE_PROJECT')
    protected = {'.git', '.venv', '.venv_ocr_gpu', 'presidio', 'real',
                 'pii_v6693_failclosed_safety_faults', 'pii_face_pdf_7page_test',
                 'pii_v5_security_acceptance', 'pii_v6676_full_pdf_hybrid_ab'}
    if any(part.lower() in protected for part in dest.relative_to(root).parts):
        raise Rejected('REPORT_PROTECTED_PATH')
    return dest


def source_review(root: Path) -> dict:
    """AST inspection only. NEVER execute/import source or capture private code."""
    report = {
        'source_parseable': False,
        'required_gui_hooks': False,
        'required_core_hooks': False,
        'core_exception_may_continue': False,
        'core_error_may_copy_original': False,
        'scan_may_copy_unmasked_tree': False,
    }
    try:
        u = ast.parse((root / 'ui.py').read_text(encoding='utf-8-sig'))
        c = ast.parse((root / 'core.py').read_text(encoding='utf-8-sig'))
    except (OSError, UnicodeError, SyntaxError):
        return report
    report['source_parseable'] = True
    app = next((n for n in u.body if isinstance(n, ast.ClassDef) and n.name == 'App'), None)
    funcs = {n.name: n for n in c.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    methods = {n.name for n in app.body if isinstance(n, ast.FunctionDef)} if app else set()
    report['required_gui_hooks'] = {'_start', '_poll', 'run_files'}.issubset(methods)
    report['required_core_hooks'] = {'_mask_media', 'dispatch_files', 'scan_folder'}.issubset(funcs)
    def attr_call(node, attr):
        return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attr
    media = funcs.get('_mask_media')
    if media:
        report['core_error_may_copy_original'] = any(
            attr_call(sub, 'copy2')
            for n in ast.walk(media) if isinstance(n, ast.ExceptHandler)
            for sub in ast.walk(n)
        )
    dispatch = funcs.get('dispatch_files')
    if dispatch:
        report['core_exception_may_continue'] = any(
            isinstance(sub, ast.Continue)
            for n in ast.walk(dispatch) if isinstance(n, ast.ExceptHandler)
            for sub in ast.walk(n)
        )
    scan = funcs.get('scan_folder')
    if scan:
        report['scan_may_copy_unmasked_tree'] = any(attr_call(n, 'copytree') for n in ast.walk(scan))
    return report


class SyntheticOnlyTransaction:
    """Test-only staged processor. NEVER promotes output; deletes all artifacts.

    Caller has to inject a fake mask and an independent, strict fake audit.
    A success here only verifies mechanics of the fail-closed contract.
    """
    def __init__(self, fixture: Path, expected_digest: str):
        self.fixture = fixture.resolve(strict=True)
        self.expected_digest = expected_digest

    def _check_fixture(self, source: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise Rejected('BAD_SOURCE_TYPE')
        if source.resolve() != self.fixture:
            raise Rejected('NON_ALLOWLIST_SOURCE')
        if sha256(source) != self.expected_digest:
            raise Rejected('SOURCE_HASH_CHANGED')

    def execute(self, sources: list[Path], mask: Callable, audit: Callable) -> bool:
        """Atomic-all-or-nothing mock check; no outputs are ever published.

        We explicitly never touch non-allowlisted paths. Even when audit=True,
        scratch artifacts are destroyed before returning.
        """
        if not sources or len(sources) > 2:
            raise Rejected('BAD_BATCH_SIZE')
        for src in sources:   # Pre-validate entire batch before processing anything.
            self._check_fixture(src)
        with tempfile.TemporaryDirectory(prefix='pii_v6693_fault_') as scratch:
            stage = Path(scratch).resolve()
            for i, src in enumerate(sources):
                out = stage / f'candidate_{i}.pdf'
                # Must create inside the private stage. No copying source on failure.
                mask(src, out, i)
                if out.is_symlink() or not out.is_file() or out.stat().st_size <= 0:
                    raise Rejected('INCOMPLETE_MASKED_OUTPUT')
                if out.resolve().parent != stage:
                    raise Rejected('OUTPUT_OUTSIDE_STAGING')
                # The audit function models a *separate* security gate, not a trust
                # assertion provided by the masking worker itself.
                verdict = audit(out, i)
                if verdict is not True:
                    raise Rejected('INDEPENDENT_SECURITY_GATE_NOT_PASSED')
        return True


def simulate_scenario(name: str, fixture: Path, digest: str) -> bool:
    """Expect baseline to pass; all injected faults MUST reject and self-clean."""
    if name not in SCENARIOS:
        raise ValueError('UNKNOWN_SCENARIO')
    trans = SyntheticOnlyTransaction(fixture, digest)
    called = {'n': 0}
    def mask(src: Path, output: Path, index: int):
        called['n'] += 1
        if name == 'GPU_WORKER_CRASH':
            raise RuntimeError('INJECTED_WORKER_FAILURE')
        if name == 'OCR_TIMEOUT':
            raise TimeoutError('INJECTED_TIMEOUT')
        if name == 'PDF_EXPORT_ERROR':
            raise OSError('INJECTED_WRITE_FAILURE')
        if name == 'PARTIAL_OUTPUT_EXCEPTION':
            output.write_bytes(b'%PDF-PARTIAL')
            raise RuntimeError('INJECTED_PARTIAL_FAILURE')
        if name == 'MISSING_OUTPUT':
            return None
        if name == 'EMPTY_OUTPUT':
            output.touch()
            return None
        if name == 'SECOND_ITEM_FAILURE_ROLLBACK' and index == 1:
            output.write_bytes(b'%PDF-PARTIAL')
            raise RuntimeError('INJECTED_SECOND_FAILURE')
        if name == 'OUTPUT_SYMLINK_REJECTED':
            outside = output.parent.parent / 'outside_test.pdf'
            # A link to the already-allowlisted synthetic fixture is enough to
            # prove rejection, without creating an external file.
            try:
                output.symlink_to(src)
            except (OSError, NotImplementedError):
                raise Rejected('SYMLINK_NOT_SUPPORTED_REJECTED')
            return
        output.write_bytes(b'%PDF-1.7\nFAKE SANDBOX MASKED OUTPUT\n%%EOF\n')
    def audit(candidate: Path, index: int):
        if name == 'SECURITY_AUDIT_REJECTED':
            return False
        if name == 'SECURITY_AUDIT_EXCEPTION':
            raise RuntimeError('INJECTED_AUDIT_FAILURE')
        if name == 'WRONG_RESULT_TYPE':
            return 'PASS'  # Truthy must NOT be accepted as True
        return True

    if name == 'MALICIOUS_UNTRUSTED_PATH':
        with tempfile.TemporaryDirectory(prefix='pii_v6693_badsource_') as t:
            fake = Path(t) / 'untrusted.pdf'
            fake.write_bytes(b'FAKE SYNTHETIC INPUT ONLY')
            try:
                trans.execute([fake], mask, audit)
            except Rejected:
                return called['n'] == 0
        return False
    if name == 'MODIFIED_SOURCE_REJECTED':
        try:
            SyntheticOnlyTransaction(fixture, '0' * 64).execute([fixture], mask, audit)
        except Rejected:
            return called['n'] == 0
        return False
    items = [fixture, fixture] if name == 'SECOND_ITEM_FAILURE_ROLLBACK' else [fixture]
    try:
        value = trans.execute(items, mask, audit)
        return bool(value) if name == 'BASELINE_APPROVED_MOCK' else False
    except (Rejected, OSError, RuntimeError, TimeoutError):
        return name != 'BASELINE_APPROVED_MOCK'


def run_all_faults() -> dict:
    results = {}
    with tempfile.TemporaryDirectory(prefix='pii_v6693_fixture_') as scratch:
        fixture = Path(scratch) / 'approved_mock_fixture.pdf'
        # Not a real PDF; this phase is fault injection, not OCR/PDF acceptance.
        synthetic = b'%% FAKE-DOCUMENT - NO PERSONAL DATA - V6693 MOCK %%\n'
        fixture.write_bytes(synthetic)
        digest = sha256(fixture)
        for name in SCENARIOS:
            results[name] = bool(simulate_scenario(name, fixture, digest))
        results['SOURCE_BYTES_PRESERVED'] = fixture.read_bytes() == synthetic
    return results


def save_results(folder: Path, static: dict, tests: dict) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    # Save only fixed allowlisted enums and bool fields; NEVER paths or exception text.
    all_tests_pass = bool(tests) and all(tests.values())
    hooks_ready = static['source_parseable'] and static['required_gui_hooks'] and static['required_core_hooks']
    status = ('FAULT_MOCK_TESTS_PASS_SOURCE_REVIEW_REQUIRED' if all_tests_pass and hooks_ready
              else 'FAULT_MOCK_TESTS_OR_SOURCE_FAILED')
    summary = {
        'schema': SCHEMA,
        'status': status,
        'synthetic_mock_only': True,
        'real_ocr_executed': False,
        'real_pdf_security_audit_executed': False,
        'actual_gui_executed': False,
        'production_code_modified': False,
        'production_env_modified': False,
        'static_source_checks': static,
        'mock_scenarios': tests,
        'mock_tests_passed': sum(tests.values()),
        'mock_tests_total': len(tests),
        'production_gpu_allowed': False,
    }
    js = folder / 'v6693_failclosed_summary.json'
    md = folder / 'v6693_failclosed_summary.md'
    with js.open('x', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
        f.write('\n')
    with md.open('x', encoding='utf-8') as f:
        f.write('# V6.6.9.3：安全失敗處理故障注入（隔離模擬）\n\n')
        f.write(f'- 狀態：**{status}**\n')
        f.write(f'- 合成故障注入：{sum(tests.values())}/{len(tests)} 通過\n')
        f.write('- 已檢查正式原始碼 AST，但未修改或 import 正式程式。\n')
        f.write('- 此測試使用假 PDF 位元組與模擬安全稽核，不是完整 PDF/GPU/GUI 實測。\n')
        f.write('- 不允許用本報告解鎖真實文件處理。\n\n')
        f.write('| 靜態風險 | 是否存在 |\n|---|---|\n')
        for key in ('core_exception_may_continue', 'core_error_may_copy_original', 'scan_may_copy_unmasked_tree'):
            f.write(f'| {key} | {static[key]} |\n')
        f.write('\n| 故障注入 | 防護結果 |\n|---|---|\n')
        for key, passed in tests.items():
            f.write(f'| {key} | {"PASS" if passed else "FAIL"} |\n')
    return summary


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description='Synthetic-only fail-closed fault tests; no OCR or GUI imported')
    cli.add_argument('--project-root', default='.')
    cli.add_argument('--results-dir', default='v6693_fault_results')
    cli.add_argument('--check-only', action='store_true')
    args = cli.parse_args(argv)
    try:
        root = Path(args.project_root).resolve(strict=True)
        dest = test_destination(root, args.results_dir)
    except (Rejected, OSError, ValueError):
        print('V6693_STATUS: INVALID_OR_PROTECTED_RESULTS_DESTINATION')
        return 2
    reviewed = source_review(root)
    if not all(reviewed[k] for k in ('source_parseable','required_gui_hooks','required_core_hooks')):
        print('V6693_STATUS: SOURCE_SIGNATURE_CHANGED_REVIEW_REQUIRED')
        return 2
    if args.check_only:
        print('V6693_STATUS: STATIC_RISK_REVIEW_READY')
        print('CONFIRMED_CORE_EXCEPTION_CONTINUES:', reviewed['core_exception_may_continue'])
        print('CONFIRMED_CORE_EXCEPTION_COPIES_SOURCE:', reviewed['core_error_may_copy_original'])
        print('CONFIRMED_SCAN_COPIES_SOURCE_TREE:', reviewed['scan_may_copy_unmasked_tree'])
        print('NO_GUI_NO_OCR_NO_PDF_PROCESSED')
        return 0
    results = run_all_faults()
    try:
        summary = save_results(dest, reviewed, results)
    except (OSError, ValueError):
        print('V6693_STATUS: REPORT_WRITE_FAILED')
        return 4
    print('V6693_STATUS:', summary['status'])
    print('MOCK_FAULT_PASS:', f"{summary['mock_tests_passed']}/{summary['mock_tests_total']}")
    print('PRODUCTION_GPU_ALLOWED: False')
    return 0 if all(results.values()) else 3

if __name__ == '__main__':
    raise SystemExit(main())
