#!/usr/bin/env python3
# coding: utf-8
"""Offline unit tests; no OCR models, PII originals or real PDF operations."""
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import v653_benchmark as benchmark
import v653_child as child


class ClassificationTests(unittest.TestCase):
    def make_data(self, n=5, first_base=None, first_cand=None,
                  second_base=None, second_cand=None, detect_base=30, detect_cand=24):
        first_base = first_base or [210]*n
        first_cand = first_cand or [200]*n
        second_base = second_base or [210]*n
        second_cand = second_cand or [170]*n
        def entry(first,second,eff,cd):
            return {'safe':True,'torch_effective':eff,'passes':{
                'first':{'processing_s':first,'code_detect_wall_s':cd,'code_detect_calls':278},
                'second':{'processing_s':second,'code_detect_wall_s':cd,'code_detect_calls':278},
            }}
        bases=[entry(first_base[i],second_base[i],14,detect_base) for i in range(n)]
        cands=[entry(first_cand[i],second_cand[i],4,detect_cand) for i in range(n)]
        px=[{x:{'all_identical':True} for x in
             ('first','second','baseline_passes','candidate_passes')} for _ in range(n)]
        return bases,cands,px

    def test_promising_only_when_warm_reproduces(self):
        a,b,p=self.make_data(first_base=[270,210,218,209,211])
        r=benchmark.classify(a,b,p,4)
        self.assertEqual(r['status'],'PROMISING_WARM_AB_ONLY')
        self.assertEqual(r['second']['positive_pairs'],5)

    def test_first_pass_outlier_not_called_gain(self):
        a,b,p=self.make_data(first_base=[300,250,260,240,250],
                             second_base=[210]*5,second_cand=[208]*5)
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'NO_REPRODUCIBLE_GAIN')

    def test_not_enough_repeats(self):
        a,b,p=self.make_data(n=2)
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'INSUFFICIENT_REPEATS')

    def test_incomplete(self):
        a,b,p=self.make_data()
        self.assertEqual(benchmark.classify(a[:-1],b,p,4)['status'],'INCOMPLETE')

    def test_security_gate(self):
        a,b,p=self.make_data(); b[2]['safe']=False
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'SECURITY_GATE_FAILED')

    def test_pixel_gate_first(self):
        a,b,p=self.make_data(); p[0]['first']['all_identical']=False
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'PIXEL_DIFFERENCE')

    def test_pixel_gate_second(self):
        a,b,p=self.make_data(); p[2]['second']['all_identical']=False
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'PIXEL_DIFFERENCE')

    def test_pixel_gate_within_one_thread_configuration(self):
        a,b,p=self.make_data(); p[3]['baseline_passes']['all_identical']=False
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'PIXEL_DIFFERENCE')

    def test_tuning_not_applied(self):
        a,b,p=self.make_data();b[1]['torch_effective']=14
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'TUNING_NOT_APPLIED')

    def test_no_real_change(self):
        a,b,p=self.make_data();a[0]['torch_effective']=4
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'NO_EFFECTIVE_CHANGE')

    def test_changed_detect_calls_rejected(self):
        a,b,p=self.make_data();b[1]['passes']['second']['code_detect_calls']=0
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'INSTRUMENTATION_MISMATCH')

    def test_threshold_exactly_ten(self):
        a,b,p=self.make_data(second_base=[200]*5,second_cand=[180]*5)
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'PROMISING_WARM_AB_ONLY')

    def test_only_three_positive_pairs_rejected(self):
        a,b,p=self.make_data(second_base=[200]*5,
                             second_cand=[160,160,160,210,210])
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'NO_REPRODUCIBLE_GAIN')

    def test_no_detect_gain_rejected(self):
        a,b,p=self.make_data(detect_cand=32)
        self.assertEqual(benchmark.classify(a,b,p,4)['status'],'NO_REPRODUCIBLE_GAIN')

    def test_summary_contains_only_safe_metrics(self):
        a,b,p=self.make_data()
        with tempfile.TemporaryDirectory() as tmp:
            r={'status':'INSUFFICIENT_REPEATS', 'pairs':[],
               'comparison':benchmark.classify(a[:2],b[:2],p[:2],4)}
            benchmark.write_report(r,Path(tmp))
            text=(Path(tmp)/'v653_torch_stability_summary.json').read_text(encoding='utf-8')
            self.assertNotIn('PRIVATE_restore',text)
            self.assertNotIn('OCR_text',text)
            self.assertIn('INSUFFICIENT_REPEATS',text)

    def test_strict_pixels_requires_seven(self):
        class BadGate:
            @staticmethod
            def compare_page_pixels(*args,**kwargs):
                return {'pages_compared':6,'identical_pages':6,'all_identical':True}
        self.assertFalse(benchmark.strict_pixels(BadGate(),Path('a'),Path('b'))['all_identical'])


class ChildLogicTests(unittest.TestCase):
    def test_two_passes_and_timing_no_private_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'synthetic.pdf';source.write_bytes(b'synthetic')
            runner_dir=root/'PII_V5_Security_Acceptance';runner_dir.mkdir()
            runner=runner_dir/'run_7page_pdf_test.py';runner.write_text('# simulated')
            expected=root/'expected.json';expected.write_text('{}')
            dest=root/'results'
            my_torch=types.ModuleType('torch'); my_torch.get_num_threads=lambda:14
            cur=[14]; my_torch.set_num_threads=lambda n:cur.__setitem__(0,n)
            my_torch.get_num_threads=lambda:cur[0]
            handler=types.ModuleType('handler_code')
            def original_detect(text):
                return [(0,1,'SYNTHETIC',None,'regex')]
            handler.detect=original_detect
            runner_module=types.ModuleType('run_7page_pdf_test')
            run_count=[0]
            def simulated_run(src, exp, out):
                run_count[0]+=1
                out.mkdir(parents=True,exist_ok=True)
                handler.detect('TEST SYNTHETIC')
                (out/'pdf_face_7page_audit.json').write_text(
                    json.dumps({'processing_seconds':.001}), encoding='utf-8')
            runner_module.run=simulated_run
            sys.path.insert(0,str(root))
            saved={k:sys.modules.get(k) for k in ('torch','handler_code','run_7page_pdf_test')}
            try:
                sys.modules.update({'torch':my_torch,'handler_code':handler,
                                    'run_7page_pdf_test':runner_module})
                with mock.patch.object(child,'sha256_file',return_value=child.FIXTURE_SHA256):
                    stats=child.run_two_passes(root,source,runner,expected,dest,4)
            finally:
                for k,v in saved.items():
                    if v is None:sys.modules.pop(k,None)
                    else:sys.modules[k]=v
                sys.path.remove(str(root))
            self.assertEqual(stats['torch_effective'],4)
            self.assertEqual(run_count[0],2)
            self.assertEqual(stats['passes']['first']['code_detect_calls'],1)
            self.assertEqual(stats['passes']['second']['code_detect_calls'],1)
            self.assertIs(handler.detect,original_detect)
            self.assertNotIn('TEST SYNTHETIC',json.dumps(stats))

    def test_fixture_hash_required(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);bad=root/'bad.pdf';bad.write_bytes(b'private-real-document')
            runner=root/'run_7page_pdf_test.py';runner.write_text('')
            with self.assertRaises(RuntimeError):
                child.run_two_passes(root,bad,runner,root/'expected',root/'output',4)


class BalancedOrderTests(unittest.TestCase):
    def test_orchestration_alternates_AB_BA(self):
        root=Path(benchmark.__file__).resolve().parent.parent
        fake_gate=object()
        modes=[]
        seen=[]
        def fake_run(rootp,source,gate_dir,tool_dir,threads,trial_dir,timeout,gate):
            modes.append(threads)
            return {'trial_threads':threads, 'safe':True,
                    'torch_effective':14 if threads==0 else 4,
                    'passes':{
                        'first':{'processing_s':210 if not threads else 200,
                                 'code_detect_wall_s':30,'code_detect_calls':278},
                        'second':{'processing_s':205 if not threads else 190,
                                 'code_detect_wall_s':30 if not threads else 28,
                                 'code_detect_calls':278},
                    }}
        with tempfile.TemporaryDirectory() as temp:
            with mock.patch.object(benchmark,'require_gate',return_value=(fake_gate,Path(temp))), \
                 mock.patch.object(benchmark,'run_trial',side_effect=fake_run), \
                 mock.patch.object(benchmark,'strict_pixels',return_value={'all_identical':True}), \
                 mock.patch.object(benchmark,'write_report',side_effect=lambda r,o:seen.append(r)):
                result=benchmark.main(['--project-root',str(root),'--repeats','3',
                                       '--results-dir',str(Path(temp)/'out')])
        self.assertEqual(result,0)
        self.assertEqual(modes,[0,4,4,0,0,4])
        self.assertEqual([p['order'] for p in seen[0]['pairs']],['AB','BA','AB'])
        self.assertEqual(seen[0]['status'],'INSUFFICIENT_REPEATS')


if __name__=='__main__':
    unittest.main(verbosity=2)
