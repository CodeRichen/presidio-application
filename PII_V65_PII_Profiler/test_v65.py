#!/usr/bin/env python3
from __future__ import annotations
from contextvars import ContextVar
import json
import sys
import types
import unittest
from unittest.mock import patch

from v65_profile_runtime import Metric, Profiler, _percentile
from v65_benchmark import summarize, child_env


class DummyMedia:
    def __init__(self):
        self._pii_media_cache_var = ContextVar('cache', default=None)
        textmod = types.SimpleNamespace(presidio_matches=lambda text: [('EMAIL', text)] if '@' in text else [])
        code = types.SimpleNamespace(handler_text=textmod, detect=lambda text: [('TW_PHONE', text)] if '09' in text else [])
        self.handler_code = code
        self.detect_matches = lambda text: (self.handler_code.detect(text) + self.handler_code.handler_text.presidio_matches(text))
        self._collect_pdf_ocr_boxes = lambda page, zoom: (self.detect_matches('synthetic@example.invalid'), zoom)


class Tests(unittest.TestCase):
    def setUp(self):
        self.media = DummyMedia()

    def test_latency_empty(self):
        self.assertEqual(Metric().safe_summary()['calls'], 0)
        self.assertEqual(_percentile([], 95), 0.0)

    def test_percentile(self):
        self.assertEqual(_percentile([1, 2, 3, 4], 50), 2.5)
        self.assertAlmostEqual(_percentile([1, 2, 3], 100), 3)

    def test_profiler_captures_calls(self):
        p = Profiler(self.media).install()
        try:
            self.media._collect_pdf_ocr_boxes(None, 2.0)
            self.media._collect_pdf_ocr_boxes(None, 3.0)
            d = p.safe_summary()
            self.assertEqual(d['functions']['media_detect_matches']['calls'], 2)
            self.assertEqual(d['functions']['code_detect']['calls'], 2)
            self.assertEqual(d['functions']['presidio_matches']['calls'], 2)
            self.assertEqual(d['zoom_calls'], {'zoom2': 1, 'zoom3': 1})
        finally:
            p.restore()

    def test_cache_hit_count(self):
        p = Profiler(self.media).install()
        token = self.media._pii_media_cache_var.set({'synthetic@example.invalid': ()})
        try:
            self.media.detect_matches('synthetic@example.invalid')
            self.media.detect_matches('noname@invalid.test')
            self.assertEqual(p.safe_summary()['media_cache_preexisting_hits'], 1)
            self.assertEqual(p.safe_summary()['media_cache_preexisting_misses'], 1)
        finally:
            self.media._pii_media_cache_var.reset(token)
            p.restore()

    def test_no_pii_or_hashes_in_report(self):
        sensitive = 'Private Person 0922333555 confidential@example.invalid'
        p = Profiler(self.media).install()
        try:
            self.media.detect_matches(sensitive)
            self.media.detect_matches(sensitive)
            dump = json.dumps(p.safe_summary(), ensure_ascii=False)
            self.assertNotIn(sensitive, dump)
            self.assertNotIn('0922333555', dump)
            self.assertNotIn('confidential@example.invalid', dump)
            self.assertEqual(p.safe_summary()['functions']['media_detect_matches']['repeated_input_calls'], 1)
        finally:
            p.restore()
            p.discard_sensitive()
            self.assertEqual(len(p.metric['media_detect_matches']._seen_inputs), 0)

    def test_original_methods_restored(self):
        old_code = self.media.handler_code.detect
        old_match = self.media.detect_matches
        old_collect = self.media._collect_pdf_ocr_boxes
        p = Profiler(self.media).install()
        p.restore()
        self.assertIs(self.media.handler_code.detect, old_code)
        self.assertIs(self.media.detect_matches, old_match)
        self.assertIs(self.media._collect_pdf_ocr_boxes, old_collect)

    def test_double_install_rejected(self):
        p = Profiler(self.media).install()
        try:
            with self.assertRaises(RuntimeError):
                p.install()
        finally:
            p.restore()

    def test_measure_during_exception(self):
        self.media.handler_code.detect = lambda text: (_ for _ in ()).throw(ValueError('test'))
        p = Profiler(self.media).install()
        try:
            with self.assertRaises(ValueError):
                self.media.detect_matches('dummy')
            self.assertEqual(p.safe_summary()['functions']['code_detect']['calls'], 1)
        finally:
            p.restore()

    def test_summary_profile_only(self):
        r = summarize([{'mode': 'on', 'safe': True, 'elapsed_s': 211.12}])
        self.assertEqual(r['status'], 'PROFILE_ONLY')
        self.assertEqual(r['on_median_s'], 211.12)

    def test_summary_cache_ab(self):
        r = summarize([{'mode': 'on', 'safe': True, 'elapsed_s': 200},
                       {'mode': 'off', 'safe': True, 'elapsed_s': 250}],
                      [{'all_identical': True}])
        self.assertEqual(r['status'], 'CACHE_AB_SYNTHETIC_ONLY')
        self.assertEqual(r['existing_cache_benefit_pct'], 20.0)

    def test_reject_pixel_mismatch(self):
        r = summarize([{'mode': 'on', 'safe': True, 'elapsed_s': 200},
                       {'mode': 'off', 'safe': True, 'elapsed_s': 250}],
                      [{'all_identical': False}])
        self.assertEqual(r['status'], 'PIXEL_DIFFERENCE_OR_INCOMPLETE')

    def test_reject_failed_safety(self):
        self.assertEqual(summarize([{'mode': 'on', 'safe': False, 'elapsed_s': 200}])['status'], 'SECURITY_GATE_FAILED')

    def test_synthetic_profile_env(self):
        env = child_env()
        self.assertEqual(env['PII_MEDIA_FP_PROFILE'], 'synthetic_face_test')
        self.assertEqual(env['PII_OCR_BACKEND'], 'rapid')
        self.assertEqual(env['PII_PDF_EXPORT_V5'], '1')

    def test_no_sensitive_in_debug_breakdown(self):
        p = Profiler(self.media).install()
        try:
            self.media._collect_pdf_ocr_boxes(None, 3.0)
            d = p.safe_summary()
            self.assertTrue(all(isinstance(v, (int, float, dict, str)) for v in d['functions']['code_detect'].values()))
            self.assertNotIn('synthetic@example.invalid', json.dumps(d))
        finally:
            p.restore()


if __name__ == '__main__':
    unittest.main(verbosity=2)
