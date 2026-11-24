"""Fast, offline tests for V5 acceptance checks; no OCR/NER or private data."""
from __future__ import annotations
import json
from pathlib import Path
import tempfile
import unittest

try:
    import pymupdf as fitz
except ImportError:
    import fitz

from v5_acceptance import audit_status, check_pdf, compare_page_pixels


def make_pdf(path: Path, *, mark: int = 255, hidden_text: bool = False):
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 64, 64), False)
    pix.clear_with(mark)
    with fitz.open() as doc:
        for _ in range(2):
            page = doc.new_page(width=144, height=144)
            page.insert_image(page.rect, stream=pix.tobytes('png'))
            if hidden_text:
                page.insert_text((5, 5), 'UNMASKED', fontsize=5)
        doc.save(str(path), garbage=4, deflate=True, clean=True, incremental=False)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.a = Path(self.temp.name)/'a.pdf'
        self.b = Path(self.temp.name)/'b.pdf'
        make_pdf(self.a)
        make_pdf(self.b)

    def test_structure_pass(self):
        self.assertTrue(check_pdf(self.a, expected_pages=2)['pass'])

    def test_structure_wrong_page_count(self):
        self.assertFalse(check_pdf(self.a, expected_pages=7)['pass'])

    def test_structure_text_fail(self):
        make_pdf(self.b, hidden_text=True)
        self.assertFalse(check_pdf(self.b, expected_pages=2)['pass'])

    def test_identical_pixels_pass(self):
        r = compare_page_pixels(self.a, self.b, dpi=72)
        self.assertEqual(r['identical_pages'], 2)
        self.assertTrue(r['all_identical'])

    def test_pixel_difference_fail(self):
        make_pdf(self.b, mark=0)
        self.assertFalse(compare_page_pixels(self.a, self.b, dpi=72)['all_identical'])

    def test_audit_good(self):
        mock = {'status': 'PASS_AUTOMATED', 'automated_structure_and_text_checks': 'PASS',
                'summary': {'text_regions':27,'text_regions_pass_98pct':27,
                            'face_map_regions_checked':17,'face_map_regions_pass_98pct':17,
                            'face_counts_needing_review':[]},
                'structure':{'source_attachments':1,'output_attachments':0,
                             'source_pages':7,'output_pages':7,'output_text_chars':0,
                             'output_annotations':0,'output_eof_markers':1},
                'scenarios':[{'text_pass_98pct':3,'detected_faces_from_map':1} for _ in range(7)]}
        path = Path(self.temp.name)/'audit.json'
        path.write_text(json.dumps(mock), encoding='utf8')
        self.assertTrue(audit_status(path)['automated_pass'])
        mock['summary']['face_map_regions_pass_98pct'] = 16
        path.write_text(json.dumps(mock), encoding='utf8')
        self.assertFalse(audit_status(path)['automated_pass'])

    def test_missing_face_map_fails(self):
        mock = {'status': 'REVIEW','summary': {'text_regions_pass_98pct':27}}
        path = Path(self.temp.name)/'audit.json'
        path.write_text(json.dumps(mock),encoding='utf8')
        self.assertFalse(audit_status(path)['automated_pass'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
