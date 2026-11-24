#!/usr/bin/env python3
# coding: utf-8
"""No-model, no-private-data unit tests."""
import unittest
from v652_benchmark import classify

class ClassificationTests(unittest.TestCase):
    def run_pair(self, a=200, b=160, da=32, db=23, base_threads=12, candidate_threads=4):
        baseline=[dict(safe=True,elapsed_s=a,code_detect_wall_s=da,torch_effective=base_threads)]*2
        candidate=[dict(safe=True,elapsed_s=b,code_detect_wall_s=db,torch_effective=candidate_threads)]*2
        pixels=[dict(all_identical=True,pages_compared=7)]*2
        return baseline,candidate,pixels
    def test_promising(self):
        a,b,p=self.run_pair()
        self.assertEqual(classify(a,b,p,4)['status'],'PROMISING_AB_ONLY')
    def test_no_gain(self):
        a,b,p=self.run_pair(b=198)
        self.assertEqual(classify(a,b,p,4)['status'],'NO_CONFIRMED_GAIN')
    def test_no_detect_gain(self):
        a,b,p=self.run_pair(db=35)
        self.assertEqual(classify(a,b,p,4)['status'],'NO_CONFIRMED_GAIN')
    def test_pixel_failure(self):
        a,b,p=self.run_pair();p[0]['all_identical']=False
        self.assertEqual(classify(a,b,p,4)['status'],'PIXEL_DIFFERENCE')
    def test_tuned_not_applied(self):
        a,b,p=self.run_pair();b[0]['torch_effective']=10
        self.assertEqual(classify(a,b,p,4)['status'],'TUNING_NOT_APPLIED')
    def test_baseline_same_as_candidate(self):
        a,b,p=self.run_pair(base_threads=4)
        self.assertEqual(classify(a,b,p,4)['status'],'NO_EFFECTIVE_CHANGE')
    def test_security_gate(self):
        a,b,p=self.run_pair();b[0]['safe']=False
        self.assertEqual(classify(a,b,p,4)['status'],'SECURITY_GATE_FAILED')
    def test_incomplete(self):
        a,b,p=self.run_pair();self.assertEqual(classify(a[:1],b,p,4)['status'],'INCOMPLETE')
    def test_pixel_page_count(self):
        a,b,p=self.run_pair();p[0]['pages_compared']=6
        self.assertEqual(classify(a,b,p,4)['status'],'PIXEL_DIFFERENCE')

if __name__=='__main__': unittest.main(verbosity=2)
