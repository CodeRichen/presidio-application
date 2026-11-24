# -*- coding: utf-8 -*-
"""V6.6.6 tests: no real OCR models, no real documents or PII processed."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
import v666_hybrid_trial as app

class FakeOptions:
    def __init__(self):
        self.enable_profiling=False
        self.profile_file_prefix=''

class FakeSession:
    def __init__(self, providers, path):
        self.providers = list(providers)
        self.path = path
    def get_providers(self):
        return self.providers
    def end_profiling(self):
        self.path.write_text(json.dumps([{'cat':'Node','args':{'provider':self.providers[0]}}]),encoding='utf-8')
        return str(self.path)

class FakeORT:
    SessionOptions=FakeOptions
    def __init__(self,root):
        self.root=Path(root)
        self.created=[]
        self.InferenceSession=self.orig
    def orig(self,model,opts=None, *args,**kwargs):
        providers=args[0] if args else kwargs.get('providers',['CUDAExecutionProvider','CPUExecutionProvider'])
        name=Path(model).name
        obj=FakeSession(providers,self.root/(name+str(len(self.created))+'.json'))
        self.created.append((name,list(providers),opts.enable_profiling))
        return obj

class V666Tests(unittest.TestCase):
    def test_types(self):
        self.assertEqual(app.identify_model('ch_PP-OCRv6_det_infer.onnx'),'det')
        self.assertEqual(app.identify_model('ch_PP-OCRv6_rec_infer.onnx'),'rec')
        self.assertEqual(app.identify_model('ch_ppocr_mobile_v2.0_cls_infer.onnx'),'cls')
    def test_case_and_path(self):
        self.assertEqual(app.identify_model(Path('C:/x/CH_PP-OCRV6_DET_INFER.ONNX')),'det')
    def test_false_positive_tokens(self):
        for name in ('detector.onnx','recognition.onnx','clam.onnx'):
            with self.subTest(name=name):
                with self.assertRaises(RuntimeError): app.identify_model(name)
    def test_ambiguity(self):
        with self.assertRaises(RuntimeError): app.identify_model('a_det_rec.onnx')
    def test_bytes_model(self):
        with self.assertRaises(RuntimeError): app.identify_model(b'bytes')
    def test_only_onnx(self):
        with self.assertRaises(RuntimeError): app.identify_model('ch_det_infer.pth')
    def test_force_cpu_kwargs(self):
        args,kwargs=app.force_cpu((),{'providers':['CUDAExecutionProvider'], 'provider_options':[{'device_id':0}]})
        self.assertEqual(args,())
        self.assertEqual(kwargs,{'providers':['CPUExecutionProvider']})
    def test_force_cpu_positional(self):
        args,kwargs=app.force_cpu((['CUDAExecutionProvider'],[{'device_id':0}]),{})
        self.assertEqual(args,(['CPUExecutionProvider'],None))
        self.assertEqual(kwargs,{})
    def test_no_orig_modified(self):
        with tempfile.TemporaryDirectory() as td:
            ort=FakeORT(td)
            original=ort.InferenceSession
            with app.TrialSessions(ort,td) as gate:
                self.assertNotEqual(ort.InferenceSession,original)
            self.assertEqual(ort.InferenceSession,original)
    def test_routes(self):
        with tempfile.TemporaryDirectory() as td:
            ort=FakeORT(td)
            with app.TrialSessions(ort,td) as gate:
                for mode in app.MODES:
                    gate.mode=mode
                    for kind in ('det','cls','rec'):
                        ort.InferenceSession(f'foo_{kind}_infer.onnx', sess_options=None,
                                             providers=['CUDAExecutionProvider','CPUExecutionProvider'])
                result=gate.counts()
            self.assertIsNone(app.provider_gate(result))
            self.assertEqual(result['node_events'].get('hybrid_cls_cuda',0),0)
            self.assertEqual(result['node_events'].get('hybrid_det_cuda',0),1)
            self.assertEqual(result['node_events'].get('hybrid_rec_cpu',0),1)
    def test_missing_stage(self):
        self.assertEqual(app.provider_gate({'sessions':{},'node_events':{}}),'SESSION_MODEL_ROUTE_UNVERIFIED')
    def test_no_kernel(self):
        sessions={f'{mode}_{kind}':1 for mode in app.MODES for kind in ('det','cls','rec')}
        self.assertEqual(app.provider_gate({'sessions':sessions,'node_events':{}}),'REQUIRED_PROVIDER_KERNEL_NOT_VERIFIED')
    def test_unexpected_cuda_in_cls(self):
        sessions={f'{mode}_{kind}':1 for mode in app.MODES for kind in ('det','cls','rec')}
        nodes={f'{mode}_{kind}_{"cuda" if mode=="cuda" or (mode=="hybrid" and kind=="det") else "cpu"}':1
               for mode in app.MODES for kind in ('det','cls','rec')}
        nodes['hybrid_cls_cuda']=1
        self.assertEqual(app.provider_gate({'sessions':sessions,'node_events':nodes}),'CPU_ONLY_MODEL_EXECUTED_CUDA')
    def test_generate_reports_no_secrets(self):
        with tempfile.TemporaryDirectory() as td:
            report={'status':'RUNTIME_ERROR','error_type':'ValueError','only_synthetic_documents':True}
            app.write_reports(Path(td),report)
            self.assertEqual(json.loads((Path(td)/app.REPORT_JSON).read_text())['status'],'RUNTIME_ERROR')
            body=(Path(td)/app.REPORT_MD).read_text()
            self.assertNotIn('OCR original',body)
            self.assertIn('不執行完整 PII',body)
    def test_zoom_count(self):
        self.assertEqual(len(app.ZOOMS),2)
        self.assertEqual(app.ZOOMS,(2.0,3.0))
    def test_mock_output_compatible(self):
        class Result: pass
        import numpy as np
        a=Result();b=Result()
        a.txts=['合成測試'];b.txts=['合成測試']
        a.boxes=np.array([[[0,0],[10,0],[10,10],[0,10]]]);b.boxes=a.boxes.copy()
        a.scores=np.array([.900]);b.scores=np.array([.901])
        out=app.compare_ocr_output(a,b)
        self.assertTrue(out['equivalent_for_screening'])
        self.assertTrue(out['exact_text_equal'])
    def test_mock_output_difference(self):
        class Result: pass
        import numpy as np
        a=Result();b=Result()
        a.txts=['測試'];b.txts=['不同']
        a.boxes=b.boxes=np.zeros((1,4,2));a.scores=b.scores=np.array([.5])
        self.assertFalse(app.compare_ocr_output(a,b)['equivalent_for_screening'])
    def test_report_schema(self):
        self.assertEqual(app.REPORT_JSON,'v666_hybrid_ocr_summary.json')

if __name__=='__main__':
    unittest.main(verbosity=2)
