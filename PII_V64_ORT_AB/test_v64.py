# coding: utf-8
"""Unit tests do not import project models and do not process any private data."""
import importlib.util
from pathlib import Path
import tempfile
import types
import unittest

ROOT=Path(__file__).resolve().parent

def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    mod=importlib.util.module_from_spec(spec)
    import sys
    sys.modules[name]=mod
    spec.loader.exec_module(mod)
    return mod

runtime=load('v64_runtime')
bench=load('v64_benchmark')

class FakeOptions:
    def __init__(self):
        self.intra_op_num_threads=0
        self.extra='untouched'

class FakeORT:
    SessionOptions=FakeOptions
    def __init__(self):
        self.args=[]
        self.InferenceSession=self.impl
    def impl(self,path_or_bytes,sess_options=None,**kw):
        self.args.append((path_or_bytes,sess_options,kw))
        return ('sess',path_or_bytes,sess_options)

class V64Tests(unittest.TestCase):
    def test_tunes_and_restores(self):
        ort=FakeORT(); before=ort.InferenceSession
        stats,undo=runtime.install_ort_thread_trial(4,ort)
        result=ort.InferenceSession('model.onnx',providers=['CPUExecutionProvider'])
        self.assertEqual(result[2].intra_op_num_threads,4)
        self.assertEqual(stats.attempted,1)
        self.assertEqual(stats.sessions_tuned,1)
        self.assertEqual(ort.args[0][2]['providers'],['CPUExecutionProvider'])
        undo();self.assertIs(ort.InferenceSession,before)
    def test_respects_existing_other_options(self):
        ort=FakeORT();opt=FakeOptions()
        stats,undo=runtime.install_ort_thread_trial(2,ort)
        ort.InferenceSession('a.onnx',sess_options=opt)
        self.assertIs(ort.args[0][1],opt)
        self.assertEqual(opt.extra,'untouched')
        self.assertEqual(stats.existing_options,1)
        undo()
    def test_baseline_unmodified(self):
        ort=FakeORT();before=ort.InferenceSession
        s,undo=runtime.install_ort_thread_trial(None,ort)
        self.assertIs(ort.InferenceSession,before)
        undo();self.assertEqual(s.attempted,0)
    def test_reject_invalid(self):
        for x in (-1,0,65,True,2.5):
            with self.subTest(x=x):
                with self.assertRaises(ValueError):runtime.install_ort_thread_trial(x,FakeORT())
    def test_session_error_fails_closed(self):
        ort=FakeORT()
        def err(*a,**k): raise RuntimeError('no session')
        ort.InferenceSession=err
        s,u=runtime.install_ort_thread_trial(4,ort)
        with self.assertRaises(RuntimeError):ort.InferenceSession('x')
        self.assertEqual(s.sessions_tuned,0)
        u()
    def test_classify_good(self):
        a=[{'safe':True,'elapsed_s':200}]
        b=[{'safe':True,'elapsed_s':170,'ort_sessions_tuned':3}]
        d=[{'all_identical':True}]
        self.assertEqual(bench.classify(a,b,d,4)['status'],'PROMISING_AB_ONLY')
    def test_classify_not_applied(self):
        a=[{'safe':True,'elapsed_s':200}]
        b=[{'safe':True,'elapsed_s':170,'ort_sessions_tuned':0}]
        self.assertEqual(bench.classify(a,b,[{'all_identical':True}],4)['status'],'TUNING_NOT_APPLIED')
    def test_classify_pixel_diff(self):
        a=[{'safe':True,'elapsed_s':200}]
        b=[{'safe':True,'elapsed_s':100,'ort_sessions_tuned':3}]
        self.assertEqual(bench.classify(a,b,[{'all_identical':False}],4)['status'],'PIXEL_DIFFERENCE')
    def test_classify_gt_fail(self):
        a=[{'safe':True,'elapsed_s':200}]
        b=[{'safe':False,'elapsed_s':100,'ort_sessions_tuned':3}]
        self.assertEqual(bench.classify(a,b,[{'all_identical':True}],4)['status'],'SECURITY_GATE_FAILED')
    def test_classify_little_gain(self):
        a=[{'safe':True,'elapsed_s':200}]
        b=[{'safe':True,'elapsed_s':190,'ort_sessions_tuned':3}]
        self.assertEqual(bench.classify(a,b,[{'all_identical':True}],4)['status'],'NO_CONFIRMED_GAIN')
    def test_medians(self):
        a=[{'safe':True,'elapsed_s':200},{'safe':True,'elapsed_s':220}]
        b=[{'safe':True,'elapsed_s':180,'ort_sessions_tuned':2},{'safe':True,'elapsed_s':186,'ort_sessions_tuned':2}]
        x=bench.classify(a,b,[{'all_identical':True}]*2,4)
        self.assertEqual(x['baseline_median_s'],210)
        self.assertEqual(x['candidate_median_s'],183)
    def test_report_no_PII_values(self):
        with tempfile.TemporaryDirectory() as td:
            report={'schema':'test','status':'INCOMPLETE','baseline':[],'candidate':[]}
            bench.write_report(report,Path(td))
            self.assertTrue((Path(td)/'v64_ort_ab_summary.json').is_file())
            self.assertTrue((Path(td)/'v64_ort_ab_summary.md').is_file())
            self.assertNotIn('PRIVATE_restore_map',(Path(td)/'v64_ort_ab_summary.json').read_text())

if __name__=='__main__':unittest.main(verbosity=2)
