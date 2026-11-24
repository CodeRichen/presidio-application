"""Run in project root: python -m unittest -v test_handler_media_secure_integration.py

This integration smoke test stubs only the OCR/NER processing to avoid loading
models. It DOES execute the real handler_media.mask_pdf() and PDF safe export.
It is NOT a test of the OCR or PII detector's accuracy.
"""
import base64
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

try:
    import pymupdf as fitz
except ImportError:
    import fitz
from PIL import Image, ImageDraw


def load_handler_without_nlp_models():
    module_path = Path(__file__).with_name('handler_media.py')
    old = sys.modules.get('handler_code')
    fake = types.ModuleType('handler_code')
    fake.RULES = []
    fake.handler_text = types.SimpleNamespace(
        RULES=[], _pii_zh_sp_scope=lambda fn: fn,
    )
    sys.modules['handler_code'] = fake
    try:
        spec = importlib.util.spec_from_file_location('_test_handler_media', module_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        if old is not None:
            sys.modules['handler_code'] = old
        else:
            sys.modules.pop('handler_code', None)
    return mod


def make_sample_pdf(path, *, rotation=0):
    with fitz.open() as doc:
        for i in range(3):
            img = Image.new('RGB', (800, 1100), 'white')
            draw = ImageDraw.Draw(img)
            draw.text((105, 144), f'SYNTHETIC-PRIVATE-123-{i}', fill=(0, 0, 0))
            mem = io.BytesIO()
            img.save(mem, format='PNG')
            page = doc.new_page(width=400, height=550)
            page.insert_image(page.rect, stream=mem.getvalue())
            page.add_text_annot((100, 200), 'secret annotation')
            if rotation:
                page.set_rotation(rotation)
        doc.embfile_add('original_private_attachment.txt', b'SYNTHETIC-SECRET-BACKUP')
        doc.set_metadata({'title': 'SYNTHETIC-SECRET-METADATA'})
        doc.save(str(path))


def fake_pdf_scanned_mask(page, entries, counters):
    rect = fitz.Rect(48, 65, 290, 125)
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, annots=False)
    counters['TEST_PII'] = counters.get('TEST_PII', 0) + 1
    entries.append({
        'tag': f"TEST_PII_{counters['TEST_PII']}",
        'label': 'TEST_PII',
        'page': page.number,
        'bbox': [rect.x0, rect.y0, rect.x1, rect.y1],
        'value': 'SYNTHETIC-PRIVATE',
        'crop_b64': base64.b64encode(pix.tobytes('png')).decode('ascii'),
    })
    page.add_redact_annot(rect, fill=(0, 0, 0))
    page.apply_redactions()


class HandlerPdfIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.handler = load_handler_without_nlp_models()

    def test_mask_pdf_rebuilds_without_inherited_objects(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'source.pdf'
            output = Path(d) / 'masked.pdf'
            mapping = Path(d) / 'mapping.json'
            make_sample_pdf(source)
            with patch.object(self.handler, '_mask_pdf_scanned_page', side_effect=fake_pdf_scanned_mask):
                entries = self.handler.mask_pdf(str(source), str(output), str(mapping))
            self.assertEqual(len(entries), 3)
            with fitz.open(str(source)) as src, fitz.open(str(output)) as dst:
                self.assertEqual(src.embfile_count(), 1)
                self.assertEqual(dst.embfile_count(), 0)
                self.assertEqual(len(dst), 3)
                self.assertEqual(dst.metadata['title'], '')
                for page in dst:
                    self.assertEqual(page.get_text().strip(), '')
                    self.assertIsNone(next(iter(page.annots() or ()), None))
                    self.assertEqual(len(page.get_images(full=True)), 1)
                    # Visual black over the synthetic PII box.
                    pix = page.get_pixmap(matrix=fitz.Matrix(1, 1), annots=False)
                    x, y = 140, 80
                    rgb = tuple(pix.samples[(y * pix.width + x) * pix.n:(y * pix.width + x) * pix.n + 3])
                    self.assertTrue(all(c < 5 for c in rgb), rgb)
            raw = output.read_bytes()
            self.assertNotIn(b'SYNTHETIC-SECRET-BACKUP', raw)
            self.assertNotIn(b'SYNTHETIC-SECRET-METADATA', raw)
            self.assertTrue(mapping.exists())

    def test_rotated_pdf_fails_closed_until_coordinate_mapping_supported(self):
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'source.pdf'
            output = Path(d) / 'masked.pdf'
            make_sample_pdf(source, rotation=90)
            with self.assertRaisesRegex(ValueError, '旋轉 PDF'):
                self.handler.mask_pdf(str(source), str(output), str(Path(d)/'map.json'))
            self.assertFalse(output.exists())


if __name__ == '__main__':
    unittest.main()
