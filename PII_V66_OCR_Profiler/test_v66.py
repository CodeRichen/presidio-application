#!/usr/bin/env python3
# coding: utf-8
"""Offline unit tests for privacy-safe timing and fail-closed result logic."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import v66_runtime
import v66_benchmark as bench


class StageTests(unittest.TestCase):
    def setUp(self):
        self.marker='TEST_ONLY_SECRET_DO_NOT_PERSIST'
        self.record=[]
        class FakePage:
            def __init__(self,n):
                self.number=n
            def get_pixmap(self):
                return 'pixels'
        class FakeImage:
            @staticmethod
            def frombytes(*args,**kwargs):
                return 'image'
        self.pagecls=FakePage
        self.img=FakeImage
        code=SimpleNamespace(detect=lambda text: [('x',)],
                             handler_text=SimpleNamespace(presidio_matches=lambda text: [('y',)]))
        hm=SimpleNamespace(handler_code=code)
        def preprocess(img):
            return img,1.0
        def words(img):
            return [self.marker]
        def pii(items,scale=1.0):
            code.detect(items[0]);code.handler_text.presidio_matches(items[0]);return [('redacted',)]
        def collect(page,zoom):
            page.get_pixmap()
            x=FakeImage.frombytes('RGB',(10,10),b'123')
            y, scale=hm.preprocess_for_ocr(x)
            tokens=hm.ocr_words(y)
            return hm.find_sensitive_boxes_in_words(tokens,scale=scale),x
        def scan(page):
            left=hm._collect_pdf_ocr_boxes(page,2)
            right=hm._collect_pdf_ocr_boxes(page,3)
            hm._merge_pdf_multizoom_boxes(left,right)
            return hm.save_secure_raster_pdf('doc','output')
        hm.preprocess_for_ocr=preprocess
        hm.ocr_words=words
        hm.find_sensitive_boxes_in_words=pii
        hm._collect_pdf_ocr_boxes=collect
        hm._mask_pdf_scanned_page=scan
        hm._merge_pdf_multizoom_boxes=lambda a,b: a
        hm.save_secure_raster_pdf=lambda doc,out:'DONE'
        self.hm=hm
        self.fitz=SimpleNamespace(Page=FakePage)

    def test_collect_records_all_phases_two_zoom(self):
        inst=v66_runtime.OCRTiming().install(self.hm,self.fitz,self.img)
        try:
            self.assertEqual(self.hm._mask_pdf_scanned_page(self.pagecls(0)),'DONE')
        finally:
            inst.restore()
        report=inst.summarize()
        self.assertEqual(len(report['collections']),2)
        self.assertEqual(report['by_zoom']['2x']['pages'],1)
        self.assertEqual(report['by_zoom']['3x']['pages'],1)
        self.assertEqual(report['secure_export_calls'],1)
        self.assertEqual(report['merge_calls'],1)
        self.assertEqual(report['code_detect_calls'],2)
        self.assertEqual(report['presidio_matches_calls'],2)
        for zoom in ('2x','3x'):
            for key in v66_runtime.PHASES:
                self.assertEqual(report['by_zoom'][zoom]['phase_calls'][key],1)
        self.assertNotIn(self.marker,json.dumps(report))

    def test_restore_after_success(self):
        original=self.hm.ocr_words
        inst=v66_runtime.OCRTiming().install(self.hm,self.fitz,self.img)
        self.assertIsNot(self.hm.ocr_words,original)
        inst.restore()
        self.assertIs(self.hm.ocr_words,original)
        inst.restore()

    def test_install_missing_hook_fail_closed(self):
        del self.hm.ocr_words
        with self.assertRaisesRegex(RuntimeError,'MISSING_TIMING_HOOKS'):
            v66_runtime.OCRTiming().install(self.hm,self.fitz,self.img)

    def test_no_sensitive_detail_in_summary(self):
        inst=v66_runtime.OCRTiming().install(self.hm,self.fitz,self.img)
        try:
            self.hm._mask_pdf_scanned_page(self.pagecls(0))
        finally:
            inst.restore()
        s=json.dumps(inst.summarize())
        self.assertNotIn(self.marker,s)
        self.assertNotIn('redacted',s)

    def test_no_metrics_outside_collect(self):
        inst=v66_runtime.OCRTiming().install(self.hm,self.fitz,self.img)
        try:
            self.pagecls(1).get_pixmap()
            self.hm.ocr_words(self.marker)
        finally:
            inst.restore()
        self.assertEqual(inst.collections,[])


class GateTests(unittest.TestCase):
    def fake_profile(self):
        rows=[]
        for page in range(1,8):
            for zoom in (2.,3.):
                rows.append({'page':page,'zoom':zoom,'collect_total_s':1.0,
                            **{p+'_s':0.1 for p in v66_runtime.PHASES}})
        profile={'schema':'pii_v66_ocr_profile_1','collections':rows,
                 'scanned_pages':7,'secure_export_calls':1,
                 'code_detect_calls':278,'secure_export_wall_s':2.0,
                 'by_zoom':{zoom:{'pages':7,'collect_total':7.0,
                                  'phase_calls':{p:7 for p in v66_runtime.PHASES},
                                  **{p:0.7 for p in v66_runtime.PHASES},
                                  'other_collect':3.5}
                            for zoom in ('2x','3x')}}
        return profile

    def test_good_profile_passes(self):
        self.assertTrue(bench.check_profile(self.fake_profile(),30))

    def test_missing_zoom_fails(self):
        p=self.fake_profile();p['collections'].pop()
        self.assertFalse(bench.check_profile(p,30))

    def test_missing_stage_fails(self):
        p=self.fake_profile();p['by_zoom']['2x']['phase_calls']['ocr_backend']=0
        self.assertFalse(bench.check_profile(p,30))

    def test_missing_secure_export_fails(self):
        p=self.fake_profile();p['secure_export_calls']=0
        self.assertFalse(bench.check_profile(p,30))

    def test_invalid_page_fails(self):
        p=self.fake_profile();p['collections'][0]['page']=10
        self.assertFalse(bench.check_profile(p,30))

    def test_invalid_negative_time_fails(self):
        p=self.fake_profile();p['collections'][0]['ocr_backend_s']=-1
        self.assertFalse(bench.check_profile(p,30))

    def test_summary_has_valid_main_phase(self):
        p=self.fake_profile()
        p['fixture_processing_s']=30
        result=bench.summary_for([{'processing_s':30,'profile':p}])
        self.assertEqual(result['processing_median_s'],30)
        self.assertEqual(result['collect_median_s'],14)
        self.assertIn(result['dominant_collect_phase'],bench.PHASES)

    def test_child_environment_fixed_and_safe(self):
        env=bench.child_env()
        self.assertEqual(env['PII_MEDIA_FP_PROFILE'],'synthetic_face_test')
        self.assertEqual(env['PII_OCR_BACKEND'],'rapid')
        self.assertEqual(env['PII_PDF_EXPORT_V5'],'1')
        self.assertEqual(env['PII_MEDIA_CACHE'],'1')

    def test_write_report_privacy(self):
        r={'status':'PROFILE_OK_SINGLE','runs':[],'summary':{},
           'schema':'pii_v66_ocr_profile_ab_1'}
        with tempfile.TemporaryDirectory() as path:
            bench.write_report(r,Path(path))
            raw=Path(path,'v66_ocr_profile_summary.json').read_text(encoding='utf-8')
            self.assertNotIn('private_map',raw)
            self.assertEqual(json.loads(raw)['status'],'PROFILE_OK_SINGLE')
            self.assertTrue(Path(path,'v66_ocr_profile_summary.md').is_file())

    def test_pixel_check_requires_7(self):
        class FakeGate:
            @staticmethod
            def compare_page_pixels(a,b,dpi=150):
                return {'pages_compared':7,'identical_pages':6,'all_identical':False}
        self.assertFalse(bench.strict_pixels(FakeGate(),'a','b')['all_identical'])

    def test_fail_closed_reject_total_exceeding_processing(self):
        self.assertFalse(bench.check_profile(self.fake_profile(),5))


if __name__=='__main__':
    unittest.main(verbosity=2)
