from pathlib import Path
import tempfile
import unittest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from audit_pdf_security import audit

ROOT=Path(__file__).resolve().parent
SECRET='TEST_SECRET_A9X7Z'


class TestSecurityAudit(unittest.TestCase):
    def test_scanned_overlay_is_detected_even_when_black(self):
        with tempfile.TemporaryDirectory() as t:
            from make_security_examples import main  # main requires CLI; not called
            src=ROOT/'fixtures/synthetic_scanned_pii_3pages.pdf'
            dst=Path(t)/'overlay.pdf'
            with fitz.open(src) as doc:
                for page in doc:
                    page.draw_rect(fitz.Rect(195,144,259,168),fill=(0,0,0),color=(0,0,0))
                doc.save(dst)
            result=audit(src,dst)
            codes={f['code'] for f in result['findings'] if f['status']=='FAIL'}
            self.assertIn('ORIGINAL_IMAGE_RECOVERABLE',codes)

    def test_scanned_true_redaction_removes_exact_original_images(self):
        with tempfile.TemporaryDirectory() as t:
            src=ROOT/'fixtures/synthetic_scanned_pii_3pages.pdf'
            dst=Path(t)/'redacted.pdf'
            with fitz.open(src) as doc:
                for page in doc:
                    page.add_redact_annot(fitz.Rect(195,144,259,168),fill=(0,0,0))
                    page.apply_redactions(images=2,graphics=1,text=0)
                doc.save(dst,garbage=4,deflate=True)
            result=audit(src,dst)
            codes={f['code'] for f in result['findings'] if f['status']=='FAIL'}
            self.assertNotIn('ORIGINAL_IMAGE_RECOVERABLE',codes)

    def test_native_text_black_overlay_is_not_redaction(self):
        with tempfile.TemporaryDirectory() as t:
            src=Path(t)/'original.pdf'; bad=Path(t)/'bad.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((72,100),SECRET,fontsize=13)
                doc.save(src)
            with fitz.open(src) as doc:
                doc[0].draw_rect(fitz.Rect(65,81,250,110),fill=(0,0,0))
                doc.save(bad)
            result=audit(src,bad,explicit_secrets=[SECRET])
            self.assertEqual('FAIL',result['status'])
            codes={f['code'] for f in result['findings'] if f['status']=='FAIL'}
            self.assertTrue('EXTRACTABLE_TEXT_LEAK' in codes or 'DECODED_STREAM_SECRET' in codes)

    def test_native_text_true_redaction_removes_secret(self):
        with tempfile.TemporaryDirectory() as t:
            src=Path(t)/'original.pdf'; good=Path(t)/'good.pdf'
            with fitz.open() as doc:
                page=doc.new_page()
                page.insert_text((72,100),SECRET,fontsize=13)
                doc.save(src)
            with fitz.open(src) as doc:
                doc[0].add_redact_annot(fitz.Rect(65,81,250,110),fill=(0,0,0))
                doc[0].apply_redactions(images=2,graphics=1,text=0)
                doc.save(good,garbage=4,deflate=True)
            result=audit(src,good,explicit_secrets=[SECRET])
            codes={f['code'] for f in result['findings'] if f['status']=='FAIL'}
            self.assertFalse(codes)

    def test_no_pii_logged(self):
        with tempfile.TemporaryDirectory() as t:
            src=Path(t)/'original.pdf'; bad=Path(t)/'bad.pdf'
            with fitz.open() as doc:
                doc.new_page().insert_text((72,100),SECRET)
                doc.save(src)
            with fitz.open(src) as doc: doc.save(bad)
            import json
            report=json.dumps(audit(src,bad,explicit_secrets=[SECRET]),ensure_ascii=False)
            self.assertNotIn(SECRET,report)

    def test_source_not_modified(self):
        from audit_pdf_security import digest
        src=ROOT/'fixtures/synthetic_scanned_pii_3pages.pdf'
        before=digest(src.read_bytes())
        with tempfile.TemporaryDirectory() as t:
            dst=Path(t)/'copy.pdf'
            with fitz.open(src) as doc: doc.save(dst)
            audit(src,dst)
        self.assertEqual(before,digest(src.read_bytes()))

if __name__=='__main__':unittest.main()
