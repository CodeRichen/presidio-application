"""Local structural regression tests: python -m unittest -v test_pdf_secure_output.py"""
import os
import tempfile
import unittest

import fitz
from pdf_secure_output import save_secure_raster_pdf


class SecurePdfOutputTests(unittest.TestCase):
    def test_prevents_attachment_and_hidden_pdf_content_leak(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "source.pdf")
            target = os.path.join(folder, "masked.pdf")
            with fitz.open() as original:
                for _ in range(3):
                    page = original.new_page(width=595, height=842)
                    # This secret is still in the SOURCE PDF text stream.
                    page.insert_text((72, 90), "ORIGINAL-SECRET-123")
                    # An overlay is NOT true redaction; raster flattening only
                    # uses the visible (black-covered) appearance.
                    page.draw_rect(fitz.Rect(65, 73, 255, 104),
                                   color=(0, 0, 0), fill=(0, 0, 0), overlay=True)
                    page.add_text_annot((20, 20), "private annotation")
                original.embfile_add("backup.txt", b"ORIGINAL-SECRET-123")
                original.set_metadata({"title": "ORIGINAL-SECRET-123"})
                original.save(source)

            with fitz.open(source) as original:
                self.assertEqual(original.embfile_count(), 1)
                self.assertIn("ORIGINAL-SECRET-123", original[0].get_text())
                save_secure_raster_pdf(original, target, dpi=144)
                self.assertEqual(original.embfile_count(), 1)  # input unchanged

            with fitz.open(target) as result:
                self.assertEqual(len(result), 3)
                self.assertEqual(result.embfile_count(), 0)
                self.assertEqual(result.metadata.get("title", ""), "")
                self.assertEqual(result[0].get_text().strip(), "")
                self.assertIsNone(next(iter(result[0].annots() or ()), None))
                self.assertEqual(len(result[0].get_images(full=True)), 1)
            with open(target, "rb") as fh:
                self.assertNotIn(b"ORIGINAL-SECRET-123", fh.read())

    def test_rejects_overwriting_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source = os.path.join(folder, "source.pdf")
            with fitz.open() as doc:
                doc.new_page()
                doc.save(source)
            with fitz.open(source) as doc:
                with self.assertRaises(ValueError):
                    save_secure_raster_pdf(doc, source)


if __name__ == "__main__":
    unittest.main()
