#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure offline unit tests, no model loading or PDF OCR."""
from __future__ import annotations
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock
import json

from v667_bridge import read_exact, send_frame, read_frame, strict_words_equal, MAX_RESP
from v667_benchmark import provider_gate, summarize, require_setup, EXPECTED_SHA
from v667_gpu_worker import identify_model, force_cpu, result_to_words


class FrameTests(unittest.TestCase):
    def test_roundtrip(self):
        b=io.BytesIO();send_frame(b,b'hello');b.seek(0)
        self.assertEqual(read_frame(b),b'hello')

    def test_empty_frame_fails(self):
        with self.assertRaises(RuntimeError):
            read_frame(io.BytesIO(b'\x00\x00\x00\x00'))

    def test_giant_frame_fails(self):
        with self.assertRaises(RuntimeError):
            read_frame(io.BytesIO(struct.pack('!I',MAX_RESP+1)))

    def test_short_header(self):
        with self.assertRaises(RuntimeError):
            read_frame(io.BytesIO(b'\x00'))

    def test_short_payload(self):
        with self.assertRaises(RuntimeError):
            read_frame(io.BytesIO(b'\x00\x00\x00\x04x'))

    def test_request_limit(self):
        with self.assertRaises(RuntimeError):
            send_frame(io.BytesIO(),b'x'*10,max_size=9)


class AdapterTests(unittest.TestCase):
    W={'text':'ABC','left':1,'top':2,'width':3,'height':4,'line_id':0}
    def test_exact_agreement(self):
        self.assertTrue(strict_words_equal([dict(self.W)],[dict(self.W)]))

    def test_reject_missing_fields(self):
        wrong=dict(self.W);del wrong['line_id']
        self.assertFalse(strict_words_equal([dict(self.W)],[wrong]))

    def test_reject_changed_text(self):
        wrong=dict(self.W);wrong['text']='ABD'
        self.assertFalse(strict_words_equal([dict(self.W)],[wrong]))

    def test_reject_changed_coordinates(self):
        wrong=dict(self.W);wrong['left']=1.1
        self.assertFalse(strict_words_equal([dict(self.W)],[wrong]))

    def test_reject_empty(self):
        self.assertFalse(strict_words_equal([],[]))

    def test_ocr_result_conversion(self):
        fake=type('Result',(),{'txts':['ABC'],'boxes':[[[1,2],[4,2],[4,6],[1,6]]]})()
        self.assertEqual(result_to_words(fake),[dict(self.W)])

    def test_detect_bad_polygon(self):
        fake=type('Result',(),{'txts':['ABC'],'boxes':[[1,2,3]]})()
        with self.assertRaises(RuntimeError):
            result_to_words(fake)


class RouteTests(unittest.TestCase):
    def test_classify_model(self):
        self.assertEqual(identify_model('ch_PP-OCRv4_det_infer.onnx'),'det')
        self.assertEqual(identify_model('ch_PP-OCRv4_rec_infer.onnx'),'rec')
        self.assertEqual(identify_model('ch_PP-OCRv4_cls_infer.onnx'),'cls')

    def test_unknown_model_fails(self):
        for name in ('model.onnx','det_rec.onnx',b'raw'):
            with self.assertRaises(RuntimeError):
                identify_model(name)

    def test_force_cpu(self):
        args,kwargs=force_cpu((['CUDAExecutionProvider'],None),{'provider_options':[{}]})
        self.assertEqual(args[0],['CPUExecutionProvider'])
        self.assertIsNone(args[1])
        self.assertNotIn('provider_options',kwargs)

    def test_gate_pass(self):
        self.assertTrue(provider_gate({'sessions':{'det':1,'cls':1,'rec':1},
            'node_events':{'det_cuda':10,'cls_cpu':20,'rec_cpu':30},'profiles_read':3}))

    def test_gate_reject_fallback(self):
        self.assertFalse(provider_gate({'sessions':{'det':1,'cls':1,'rec':1},
            'node_events':{'det_cuda':10,'cls_cpu':20,'rec_cpu':30,'rec_cuda':1},'profiles_read':3}))

    def test_gate_reject_missing_kernel(self):
        self.assertFalse(provider_gate({'sessions':{'det':1,'cls':1,'rec':1},
            'node_events':{'det_cpu':10,'cls_cpu':20,'rec_cpu':30},'profiles_read':3}))


class SummaryTests(unittest.TestCase):
    @staticmethod
    def pair(cpu,hybrid,pixels=True):
        return {'cpu':{'safe':True,'processing_s':cpu},
                'hybrid':{'safe':True,'processing_s':hybrid},'pixel_identical':pixels}

    def test_one_repeat_is_inconclusive(self):
        x=summarize({'pairs':[self.pair(180,110)]})
        self.assertEqual(x['status'],'INSUFFICIENT_REPEATS')
        self.assertGreater(x['improvement_pct'],30)

    def test_three_confirmed_pairs(self):
        x=summarize({'pairs':[self.pair(180,100),self.pair(180,102),self.pair(180,104)]})
        self.assertEqual(x['status'],'PROMISING_FULL_PDF_AB_ONLY')

    def test_no_gain(self):
        x=summarize({'pairs':[self.pair(180,179)]*3})
        self.assertEqual(x['status'],'NO_CONFIRMED_FULL_PDF_GAIN')

    def test_pixel_mismatch(self):
        x=summarize({'pairs':[self.pair(180,100,pixels=False)]})
        self.assertEqual(x['status'],'PDF_PIXEL_DIFFERENCE')

    def test_failed_security(self):
        x=self.pair(180,100);x['hybrid']['safe']=False
        self.assertEqual(summarize({'pairs':[x]})['status'],'SECURITY_OR_EXECUTION_FAILED')


class FailClosedIntegrationTests(unittest.TestCase):
    def test_adapter_mismatch_prevents_pdf_run(self):
        """If adapter differs, the full PDF run MUST NOT begin."""
        import types
        import sys
        import v667_child
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            pdf_runner=root/'PII_V5_Security_Acceptance'/'run_7page_pdf_test.py'
            pdf_runner.parent.mkdir()
            handler=types.ModuleType('handler_media')
            handler.__file__=str(root/'handler_media.py')
            handler.ocr_words=lambda img:[{'text':'CPU','left':1,'top':2,'width':3,'height':4,'line_id':0}]
            runner=types.ModuleType('run_7page_pdf_test')
            runner.run=mock.Mock(side_effect=AssertionError('PDF run must not start'))
            class FakeBridge:
                def __init__(self,*args):
                    self.ocr_words=lambda img:[{'text':'DIFFERENT','left':1,'top':2,'width':3,'height':4,'line_id':0}]
                def close(self): pass
            fake_image=Image.new('RGB',(4,4),'white')
            with mock.patch.dict(sys.modules,{'handler_media':handler,'run_7page_pdf_test':runner}):
                with mock.patch.object(v667_child,'HybridOCRBridge',FakeBridge):
                    with mock.patch.object(v667_child,'preview_images',return_value=[fake_image,fake_image]):
                        result=v667_child.run(root,root/'synthetic.pdf',pdf_runner,root/'expected.json',
                            root/'output','hybrid',root/'gpu_python',root/'worker.py')
            self.assertEqual(result['status'],'OCR_ADAPTER_MISMATCH')
            self.assertEqual(result['preflight_pairs'],1)
            runner.run.assert_not_called()

    def test_no_real_document_bypass(self):
        """Direct child invocation rejects any non-pinned PDF."""
        import v667_child
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with self.assertRaises(RuntimeError):
                v667_child.main(['--project-root',str(root),'--source',str(root/'real.pdf'),
                    '--runner',str(root/'runner.py'),'--expected',str(root/'gt.json'),
                    '--outdir',str(root/'output'),'--mode','cpu',
                    '--gpu-python',str(root/'gpu.exe'),'--worker',str(root/'worker.py')])


class SetupTests(unittest.TestCase):
    def test_real_fixture_is_approved(self):
        import hashlib
        p=Path(__file__).resolve().parent.parent/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
        if not p.exists():
            self.skipTest('Fixture is not co-located with test package')
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),EXPECTED_SHA)

    def test_missing_adapter_rejected(self):
        # If project adapter is absent, never try to guess its result schema.
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);tool=root/'PII_V667_Full_PDF_Hybrid_AB';tool.mkdir()
            src=root/'PII_Face_PDF_7page_Test'/'pii_face_7page_scan_source.pdf'
            with mock.patch('v667_benchmark.sys.path',[]):
                with self.assertRaises((ImportError,ModuleNotFoundError)):
                    require_setup(root,src,tool)

if __name__=='__main__':
    unittest.main(verbosity=2)
