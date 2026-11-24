"""Offline tests (no Windows GUI, GPU or model required)."""
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from gui_v2_launcher import Probe, _pctl, gap_summary, install_gui_monitor, summarize_events
from summarize_v2 import summary_dir


class V2Tests(unittest.TestCase):
    def test_nested_timing_and_restoration(self):
        p = Probe()
        class Worker:
            def inner(self):
                time.sleep(.003)
                return 'private source text'
            def outer(self):
                return self.inner()
        w = Worker()
        original_inner = Worker.inner
        p.wrap(Worker, 'inner', 'inner')
        p.wrap(Worker, 'outer', 'outer')
        rec = {'t0': time.perf_counter()}
        p.start(rec)
        self.assertEqual(w.outer(), 'private source text')
        events, dropped = p.stop(rec)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(events), 2)
        by = {e['stage']: e for e in events}
        self.assertGreater(by['outer']['duration_s'], 0)
        self.assertLess(by['outer']['exclusive_s'], by['outer']['duration_s'])
        self.assertAlmostEqual(by['inner']['duration_s'], by['inner']['exclusive_s'], delta=.002)
        self.assertNotIn('private source', json.dumps(events))
        p.restore()
        self.assertIs(Worker.inner, original_inner)

    def test_exceptions_pass_through(self):
        class Worker:
            def fail(self):
                raise ValueError('private')
        p = Probe()
        p.wrap(Worker, 'fail', 'fail')
        rec = {'t0': time.perf_counter()}
        p.start(rec)
        with self.assertRaises(ValueError):
            Worker().fail()
        events, _ = p.stop(rec)
        self.assertEqual(events[0]['error'], True)
        self.assertNotIn('private', json.dumps(events))
        p.restore()

    def test_safe_summary_and_sample(self):
        ev = [{'stage':'ocr', 'duration_s':2.0,'exclusive_s':1.0,'error':False},
              {'stage':'ocr', 'duration_s':1.0,'exclusive_s':0.5,'error':False}]
        self.assertEqual(summarize_events(ev)['ocr']['calls'], 2)
        self.assertAlmostEqual(summarize_events(ev)['ocr']['exclusive_s'], 1.5)
        stats = gap_summary(2.0, 2.2, [(2.01,'ocr',80),(2.08,'ocr',90),(2.16,'pii',100)])
        self.assertEqual(stats['phase_samples']['ocr'], 2)
        self.assertEqual(_pctl([1,2,3,4], .95), 3)

    def test_max_events_cap(self):
        class Task:
            def foo(self): return 1
        p = Probe(max_events=1)
        p.wrap(Task,'foo','foo')
        rec={'t0':time.perf_counter()}
        p.start(rec)
        Task().foo(); Task().foo()
        events, dropped = p.stop(rec)
        self.assertEqual(len(events),1)
        self.assertEqual(dropped,1)
        p.restore()

    def test_file_gui_capture_without_names(self):
        class FakeApp:
            def __init__(self):
                self.busy = False
                self.files = ['/secret/林家宇_身分證.pdf', '/secret/password.jpg']
                self.scheduled = []
            def after(self, ms, cb):
                self.scheduled.append((ms, cb))
            def _set_busy(self,busy,msg=''):
                self.busy=busy
            def _start(self,func,msg,on_done):
                self._set_busy(True,msg)
                try:
                    out=func()
                    self._set_busy(False)
                    on_done(out)
                except Exception:
                    self._set_busy(False)
            def _poll(self): pass
        probe=Probe()
        probe.wrap(FakeApp,'_poll','ui.poll')
        with tempfile.TemporaryDirectory() as tmp:
            install_gui_monitor(FakeApp,probe,results_dir=tmp,
                                heartbeat_ms=100,sample_ms=20,slow_threshold_ms=500)
            app=FakeApp()
            app._start(lambda:['/secret/財務資訊.pdf'], '處理檔案中…',lambda _r:None)
            # Find the finish callback, ignoring repeating heartbeat callbacks.
            for ms, cb in list(app.scheduled):
                if ms == 50:
                    cb()
            reports=list(Path(tmp).glob('v2_*.json'))
            self.assertEqual(len(reports),1)
            text=reports[0].read_text(encoding='utf-8')
            self.assertNotIn('林家宇',text)
            self.assertNotIn('password',text)
            self.assertNotIn('財務資訊',text)
            d=json.loads(text)
            self.assertEqual(d['input_extensions'],{'.pdf':1,'.jpg':1})
            self.assertEqual(d['output_count'],1)
            self.assertEqual(d['status'],'OK')
            out=summary_dir(tmp)
            self.assertEqual(out['runs'],1)
            self.assertTrue((Path(tmp)/'gui_v2_summary.md').exists())

    def test_gui_error_recovery(self):
        class FakeApp:
            def __init__(self):
                self.busy = False
                self.files=['secret.pdf']
                self.pending=[]
            def after(self,ms,cb): self.pending.append((ms,cb))
            def _set_busy(self,busy,msg=''): self.busy=busy
            def _start(self,func,msg,on_done):
                self._set_busy(True,msg)
                try:
                    out=func()
                    self._set_busy(False)
                    on_done(out)
                except Exception:
                    self._set_busy(False)
        probe=Probe()
        with tempfile.TemporaryDirectory() as tmp:
            install_gui_monitor(FakeApp,probe,results_dir=tmp)
            app=FakeApp()
            def bad(): raise RuntimeError('PII is private')
            app._start(bad,'處理檔案中…',lambda x:None)
            for ms,cb in list(app.pending):
                if ms == 50: cb()
            app.pending.clear()
            app._start(lambda:[], '處理檔案中…',lambda x:None)
            for ms,cb in list(app.pending):
                if ms == 50: cb()
            statuses=[json.loads(x.read_text(encoding='utf-8'))['status'] for x in Path(tmp).glob('v2_*.json')]
            self.assertCountEqual(statuses,['ERROR','OK'])


if __name__=='__main__':
    unittest.main(verbosity=2)
