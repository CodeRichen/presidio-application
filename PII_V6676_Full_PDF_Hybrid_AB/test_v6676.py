#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure offline tests for V6.6.7.6. No full OCR/PDF model execution."""
from __future__ import annotations
import io
import json
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from v667_bridge import send_frame, read_frame, read_exact, strict_words_equal, MAX_RESP
from v667_gpu_worker import identify_model, force_cpu, app_adapter_words, run_official_ocr
from v6675_adapter_probe import mismatch_kind, schema_summary
from v6676_benchmark import (provider_gate, summarize, strictly_identical_pages,
                             child_env, run_trial, EXPECTED_SHA, write_report)
import v6676_child


def word(txt='SYNTHETIC', confidence=.987):
    out={'text':txt,'left':1,'top':2,'width':3,'height':4,'line_id':0}
    if confidence is not None:out['conf']=confidence
    return out


class ProtocolTests(unittest.TestCase):
    def test_frame_roundtrip(self):
        p=io.BytesIO();send_frame(p,b'SYNTHETIC');p.seek(0)
        self.assertEqual(read_frame(p),b'SYNTHETIC')
    def test_header_short(self):
        with self.assertRaises(RuntimeError):read_frame(io.BytesIO(b'\x00'))
    def test_payload_short(self):
        with self.assertRaises(RuntimeError):read_frame(io.BytesIO(b'\x00\x00\x00\x04a'))
    def test_payload_large(self):
        with self.assertRaises(RuntimeError):read_frame(io.BytesIO(struct.pack('!I',MAX_RESP+1)))
    def test_empty_framing(self):
        with self.assertRaises(RuntimeError):read_frame(io.BytesIO(b'\x00\x00\x00\x00'))
    def test_send_limit(self):
        with self.assertRaises(RuntimeError):send_frame(io.BytesIO(),b'1234',3)
    def test_read_exact(self):self.assertEqual(read_exact(io.BytesIO(b'abc'),3),b'abc')

class AdapterTests(unittest.TestCase):
    def test_equal_word_box_conf(self):self.assertTrue(strict_words_equal([word()],[word(confidence=.988)]))
    def test_reject_mismatched_text(self):self.assertFalse(strict_words_equal([word()],[word('DIFFERENT')]))
    def test_reject_mismatched_location(self):
        x=word();x['left']+=1
        self.assertFalse(strict_words_equal([word()],[x]))
    def test_reject_mismatched_line_id(self):
        x=word();x['line_id']=1
        self.assertFalse(strict_words_equal([word()],[x]))
    def test_reject_mismatched_conf(self):self.assertFalse(strict_words_equal([word()],[word(confidence=.991)]))
    def test_reject_missing_conf(self):self.assertFalse(strict_words_equal([word()],[word(confidence=None)]))
    def test_reject_empty_word_lists(self):self.assertFalse(strict_words_equal([],[]))
    def test_reject_unknown_field(self):
        x=word();x['PRIVATE']='PRIVATE_PII'
        self.assertFalse(strict_words_equal([x],[word()]))
    def test_result_converter_requires_word_boxes(self):
        calls=[]
        class Engine:
            def __call__(self,img,**kwargs):
                calls.append(kwargs);return object()
        adapter=types.ModuleType('ocr_backend')
        adapter.rapid_result_to_words=lambda result,w,h:[word()]
        with mock.patch.dict(sys.modules,{'ocr_backend':adapter}):
            output=run_official_ocr(Engine(),types.SimpleNamespace(width=99,height=55))
        self.assertEqual(calls,[{'return_word_box':True}]);self.assertEqual(len(output),1)
    def test_adapter_dimensions(self):
        calls=[];adapter=types.ModuleType('ocr_backend')
        def converter(out,w,h):calls.extend([w,h]);return [word()]
        adapter.rapid_result_to_words=converter
        with mock.patch.dict(sys.modules,{'ocr_backend':adapter}):
            app_adapter_words(None,301,77)
        self.assertEqual(calls,[301,77])
    def test_rapid_schema(self):self.assertTrue(schema_summary([word()])['schema_ok'])
    def test_word_mismatch_diagnostic_safe(self):self.assertEqual(mismatch_kind([word()],[word('OTHER')]),'OCR_TEXT_DIFFERENCE')

class RoutingTests(unittest.TestCase):
    good={'sessions':{'det':1,'cls':1,'rec':1},'node_events':{'det_cuda':10,'cls_cpu':20,'rec_cpu':30},'profiles_read':3}
    def test_identify_det(self):self.assertEqual(identify_model('ch_PP-OCRv4_det_infer.onnx'),'det')
    def test_identify_cls(self):self.assertEqual(identify_model('ch_PP-OCRv4_cls_infer.onnx'),'cls')
    def test_identify_rec(self):self.assertEqual(identify_model('ch_PP-OCRv4_rec_infer.onnx'),'rec')
    def test_reject_ambiguous(self):
        with self.assertRaises(RuntimeError):identify_model('det_rec.onnx')
    def test_reject_unknown(self):
        with self.assertRaises(RuntimeError):identify_model('model.onnx')
    def test_force_cpu(self):
        args,kwargs=force_cpu((),{})
        self.assertEqual(kwargs['providers'],['CPUExecutionProvider'])
    def test_valid_hybrid_route(self):self.assertTrue(provider_gate(self.good))
    def test_reject_cpu_det(self):self.assertFalse(provider_gate({'sessions':self.good['sessions'],'node_events':{'det_cpu':20,'cls_cpu':20,'rec_cpu':30},'profiles_read':3}))
    def test_reject_cuda_cls(self):
        x=dict(self.good,node_events=dict(self.good['node_events'],cls_cuda=1))
        self.assertFalse(provider_gate(x))
    def test_reject_cuda_rec(self):
        x=dict(self.good,node_events=dict(self.good['node_events'],rec_cuda=1))
        self.assertFalse(provider_gate(x))
    def test_reject_incomplete_profiles(self):
        x=dict(self.good,profiles_read=2)
        self.assertFalse(provider_gate(x))

class TimingTests(unittest.TestCase):
    @staticmethod
    def pair(cpu,hybrid,pixels=True):
        return {'cpu':{'safe':True,'processing_s':cpu},'hybrid':{'safe':True,'processing_s':hybrid},'pixel_identical':pixels}
    def test_one_pair_not_confirmed(self):
        x=summarize({'pairs':[self.pair(180,95)]})
        self.assertEqual(x['status'],'INSUFFICIENT_REPEATS')
    def test_three_pairs_promising(self):
        x=summarize({'pairs':[self.pair(180,95),self.pair(180,96),self.pair(181,97)]})
        self.assertEqual(x['status'],'PROMISING_FULL_PDF_AB_ONLY')
    def test_no_gain(self):
        x=summarize({'pairs':[self.pair(180,179)]*3})
        self.assertEqual(x['status'],'NO_CONFIRMED_FULL_PDF_GAIN')
    def test_pixels_mismatch_fails(self):
        x=summarize({'pairs':[self.pair(180,95,pixels=False)]})
        self.assertEqual(x['status'],'PDF_PIXEL_DIFFERENCE')
    def test_unsafe_pair_fails(self):
        x=self.pair(180,95);x['hybrid']['safe']=False
        self.assertEqual(summarize({'pairs':[x]})['status'],'SECURITY_OR_EXECUTION_FAILED')
    def test_page_pixel_gate_pass(self):
        gate=types.SimpleNamespace(compare_page_pixels=lambda *args,**kwargs:{'all_identical':True,'pages_compared':7,'identical_pages':7})
        self.assertTrue(strictly_identical_pages(gate,'a','b'))
    def test_page_pixel_gate_reject_one(self):
        gate=types.SimpleNamespace(compare_page_pixels=lambda *args,**kwargs:{'all_identical':False,'pages_compared':7,'identical_pages':6})
        self.assertFalse(strictly_identical_pages(gate,'a','b'))
    def test_env_requires_secure_export(self):
        e=child_env()
        self.assertEqual(e['PII_PDF_EXPORT_V5'],'1')
        self.assertEqual(e['PII_OCR_BACKEND'],'rapid')
        self.assertEqual(e['PII_FACE_ENABLED'],'1')
        self.assertEqual(e['PII_MEDIA_FP_PROFILE'],'synthetic_face_test')

class FullRunStubs(unittest.TestCase):
    def test_first_page_mismatch_stops_before_pdf(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); runner=root/'PII_V5_Security_Acceptance'/'run_7page_pdf_test.py'
            runner.parent.mkdir()
            handler=types.ModuleType('handler_media');handler.__file__=str(root/'handler_media.py')
            old_ocr=lambda img:[word('FORMAL')]
            handler.ocr_words=old_ocr
            app_run=types.ModuleType('run_7page_pdf_test')
            app_run.run=mock.Mock(side_effect=AssertionError('MUST_NOT_EXECUTE'))
            class BadBridge:
                def __init__(self,*args):pass
                def ocr_words(self,im):return [word('DIFFERENT')]
                def close(self):pass
            with mock.patch.dict(sys.modules,{'handler_media':handler,'run_7page_pdf_test':app_run}):
                with mock.patch.object(v6676_child,'HybridOCRBridge',BadBridge):
                    with mock.patch.object(v6676_child,'preview_images',return_value=['fake','fake']):
                        res=v6676_child.run(root,root/'fake.pdf',runner,root/'gt.json',root/'out','hybrid','python','worker')
            self.assertEqual(res['status'],'OCR_ADAPTER_MISMATCH')
            self.assertEqual(res['preflight_mismatch_kind'],'OCR_TEXT_DIFFERENCE')
            self.assertEqual(res['preflight_equal_pairs'],0)
            app_run.run.assert_not_called()
            self.assertIs(handler.ocr_words,old_ocr)

    def test_two_zoom_good_then_complete_raster_runner(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); runner=root/'PII_V5_Security_Acceptance'/'run_7page_pdf_test.py'
            runner.parent.mkdir()
            handler=types.ModuleType('handler_media');handler.__file__=str(root/'handler_media.py')
            old_ocr=lambda img:[word()];handler.ocr_words=old_ocr
            app_run=types.ModuleType('run_7page_pdf_test')
            def synthetic_run(source,expected,outdir):
                outdir.mkdir(exist_ok=True)
                (outdir/'pdf_face_7page_audit.json').write_text(json.dumps({'processing_seconds':88.123}))
                (outdir/'pii_face_7page_masked.pdf').write_bytes(b'SYNTHETIC_ONLY')
            app_run.run=mock.Mock(side_effect=synthetic_run)
            class GoodBridge:
                ocr_calls=16;ocr_wall_s=17.0
                def __init__(self,*args):pass
                def ocr_words(self,im):return [word(confidence=.988)]
                def finish(self):return RoutingTests.good
                def close(self):pass
            with mock.patch.dict(sys.modules,{'handler_media':handler,'run_7page_pdf_test':app_run}):
                with mock.patch.object(v6676_child,'HybridOCRBridge',GoodBridge):
                    with mock.patch.object(v6676_child,'preview_images',return_value=['fake','fake']):
                        res=v6676_child.run(root,root/'fake.pdf',runner,root/'gt.json',root/'out','hybrid','python','worker')
            self.assertEqual(res['status'],'COMPLETE')
            self.assertEqual(res['preflight_equal_pairs'],2)
            self.assertEqual(res['worker_ocr_calls'],14)
            self.assertEqual(res['processing_s'],88.123)
            self.assertIs(handler.ocr_words,old_ocr)

    def test_exception_stage_is_kept_private(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); runner=root/'PII_V5_Security_Acceptance'/'run_7page_pdf_test.py';runner.parent.mkdir()
            handler=types.ModuleType('handler_media');handler.__file__=str(root/'handler_media.py');handler.ocr_words=lambda im:[]
            app_run=types.ModuleType('run_7page_pdf_test');app_run.run=lambda *a:None
            with mock.patch.dict(sys.modules,{'handler_media':handler,'run_7page_pdf_test':app_run}):
                with mock.patch.object(v6676_child,'HybridOCRBridge',side_effect=RuntimeError('PRIVATE_PII_TEXT')):
                    res=v6676_child.run(root,root/'fake.pdf',runner,root/'gt.json',root/'out','hybrid','python','worker')
            self.assertEqual(res['status'],'CHILD_EXCEPTION')
            self.assertEqual(res['stage'],'GPU_WORKER_START')
            self.assertEqual(res['error_type'],'RuntimeError')
            self.assertNotIn('PRIVATE_PII_TEXT',json.dumps(res))

    def test_incorrect_document_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            with self.assertRaises(RuntimeError):
                v6676_child.main(['--project-root',str(root),'--source',str(root/'real.pdf'),
                    '--runner',str(root/'runner.py'),'--expected',str(root/'gt.json'),
                    '--outdir',str(root/'out'),'--mode','hybrid',
                    '--gpu-python',str(root/'gpu.exe'),'--worker',str(root/'worker.py')])

    def test_report_no_ocr_payload(self):
        with tempfile.TemporaryDirectory() as td:
            report={'status':'INSUFFICIENT_REPEATS','pairs':[{'pair':1,'order':'BA',
                'cpu':{'safe':True,'processing_s':182},'hybrid':{'safe':True,'processing_s':130},
                'pixel_identical':True}],'comparison':{'improvement_pct':28.5}}
            write_report(Path(td),report)
            text=(Path(td)/'v6676_full_pdf_hybrid_summary.md').read_text('utf-8')
            self.assertNotIn('PRIVATE_PII_TEXT',text)
            self.assertIn('28.5',text)

    def test_sha_pinned_fixture(self):
        import hashlib
        root=Path(__file__).resolve().parent.parent
        src=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not src.is_file():self.skipTest('No synthetic fixture co-located')
        self.assertEqual(hashlib.sha256(src.read_bytes()).hexdigest(),EXPECTED_SHA)

if __name__=='__main__':unittest.main(verbosity=2)
