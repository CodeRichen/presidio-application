"""Offline tests. No actual face model, Presidio, RapidOCR or Internet needed.

Use a stub face detector to verify image/PDF integration, secure PDF output,
and failure behavior. Separately test real YuNet with real photographs locally.
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from PIL import Image

try:
    import pymupdf as fitz
except ImportError:
    import fitz

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def load_handler():
    # Avoid initializing the user's heavyweight PII modules for unit tests.
    code_stub = types.ModuleType("handler_code")
    code_stub.RULES = []
    code_stub.handler_text = types.SimpleNamespace(_pii_zh_sp_scope=lambda f: f, RULES=[])
    spec = importlib.util.spec_from_file_location("handler_media_face_unit_test", ROOT / "handler_media.py")
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"handler_code": code_stub}):
        spec.loader.exec_module(mod)
    return mod


hm = load_handler()


class FaceIntegrationTests(unittest.TestCase):
    def test_padded_box_is_clipped(self):
        from face_detector import padded_face_box
        self.assertEqual(padded_face_box((0, 0, 50, 50, 0.9), (100, 100), padding=0.2), (0, 0, 60, 60))

    def test_image_face_and_text_blackout_and_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            source, output, map_path = t / "a.png", t / "a_masked.png", t / "map.json"
            Image.new("RGB", (200, 180), (200, 200, 200)).save(source)
            with patch.object(hm, "detect_faces", return_value=[(30, 40, 80, 100, 0.96)]), \
                    patch.object(hm, "ocr_words", return_value=[]), \
                    patch.object(hm, "find_sensitive_boxes_in_words", return_value=[(120, 20, 145, 30, "EMAIL", "test@example.com", None, "test")]):
                entries = hm.mask_image(str(source), str(output), str(map_path))
            self.assertEqual({e["label"] for e in entries}, {"FACE", "EMAIL"})
            with Image.open(output) as masked:
                self.assertEqual(masked.getpixel((50, 60)), (0, 0, 0))
                self.assertEqual(masked.getpixel((130, 25)), (0, 0, 0))
                self.assertEqual(masked.getpixel((190, 170)), (200, 200, 200))
            self.assertEqual(len(json.loads(map_path.read_text(encoding="utf-8"))[output.name]), 2)
            restored = t / "restored.png"
            hm.restore_image(str(output), str(restored), str(map_path))
            with Image.open(restored) as restored_image:
                self.assertEqual(restored_image.getpixel((50, 60)), (200, 200, 200))

    def test_missing_face_model_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            source, output = t / "a.png", t / "a_masked.png"
            Image.new("RGB", (64, 64), "white").save(source)
            with patch.object(hm, "detect_faces", side_effect=FileNotFoundError("YuNet missing")):
                with self.assertRaises(FileNotFoundError):
                    hm.mask_image(str(source), str(output), str(t / "map.json"))
            self.assertFalse(output.exists())
            self.assertFalse((t / "map.json").exists())

    def test_image_jpeg_supported_and_no_source_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            source, output = t / "photo.jpg", t / "photo_masked.jpg"
            Image.new("RGB", (200, 180), "white").save(source)
            before = source.read_bytes()
            with patch.object(hm, "detect_faces", return_value=[(30, 40, 80, 100, 0.93)]), \
                    patch.object(hm, "ocr_words", return_value=[]), \
                    patch.object(hm, "find_sensitive_boxes_in_words", return_value=[]):
                entries = hm.mask_image(str(source), str(output), str(t / "map.json"))
                with self.assertRaises(ValueError):
                    hm.mask_image(str(source), str(source), str(t / "map.json"))
            self.assertEqual(len(entries), 1)
            self.assertEqual(before, source.read_bytes())
            with Image.open(output) as im:
                self.assertLess(max(im.getpixel((50, 60))), 10)

    def test_offline_yunet_box_conversion_with_stub_network(self):
        import numpy as np
        import face_detector
        class Detector:
            def setInputSize(self, size):
                self.size = size
            def detect(self, arr):
                self.arr_shape = arr.shape
                return 1, np.asarray([[32, 48, 80, 80] + [0] * 10 + [0.92]], dtype='float32')
        fake = Detector()
        with patch.object(face_detector, "_get_detector", return_value=fake):
            faces = face_detector.detect_faces(Image.new("RGB", (200, 100), "white"), threshold=0.6)
        self.assertEqual(fake.size, (320, 160))
        self.assertEqual(fake.arr_shape, (160, 320, 3))
        self.assertEqual(len(faces), 1)
        self.assertAlmostEqual(faces[0][0], 20.0)
        self.assertAlmostEqual(faces[0][1], 30.0)
        self.assertAlmostEqual(faces[0][2], 70.0)
        self.assertAlmostEqual(faces[0][3], 80.0)

    def test_pdf_scanned_face_secure_output_and_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            source, output, map_path = t / "in.pdf", t / "out.pdf", t / "map.json"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=180)
                page.draw_rect(fitz.Rect(0, 0, 200, 180), color=None, fill=(0.8, 0.8, 0.8))
                doc.embfile_add("private.txt", b"PRIVATE ORIGINAL DATA")
                doc.save(source)
            def fake_collect(page, zoom):
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False, annots=False)
                return [], Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            with patch.object(hm, "detect_faces", return_value=[(40, 45, 95, 100, 0.99)]), \
                    patch.object(hm, "_collect_pdf_ocr_boxes", side_effect=fake_collect):
                result = hm.mask_pdf(str(source), str(output), str(map_path))
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["label"], "FACE")
            with fitz.open(output) as doc:
                self.assertEqual(doc.embfile_count(), 0)
                self.assertEqual(doc[0].get_text("text"), "")
                pix = doc[0].get_pixmap(matrix=fitz.Matrix(1, 1), colorspace=fitz.csRGB, alpha=False)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                self.assertEqual(im.getpixel((30, 35)), (0, 0, 0))
                self.assertNotEqual(im.getpixel((150, 150)), (0, 0, 0))
            self.assertEqual(len(json.loads(map_path.read_text(encoding="utf-8"))[output.name]), 1)
            # Restoration is deliberately privileged: only private map users can undo masks.
            restored = t / "restored.pdf"
            hm.restore_pdf(str(output), str(restored), str(map_path))
            with fitz.open(restored) as doc:
                im = Image.frombytes("RGB", (200, 180), doc[0].get_pixmap(colorspace=fitz.csRGB, alpha=False).samples)
                self.assertNotEqual(im.getpixel((30, 35)), (0, 0, 0))

    def test_pdf_text_page_still_masks_faces(self):
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            source, output, map_path = t / "text.pdf", t / "out.pdf", t / "map.json"
            with fitz.open() as doc:
                page = doc.new_page(width=200, height=180)
                page.draw_rect(fitz.Rect(0, 0, 200, 180), color=None, fill=(0.8,0.8,0.8))
                page.insert_text((10, 20), "one two three four five six")
                doc.save(source)
            with patch.object(hm, "detect_faces", return_value=[(40,45,95,100,0.99)]), \
                    patch.object(hm, "detect_matches", return_value=[]):
                entries = hm.mask_pdf(str(source), str(output), str(map_path))
            self.assertEqual(len(entries), 1)
            with fitz.open(output) as doc:
                self.assertEqual(doc.embfile_count(), 0)
                self.assertEqual(doc[0].get_text("text"), "")
                pix = doc[0].get_pixmap(colorspace=fitz.csRGB, alpha=False)
                im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                self.assertEqual(im.getpixel((30, 35)), (0,0,0))


if __name__ == "__main__":
    unittest.main()
