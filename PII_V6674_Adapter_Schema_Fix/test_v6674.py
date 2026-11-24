#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6674 tests: no OCR models, no internet and no real document content."""
from __future__ import annotations
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from v6674_adapter_probe import (schema_summary, mismatch_kind, compare_without_text,
                                 provider_gate, main as probe_main)
from v6674_diagnose import (only_allowlisted_child_data, status,
                            run_child, validate, write_reports)
from v667_bridge import strict_words_equal, send_frame, read_frame
from v667_gpu_worker import identify_model, force_cpu


def w(text='FAKE_TEST', line_id=0):
    return {'text':text,'left':1.0,'top':2.0,'width':3.0,'height':4.0,
            'line_id':line_id}


class SchemaTests(unittest.TestCase):
    def test_full_valid(self):
        x=schema_summary([w()])
        self.assertTrue(x['schema_ok'])
        self.assertEqual(x['word_count'],1)
        self.assertEqual(x['line_id_groups'],1)
    def test_missing_field_observed(self):
        x=w('SECRET_RAW');del x['line_id']
        s=schema_summary([x])
        self.assertFalse(s['schema_ok'])
        self.assertEqual(s['required_missing']['line_id'],1)
        self.assertIsNone(s['line_id_groups'])
        self.assertNotIn('SECRET_RAW',json.dumps(s))
    def test_extra_field_names_never_output(self):
        x=w();x['PRIVATE_KEY_NAME']='SECRET_RAW'
        s=schema_summary([x])
        self.assertEqual(s['unexpected_field_total'],1)
        self.assertEqual(s['unexpected_field_rows'],1)
        self.assertNotIn('PRIVATE_KEY_NAME',json.dumps(s))
        self.assertNotIn('SECRET_RAW',json.dumps(s))
    def test_wrong_coordinate_type(self):
        x=w();x['left']=True
        s=schema_summary([x])
        self.assertEqual(s['required_wrong_type']['left'],1)
    def test_wrong_text_type(self):
        x=w();x['text']=None
        s=schema_summary([x])
        self.assertIsNone(s['text_codepoints_total'])
        self.assertEqual(s['required_wrong_type']['text'],1)
    def test_non_dict_words(self):
        s=schema_summary([('private','tuple')])
        self.assertEqual(s['non_dict_rows'],1)
        self.assertFalse(s['schema_ok'])
    def test_nonlist_adapter(self):
        s=schema_summary({'text':'SECRET_RAW'})
        self.assertFalse(s['schema_ok'])
        self.assertIsNone(s['word_count'])
    def test_invalid_mixed_records(self):
        s=schema_summary([w(),None,[]])
        self.assertEqual(s['dict_rows'],1)
        self.assertEqual(s['non_dict_rows'],2)
    def test_aggregate_text_only(self):
        s=schema_summary([w(' A B '),w('C')])
        self.assertEqual(s['text_codepoints_total'],6)
        self.assertEqual(s['text_codepoints_without_whitespace'],3)
        self.assertEqual(s['words_with_internal_whitespace'],1)
    def test_empty_list_does_not_pass_equality(self):
        x=compare_without_text([],[])
        self.assertFalse(x['exact_adapter_equal'])
        self.assertEqual(x['mismatch_kind'],'EMPTY_OCR_OUTPUT')


class NoKeyErrorRegression(unittest.TestCase):
    def test_previous_6673_keyerror_case(self):
        broken={'text':'TOP_SECRET'}
        r=compare_without_text([broken],[w()])
        self.assertFalse(r['cpu_schema_ok'])
        self.assertTrue(r['hybrid_schema_ok'])
        self.assertFalse(r['exact_adapter_equal'])
        self.assertIsNone(r['ordered_text_equal_after_whitespace_removal'])
        self.assertEqual(r['hypothesis'],'CPU_ADAPTER_SCHEMA_INVALID')
        self.assertNotIn('TOP_SECRET',json.dumps(r))
    def test_mismatch_counts_and_invalid_schema(self):
        r=compare_without_text([{'text':'PII'}]*2,[w()])
        self.assertEqual(r['mismatch_kind'],'WORD_COUNT_DIFFERENCE')
        self.assertEqual(r['cpu']['word_count'],2)
        self.assertEqual(r['hybrid']['word_count'],1)
        self.assertFalse(r['exact_adapter_equal'])
    def test_valid_equal(self):
        self.assertTrue(compare_without_text([w()],[w()])['exact_adapter_equal'])
    def test_different_order(self):
        r=compare_without_text([w('A'),w('B')],[w('B'),w('A')])
        self.assertFalse(r['ordered_text_equal_after_whitespace_removal'])
    def test_split_with_same_stream_still_fails(self):
        r=compare_without_text([w('AB'),w('CD')],[w('ABCD')])
        self.assertTrue(r['ordered_text_equal_after_whitespace_removal'])
        self.assertFalse(r['exact_adapter_equal'])
    def test_coordinates_different_no_text_leak(self):
        x=w('SENSITIVE_DOC');y=dict(x,left=4)
        r=compare_without_text([x],[y])
        self.assertEqual(r['mismatch_kind'],'BOX_COORDINATE_DIFFERENCE')
        self.assertNotIn('SENSITIVE_DOC',json.dumps(r))
    def test_line_ids_different(self):
        r=compare_without_text([w('A',0)],[w('A',9)])
        self.assertEqual(r['mismatch_kind'],'LINE_ID_DIFFERENCE')
    def test_unknown_extra_field_name(self):
        x=w();x['SENSITIVE_CUSTOM_FIELD']='RAW'
        r=compare_without_text([x],[w()])
        self.assertNotIn('SENSITIVE_CUSTOM_FIELD',json.dumps(r))
        self.assertNotIn('RAW',json.dumps(r))
        self.assertFalse(r['exact_adapter_equal'])


class RoutesAndReports(unittest.TestCase):
    good={'sessions':{'det':1,'cls':1,'rec':1},'profiles_read':3,
          'node_events':{'det_cuda':2,'cls_cpu':3,'rec_cpu':4}}
    def test_provider_ok(self): self.assertTrue(provider_gate(self.good))
    def test_cls_cuda_reject(self):
        e={**self.good,'node_events':{**self.good['node_events'],'cls_cuda':1}}
        self.assertFalse(provider_gate(e))
    def test_incomplete_sessions_reject(self):
        self.assertFalse(provider_gate({'sessions':{'det':1}}))
    def test_status_schema_mismatch(self):
        self.assertEqual(status({'status':'OCR_ADAPTER_SCHEMA_MISMATCH'}),
                         'OCR_ADAPTER_SCHEMA_MISMATCH')
    def test_status_failclosed_without_route(self):
        self.assertEqual(status({'status':'ADAPTER_PREFLIGHT_OK','return_code':2,
                                 'pairs_equal':2,'provider_route_verified':True}),
                         'DIAGNOSTIC_FAILED')
    def test_report_allowlist_nested_fields(self):
        raw={'schema':'pii_v6674_adapter_probe_v1','status':'OCR_ADAPTER_SCHEMA_MISMATCH',
             'private':'SECRET', 'pairs':[{
             'zoom':2,'hypothesis':'CPU_ADAPTER_SCHEMA_INVALID',
             'raw_text':'SENSITIVE',
             'cpu':{'word_count':7,'required_missing':{'text':1,'PRIVATE':999},
                    'extra': 'SECRET'}, 'hybrid':{'word_count':7}}]}
        r=only_allowlisted_child_data(raw)
        output=json.dumps(r)
        self.assertNotIn('SECRET',output)
        self.assertNotIn('SENSITIVE',output)
        self.assertNotIn('PRIVATE',output)
        self.assertEqual(r['pairs'][0]['cpu']['required_missing']['text'],1)
    def test_bad_child_schema(self):
        self.assertEqual(only_allowlisted_child_data({'schema':'untrusted'}),
                         {'status':'INVALID_CHILD_SCHEMA'})
    def test_write_summary_is_sanitized(self):
        with tempfile.TemporaryDirectory() as td:
            report={'status':'OCR_ADAPTER_SCHEMA_MISMATCH','probe':{
                'status':'OCR_ADAPTER_SCHEMA_MISMATCH','stage':'COMPLETE','pairs_equal':0,
                'pairs_checked':2,'provider_route_verified':True,'pairs':[
                    {'zoom':2,'cpu':{'word_count':7,'dict_rows':7,'non_dict_rows':0,
                     'unexpected_field_rows':0,'required_missing':{'line_id':7}},
                     'hybrid':{'word_count':7,'dict_rows':7,'non_dict_rows':0},
                     'hypothesis':'CPU_ADAPTER_SCHEMA_INVALID',
                     'mismatch_kind':'WORD_SCHEMA_DIFFERENCE'}]}}
            write_reports(Path(td),report)
            txt=(Path(td)/'v6674_adapter_diagnosis_summary.md').read_text(encoding='utf8')
            self.assertIn('line_id=7',txt)
            self.assertIn('OCR_ADAPTER_SCHEMA_MISMATCH',txt)
    def test_timeout_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch('subprocess.run',side_effect=subprocess.TimeoutExpired('x',5)):
                r=run_child(Path('python'),Path(td),Path(td),Path('gpu'),Path(td)/'missing',30)
            self.assertEqual(r['status'],'PROBE_TIMEOUT')
    def test_spawn_error(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch('subprocess.run',side_effect=OSError('secret')):
                r=run_child(Path('python'),Path(td),Path(td),Path('gpu'),Path(td)/'missing',30)
            self.assertEqual(r['return_code'],'SPAWN_ERROR')
            self.assertNotIn('secret',json.dumps(r))
    def test_fixture_missing(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            tool=root/'PII_V6674_Adapter_Schema_Fix';tool.mkdir()
            with self.assertRaises(RuntimeError): validate(root,tool)
    def test_bridge_protocol(self):
        buf=io.BytesIO();send_frame(buf,b'SYNTHETIC');buf.seek(0)
        self.assertEqual(read_frame(buf),b'SYNTHETIC')
    def test_strict_gate_kept(self):
        self.assertFalse(strict_words_equal([w('A'),w('B')],[w('AB')]))
    def test_worker_model_identification(self):
        self.assertEqual(identify_model('model_det.onnx'),'det')
        self.assertEqual(force_cpu((),{})[1]['providers'],['CPUExecutionProvider'])


class MockedEndToEndProbe(unittest.TestCase):
    def test_both_zooms_continue_after_cpu_schema_failure_and_verify_route(self):
        from v6674_adapter_probe import main as run
        # Fixture is SHA-pinned. This test NEVER imports actual OCR/PII models.
        root=Path(__file__).resolve().parents[1]
        fixture=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not fixture.exists():self.skipTest('Pinned synthetic fixture unavailable')
        expected='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
        import hashlib
        if hashlib.sha256(fixture.read_bytes()).hexdigest()!=expected:
            self.skipTest('Pinned synthetic fixture SHA mismatch')
        fake_media=types.ModuleType('handler_media')
        fake_media.__file__=str(root/'handler_media.py')
        fake_media.ocr_words=lambda img:[{'text':'SECRET_FAKE','left':1,'top':2,
                                           'width':3,'height':4}] # missing line_id
        class StubBridge:
            def __init__(self,*args):pass
            def ocr_words(self,img):return [w('SECRET_FAKE')]
            def finish(self):return RoutesAndReports.good
            def close(self):pass
        fake_bridge=types.ModuleType('v667_bridge')
        fake_bridge.HybridOCRBridge=StubBridge
        tool=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as td:
            out=Path(td)/'safe.json'
            with mock.patch.dict(sys.modules,{'handler_media':fake_media,'v667_bridge':fake_bridge}):
                with mock.patch('v6674_adapter_probe.images',return_value=iter([(2,'img'),(3,'img')])):
                    rc=run(['--project-root',str(root),'--tool-dir',str(tool),
                            '--gpu-python','unused','--out',str(out)])
            report=json.loads(out.read_text(encoding='utf8'))
            self.assertEqual(rc,2)
            self.assertEqual(report['pairs_checked'],2)
            self.assertEqual(report['status'],'OCR_ADAPTER_SCHEMA_MISMATCH')
            self.assertTrue(report['provider_route_verified'])
            self.assertNotIn('SECRET_FAKE',json.dumps(report))


if __name__=='__main__':
    unittest.main(verbosity=2)
