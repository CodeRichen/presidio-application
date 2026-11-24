#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-contained tests; none touch GPU, models, PDF data or network."""
from __future__ import annotations
import importlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from v6672_adapter_probe import mismatch_kind,provider_gate
from v6672_diagnose import derive,safe_child,write_reports
from v6672_gpu_probe import probe,session_flags
from v667_bridge import strict_words_equal,send_frame,read_frame
from v667_gpu_worker import identify_model,force_cpu,Meter

SAMPLE={'text':'SYNTHETIC','left':1.0,'top':2.0,'width':3.0,'height':4.0,'line_id':0}

class AdapterRules(unittest.TestCase):
    def test_exact_words(self):
        self.assertEqual(mismatch_kind([SAMPLE],[dict(SAMPLE)]),'EQUAL')
        self.assertTrue(strict_words_equal([SAMPLE],[dict(SAMPLE)]))
    def test_count(self):
        self.assertEqual(mismatch_kind([SAMPLE],[]),'WORD_COUNT_DIFFERENCE')
    def test_empty_rejected(self):
        self.assertEqual(mismatch_kind([],[]),'EMPTY_OCR_OUTPUT')
    def test_text_difference(self):
        x=dict(SAMPLE,text='PRIVATE');self.assertEqual(mismatch_kind([SAMPLE],[x]),'OCR_TEXT_DIFFERENCE')
    def test_line_id_difference(self):
        x=dict(SAMPLE,line_id=1);self.assertEqual(mismatch_kind([SAMPLE],[x]),'LINE_ID_DIFFERENCE')
    def test_box_difference(self):
        x=dict(SAMPLE,left=1.1);self.assertEqual(mismatch_kind([SAMPLE],[x]),'BOX_COORDINATE_DIFFERENCE')
    def test_schema_difference(self):
        x=dict(SAMPLE,secret='DONOTWRITE');self.assertEqual(mismatch_kind([SAMPLE],[x]),'WORD_SCHEMA_DIFFERENCE')
    def test_nonstrict_type(self):
        self.assertEqual(mismatch_kind(None,[]),'INVALID_ADAPTER_TYPE')
    def test_provider_requires_all_models(self):
        full={'sessions':{'det':1,'cls':1,'rec':1},'profiles_read':3,
              'node_events':{'det_cuda':8,'cls_cpu':7,'rec_cpu':14}}
        self.assertTrue(provider_gate(full))
        full['sessions']['cls']=0
        self.assertFalse(provider_gate(full))
    def test_provider_no_undetected_cuda_cls(self):
        full={'sessions':{'det':1,'cls':1,'rec':1},'profiles_read':3,
              'node_events':{'det_cuda':8,'cls_cpu':7,'rec_cpu':14,'cls_cuda':1}}
        self.assertFalse(provider_gate(full))

class RoutingRules(unittest.TestCase):
    def test_models(self):
        self.assertEqual(identify_model('foo_det.onnx'),'det')
        self.assertEqual(identify_model('x_cls.onnx'),'cls')
        self.assertEqual(identify_model('x-rec.onnx'),'rec')
    def test_ambiguous_or_bytes_model_rejected(self):
        for value in ('x_det_rec.onnx','xx.onnx',b'opaque'):
            with self.assertRaises(RuntimeError):identify_model(value)
    def test_force_cpu_keyword(self):
        args,kwargs=force_cpu((),{'providers':['CUDAExecutionProvider']})
        self.assertEqual(args,())
        self.assertEqual(kwargs['providers'],['CPUExecutionProvider'])
    def test_force_cpu_positional(self):
        args,kwargs=force_cpu((['CUDAExecutionProvider'],{'x':1}),{})
        self.assertEqual(args[0],['CPUExecutionProvider'])
        self.assertIsNone(args[1])

class Decisions(unittest.TestCase):
    GPU={'return_code':0,'status':'GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY'}
    GOOD={'return_code':0,'status':'ADAPTER_PREFLIGHT_OK','pairs_equal':2}
    def test_success(self):
        self.assertEqual(derive(self.GPU,self.GOOD),'WORKER_AND_ADAPTER_PREFLIGHT_OK')
    def test_gpu_fail(self):
        self.assertEqual(derive({'return_code':2,'status':'INCOMPLETE'},{}),'GPU_DET_NOT_VERIFIED')
    def test_adapter_fail(self):
        self.assertEqual(derive(self.GPU,{'status':'OCR_ADAPTER_MISMATCH'}),'OCR_ADAPTER_MISMATCH')
    def test_provider_fail(self):
        self.assertEqual(derive(self.GPU,{'status':'PROVIDER_ROUTE_NOT_VERIFIED'}),'PROVIDER_ROUTE_NOT_VERIFIED')
    def test_adapter_exit_code_must_be_zero(self):
        x=dict(self.GOOD,return_code=3)
        self.assertEqual(derive(self.GPU,x),'WORKER_OR_ADAPTER_EXCEPTION')
    def test_gpu_exit_code_must_be_zero(self):
        x=dict(self.GPU,return_code=2)
        self.assertEqual(derive(x,self.GOOD),'GPU_DET_NOT_VERIFIED')
    def test_adapter_no_partial_pass(self):
        x=dict(self.GOOD,pairs_equal=1)
        self.assertEqual(derive(self.GPU,x),'WORKER_OR_ADAPTER_EXCEPTION')

class Sanitization(unittest.TestCase):
    def test_report_does_not_save_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            report={'status':'OCR_ADAPTER_MISMATCH','gpu_probe':{'status':'GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY'},
                    'adapter_probe':{'status':'OCR_ADAPTER_MISMATCH','mismatch_kind':'OCR_TEXT_DIFFERENCE'}}
            write_reports(Path(tmp),report)
            txt=(Path(tmp)/'v6672_lazy_session_summary.md').read_text(encoding='utf-8')
            self.assertIn('OCR_TEXT_DIFFERENCE',txt)
            self.assertNotIn('TOP_SECRET_OCR_CONTENT',txt)
    def test_safe_child_only_allowlisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'child.json'
            path.write_text(json.dumps({'schema':'pii_v6672_gpu_probe_v1',
                'status':'GPU_PROBE_EXCEPTION','stage':'RAPIDOCR_IMPORT',
                'secret_ocr_text':'LEAK','image_base64':'LEAK'}))
            result=types.SimpleNamespace(returncode=2)
            with mock.patch('subprocess.run',return_value=result):
                values=safe_child(['python','probe'],Path(tmp),{},path,30,'pii_v6672_gpu_probe_v1')
            self.assertNotIn('secret_ocr_text',values)
            self.assertNotIn('image_base64',values)
    def test_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('subprocess.run',side_effect=subprocess.TimeoutExpired('x',3)):
                info=safe_child(['x'],Path(tmp),{},Path(tmp)/'missing',3,'pii_v6672_gpu_probe_v1')
            self.assertEqual(info['status'],'PROBE_TIMEOUT')
    def test_binary_protocol_roundtrip(self):
        pipe=io.BytesIO()
        send_frame(pipe,b'\x89PNG\r\n\x1a\nFAKE')
        pipe.seek(0)
        self.assertEqual(read_frame(pipe),b'\x89PNG\r\n\x1a\nFAKE')

class SessionImportAndLaziness(unittest.TestCase):
    """Fake RapidOCR binds the session constructor during module import.

    If RapidOCR is imported BEFORE installing Meter, the fake session would be
    invisible, just as in V667.1. The new probe must pass with lazy creation.
    """
    def fake_ort(self):
        ort=types.ModuleType('onnxruntime')
        class Opts:
            enable_profiling=False
            profile_file_prefix=''
        class Sess:
            def __init__(self,model, sess_options=None,*args,**kwargs):
                kind=identify_model(model)
                providers=kwargs.get('providers',args[0] if args else ['CUDAExecutionProvider','CPUExecutionProvider'])
                self.providers=tuple(providers)
                self.kind=kind
                self.path=Path(sess_options.profile_file_prefix+'_fake.json')
            def get_providers(self):return self.providers
            def end_profiling(self):
                provider='CUDAExecutionProvider' if self.kind=='det' else 'CPUExecutionProvider'
                self.path.write_text(json.dumps([{'cat':'Node','args':{'provider':provider}}]))
                return str(self.path)
        ort.SessionOptions=Opts
        ort.InferenceSession=Sess
        return ort
    def test_lazy_import_bound_session_is_intercepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            src=Path(tmp)/'rapidocr.py'
            src.write_text('''from onnxruntime import InferenceSession\nclass RapidOCR:\n    def __init__(self,params=None):\n        self.loaded=False\n    def __call__(self,img):\n        if not self.loaded:\n            self.loaded=True\n            self.det=InferenceSession("fake_det.onnx",providers=["CUDAExecutionProvider","CPUExecutionProvider"])\n            self.cls=InferenceSession("fake_cls.onnx",providers=["CUDAExecutionProvider","CPUExecutionProvider"])\n            self.rec=InferenceSession("fake_rec.onnx",providers=["CUDAExecutionProvider","CPUExecutionProvider"])\n        return []\n''',encoding='utf-8')
            old=list(sys.path)
            sys.path.insert(0,tmp)
            try:
                with mock.patch.dict(sys.modules,{'onnxruntime':self.fake_ort()}):
                    sys.modules.pop('rapidocr',None)
                    out=probe(sys.modules['onnxruntime'],Path(tmp),lambda w,h:None)
                    self.assertEqual(out['status'],'GPU_DET_CUDA_VERIFIED_SYNTHETIC_ONLY')
                    self.assertEqual(out['sessions_after_construct'],{'det':0,'cls':0,'rec':0})
                    self.assertEqual(out['sessions_after_synthetic'],{'det':1,'cls':1,'rec':1})
                    self.assertEqual(out['det_cuda_node_events'],1)
            finally:
                sys.modules.pop('rapidocr',None)
                sys.path[:]=old
    def test_worker_ready_does_not_prematurely_require_three_sessions(self):
        import v667_gpu_worker as w
        ort=self.fake_ort()
        ort.__version__='1.31.1'
        ort.get_available_providers=lambda:['CUDAExecutionProvider','CPUExecutionProvider']
        ort.preload_dlls=lambda **kwargs:None
        rapid=types.ModuleType('rapidocr')
        class RapidOCR:
            def __init__(self,params=None):pass
        rapid.RapidOCR=RapidOCR
        sent=[]
        with mock.patch.dict(sys.modules,{'onnxruntime':ort,'rapidocr':rapid}):
            with mock.patch.object(w,'read_frame',return_value=b'!DONE'):
                with mock.patch.object(w,'send_frame',side_effect=lambda stream,body:sent.append(json.loads(body))):
                    w.worker(io.BytesIO())
        self.assertEqual([x['status'] for x in sent],['READY','SUMMARY'])
        self.assertFalse(provider_gate(sent[1]['provider_evidence']))

    def test_meter_restores_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            ort=self.fake_ort()
            original=ort.InferenceSession
            with Meter(ort,Path(tmp)) as meter:
                self.assertIsNot(ort.InferenceSession,original)
            self.assertIs(ort.InferenceSession,original)

if __name__=='__main__':unittest.main(verbosity=2)
