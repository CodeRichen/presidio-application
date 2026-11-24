#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import ast
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location('v6692_gui', Path(__file__).with_name('v6692_gui.py'))
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)

class TestV6692(unittest.TestCase):
    def test_modes(self): self.assertEqual(d.ALLOWED_MODES, ('hybrid', 'cpu', 'pair'))
    def test_heartbeat_short(self): self.assertEqual(d.HEARTBEAT_MS, 100)
    def test_percentile_empty(self): self.assertEqual(d.percentile([], .95), 0.)
    def test_percentile_median(self): self.assertEqual(d.percentile([1,3,2],.5),2.)
    def test_percentile_one(self): self.assertEqual(d.percentile([7],.95),7.)
    def test_percentile_no_mutation(self):
        x=[9,2,5];d.percentile(x,.95);self.assertEqual(x,[9,2,5])
    def test_heartbeat_empty(self): self.assertEqual(d.summarize_heartbeat([])['over_500ms'],0)
    def test_heartbeat_percentiles(self):
        s=d.summarize_heartbeat([1.,10.,22.,30.]);self.assertGreater(s['p99_lag_ms'],s['p95_lag_ms'])
    def test_heartbeat_over_500(self):
        s=d.summarize_heartbeat([0,100.1,250.1,500.1]);self.assertEqual((s['over_100ms'],s['over_250ms'],s['over_500ms']),(3,2,1))
    def test_filtered_trial_no_extra(self):
        v=d.filtered_trial({'mode':'hybrid','safe':True,'processing_s':10.,'secret':'PERSON_A','file':'C:/private.pdf'})
        self.assertEqual(v,{'mode':'hybrid','safe':True,'processing_s':10.})
    def test_provider_true(self):
        data={'provider_evidence':{'node_events':{'det_cuda':1,'cls_cpu':1,'rec_cpu':1,'cls_cuda':0,'rec_cuda':0}}}
        self.assertTrue(d.filtered_trial(data)['provider_route_verified'])
    def test_provider_false(self):
        data={'provider_evidence':{'node_events':{'det_cuda':1,'cls_cpu':1,'rec_cpu':1,'cls_cuda':1,'rec_cuda':0}}}
        self.assertFalse(d.filtered_trial(data)['provider_route_verified'])
    def test_destination_inside(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve(); self.assertEqual(d.validate_destination(p,'v6692_results'),(p/'v6692_results').resolve())
    def test_destination_root_rejected(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve()
            with self.assertRaises(ValueError): d.validate_destination(p,'.')
    def test_destination_outside_rejected(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve()
            with self.assertRaises(ValueError): d.validate_destination(p,'../outside')
    def test_destination_protected_rejected(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve()
            with self.assertRaises(ValueError): d.validate_destination(p,'presidio/real')
    def test_destination_venv_rejected(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve()
            with self.assertRaises(ValueError): d.validate_destination(p,'.venv/secret')
    def test_destination_nul_rejected(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x).resolve()
            with self.assertRaises(ValueError): d.validate_destination(p,'x\x00y')
    def test_source_hooks_missing(self):
        with tempfile.TemporaryDirectory() as x:
            self.assertFalse(d.source_hooks(Path(x))['sources_parseable'])
    def test_source_hooks_fake_good(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x)
            (p/'ui.py').write_text('class App:\n def _build(self):pass\n def _start(self):pass\n def _poll(self):pass\n def run_files(self):pass\n def run_scan(self):pass\n def run_text(self):pass\n')
            (p/'core.py').write_text('def _mask_media():pass\ndef dispatch_files():pass\n')
            (p/'handler_media.py').write_text('def mask_pdf():pass\n')
            (p/'ocr_backend.py').write_text('def rapid_ocr_words():\n return obj(return_word_box=True)\n')
            self.assertTrue(all(d.source_hooks(p).values()))
    def test_source_hooks_fake_bad(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x)
            for file in ('ui.py','core.py','handler_media.py','ocr_backend.py'):(p/file).write_text('pass\n')
            self.assertFalse(all(d.source_hooks(p).values()))
    def test_write_report(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x)
            report={'status':'SYNTHETIC_GUI_PAIR_PASS','mode':'pair','gui':{'click_to_finish_s':15,'p95_lag_ms':1,'p99_lag_ms':2,'max_lag_ms':3,'over_500ms':0},'trials':{},'pixel_identical':True}
            self.assertEqual(d.write_reports(p,report),1)
            self.assertEqual(d.write_reports(p,report),2)
            self.assertEqual(json.loads((p/'v6692_run_001.json').read_text())['mode'],'pair')
    def test_write_report_no_override(self):
        with tempfile.TemporaryDirectory() as x:
            p=Path(x)
            (p/'v6692_run_001.json').write_text('old')
            self.assertEqual(d.next_run_index(p),2)
            self.assertEqual((p/'v6692_run_001.json').read_text(),'old')
    def test_single_hypothesis_flag(self):
        self.assertNotIn('pixel_identical',d.filtered_trial({'safe':True,'pixel_identical':True}))
    def test_main_requires_project(self):
        with tempfile.TemporaryDirectory() as x:
            self.assertEqual(d.main(['--project-root',x,'--check-only']),2)
    def test_launcher_has_no_production_file_execution_api(self):
        tree=ast.parse(Path(__file__).with_name('v6692_gui.py').read_text(encoding='utf-8'))
        funcs={n.name for n in tree.body if isinstance(n,ast.FunctionDef)}
        self.assertIn('do_experiment',funcs)
        self.assertIn('launch_gui',funcs)
        self.assertIn('validate_destination',funcs)
    def test_report_has_no_source_code(self):
        self.assertNotIn('input_pdf',d.filtered_trial({'input_pdf':'secret','safe':True}))

if __name__=='__main__':unittest.main(verbosity=2)
