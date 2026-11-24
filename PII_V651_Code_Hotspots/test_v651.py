import json
import unittest
from types import SimpleNamespace
from v651_runtime import CodeHotspots, safe_file_label, safe_func_label
from v651_benchmark import classify, safe_env

class Tests(unittest.TestCase):
    def test_sanitized_paths(self):
        self.assertEqual(safe_file_label('C:\\Users\\private\\project\\handler_code.py'), 'handler_code.py')
        self.assertEqual(safe_file_label('/home/private/my.py'), 'my.py')

    def test_label_sanitization(self):
        self.assertEqual(safe_func_label('a b/secret'), 'a_b_secret')

    def test_keeps_return_value(self):
        mod = SimpleNamespace(detect=lambda s: [len(s)])
        p = CodeHotspots(mod, every=1).install()
        try:
            self.assertEqual(mod.detect('hello'), [5])
            self.assertEqual(p.calls, 1)
        finally:
            p.restore()

    def test_restore_original(self):
        f = lambda x: x
        mod = SimpleNamespace(detect=f)
        p = CodeHotspots(mod).install()
        p.restore()
        self.assertIs(mod.detect, f)

    def test_double_install_rejected(self):
        mod = SimpleNamespace(detect=lambda s: s)
        p = CodeHotspots(mod).install()
        try:
            with self.assertRaises(RuntimeError): p.install()
        finally: p.restore()

    def test_profile_frequency(self):
        mod = SimpleNamespace(detect=lambda s: s[::-1])
        p = CodeHotspots(mod, every=3, max_profiled_calls=2).install()
        try:
            for i in range(12): mod.detect(str(i))
            d = p.safe_summary()
            self.assertEqual(d['total_calls'], 12)
            self.assertEqual(d['profiled_calls'], 2)
        finally: p.restore()

    def test_no_pii_in_json(self):
        secret = '0955123123 fake@example.invalid'
        mod = SimpleNamespace(detect=lambda x: x + x)
        p = CodeHotspots(mod, every=1).install()
        try:
            mod.detect(secret)
            j = json.dumps(p.safe_summary(), ensure_ascii=False)
            self.assertNotIn(secret, j)
            self.assertNotIn('0955123123', j)
            self.assertNotIn('fake@example.invalid', j)
        finally: p.restore()

    def test_exception_preserves(self):
        def fail(_): raise ValueError('test')
        mod = SimpleNamespace(detect=fail)
        p = CodeHotspots(mod, every=1).install()
        try:
            with self.assertRaises(ValueError): mod.detect('x')
            self.assertEqual(p.safe_summary()['profiled_calls'], 1)
        finally: p.restore()

    def test_bad_sampling(self):
        with self.assertRaises(ValueError): CodeHotspots(SimpleNamespace(detect=lambda x:x), 0, 1)
        with self.assertRaises(ValueError): CodeHotspots(SimpleNamespace(detect=lambda x:x), 1, 0)

    def test_no_callable(self):
        with self.assertRaises(RuntimeError): CodeHotspots(SimpleNamespace(detect=3)).install()

    def test_profiler_rows_do_not_include_abs_path(self):
        mod = SimpleNamespace(detect=lambda x: len(x))
        p = CodeHotspots(mod, every=1).install()
        try:
            mod.detect('fake@example.invalid')
            rows = p.safe_summary()['top_self_time']
            self.assertTrue(rows)
            self.assertTrue(all('/' not in r['module_file'] and '\\' not in r['module_file'] for r in rows))
        finally: p.restore()

    def test_classify(self):
        self.assertEqual(classify(True,True,1), 'PROFILE_OK')
        self.assertEqual(classify(True,True,0), 'NO_PROFILE_SAMPLES')
        self.assertEqual(classify(True,False,1), 'SECURITY_GATE_FAILED')
        self.assertEqual(classify(False,True,1), 'RUN_FAILED')

    def test_fixture_env(self):
        e = safe_env()
        self.assertEqual(e['PII_MEDIA_FP_PROFILE'], 'synthetic_face_test')
        self.assertEqual(e['PII_MEDIA_CACHE'], '1')
        self.assertEqual(e['PII_PDF_EXPORT_V5'], '1')

if __name__ == '__main__': unittest.main(verbosity=2)
