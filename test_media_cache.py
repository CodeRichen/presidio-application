"""No-model regression for the installer and ContextVar based cache logic."""
import ast
import contextlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from install_pii_cache import BEGIN, patch_source

FAKE_SOURCE = '''import os
calls = []
def detect_matches(text):
    calls.append(text)
    return [(0, len(text), "PERSON", 0.91, "zh")]
def find_sensitive_boxes_in_words(words, scale=1.0):
    output = []
    for w in words:
        output.extend(detect_matches(w))
    return output
# Other PDF pages also use the same detector.
def _mask_pdf_text_page(page, entries, counters):
    return find_sensitive_boxes_in_words(page)
def _mask_pdf_scanned_page(page, entries, counters):
    return find_sensitive_boxes_in_words(page) + find_sensitive_boxes_in_words(page)
'''


def fresh():
    namespace = {'__name__': 'fake_handler_media'}
    exec(compile(patch_source(FAKE_SOURCE), '<test>', 'exec'), namespace)
    return namespace


class PiiCacheRegression(unittest.TestCase):
    def test_patch_parses(self):
        ast.parse(patch_source(FAKE_SOURCE))

    def test_patch_is_idempotent(self):
        patched = patch_source(FAKE_SOURCE)
        self.assertEqual(patch_source(patched), patched)
        self.assertEqual(patched.count(BEGIN), 1)

    def test_output_identical(self):
        n = fresh()
        expected = n['find_sensitive_boxes_in_words'](['a','a','b','a'])
        with patch.dict(os.environ, {'PII_MEDIA_CACHE': '0'}):
            result = n['find_sensitive_boxes_in_words'](['a','a','b','a'])
        self.assertEqual(expected, result)

    def test_same_text_called_once_per_scope(self):
        n = fresh()
        n['find_sensitive_boxes_in_words'](['a', 'a', 'b', 'a'])
        self.assertEqual(n['calls'], ['a', 'b'])

    def test_disabled_calls_again(self):
        n = fresh()
        with patch.dict(os.environ, {'PII_MEDIA_CACHE': '0'}):
            n['find_sensitive_boxes_in_words'](['a', 'a', 'b'])
        self.assertEqual(n['calls'], ['a', 'a', 'b'])

    def test_scope_ends_after_each_image(self):
        n = fresh()
        n['find_sensitive_boxes_in_words'](['a','a'])
        n['find_sensitive_boxes_in_words'](['a','a'])
        self.assertEqual(n['calls'], ['a', 'a'])
        self.assertIsNone(n['_pii_media_cache_var'].get())

    def test_nested_scanned_pdf_scope_shares_cache(self):
        n = fresh()
        result = n['_mask_pdf_scanned_page'](['a', 'a', 'b'], [], {})
        self.assertEqual(len(result), 6)
        self.assertEqual(n['calls'], ['a', 'b'])

    def test_text_pdf_scope(self):
        n = fresh()
        n['_mask_pdf_text_page'](['a', 'a'], [], {})
        self.assertEqual(n['calls'], ['a'])

    def test_cache_does_not_escape(self):
        n = fresh()
        n['find_sensitive_boxes_in_words'](['a', 'a'])
        n['detect_matches']('a')  # no scope, should compute anew
        self.assertEqual(n['calls'], ['a', 'a'])

    def test_exception_clears_cache(self):
        code = FAKE_SOURCE.replace('output.extend(detect_matches(w))', 'output.extend(detect_matches(w))\n        if w == "boom": raise ValueError("test")')
        namespace = {'__name__': 'fake_handler_media'}
        exec(compile(patch_source(code), '<test>', 'exec'), namespace)
        with self.assertRaises(ValueError):
            namespace['find_sensitive_boxes_in_words'](['x', 'boom'])
        self.assertIsNone(namespace['_pii_media_cache_var'].get())
        namespace['find_sensitive_boxes_in_words'](['x', 'x'])
        self.assertEqual(namespace['calls'], ['x', 'boom', 'x'])

    def test_missing_function_refused(self):
        with self.assertRaises(RuntimeError):
            patch_source('import os\ndef detect_matches(t): return []\n')

    def test_source_functions_unchanged(self):
        original = ast.parse(FAKE_SOURCE)
        patched = ast.parse(patch_source(FAKE_SOURCE))
        def fnmaps(tree):
            return {f.name: f for f in tree.body if isinstance(f, ast.FunctionDef)}
        a, b = fnmaps(original), fnmaps(patched)
        for name in a:
            a[name].decorator_list.clear()
            b[name].decorator_list.clear()
            self.assertEqual(ast.dump(a[name], include_attributes=False),
                             ast.dump(b[name], include_attributes=False))


if __name__ == '__main__':
    unittest.main()
