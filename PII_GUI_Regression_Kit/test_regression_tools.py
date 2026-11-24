"""Run without the Windows UI; these are test-tool unit tests, not end-to-end GUI tests."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from verify_gui_outputs import check_file
from summarize_gui_regression import aggregate
from gui_regression_launcher import safe_file_info, atomic_json_write, install_monitor


class ToolsTests(unittest.TestCase):
    def test_missing_file_fails(self):
        with TemporaryDirectory() as folder:
            r = check_file(Path(folder) / 'missing.pdf')
            self.assertEqual(r['status'], 'FAIL')

    def test_valid_png(self):
        from PIL import Image
        with TemporaryDirectory() as folder:
            p = Path(folder) / 's.png'
            Image.new('RGB', (24, 40), 'white').save(p)
            r = check_file(p)
            self.assertEqual(r['status'], 'PASS')
            self.assertEqual(r['dimensions'], [24, 40])

    def test_pdf_with_attachment_flagged(self):
        import pymupdf as fitz
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'unsafe.pdf'
            with fitz.open() as d:
                d.new_page()
                d.embfile_add('sample.txt', b'only synthetic bytes')
                d.save(p)
            r = check_file(p)
            self.assertEqual(r['status'], 'REVIEW')
            self.assertFalse(r['checks']['no_attachments'])

    def test_pdf_without_objects_passes(self):
        import pymupdf as fitz
        with TemporaryDirectory() as folder:
            p = Path(folder) / 'safe.pdf'
            with fitz.open() as d:
                d.new_page()
                d.save(p)
            r = check_file(p, expect_pages=1)
            self.assertEqual(r['status'], 'PASS')

    def test_aggregate_and_no_pii_content_in_json(self):
        records = [{'status':'OK','click_to_ui_s':12.0,'worker_s':11.8,
                    'ui_lag_max_ms':52.0,'ui_lag_p95_ms':15.0,'rss_peak_mb':700.0},
                   {'status':'OK','click_to_ui_s':14.0,'worker_s':13.8,
                    'ui_lag_max_ms':100.0,'ui_lag_p95_ms':20.0,'rss_peak_mb':720.0},
                   {'status':'ERROR','simulated_error': True}]
        a = aggregate(records)
        self.assertEqual(a['runs'], 3)
        self.assertEqual(a['successful_runs'], 2)
        self.assertEqual(a['error_recovery_simulations'], 1)
        self.assertEqual(a['click_to_ui_s']['average'], 13.0)
        with TemporaryDirectory() as folder:
            p = Path(folder)/'a.json'
            atomic_json_write(p, a)
            self.assertEqual(json.loads(p.read_text(encoding='utf-8'))['runs'], 3)
            info = safe_file_info(p)
            self.assertNotIn(str(Path(folder)), str(info))


class MonitorHookTests(unittest.TestCase):
    def test_ui_hook_normal_and_injected_error(self):
        # Headless fake Tk event loop to verify instrumentation. Not a Windows GUI test.
        with TemporaryDirectory() as folder:
            class FakeApp:
                def __init__(self):
                    self.busy = False
                    self.files = ['synthetic_input.png']
                    self.outputs = []
                    self.callbacks = []
                    self.error = None

                def after(self, ms, func):
                    self.callbacks.append(func)

                def _set_busy(self, busy, msg=''):
                    self.busy = busy

                def _start(self, func, msg, done):
                    self._set_busy(True, msg)
                    try:
                        result = func()
                    except Exception as e:
                        self._set_busy(False)
                        self.error = e
                    else:
                        self._set_busy(False)
                        done(result)

                def drain_once(self):
                    calls = self.callbacks
                    self.callbacks = []
                    for fn in calls:
                        fn()

            install_monitor(FakeApp, results=Path(folder), interval_ms=100,
                            simulate_error_once=True)
            app = FakeApp()
            calls = {'processed': 0}

            def work():
                calls['processed'] += 1
                return ['masked.png']

            def finished(out):
                app.outputs = out

            app._start(work, '處理檔案中…', finished)
            app.drain_once()
            self.assertEqual(calls['processed'], 0)
            self.assertIsInstance(app.error, RuntimeError)
            app._start(work, '處理檔案中…', finished)
            app.drain_once()
            self.assertEqual(calls['processed'], 1)
            records = [json.loads(p.read_text(encoding='utf-8'))
                       for p in Path(folder).glob('run_*.json')]
            self.assertEqual(len(records), 2)
            self.assertEqual(sorted(r['status'] for r in records), ['ERROR', 'OK'])
            self.assertEqual(sum(bool(r['simulated_error']) for r in records), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
