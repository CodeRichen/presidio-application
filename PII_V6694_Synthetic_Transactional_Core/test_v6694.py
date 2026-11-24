#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9.4 synthetic-only unit tests (no project imports)."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import v6694_fault_runner as runner
from v6694_core_preview import (SOURCE_MAGIC, MASKED_MAGIC, SyntheticCorePreview,
                                SafetyReject, digest)


class TransactionUnitTests(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(prefix='pii_v6694_ut_')
        self.addCleanup(self.td.cleanup)
        self.root = Path(self.td.name)
        self.source = self.root / 'fixture.fake'
        self.source.write_bytes(SOURCE_MAGIC + b'FAKE_CONTENT')
        self.allowlist = {self.source: digest(self.source)}

    def proc(self, src, dst, index):
        dst.write_bytes(MASKED_MAGIC + b'REDACTED')

    def audit(self, p, index):
        return p.read_bytes().startswith(MASKED_MAGIC)

    def test_all_scenarios(self):
        for name in runner.CASE_NAMES:
            with self.subTest(name=name):
                self.assertTrue(runner.exercise(name))

    def test_one_item_success_result_and_cleanup(self):
        source_data = self.source.read_bytes()
        with SyntheticCorePreview(self.allowlist) as t:
            root = t.base
            r = t.run_batch([self.source], self.proc, self.audit)
            self.assertEqual(r.count, 1)
            self.assertEqual(len(t.files_now()), 1)
            self.assertTrue(t.files_now()[0].is_file())
        self.assertFalse(root.exists())
        self.assertEqual(self.source.read_bytes(), source_data)

    def test_reject_real_pdf_even_with_own_sha_allowlisted(self):
        self.source.write_bytes(b'%PDF-1.7\nREAL_INPUT_NOT_ALLOWED')
        with SyntheticCorePreview({self.source: digest(self.source)}) as t:
            with self.assertRaisesRegex(SafetyReject, 'REAL_DOCUMENT_REJECTED'):
                t.run_batch([self.source], self.proc, self.audit)
            self.assertFalse(t.files_now())

    def test_wrong_gate_nonliteral_true_rejected(self):
        with SyntheticCorePreview(self.allowlist) as t:
            with self.assertRaises(SafetyReject):
                t.run_batch([self.source], self.proc, lambda p, i: 1)
            self.assertFalse(t.files_now())

    def test_hash_wrong_or_outside_prevents_processor_call(self):
        calls = []
        with SyntheticCorePreview({self.source: '0'*64}) as t:
            with self.assertRaises(SafetyReject):
                t.run_batch([self.source], lambda *a: calls.append(1), self.audit)
            self.assertEqual(calls, [])
        other = self.root / 'other.fake'
        other.write_bytes(SOURCE_MAGIC + b'OTHER')
        with SyntheticCorePreview(self.allowlist) as t:
            with self.assertRaises(SafetyReject):
                t.run_batch([other], lambda *a: calls.append(1), self.audit)
        self.assertEqual(calls, [])

    def test_fail_after_commit_link_rolls_back(self):
        source2 = self.root / 'other.fake'
        source2.write_bytes(SOURCE_MAGIC + b'OTHER')
        a = {**self.allowlist, source2: digest(source2)}
        with SyntheticCorePreview(a) as t:
            with self.assertRaisesRegex(SafetyReject, 'INJECTED_COMMIT_FAILURE'):
                t.run_batch([self.source, source2], self.proc, self.audit, commit_fail_after=1)
            self.assertFalse(t.files_now())

    def test_existing_publication_preserved(self):
        with SyntheticCorePreview(self.allowlist) as t:
            (t.published / 'approved_00.pdf').write_bytes(b'EXISTING')
            with self.assertRaisesRegex(SafetyReject, 'PUBLICATION_AREA_NOT_EMPTY'):
                t.run_batch([self.source], self.proc, self.audit)
            self.assertEqual((t.published / 'approved_00.pdf').read_bytes(), b'EXISTING')

    def test_context_required(self):
        t = SyntheticCorePreview(self.allowlist)
        with self.assertRaisesRegex(SafetyReject, 'CONTEXT_REQUIRED'):
            t.run_batch([self.source], self.proc, self.audit)

    def test_destination_forbid_root_traversal_and_privacy(self):
        for bad in ('..', '/', './', 'presidio/real', '.venv/t', 'PII_V6694_Synthetic_Transactional_Core/reports', '\0bad'):
            with self.subTest(bad=bad):
                with self.assertRaises(SafetyReject):
                    runner.safe_destination(self.root, bad)
        self.assertEqual(runner.safe_destination(self.root, 'v6694_good'), (self.root/'v6694_good').resolve())

    def test_source_ast_detect_risks(self):
        # Minimal representation, no private data or executable project imports.
        (self.root/'ui.py').write_text('class App:\n def _start(self): pass\n def _poll(self): pass\n def run_files(self): pass\n')
        (self.root/'core.py').write_text('''
def _mask_media():
 try: pass
 except Exception: shutil.copy2('a', 'b')
def dispatch_files():
 for v in []:
  try: pass
  except Exception: continue
def scan_folder():
 shutil.copytree('a','b')
''')
        review = runner.safe_source_review(self.root)
        self.assertTrue(all(review.values()))

    def test_privacy_report_fields(self):
        # Full subprocess test: report must contain only allowlisted booleans,
        # counts and scenario labels, never input bytes, source/hash or paths.
        import shutil
        project = self.root / 'mock_project'
        kit = project / 'PII_V6694_Synthetic_Transactional_Core'
        kit.mkdir(parents=True)
        for name in ('v6694_core_preview.py', 'v6694_fault_runner.py'):
            shutil.copy2(Path(__file__).parent / name, kit / name)
        (project / 'ui.py').write_text(
            'class App:\n def _start(self): pass\n def _poll(self): pass\n def run_files(self): pass\n'
        )
        (project / 'core.py').write_text(
            'def _mask_media(): pass\ndef dispatch_files(): pass\ndef scan_folder(): pass\n'
        )
        proc = subprocess.run([sys.executable, str(kit / 'v6694_fault_runner.py'),
                               '--results-dir', 'reports'], cwd=project,
                              capture_output=True, text=True, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        summary = (project / 'reports' / 'v6694_safe_transaction_summary.json').read_text(encoding='utf-8')
        report = json.loads(summary)
        self.assertEqual(report['total'], len(runner.CASE_NAMES))
        self.assertEqual(report['passed'], report['total'])
        self.assertFalse(report['production_gpu_allowed'])
        self.assertFalse(report['real_ocr_executed'])
        self.assertEqual(set(report), {
            'schema','status','mock_only','real_ocr_executed','real_pdf_security_audit_executed',
            'actual_gui_executed','production_files_modified','production_gpu_allowed',
            'mock_scenarios','passed','total','source_review','warning'
        })
        for forbidden in ('FAKE_CONTENT', 'CANDIDATE_ESCAPED_STAGE', str(self.root),
                          SOURCE_MAGIC.decode('ascii').strip(), 'SHA256', 'file_paths'):
            self.assertNotIn(forbidden, summary)

    def test_no_sensitive_imports_in_core_preview(self):
        raw = (Path(__file__).parent/'v6694_core_preview.py').read_text(encoding='utf-8')
        for banned in ('import handler_media', 'import core', 'import win32clipboard', 'import ui',
                       'import onnxruntime', 'import rapidocr'):
            self.assertNotIn(banned, raw)

    def test_no_stage_left_after_rejection(self):
        with SyntheticCorePreview(self.allowlist) as t:
            with self.assertRaises(RuntimeError):
                t.run_batch([self.source], lambda a,b,c: (_ for _ in ()).throw(RuntimeError('MOCK')),
                            self.audit)
            self.assertFalse((t.base / 'stage').exists())

    def test_source_modified_between_audit_and_commit_rejected(self):
        def sabotage():
            self.source.write_bytes(SOURCE_MAGIC + b'CHANGED_BEFORE_COMMIT')
        with SyntheticCorePreview(self.allowlist) as t:
            with self.assertRaisesRegex(SafetyReject, 'SOURCE_DIGEST_MISMATCH'):
                t.run_batch([self.source], self.proc, self.audit, before_commit=sabotage)
            self.assertFalse(t.files_now())

    def test_all_output_paths_private(self):
        with SyntheticCorePreview(self.allowlist) as t:
            result = t.run_batch([self.source], self.proc, self.audit)
            for p in result.files:
                self.assertEqual(p.parent, t.published)
                self.assertEqual(p.parent.parent, t.base)


if __name__ == '__main__':
    unittest.main(verbosity=2)
