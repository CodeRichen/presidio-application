"""Regression for observed actual false positives. Run from project root."""
import os
import unittest
from unittest.mock import patch
from media_fp_guard import filter_synthetic_test_false_positives


class FixtureFPTest(unittest.TestCase):
    def setUp(self):
        self.noise = [
            (1530, 95, 1735, 143, "ORGANIZATION", "PII + FACE", 0.85, "en"),
            (1640, 480, 1688, 528, "DATE_TIME", "0 1", 0.85, "en"),
            (1490, 850, 1620, 903, "PERSON", "Email", 0.85, "en"),
        ]
        self.valid = [
            (1630, 590, 1790, 650, "PERSON", "林 家 宇", .94, "zh"),
            (1630, 720, 1950, 770, "TW_PHONE", "0912-345-678", None, "regex"),
            (1670, 850, 2180, 910, "EMAIL", "demo@example.com", None, "regex"),
            (600, 250, 1120, 1320, "FACE", "[人臉]", .905, "face_yunet"),
        ]

    def test_disabled_by_default(self):
        with patch.dict(os.environ, {"PII_MEDIA_FP_PROFILE": ""}):
            kept, suppressed=filter_synthetic_test_false_positives(self.noise+self.valid, [])
        self.assertEqual(kept,self.noise+self.valid)
        self.assertEqual(suppressed,[])

    def test_observed_actual_logs(self):
        with patch.dict(os.environ, {"PII_MEDIA_FP_PROFILE": "synthetic_face_test"}):
            kept, suppressed=filter_synthetic_test_false_positives(self.noise+self.valid, [])
        self.assertEqual(suppressed,self.noise)
        self.assertEqual(kept,self.valid)

    def test_different_source_kept(self):
        changed=[(*self.noise[0][:7],"regex"),(*self.noise[1][:7],"zh"),(*self.noise[2][:7],"zh")]
        with patch.dict(os.environ, {"PII_MEDIA_FP_PROFILE": "synthetic_face_test"}):
            kept,suppressed=filter_synthetic_test_false_positives(changed, [])
        self.assertEqual(kept,changed)
        self.assertEqual(suppressed,[])

    def test_same_label_but_different_text_kept(self):
        changed=[(*self.noise[0][:5], "保險公司", .85,"en"),
                 (*self.noise[1][:5], "2026-11-18", .85,"en"),
                 (*self.noise[2][:5], "Emily", .85,"en")]
        with patch.dict(os.environ, {"PII_MEDIA_FP_PROFILE": "synthetic_face_test"}):
            kept,suppressed=filter_synthetic_test_false_positives(changed, [])
        self.assertEqual(kept,changed)
        self.assertEqual(suppressed,[])

    def test_protected_entities_never_filtered(self):
        changed=[(*self.noise[0][:4],"EMAIL","demo@example.com",None,"regex"),
                 (*self.noise[1][:4],"TW_PHONE","0912-345-678",None,"regex")]
        with patch.dict(os.environ, {"PII_MEDIA_FP_PROFILE": "synthetic_face_test"}):
            kept,suppressed=filter_synthetic_test_false_positives(changed, [])
        self.assertEqual(kept,changed)
        self.assertEqual(suppressed,[])


if __name__ == "__main__":
    unittest.main()
