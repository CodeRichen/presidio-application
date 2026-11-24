import ast
import unittest
from fix_ocr_compact_pii import patch

EXAMPLE = '''def x():
    for v in []:
            COMPACT_LABELS = {
                "ADDRESS",
                "LICENSE_NUMBER",
            }
            print(COMPACT_LABELS)
'''

class PatchTests(unittest.TestCase):
    def test_add(self):
        new, added = patch(EXAMPLE)
        self.assertIn('EMAIL', added)
        self.assertIn('TW_PHONE', added)
        ast.parse(new)
        self.assertIn('"EMAIL",', new)
    def test_idempotent(self):
        once, _ = patch(EXAMPLE)
        twice, added = patch(once)
        self.assertEqual(once, twice)
        self.assertEqual(added, [])
    def test_refuses_unknown_layout(self):
        with self.assertRaises(ValueError):
            patch('print("hello")')
    def test_preserves_other_code(self):
        updated, _ = patch(EXAMPLE)
        self.assertEqual(updated.split('def x():')[0], EXAMPLE.split('def x():')[0])
        self.assertIn('print(COMPACT_LABELS)', updated)

if __name__ == '__main__':
    unittest.main()
