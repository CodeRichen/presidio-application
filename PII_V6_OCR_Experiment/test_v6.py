#!/usr/bin/env python3
# coding:utf-8
import sys, unittest
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parent))
from v6_child import install_observer
from v6_experiment import FIXTURE_SHA256, sha256

class FakeHandler:
    def _mask_pdf_scanned_page(self, page, entries, counters, zoom=2.0):
        primary_zoom = zoom
        fallback_zoom = 3.0
        primary_boxes, primary_img = self._collect_pdf_ocr_boxes(page, primary_zoom)
        fallback_boxes, fallback_img = self._collect_pdf_ocr_boxes(page, fallback_zoom)
        boxes = self._merge_pdf_multizoom_boxes(primary_boxes, fallback_boxes)
        return boxes
    def _collect_pdf_ocr_boxes(self, page, zoom):
        return ([(0,0,1,1,'PERSON','private',None,'test')] if zoom == 2.0
                else [(2,2,3,3,'EMAIL','private',None,'test')]),object()
    def _merge_pdf_multizoom_boxes(self, primary, fallback):
        return primary + fallback

class Tests(unittest.TestCase):
    def test_baseline_collects_both(self):
        h=FakeHandler(); s=install_observer(h,'dual_zoom'); boxes=h._mask_pdf_scanned_page(None,[],{})
        self.assertEqual(len(boxes),2)
        self.assertEqual(s['fallback_calls'],1)
        self.assertEqual(s['extra_boxes_per_page'],[1])
    def test_candidate_skips_fallback(self):
        h=FakeHandler(); s=install_observer(h,'zoom2_only'); boxes=h._mask_pdf_scanned_page(None,[],{})
        self.assertEqual(len(boxes),1)
        self.assertEqual(s['fallback_skipped'],1)
        self.assertEqual(s['extra_boxes_per_page'],[0])
    def test_nonfallback_still_called(self):
        h=FakeHandler();s=install_observer(h,'zoom2_only');h._collect_pdf_ocr_boxes(None,2.0)
        self.assertEqual(s['primary_calls'],1)
    def test_no_private_data_in_report(self):
        h=FakeHandler();s=install_observer(h,'dual_zoom');h._mask_pdf_scanned_page(None,[],{})
        self.assertNotIn('private',str(s))
    def test_wrong_mode_rejects(self):
        with self.assertRaises(RuntimeError):
            h=FakeHandler(); h._mask_pdf_scanned_page=lambda *a: None
            install_observer(h,'zoom2_only')
    def test_fixture_hash_is_full_sha256(self):
        self.assertEqual(len(FIXTURE_SHA256),64)

if __name__ == '__main__':unittest.main(verbosity=2)
