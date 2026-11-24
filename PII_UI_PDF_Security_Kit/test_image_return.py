import unittest
from check_image_return import analyze,patched

class TestImageReturn(unittest.TestCase):
    def test_add_return_with_no_other_change(self):
        source='def mask_image(x):\n    entries = []\n    print(x)\n\ndef restore_image(x):\n    pass\n'
        status,after=patched(source)
        self.assertEqual(status,'NEEDS_FIX')
        self.assertIn('    return entries\n\ndef restore_image',after)
        self.assertIn('def restore_image(x):\n    pass',after)
        self.assertEqual(patched(after)[0],'OK')
    def test_no_change_if_already_returns(self):
        src='def mask_image(x):\n    entries = []\n    return entries\n'
        status,new=patched(src)
        self.assertEqual(status,'OK')
        self.assertEqual(src,new)
    def test_refuse_unrecognized_structure(self):
        src='def mask_image(x):\n    if x:\n        return []\n    entries=[]\n'
        status,_=patched(src)
        self.assertEqual(status,'MANUAL')
if __name__=='__main__':unittest.main()
