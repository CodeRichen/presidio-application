import unittest
import sys
from pathlib import Path

HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from set_rapid_threads import patch_content
from run_rapid_speed import configs, summarize

SAMPLE='''import os
import threading
_RAPID=None
_RAPID_LOCK=threading.Lock()
def _rapid_engine():
    global _RAPID
    if _RAPID is not None:
        return _RAPID
    with _RAPID_LOCK:
        if _RAPID is None:
            from rapidocr import RapidOCR
            _RAPID = RapidOCR()
    return _RAPID

def normal():
    from rapidocr import RapidOCR
    engine = RapidOCR()
    return engine
'''
class TestPatch(unittest.TestCase):
    def test_4_threads(self):
        updated,changed=patch_content(SAMPLE,4)
        self.assertTrue(changed)
        self.assertIn('DEFAULT_RAPID_CPU_THREADS = 4',updated)
        compile(updated,'test.py','exec')
        self.assertIn('engine = RapidOCR()',updated)
    def test_reapply(self):
        s,_=patch_content(SAMPLE,4)
        t,changed=patch_content(s,4)
        self.assertEqual(s,t)
        self.assertFalse(changed)
        t,changed=patch_content(s,6)
        self.assertTrue(changed)
        self.assertEqual(t.count('# RAPID_SPEED_PROFILE_BEGIN'),1)
        self.assertIn('DEFAULT_RAPID_CPU_THREADS = 6',t)
        compile(t,'test.py','exec')
    def test_windows_newline(self):
        s,_=patch_content(SAMPLE.replace('\n','\r\n'),8)
        self.assertIn('\r\n',s)
        compile(s,'test.py','exec')
    def test_reject_unknown(self):
        with self.assertRaises(ValueError):patch_content(SAMPLE,3)
    def test_missing_engine(self):
        with self.assertRaises(ValueError):patch_content('def _rapid_engine():\n    pass\n',4)
    def test_double_engine_failclosed(self):
        s=SAMPLE.replace('            _RAPID = RapidOCR()', '            _RAPID = RapidOCR()\n            _RAPID = RapidOCR()')
        with self.assertRaises(ValueError):patch_content(s,4)
    def test_profiles(self):
        self.assertEqual(configs('baseline'),{})
        self.assertEqual(configs('cpu4')['EngineConfig.onnxruntime.intra_op_num_threads'],4)
        self.assertEqual(configs('cpu4_no_cls')['Global.use_cls'],False)
    def test_summary_quality_gate(self):
        items=[]
        for profile,covered,seconds,loss in [('baseline',4,5,0.07),('cpu4',4,2,0.07),('cpu8',3,1,0.07)]:
            items.append({'profile':profile,'case':'x','condition':'clear','error':None,'targets':4,'covered':covered,'target_flags':{'name':True,'phone':covered==4},'ocr_seconds':seconds,'pii_seconds':1,'safe_ink_fraction':loss})
        results=summarize(items,['baseline','cpu4','cpu8'])
        self.assertTrue(results[1]['passes_baseline_gate'])
        self.assertFalse(results[2]['passes_baseline_gate'])
        self.assertEqual(results[1]['speedup'],2.5)
    def test_one_target_regression_even_if_total_same(self):
        a={'profile':'baseline','case':'x','condition':'clear','error':None,'targets':2,'covered':1,'target_flags':{'a':True,'b':False},'ocr_seconds':5,'pii_seconds':1,'safe_ink_fraction':.07}
        b={**a,'profile':'cpu4','target_flags':{'a':False,'b':True},'ocr_seconds':2}
        self.assertFalse(summarize([a,b],['baseline','cpu4'])[1]['passes_baseline_gate'])

    def test_quality_safe_guard(self):
        items=[{'profile':p,'case':'x','condition':'clear','error':None,'targets':4,'covered':4,'target_flags':{'name':True,'phone':True},'ocr_seconds':n,'pii_seconds':1,'safe_ink_fraction':loss} for p,n,loss in [('baseline',5,.01),('cpu4',2,.1)]]
        self.assertFalse(summarize(items,['baseline','cpu4'])[1]['passes_baseline_gate'])
if __name__=='__main__':unittest.main()
