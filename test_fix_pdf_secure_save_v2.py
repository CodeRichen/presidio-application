import unittest
from fix_pdf_secure_save_v2 import patch, UnsupportedSave

HEADER = '''import os\n\ndef mask_pdf(input_path: str, output_path: str):\n    doc = fitz.open(input_path)\n    for page in doc:\n        page.apply_redactions()\n'''
FOOTER = '''\n    doc.close()\n\ndef restore_pdf(masked_path, output_path):\n    doc.save(output_path)\n'''

class Tests(unittest.TestCase):
    def check(self, save_line):
        src = HEADER + '    ' + save_line + FOOTER
        out, changed, old = patch(src)
        self.assertTrue(changed)
        self.assertIn('garbage=4', out)
        self.assertIn('deflate=True', out)
        self.assertIn('def restore_pdf(masked_path, output_path):\n    doc.save(output_path)', out)
        out2, changed2, _ = patch(out)
        self.assertFalse(changed2)
        self.assertEqual(out2, out)

    def test_standard(self): self.check('doc.save(output_path)')
    def test_end_comment(self): self.check('doc.save(output_path)  # save only')
    def test_reordered_options(self): self.check('doc.save(output_path, deflate=False, garbage=0)')
    def test_existing_optional(self): self.check('doc.save(output_path, deflate_images=True)')
    def test_multiline(self): self.check('doc.save(\n        output_path,\n        garbage=0,\n    )')
    def test_other_keyword(self): self.check('doc.save(output_path, encryption=0, garbage=1)')
    def test_incremental_false(self): self.check('doc.save(output_path, incremental=False)')

    def test_already_fixed(self):
        src = HEADER + '    doc.save(output_path, deflate=True, garbage=4)' + FOOTER
        out, changed, _ = patch(src)
        self.assertFalse(changed)
        self.assertEqual(out, src)

    def test_kwargs_refused(self):
        with self.assertRaises(UnsupportedSave): patch(HEADER+'    doc.save(output_path, **options)'+FOOTER)

    def test_incremental_refused(self):
        with self.assertRaises(UnsupportedSave): patch(HEADER+'    doc.save(output_path, incremental=True)'+FOOTER)

    def test_other_save_refused(self):
        with self.assertRaises(UnsupportedSave): patch(HEADER+'    doc.saveIncr()'+FOOTER)

    def test_other_arg_refused(self):
        with self.assertRaises(UnsupportedSave): patch(HEADER+'    doc.save(otherpath)'+FOOTER)

if __name__=='__main__': unittest.main()
