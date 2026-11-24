"""V6.6.5 tests; no RapidOCR, GPU, user PDF, or network needed."""
from __future__ import annotations
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import v665_ocr_trial as v

class TestV665(unittest.TestCase):
    def test_status_all_pass(self):
        self.assertEqual(v.status_for(14,3,3,12,45,0), 'OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY')

    def test_status_repeated_pass(self):
        self.assertEqual(v.status_for(28,3,3,12,45,0,expected=28),'OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY')

    def test_incomplete_dual_zoom(self):
        self.assertEqual(v.status_for(13,3,3,12,45,0),'INCOMPLETE_DUAL_ZOOM')

    def test_gpu_missing(self):
        self.assertEqual(v.status_for(14,3,0,12,0,0),'NO_CONFIRMED_CUDA_OCR_KERNEL')

    def test_cpu_missing(self):
        self.assertEqual(v.status_for(14,0,3,0,45,0),'CPU_PROVIDER_NOT_VERIFIED')

    def test_mismatch(self):
        self.assertEqual(v.status_for(14,3,3,12,45,1),'OCR_OUTPUT_DIFFERENCE_NEEDS_REVIEW')

    def test_compare_identical(self):
        import numpy as np
        a=SimpleNamespace(txts=('Synthetic Secret 123',),boxes=np.array([[[1,2],[3,2],[3,4],[1,4]]]), scores=(0.99,))
        b=SimpleNamespace(txts=('Synthetic Secret 123',),boxes=np.array([[[1.4,2],[3,2],[3,4],[1,4]]]), scores=(0.989,))
        r=v.compare_ocr_output(a,b)
        self.assertTrue(r['equivalent_for_screening'])
        self.assertNotIn('Synthetic Secret',json.dumps(r))

    def test_compare_text_different(self):
        a=SimpleNamespace(txts=('secret',),boxes=[[[1,2]]],scores=[0.9])
        b=SimpleNamespace(txts=('SECRET',),boxes=[[[1,2]]],scores=[0.9])
        self.assertFalse(v.compare_ocr_output(a,b)['equivalent_for_screening'])

    def test_compare_box_different(self):
        a=SimpleNamespace(txts=('ABC',),boxes=[[[1,2]]],scores=[0.9])
        b=SimpleNamespace(txts=('ABC',),boxes=[[[3,2]]],scores=[0.9])
        self.assertFalse(v.compare_ocr_output(a,b)['equivalent_for_screening'])

    def test_compare_none(self):
        a=SimpleNamespace(txts=None,boxes=None,scores=None)
        self.assertTrue(v.compare_ocr_output(a,a)['equivalent_for_screening'])

    def test_compare_numpy_scores(self):
        import numpy as np
        a=SimpleNamespace(txts=('A','B'),boxes=np.zeros((2,4,2)),scores=np.array([0.9,0.8]))
        self.assertTrue(v.compare_ocr_output(a,a)['equivalent_for_screening'])

    def test_phase_seconds(self):
        self.assertEqual(v.phase_seconds(SimpleNamespace(elapse_list=[1,2,3])),{'det':1.,'cls':2.,'rec':3.})

    def test_phase_missing(self):
        self.assertEqual(v.phase_seconds(SimpleNamespace(elapse_list=None)),{})

    def test_report_no_raw_ocr(self):
        with tempfile.TemporaryDirectory() as temp:
            report={'status':'OCR_GPU_EQUIVALENT_SYNTHETIC_ONLY', 'checks':{'pairs_completed':14}}
            v.write_reports(Path(temp),report)
            content=(Path(temp)/v.REPORT_MD).read_text(encoding='utf-8')
            self.assertNotIn('Synthetic Secret 123',content)
            self.assertTrue((Path(temp)/v.REPORT_JSON).is_file())

    def test_sha_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            src=Path(temp)/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
            src.parent.mkdir(parents=True)
            src.write_bytes(b'not the original synthetic pdf')
            with self.assertRaisesRegex(RuntimeError,'SHA_MISMATCH'):
                v.require_fixture(Path(temp))

    def test_provider_meter(self):
        class FakeSession:
            def __init__(self, providers, filename):
                self._providers=providers; self.filename=filename
            def get_providers(self): return self._providers
            def end_profiling(self): return str(self.filename)
        with tempfile.TemporaryDirectory() as temp:
            temp=Path(temp)
            calls=[]
            class FakeOpts:
                def __init__(self): self.enable_profiling=False; self.profile_file_prefix=''
            def constructor(model, opts, *args, **kwargs):
                prov=kwargs.get('providers') or (args[0] if args else ['CUDAExecutionProvider'])
                calls.append((prov, opts.enable_profiling))
                filename=temp/f'file{len(calls)}.json'
                filename.write_text(json.dumps([{'cat':'Node','args':{'provider':prov[0]}}]))
                return FakeSession(prov,filename)
            ort=SimpleNamespace(InferenceSession=constructor,SessionOptions=FakeOpts)
            with v.SessionMeter(ort,temp) as meter:
                meter.mode='cpu'; c=ort.InferenceSession('synthetic-model')
                meter.mode='cuda'; g=ort.InferenceSession('synthetic-model',providers=['CUDAExecutionProvider','CPUExecutionProvider'])
                r=meter.counts()
            self.assertIs(ort.InferenceSession,constructor)
            self.assertEqual(c.get_providers(),['CPUExecutionProvider'])
            self.assertEqual(r['cpu_node_events'],1)
            self.assertEqual(r['cuda_node_events'],1)
            self.assertEqual(r['cpu_sessions'],1)
            self.assertEqual(r['gpu_sessions_with_cuda'],1)
            self.assertTrue(all(x[1] for x in calls))

    def test_pdf_preprocess_matches_properties(self):
        from PIL import Image
        raw=Image.new('RGB',(100,50),'white')
        result=v.preprocess(raw)
        self.assertEqual(result.size,(100,50))
        self.assertEqual(result.mode,'RGB')

    def test_fixed_pdf_zoom_render(self):
        # Uses optional mounted public/synthetic fixture only; no user data is read.
        repo=Path(__file__).resolve().parent.parent
        path=repo/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not path.is_file(): self.skipTest('fixture not installed in local test project')
        try:
            import fitz
        except ImportError:
            self.skipTest('PyMuPDF missing in tests')
        self.assertEqual(v.require_fixture(repo),path)
        with fitz.open(path) as pdf:
            self.assertEqual(len(pdf),7)
            p=v.render_page(pdf[0],2.)
            self.assertEqual(p.mode,'RGB')
            self.assertGreater(p.width,400)

if __name__ == '__main__':
    unittest.main(verbosity=2)
