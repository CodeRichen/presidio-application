#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from v6671_adapter_probe import mismatch_kind
from v6671_diagnose import safe_child, derive_status, write_report

W={'text':'SYNTHETIC','left':1.,'top':2.,'width':3.,'height':4.,'line_id':0}

class AdapterTests(unittest.TestCase):
    def test_equal(self): self.assertEqual(mismatch_kind([W],[dict(W)]),'EQUAL')
    def test_empty(self): self.assertEqual(mismatch_kind([],[]),'EMPTY_OCR_OUTPUT')
    def test_count(self): self.assertEqual(mismatch_kind([W],[]),'WORD_COUNT_DIFFERENCE')
    def test_not_list(self): self.assertEqual(mismatch_kind(None,[]),'INVALID_ADAPTER_TYPE')
    def test_schema(self):
        w=dict(W);w['unused']='X'
        self.assertEqual(mismatch_kind([W],[w]),'WORD_SCHEMA_DIFFERENCE')
    def test_text(self):
        w=dict(W);w['text']='DIFFERENT'
        self.assertEqual(mismatch_kind([W],[w]),'OCR_TEXT_DIFFERENCE')
    def test_line(self):
        w=dict(W);w['line_id']=1
        self.assertEqual(mismatch_kind([W],[w]),'LINE_ID_DIFFERENCE')
    def test_box(self):
        w=dict(W);w['left']=1.1
        self.assertEqual(mismatch_kind([W],[w]),'BOX_COORDINATE_DIFFERENCE')

class Decisions(unittest.TestCase):
    def test_gpu_fail(self):
        self.assertEqual(derive_status({'status':'GPU_PROBE_EXCEPTION'},{}),
                         'GPU_WORKER_INITIALIZATION_NEEDS_DIAGNOSIS')
    def test_adapter_mismatch(self):
        self.assertEqual(derive_status({'status':'GPU_PROBE_OK'},
                                       {'status':'OCR_ADAPTER_MISMATCH'}),'OCR_ADAPTER_MISMATCH')
    def test_provider_fail(self):
        self.assertEqual(derive_status({'status':'GPU_PROBE_OK'},
                                       {'status':'PROVIDER_ROUTE_NOT_VERIFIED'}),'PROVIDER_ROUTE_NOT_VERIFIED')
    def test_ok(self):
        self.assertEqual(derive_status({'status':'GPU_PROBE_OK'},
                                       {'status':'ADAPTER_PREFLIGHT_OK'}),'WORKER_AND_ADAPTER_PREFLIGHT_OK')
    def test_worker_fail(self):
        self.assertEqual(derive_status({'status':'GPU_PROBE_OK'},
                                       {'status':'ADAPTER_PROBE_EXCEPTION'}),'WORKER_OR_ADAPTER_EXCEPTION')

class ReportTests(unittest.TestCase):
    def test_report_no_ocr_text(self):
        with tempfile.TemporaryDirectory() as d:
            report={'status':'OCR_ADAPTER_MISMATCH',
                    'gpu_probe':{'status':'GPU_PROBE_OK','stage':'ORT_PROFILING'},
                    'adapter_probe':{'status':'OCR_ADAPTER_MISMATCH','stage':'COMPARE_1',
                                     'mismatch_kind':'OCR_TEXT_DIFFERENCE','pairs_checked':1}}
            write_report(Path(d),report)
            text=(Path(d)/'v6671_hybrid_diagnosis_summary.md').read_text(encoding='utf-8')
            self.assertIn('OCR_TEXT_DIFFERENCE',text)
            self.assertNotIn('SYNTHETIC ABC 12345',text)
    def test_child_report_allowlist(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'probe.json'
            p.write_text(json.dumps({'schema':'pii_v6671_gpu_probe_v1',
                                     'status':'GPU_PROBE_EXCEPTION','stage':'ORT_IMPORT',
                                     'extra_sensitive':'SHOULD_NOT_APPEAR'}),encoding='utf-8')
            done=type('SubProcess',(),{'returncode':2})()
            with mock.patch('subprocess.run',return_value=done):
                result=safe_child(['python','probe.py'],Path(d),{},p,20)
            self.assertNotIn('extra_sensitive',result)
    def test_timeout_status(self):
        import subprocess
        with tempfile.TemporaryDirectory() as d:
            with mock.patch('subprocess.run',side_effect=subprocess.TimeoutExpired('x',5)):
                r=safe_child(['x'],Path(d),{},Path(d)/'missing.json',5)
            self.assertEqual(r['status'],'PROBE_TIMEOUT')

if __name__=='__main__':unittest.main(verbosity=2)
