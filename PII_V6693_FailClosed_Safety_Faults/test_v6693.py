#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline stdlib tests, no production imports or private data."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

import v6693_failclosed as s

UI = '''class App:
    def _start(self, func, msg, on_done): pass
    def _poll(self): pass
    def run_files(self): pass
'''
CORE_UNSAFE = '''import shutil

def _mask_media(path, out=None):
    try:
        return func(path, out)
    except Exception:
        if out: shutil.copy2(path, out)
        raise

def dispatch_files(paths, mapping):
    for path in paths:
        try:
            _mask_media(path)
        except Exception:
            continue

def scan_folder(folder, mapping):
    shutil.copytree(folder, folder + '_masked')
'''
CORE_SAFE = '''def _mask_media(path, out=None):
    raise RuntimeError('FAILED')

def dispatch_files(paths, mapping):
    return None

def scan_folder(folder, mapping):
    raise RuntimeError('DISABLED')
'''

class BasicTests(unittest.TestCase):
    def fixture(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'test.pdf'
        path.write_bytes(b'NON-PII-SYNTHETIC-DATA')
        return path
    def test_all_scenarios_enumerated(self):
        self.assertEqual(len(s.SCENARIOS), 14)
    def test_all_mock_faults(self):
        self.assertTrue(all(s.run_all_faults().values()))
    def test_positive_baseline(self):
        f = self.fixture()
        self.assertTrue(s.simulate_scenario('BASELINE_APPROVED_MOCK', f, s.sha256(f)))
    def test_good_file_unchanged(self):
        f = self.fixture()
        before = f.read_bytes()
        self.assertTrue(s.simulate_scenario('BASELINE_APPROVED_MOCK', f, s.sha256(f)))
        self.assertEqual(f.read_bytes(), before)
    def test_no_scenario_typos(self):
        f = self.fixture()
        with self.assertRaises(ValueError):
            s.simulate_scenario('OTHER', f, s.sha256(f))
    def test_empty_batch(self):
        f = self.fixture()
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,s.sha256(f)).execute([],lambda a,b,i:None,lambda a,i:True)
    def test_many_batch(self):
        f = self.fixture()
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,s.sha256(f)).execute([f]*3,lambda a,b,i:None,lambda a,i:True)
    def test_untrusted_batch_not_executed(self):
        f = self.fixture()
        other = f.with_name('evil.pdf'); other.write_bytes(f.read_bytes())
        called=[]
        def mask(src,out,i): called.append(i)
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,s.sha256(f)).execute([f,other],mask,lambda a,i:True)
        self.assertEqual(called, [])
    def test_corrupt_digest_not_executed(self):
        f = self.fixture()
        called=[]
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,'0'*64).execute([f],lambda *a:called.append(1),lambda a,i:True)
        self.assertEqual(called, [])
    def test_non_bool_true_audit(self):
        f = self.fixture()
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,s.sha256(f)).execute([f],lambda a,b,i:b.write_bytes(b'FAKE'),lambda a,i:1)
    def test_no_output_rejected(self):
        f = self.fixture()
        with self.assertRaises(s.Rejected):
            s.SyntheticOnlyTransaction(f,s.sha256(f)).execute([f],lambda *a:None,lambda a,i:True)
    def test_faults_are_rejected(self):
        f = self.fixture()
        for name in s.SCENARIOS:
            with self.subTest(name=name):
                self.assertTrue(s.simulate_scenario(name,f,s.sha256(f)))
    def test_source_inspection(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t);(r/'ui.py').write_text(UI);(r/'core.py').write_text(CORE_UNSAFE)
            outcome=s.source_review(r)
            self.assertTrue(all(outcome.values()),outcome)
    def test_safe_source_no_risk(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t);(r/'ui.py').write_text(UI);(r/'core.py').write_text(CORE_SAFE)
            x=s.source_review(r)
            self.assertTrue(x['source_parseable'])
            for key in ('core_exception_may_continue','core_error_may_copy_original','scan_may_copy_unmasked_tree'):
                self.assertFalse(x[key])
    def test_source_missing_false(self):
        with tempfile.TemporaryDirectory() as t:
            self.assertFalse(s.source_review(Path(t))['source_parseable'])
    def test_destination_inside(self):
        with tempfile.TemporaryDirectory() as t:
            r=Path(t).resolve()
            self.assertEqual(s.test_destination(r,'reports'),(r/'reports').resolve())
    def test_destination_absolute_external_rejected(self):
        with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
            with self.assertRaises(s.Rejected):
                s.test_destination(Path(t1), t2)
    def test_destination_root_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(s.Rejected):
                s.test_destination(Path(t), '.')
    def test_destination_protected_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            for part in ['presidio/real/results','.venv/results','.venv_ocr_gpu/results','.git/results',
                         'PII_V6693_FailClosed_Safety_Faults/results','PII_Face_PDF_7page_Test/results']:
                with self.subTest(part=part), self.assertRaises(s.Rejected):
                    s.test_destination(Path(t), part)
    def test_destination_nul_rejected(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(s.Rejected):
                s.test_destination(Path(t),'x\0y')
    def test_no_overwrite_reports(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t);rv=s.source_review(Path(t))
            s.save_results(folder,rv,{'SYNTHETIC':True})
            with self.assertRaises(FileExistsError):
                s.save_results(folder,rv,{'SYNTHETIC':True})
    def test_report_no_payloads(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            data=s.save_results(folder,s.source_review(folder),s.run_all_faults())
            serialized=(folder/'v6693_failclosed_summary.json').read_text()
            self.assertNotIn(str(folder),serialized)
            for sensitive in ['FAKE SANDBOX MASKED OUTPUT','INJECTED_TIMEOUT','NON-PII-SYNTHETIC-DATA']:
                self.assertNotIn(sensitive,serialized)
            self.assertFalse(data['production_gpu_allowed'])
    def test_json_schema_safe(self):
        with tempfile.TemporaryDirectory() as t:
            folder=Path(t)
            data=s.save_results(folder,s.source_review(folder),{'BASELINE_APPROVED_MOCK':True})
            parsed=json.loads((folder/'v6693_failclosed_summary.json').read_text())
            self.assertEqual(parsed['schema'],s.SCHEMA)
            self.assertFalse(parsed['real_ocr_executed'])
            self.assertFalse(parsed['actual_gui_executed'])
    def test_scenarios_report_complete(self):
        results=s.run_all_faults()
        self.assertSetEqual(set(results),set(s.SCENARIOS)|{'SOURCE_BYTES_PRESERVED'})

if __name__ == '__main__':
    unittest.main(verbosity=2)
