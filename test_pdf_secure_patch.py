import unittest
from fix_pdf_secure_save import patch

class PatchTests(unittest.TestCase):
    def test_only_mask_pdf_changes(self):
        src = ('def mask_pdf(input_path, output_path):\n'
               '    doc.save(output_path)\n\n'
               'def restore_pdf(masked_path, output_path):\n'
               '    doc.save(output_path)\n')
        result, changed = patch(src)
        self.assertTrue(changed)
        self.assertEqual(result.count('garbage=4'), 1)
        self.assertIn('def restore_pdf(masked_path, output_path):\n    doc.save(output_path)',result)
        same, changed2 = patch(result)
        self.assertFalse(changed2)
        self.assertEqual(same,result)

    def test_fails_closed_on_unrecognized_format(self):
        with self.assertRaises(ValueError):
            patch('def mask_pdf(x):\n    pass\n')

    def test_works_with_following_function(self):
        src = ('def mask_pdf(x, output_path):\n    doc.save(output_path)\n\n'
               'def foo():\n    doc.save(output_path)\n')
        patched, _ = patch(src)
        self.assertEqual(patched.count('garbage=4'),1)

if __name__=='__main__':
    unittest.main()
