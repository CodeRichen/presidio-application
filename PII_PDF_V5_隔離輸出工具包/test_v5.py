"""V5 safety/regression smoke tests. Run inside presidio_app_allen with its .venv.

Does not use real private data. Writes synthetic documents to TemporaryDirectory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

try:
    import pymupdf as fitz
except ImportError:
    import fitz

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_modules():
    import pdf_secure_output as old
    import pdf_secure_v5 as v5
    return old, v5


def _make_fixture(dest: Path):
    with fitz.open() as doc:
        p = doc.new_page(width=310,height=250)
        p.insert_text((22,50),'SYNTHETIC_TEST_PRIVATE_123',fontsize=14)
        p.insert_text((22,85),'SAFE_LABEL',fontsize=11)
        p.add_redact_annot(fitz.Rect(20,36,270,55), fill=(0,0,0))
        p.apply_redactions()
        p2=doc.new_page(width=420,height=297)
        p2.draw_rect(fitz.Rect(25,35,280,140),color=(0.5,0.5,0.5), fill=(0.8,0.8,0.8))
        p2.draw_rect(fitz.Rect(80,70,180,130),color=(0,0,0),fill=(0,0,0))
        # An embedded object in source must NEVER propagate to new PDF.
        doc.embfile_add('synthetic-attachment.txt', b'SYNTHETIC-ATTACHMENT-ONLY')
        doc.set_metadata({'title':'SYNTHETIC META'})
        doc.save(str(dest),garbage=4,deflate=True)


def _verify_pixels(a:Path,b:Path):
    with fitz.open(a) as x, fitz.open(b) as y:
        assert len(x)==len(y), 'page count differs'
        for i in range(len(x)):
            im1=x[i].get_pixmap(dpi=150,colorspace=fitz.csRGB,alpha=False,annots=False)
            im2=y[i].get_pixmap(dpi=150,colorspace=fitz.csRGB,alpha=False,annots=False)
            assert im1.width==im2.width and im1.height==im2.height, 'page geometry differs'
            assert im1.samples==im2.samples, f'page {i+1}: pixel difference'


class V5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.orig,cls.v5=_load_modules()

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pii_v5_test_')
        self.addCleanup(self.tmp.cleanup)
        self.dir=Path(self.tmp.name)
        self.src=self.dir/'synthetic_redacted.pdf'
        _make_fixture(self.src)

    def test_baseline_and_v5_raster_pixel_parity(self):
        a=self.dir/'baseline.pdf'
        b=self.dir/'v5.pdf'
        with fitz.open(self.src) as doc:
            old=os.environ.pop('PII_PDF_EXPORT_V5',None)
            try:
                self.orig.save_secure_raster_pdf(doc,str(a),dpi=150)
            finally:
                if old is not None: os.environ['PII_PDF_EXPORT_V5']=old
            self.v5.save_secure_raster_pdf_v5(doc,str(b),dpi=150,
                verify_fn=self.orig._verify_secure_raster_pdf)
        _verify_pixels(a,b)
        with fitz.open(b) as pdf:
            self.assertEqual(pdf.embfile_count(),0)
            self.assertEqual(sum(len(page.get_text('text')) for page in pdf),0)
            self.assertEqual(len(pdf),2)
            self.assertTrue(all(len(page.get_images(full=True))==1 for page in pdf))
            self.assertFalse(any(page.get_links() for page in pdf))
        self.assertFalse(list(self.dir.glob('.pii_v5_secure_*')))

    def test_worker_missing_fails_without_overwrite(self):
        dest=self.dir/'keep_old.pdf'
        dest.write_bytes(b'EXISTING_USER_RESULT')
        with fitz.open(self.src) as doc:
            with patch.object(self.v5,'_worker_script',return_value=self.dir/'not_found_worker.py'):
                with self.assertRaises(RuntimeError):
                    self.v5.save_secure_raster_pdf_v5(doc,str(dest),dpi=150,
                        verify_fn=self.orig._verify_secure_raster_pdf)
        self.assertEqual(dest.read_bytes(),b'EXISTING_USER_RESULT')
        self.assertFalse(list(self.dir.glob('.pii_v5_secure_*')))

    def test_invalid_dpi_fails_without_output(self):
        with fitz.open(self.src) as doc:
            with self.assertRaises(ValueError):
                self.v5.save_secure_raster_pdf_v5(doc,str(self.dir/'bad.pdf'),dpi=10,
                    verify_fn=self.orig._verify_secure_raster_pdf)
        self.assertFalse((self.dir/'bad.pdf').exists())

    def test_source_path_refusal(self):
        with fitz.open(self.src) as doc:
            with self.assertRaises(ValueError):
                self.v5.save_secure_raster_pdf_v5(doc,str(self.src),dpi=150,
                    verify_fn=self.orig._verify_secure_raster_pdf)
        with fitz.open(self.src) as src:
            self.assertEqual(src.embfile_count(),1)

    def test_wrapper_flag_off_and_on(self):
        # This test confirms handler_media import will see wrapper after V5 install.
        import inspect
        src=inspect.getsource(self.orig.save_secure_raster_pdf)
        self.assertIn('PII_PDF_EXPORT_V5',src)
        a=self.dir/'off.pdf'; b=self.dir/'on.pdf'
        with fitz.open(self.src) as doc:
            with patch.dict(os.environ, {'PII_PDF_EXPORT_V5':'0'}):
                self.orig.save_secure_raster_pdf(doc,str(a),dpi=150)
            with patch.dict(os.environ, {'PII_PDF_EXPORT_V5':'1'}):
                self.orig.save_secure_raster_pdf(doc,str(b),dpi=150)
        _verify_pixels(a,b)

    def test_parent_verification_failure_does_not_replace(self):
        dest=self.dir/'keep.pdf'
        dest.write_bytes(b'UNCHANGED')
        def fail_verify(_path,_sizes):
            raise RuntimeError('synthetic verification failure')
        with fitz.open(self.src) as doc:
            with self.assertRaisesRegex(RuntimeError,'synthetic verification'):
                self.v5.save_secure_raster_pdf_v5(doc,str(dest),dpi=150,
                    verify_fn=fail_verify)
        self.assertEqual(dest.read_bytes(),b'UNCHANGED')
        self.assertFalse(list(self.dir.glob('.pii_v5_secure_*')))


if __name__=='__main__':
    unittest.main(verbosity=2)
