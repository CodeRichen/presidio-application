"""Dependency-free tests for OCR conversion and source installation.
Usage: python -m unittest -v test_ocr_backend.py
"""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ocr_backend as backend
from install_ocr_backend import transform, MARKER_BEGIN


def rect(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def output(texts=None, boxes=None, scores=None, word_results=None):
    return SimpleNamespace(txts=texts, boxes=boxes, scores=scores, word_results=word_results)


class AdapterTests(unittest.TestCase):
    def test_default_windows(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(backend.selected_backend(), "windows")
            fake = lambda image: [{"text": "Windows"}]
            self.assertEqual(backend.ocr_words_with_backend(object(), fake)[0]["text"], "Windows")

    def test_rapid_selection(self):
        with patch.dict(os.environ, {"PII_OCR_BACKEND": "rapid"}):
            with patch.object(backend, "rapid_ocr_words", return_value=[{"text": "Rapid"}]):
                result = backend.ocr_words_with_backend(object(), lambda _: self.fail("Windows called"))
        self.assertEqual(result[0]["text"], "Rapid")

    def test_invalid_mode_rejected(self):
        with patch.dict(os.environ, {"PII_OCR_BACKEND": "online"}):
            with self.assertRaises(ValueError):
                backend.selected_backend()

    def test_word_box_mapping(self):
        r = output(["林家宇的電話"], [rect(10, 20, 130, 40)], [.99], [
            [("林",.95,rect(10,20,30,40)),("家",.95,rect(30,20,50,40)),
             ("宇",.95,rect(50,20,70,40)),("的",.95,rect(70,20,90,40)),
             ("電話",.95,rect(90,20,130,40))]
        ])
        w = backend.rapid_result_to_words(r, 180, 90)
        self.assertEqual([x["text"] for x in w], ["林", "家", "宇", "的", "電話"])
        self.assertTrue(all(x["line_id"] == 0 for x in w))
        self.assertEqual(w[2]["left"], 50)

    def test_line_bbox_fallback_if_missing_words(self):
        r = output(["0912-345-678"], [rect(5,5,95,25)], [.82], None)
        self.assertEqual(backend.rapid_result_to_words(r, 100,100)[0]["text"],"0912-345-678")

    def test_line_bbox_fallback_on_word_mismatch(self):
        r = output(["test@example.com"], [rect(10,10,140,30)], [.8], [[("test",.8,rect(10,10,50,30))]])
        words=backend.rapid_result_to_words(r,180,100)
        self.assertEqual(len(words),1)
        self.assertEqual(words[0]["width"],130)
        self.assertEqual(words[0]["text"],"test@example.com")

    def test_missing_or_invalid_word_bbox_falls_back(self):
        r=output(["王小明"],[rect(10,10,50,30)],[.99],[[('王',.99,None),('小明',.99,rect(20,10,50,30))]])
        words=backend.rapid_result_to_words(r,70,50)
        self.assertEqual(len(words),1)
        self.assertEqual(words[0]["text"],"王小明")

    def test_multiple_lines_ids(self):
        r=output(["林家宇", "王小明"],[rect(0,0,60,20),rect(0,25,60,45)],[1,1],None)
        words=backend.rapid_result_to_words(r,100,100)
        self.assertEqual([x["line_id"] for x in words],[0,1])

    def test_box_clipping(self):
        r=output(["Email"],[rect(-5,-2,120,33)],[.9],None)
        words=backend.rapid_result_to_words(r,100,50)
        self.assertEqual((words[0]["left"],words[0]["top"],words[0]["width"],words[0]["height"]),(0,0,100,33))

    def test_no_lines(self):
        self.assertEqual(backend.rapid_result_to_words(output(),100,100),[])
        self.assertEqual(backend.rapid_result_to_words(output([],[],[],[]),100,100),[])

    def test_mismatched_line_boxes_rejected(self):
        with self.assertRaises(RuntimeError):
            backend.rapid_result_to_words(output(["a","b"],[rect(0,0,50,20)]),100,100)

    def test_invalid_line_polygon_rejected(self):
        with self.assertRaises(ValueError):
            backend.rapid_result_to_words(output(["a"],[[[0,0]]]),100,100)

    def test_rapid_engine_call(self):
        r=output(["王小明"],[rect(10,10,70,40)],[.9],None)
        fake_engine = lambda img, return_word_box: r
        fake_image = SimpleNamespace(width=100,height=70,convert=lambda mode: SimpleNamespace(width=100,height=70))
        with patch.object(backend,"_rapid_engine",return_value=fake_engine):
            w=backend.rapid_ocr_words(fake_image)
        self.assertEqual(w[0]["text"],"王小明")


SOURCE='''from typing import List\nclass Image: pass\ndef ocr_words(pil_image: Image.Image) -> List[dict]:\n    return [{"text":"WINDOWS"}]\n\ndef _group_lines(words: List[dict]) -> List[List[dict]]:\n    return [words]\n'''

class InstallerTests(unittest.TestCase):
    def test_patch_compiles_and_preserves_body(self):
        result=transform(SOURCE)
        self.assertIn("def _windows_ocr_words(",result)
        self.assertIn("return [{\"text\":\"WINDOWS\"}]", result)
        self.assertIn(MARKER_BEGIN,result)
        self.assertLess(result.index("def ocr_words("),result.index("def _group_lines("))

    def test_repeat_patch_is_noop(self):
        patched=transform(SOURCE)
        self.assertEqual(transform(patched),patched)

    def test_unknown_layout_refused(self):
        with self.assertRaises(ValueError):
            transform("def different(): pass\n")


if __name__ == "__main__":
    unittest.main()
