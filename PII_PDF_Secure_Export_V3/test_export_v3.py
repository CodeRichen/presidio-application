"""V3 patch self-tests on *synthetic* PDF data only.
Runs without any Presidio, OCR, GUI or Windows dependencies.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch

import pymupdf as fitz
import apply_export_v3 as ap
from summarize_export_v3 import summarize

REF_SOURCE = Path(__file__).with_name('_reference_pdf_secure_output.py')


class TestSecurePDFProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.p = Path(self.temp.name)
        self.src = self.p / 'pdf_secure_output.py'
        self.src.write_text(REF_SOURCE.read_text(encoding='utf-8'), encoding='utf-8')
        shutil.copy2(Path(__file__).with_name('pdf_export_probe_v3.py'), self.p / 'pdf_export_probe_v3.py')

    def test_01_patch_parses(self):
        patched = ap.patch(self.src.read_text(encoding='utf-8'))
        self.assertIn('page.encode_png', patched)
        self.assertIn('pdf.save', patched)

    def test_02_reject_unknown_layout(self):
        with self.assertRaises(ValueError):
            ap.patch('def save_secure_raster_pdf():\n    pass\n')

    def test_03_check_does_not_change(self):
        before = self.src.read_bytes()
        self.assertEqual(ap.main(['--file', str(self.src), '--check']), 0)
        self.assertEqual(self.src.read_bytes(), before)

    def test_04_patch_restore(self):
        before = self.src.read_bytes()
        self.assertEqual(ap.main(['--file', str(self.src), '--apply']), 0)
        self.assertNotEqual(self.src.read_bytes(), before)
        self.assertEqual(ap.main(['--file', str(self.src), '--restore']), 0)
        self.assertEqual(self.src.read_bytes(), before)

    def _load_module(self, name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        self.addCleanup(lambda: sys.modules.pop(name, None))
        return mod

    def test_05_same_pixels_and_safe_structure(self):
        # Generate synthetic source containing text, markup and an attachment.
        with fitz.open() as doc:
            for i in range(2):
                page = doc.new_page(width=400, height=270)
                page.insert_text((25,40), f'SYNTHETIC PRIVATE {i}', fontsize=18)
                page.draw_rect(fitz.Rect(50,90,350,200), color=(0,0,1), fill=(0,1,0))
            doc.embfile_add('test_only.txt', b'SYNTHETIC private attachment')
            source_path = str(self.p / 'synthetic_source.pdf')
            doc.save(source_path)

        ap.main(['--file', str(self.src), '--apply'])
        orig = self.p / 'baseline_source.py'
        orig.write_text(REF_SOURCE.read_text(encoding='utf-8'), encoding='utf-8')
        sys.path.insert(0, str(self.p))
        self.addCleanup(lambda: sys.path.remove(str(self.p)))
        baseline = self._load_module('baseline_v3_test', orig)
        modified = self._load_module('patched_v3_test', self.src)
        results = self.p / 'results'
        with mock_patch.dict(os.environ, {'PII_PDF_EXPORT_V3':'1','PII_PDF_EXPORT_V3_DIR':str(results)}):
            with fitz.open(source_path) as masked:
                for page in masked:
                    page.add_redact_annot(fitz.Rect(0,10,399,60), fill=(0,0,0))
                    page.apply_redactions()
                original_out = str(self.p / 'baseline_output.pdf')
                probe_out = str(self.p / 'v3_output.pdf')
                baseline.save_secure_raster_pdf(masked, original_out, dpi=100)
                modified.save_secure_raster_pdf(masked, probe_out, dpi=100)
        with fitz.open(original_out) as a, fitz.open(probe_out) as b:
            self.assertEqual(len(a), len(b))
            for apage, bpage in zip(a,b):
                self.assertEqual(apage.get_pixmap().samples, bpage.get_pixmap().samples)
                self.assertEqual(bpage.get_text('text'), '')
                self.assertEqual(len(bpage.get_images(full=True)),1)
                self.assertEqual(bpage.get_links(),[])
                self.assertIsNone(next(iter(bpage.annots() or ()),None))
            self.assertEqual(b.embfile_count(), 0)
        with fitz.open(source_path) as source:
            self.assertEqual(source.embfile_count(), 1)
            self.assertIn('SYNTHETIC PRIVATE',source[0].get_text())
        artifacts=list(results.glob('pdf_v3_*.json'))
        self.assertEqual(len(artifacts),1)
        data=json.loads(artifacts[0].read_text(encoding='utf-8'))
        self.assertEqual(data['status'],'OK')
        self.assertEqual(data['page_count'],2)
        for name in ('page.render','page.encode_png','page.new_page','page.insert_image',
                     'pdf.save','pdf.verify','pdf.atomic_replace'):
            self.assertIn(name,[v['stage'] for v in data['steps']])
        self.assertNotIn('synthetic_source',json.dumps(data))
        summary=summarize(results)
        self.assertEqual(summary['runs'],1)

    def test_06_disabled_does_not_write(self):
        # No private outputs should appear when instrument is disabled by default.
        from pdf_export_probe_v3 import ExportProbe
        with mock_patch.dict(os.environ, {'PII_PDF_EXPORT_V3':'0','PII_PDF_EXPORT_V3_DIR':str(self.p/'logs')}):
            probe = ExportProbe(1,200)
            probe.end('pdf.save',probe.begin())
            probe.finish(success=True)
        self.assertFalse((self.p/'logs').exists())


if __name__=='__main__':
    unittest.main(verbosity=2)
