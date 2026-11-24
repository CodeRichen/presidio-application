import contextlib
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import v662_runtime as r
import v662_benchmark as b
from v662_child import checksum, FIXTURE_SHA256

class FakeOpts:
    enable_profiling=False
    profile_file_prefix=''

class FakeSession:
    def __init__(self, providers, profile):
        self.providers=providers
        self.profile=profile
    def get_providers(self):return self.providers
    def end_profiling(self):return self.profile

class FakeORT:
    def __init__(self):
        self.InferenceSession=self.construct
        self.sessions=[]
    def get_available_providers(self):return ['CUDAExecutionProvider','CPUExecutionProvider']
    def SessionOptions(self):return FakeOpts()
    def construct(self,path, options, *args,**kwargs):
        provider=kwargs.get('providers',args[0] if args else ['CPUExecutionProvider'])
        s=FakeSession(provider,self.profile)
        self.sessions.append((path,options,provider))
        return s

class TestTrial(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p=Path(self.tmp.name)
        self.fake=FakeORT()
        self.fake.profile=str(self.p/'trace.json')
        (self.p/'trace.json').write_text(json.dumps([
            {'args':{'provider':'CUDAExecutionProvider'},'name':'fake'},
            {'args':{'provider':'CPUExecutionProvider'},'name':'fake'},
            {'args':{'provider':'CUDAExecutionProvider'},'name':'fake'}]),encoding='utf-8')
    def test_ocr_callsite_detection(self):
        with patch.object(r,'_ocr_callsite',return_value=True):
            self.assertTrue(r._ocr_callsite())
    def test_cuda_install_and_restore(self):
        original=self.fake.InferenceSession
        trial=r.ProviderTrial(self.fake,'cuda',self.p).install()
        with patch.object(r,'_ocr_callsite',return_value=True):
            s=self.fake.InferenceSession('fake')
        v=trial.finish()
        self.assertEqual(s.get_providers()[0],'CUDAExecutionProvider')
        self.assertEqual(v['cuda_kernel_events'],2)
        self.assertEqual(v['ocr_session_create_calls'],1)
        self.assertEqual(self.fake.InferenceSession,original)
    def test_cpu_profiling_also_enabled(self):
        trial=r.ProviderTrial(self.fake,'cpu',self.p).install()
        with patch.object(r,'_ocr_callsite',return_value=True):
            self.fake.InferenceSession('fake')
        v=trial.finish()
        self.assertEqual(v['cpu_only_sessions'],1)
        self.assertTrue(self.fake.sessions[0][1].enable_profiling)
        self.assertEqual(self.fake.sessions[0][2],['CPUExecutionProvider'])
    def test_non_ocr_pass_through(self):
        trial=r.ProviderTrial(self.fake,'cuda',self.p).install()
        with patch.object(r,'_ocr_callsite',return_value=False):
            self.fake.InferenceSession('face')
        v=trial.finish()
        self.assertEqual(v['other_ort_sessions_bypassed'],1)
        self.assertEqual(v['ocr_session_create_calls'],0)
    def test_cuda_injected_positional(self):
        trial=r.ProviderTrial(self.fake,'cuda',self.p).install()
        with patch.object(r,'_ocr_callsite',return_value=True):
            self.fake.InferenceSession('a',None,['CPUExecutionProvider'])
        trial.finish()
        self.assertEqual(self.fake.sessions[0][2][0],'CUDAExecutionProvider')
    def test_no_cuda_provider_rejected(self):
        self.fake.get_available_providers=lambda:['CPUExecutionProvider']
        trial=r.ProviderTrial(self.fake,'cuda',self.p).install()
        with patch.object(r,'_ocr_callsite',return_value=True):
            with self.assertRaises(RuntimeError): self.fake.InferenceSession('fake')
        trial.finish()
    def test_profile_aggregate_counts(self):
        self.assertEqual(r._provider_counts(self.fake.profile),{'CUDAExecutionProvider':2,'CPUExecutionProvider':1})
    def test_bad_profile_no_leak(self):
        (self.p/'trace.json').write_text('{not json',encoding='utf-8')
        self.assertEqual(r._provider_counts(self.fake.profile),{})
    def test_bad_mode(self):
        with self.assertRaises(ValueError):r.ProviderTrial(self.fake,'bad',self.p)

class TestBenchmark(unittest.TestCase):
    def _run(self, seconds, reason=None):
        return {'elapsed_s':seconds,'safe':True,'reason':reason}
    def test_fast_results(self):
        x=b.classify([self._run(200),self._run(205)],[self._run(150),self._run(160)],[{'all_identical':True},{'all_identical':True}])
        self.assertEqual(x['status'],'PROMISING_AB_ONLY')
    def test_small_gain(self):
        x=b.classify([self._run(200)],[self._run(192)],[{'all_identical':True}])
        self.assertEqual(x['status'],'NO_CONFIRMED_GAIN')
    def test_security_failed(self):
        c=self._run(150);c['safe']=False
        self.assertEqual(b.classify([self._run(200)],[c],[{'all_identical':True}])['status'],'SECURITY_GATE_FAILED')
    def test_kernel_unverified(self):
        c=self._run(150,'CUDA_EXECUTION_UNVERIFIED')
        self.assertEqual(b.classify([self._run(200)],[c],[{'all_identical':True}])['status'],'CUDA_EXECUTION_UNVERIFIED')
    def test_pixel_difference(self):
        self.assertEqual(b.classify([self._run(200)],[self._run(150)],[{'all_identical':False}])['status'],'PIXEL_DIFFERENCE')
    def test_no_pair(self):
        self.assertEqual(b.classify([],[],[])['status'],'INCOMPLETE')
    def test_missing_pixel_comparison(self):
        self.assertEqual(b.classify([self._run(200)],[self._run(100)],[])['status'],'INCOMPLETE')
    def test_report_is_aggregate_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            d={'status':'CUDA_EP_NOT_AVAILABLE','baseline':[],'candidate':[],
               'pixel_checks':[],'comparison':{}}
            b.write_report(d,Path(tmp))
            self.assertIn('CUDA_EP_NOT_AVAILABLE',(Path(tmp)/'v662_gpu_ab_summary.md').read_text(encoding='utf-8'))
    def test_fixture_hash(self):
        src=Path(__file__).resolve().parent.parent/'PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf'
        if src.exists():self.assertEqual(checksum(src),FIXTURE_SHA256)
    def test_ort_preflight_returns_only_booleans(self):
        self.assertTrue(set(b.ort_status()).issubset({'ort_import_ok','cuda_ep_available','cpu_ep_available'}))

if __name__=='__main__':unittest.main(verbosity=2)
