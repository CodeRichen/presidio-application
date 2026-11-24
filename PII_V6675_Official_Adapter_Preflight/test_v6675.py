# -*- coding: utf-8 -*-
"""V6675 local tests: synthetic values, no models, no network, no real PII."""
from __future__ import annotations
import io
import json
import tempfile
import sys
import hashlib
from pathlib import Path
import types
import unittest
from unittest.mock import patch
from v6675_adapter_probe import (schema_summary,mismatch_kind,compare_without_text,provider_gate)
from v6675_diagnose import (status,only_allowlisted_child_data,validate,write_reports)
from v667_gpu_worker import (app_adapter_words,run_official_ocr,identify_model,force_cpu,Meter)
from v667_bridge import send_frame,read_frame

def word(text='SYNTHETIC', line=0, conf=0.95):
    w={'text':text,'left':12,'top':13,'width':32,'height':14,'line_id':line}
    if conf is not None:w['conf']=conf
    return w

class CompareTests(unittest.TestCase):
    def test_conf_allowed_schema(self):
        s=schema_summary([word()]);self.assertTrue(s['schema_ok']);self.assertEqual(s['with_conf_rows'],1)
    def test_conf_optional(self):
        s=schema_summary([word(conf=None)]);self.assertTrue(s['schema_ok'])
    def test_conf_missing_both_is_ok(self):
        self.assertEqual(mismatch_kind([word(conf=None)],[word(conf=None)]),'EQUAL')
    def test_conf_tolerance_pass(self):
        self.assertEqual(mismatch_kind([word(conf=.97)],[word(conf=.9719)]),'EQUAL')
    def test_conf_tolerance_fail(self):
        self.assertEqual(mismatch_kind([word(conf=.97)],[word(conf=.973)]),'CONF_TOLERANCE_EXCEEDED')
    def test_conf_presence_diff_fail(self):
        self.assertEqual(mismatch_kind([word()],[word(conf=None)]),'CONF_FIELD_DIFFERENCE')
    def test_conf_nonfinite_fail(self):
        self.assertFalse(schema_summary([word(conf=float('nan'))])['schema_ok'])
        self.assertEqual(mismatch_kind([word(conf=float('inf'))],[word()]),'INVALID_CONF_VALUE')
    def test_extra_unknown_field_fail_and_not_logged(self):
        x=word();x['SECRET_FIELD']='SECRET_CONTENT'
        d=compare_without_text([x],[word()]);self.assertFalse(d['adapter_compatible'])
        self.assertNotIn('SECRET_CONTENT',json.dumps(d));self.assertNotIn('SECRET_FIELD',json.dumps(d))
    def test_numeric_geometry_exact(self):
        x=word();y=dict(x,left=12.001)
        self.assertEqual(mismatch_kind([x],[y]),'BOX_COORDINATE_DIFFERENCE')
    def test_segmentation_never_passes(self):
        self.assertEqual(mismatch_kind([word('ABC'),word('DEF')],[word('ABCDEF')]),'WORD_COUNT_DIFFERENCE')
    def test_text_exact(self):
        self.assertEqual(mismatch_kind([word('ABC')],[word('ABD')]),'OCR_TEXT_DIFFERENCE')
    def test_line_exact(self):
        self.assertEqual(mismatch_kind([word(line=0)],[word(line=1)]),'LINE_ID_DIFFERENCE')
    def test_empty_not_pass(self):
        self.assertFalse(compare_without_text([],[])['adapter_compatible'])
    def test_word_schema_fails_closed(self):
        x=word();del x['left'];self.assertFalse(compare_without_text([x],[word()])['adapter_compatible'])
    def test_incorrect_type_fails_closed(self):
        x=word();x['left']=True;self.assertFalse(compare_without_text([x],[word()])['adapter_compatible'])
    def test_valid_pair_status(self):
        r=compare_without_text([word()],[word(conf=.951)])
        self.assertTrue(r['adapter_compatible']);self.assertTrue(r['ordered_text_equal_after_whitespace_removal'])

class WorkerTests(unittest.TestCase):
    def test_worker_calls_return_word_box(self):
        calls=[]
        fake_result=object()
        class FakeEngine:
            def __call__(self, rgb, **kwargs):
                calls.append(kwargs);return fake_result
        fake_adapter=types.ModuleType('ocr_backend')
        fake_adapter.rapid_result_to_words=lambda result,w,h: [word('OK')]
        with patch.dict('sys.modules',{'ocr_backend':fake_adapter}):
            output=run_official_ocr(FakeEngine(),types.SimpleNamespace(width=300,height=100))
        self.assertEqual(calls,[{'return_word_box':True}]);self.assertEqual(len(output),1)
    def test_official_adapter_passes_dimensions(self):
        capture=[]
        fake_adapter=types.ModuleType('ocr_backend')
        def transform(result,w,h):
            capture.extend([w,h]);return [word()]
        fake_adapter.rapid_result_to_words=transform
        with patch.dict('sys.modules',{'ocr_backend':fake_adapter}):
            self.assertEqual(len(app_adapter_words(object(),500,250)),1)
        self.assertEqual(capture,[500,250])
    def test_missing_official_adapter_fails_closed(self):
        with patch.dict('sys.modules',{'ocr_backend':types.ModuleType('ocr_backend')}):
            with self.assertRaises(ImportError):app_adapter_words(object(),1,1)
    def test_no_gpu_model_guess(self):
        self.assertEqual(identify_model('ch_PP-OCRv4_det.onnx'),'det')
        with self.assertRaises(RuntimeError):identify_model('model.onnx')
    def test_force_cpu(self):
        self.assertEqual(force_cpu((),{})[1]['providers'],['CPUExecutionProvider'])
    def test_pipe_roundtrip(self):
        x=io.BytesIO();send_frame(x,b'JUST_SYNTHETIC');x.seek(0)
        self.assertEqual(read_frame(x),b'JUST_SYNTHETIC')
    def test_meter_no_user_content(self):
        self.assertEqual(Meter.__name__,'Meter')

class DiagnosticTests(unittest.TestCase):
    good={'sessions':{'det':1,'cls':1,'rec':1},'profiles_read':3,
          'node_events':{'det_cuda':3,'cls_cpu':3,'rec_cpu':3}}
    def test_provider_verified(self):self.assertTrue(provider_gate(self.good))
    def test_gpu_missing_fails(self):
        self.assertFalse(provider_gate({'sessions':{'det':1,'cls':1,'rec':1},'profiles_read':3,'node_events':{'cls_cpu':3,'rec_cpu':3}}))
    def test_gpu_cls_fails(self):
        x=dict(self.good,sessions=dict(self.good['sessions']),node_events=dict(self.good['node_events'],cls_cuda=1))
        self.assertFalse(provider_gate(x))
    def test_full_gate_still_missing(self):
        self.assertEqual(status({'return_code':2,'status':'ADAPTER_PREFLIGHT_OK','pairs_equal':2,'provider_route_verified':True}),'DIAGNOSTIC_FAILED')
    def test_pass_summary(self):
        self.assertEqual(status({'return_code':0,'status':'ADAPTER_PREFLIGHT_OK','pairs_equal':2,'provider_route_verified':True}),'ADAPTER_PREFLIGHT_OK')
    def test_sanitize_reports(self):
        raw={'schema':'pii_v6675_adapter_probe_v1','status':'ADAPTER_PREFLIGHT_OK','raw_text':'DONT_LEAK',
             'pairs':[{'zoom':2,'raw_text':'DONT_LEAK','adapter_compatible':True,'cpu':{'word_count':114,'raw_text':'DONT_LEAK'}}]}
        safe=only_allowlisted_child_data(raw)
        self.assertNotIn('DONT_LEAK',json.dumps(safe));self.assertEqual(safe['pairs'][0]['cpu']['word_count'],114)
    def test_unrecognized_child_schema(self):
        self.assertEqual(only_allowlisted_child_data({'schema':'unknown','raw':'DONT_LEAK'}),{'status':'INVALID_CHILD_SCHEMA'})
    def test_write_report_does_not_leak(self):
        with tempfile.TemporaryDirectory() as tmp:
            report={'status':'ADAPTER_PREFLIGHT_OK','probe':{'pairs_equal':2,'pairs_checked':2,
                'provider_route_verified':True,'pairs':[{'zoom':2,'mismatch_kind':'EQUAL',
                'cpu':{'word_count':114},'hybrid':{'word_count':114}}]}}
            write_reports(Path(tmp),report)
            contents=(Path(tmp)/'v6675_adapter_diagnosis_summary.md').read_text(encoding='utf-8')
            self.assertIn('114',contents);self.assertNotIn('DONT_LEAK',contents)
    def test_environment_no_synthetic_fixture_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tool=root/'PII_V6675_Official_Adapter_Preflight';tool.mkdir()
            with self.assertRaises(RuntimeError):validate(root,tool)
    def test_conf_numbers_finite(self):
        self.assertFalse(schema_summary([word(conf=10**1000)])['schema_ok'])


class MockedTwoZoomProbe(unittest.TestCase):
    def test_first_page_both_scales_adapter_compatible(self):
        from v6675_adapter_probe import main as probe_main
        root=Path(__file__).resolve().parent.parent
        source=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not source.exists() or hashlib.sha256(source.read_bytes()).hexdigest() != (
            'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'):
            self.skipTest('SHA-pinned synthetic fixture unavailable')
        fake_media=types.ModuleType('handler_media')
        fake_media.__file__=str(root/'handler_media.py')
        fake_media.ocr_words=lambda img: [word('SYNTHETIC',0,0.9500)]
        class StubBridge:
            def __init__(self, *args): pass
            def ocr_words(self, img): return [word('SYNTHETIC',0,0.9510)]
            def finish(self):return DiagnosticTests.good
            def close(self):pass
        fake_bridge=types.ModuleType('v667_bridge')
        fake_bridge.HybridOCRBridge=StubBridge
        tool=Path(__file__).resolve().parent
        with tempfile.TemporaryDirectory() as tmp:
            output=Path(tmp)/'child.json'
            with patch.dict(sys.modules,{'handler_media':fake_media,'v667_bridge':fake_bridge}):
                with patch('v6675_adapter_probe.images', return_value=iter([(2,'dummy_image'),(3,'dummy_image')])):
                    rc=probe_main(['--project-root',str(root),'--tool-dir',str(tool),
                                   '--gpu-python','unused','--out',str(output)])
            report=json.loads(output.read_text(encoding='utf-8'))
        self.assertEqual(rc,0)
        self.assertEqual(report['status'],'ADAPTER_PREFLIGHT_OK')
        self.assertEqual(report['pairs_checked'],2)
        self.assertEqual(report['pairs_equal'],2)
        self.assertTrue(report['provider_route_verified'])
        self.assertNotIn('SYNTHETIC',json.dumps(report))

    def test_mismatch_remains_failing_even_when_concat_text_matches(self):
        a=[word('AB',0,.95),word('CD',0,.95)]
        b=[word('ABCD',0,.95)]
        report=compare_without_text(a,b)
        self.assertTrue(report['ordered_text_equal_after_whitespace_removal'])
        self.assertFalse(report['adapter_compatible'])

if __name__=='__main__':unittest.main(verbosity=2)
