"""Offline tests: no OCR backend required; no user documents needed."""
from __future__ import annotations
import hashlib,subprocess,sys,tempfile,unittest
from pathlib import Path
try: import pymupdf as fitz
except ImportError: import fitz
from v63_fixture import build,render,RECIPES,WIDTH,HEIGHT
from v63_experiment import summarize,verdict,sha256,render_markdown
from v63_child import classified
from v63_policy import Features,decide,coverage,audit_gt

class TestV63(unittest.TestCase):
    def test_policy_stays_conservative_for_small_word(self):
        decision,rs=decide(Features(words=50,min_word_height_pt=7,primary_pii_boxes=3,contrast_std=40,edge_mean=10))
        self.assertFalse(decision)
        self.assertIn('small_or_missing_2x_words',rs)

    def test_policy_may_skip_healthy_page(self):
        decision,_=decide(Features(words=50,min_word_height_pt=13,primary_pii_boxes=3,contrast_std=40,edge_mean=10))
        self.assertTrue(decision)

    def test_genuine_rescue_and_dangerous_skip(self):
        gt=[{'label':'EMAIL','role':'probe','bbox_pdf':[100,100,200,120]}]
        extra=[(98,98,202,123,'EMAIL','SYNTHETIC',None,'mock')]
        rows=classified(gt,[],extra,extra,True)
        self.assertTrue(rows[0]['rescue_by_3x'])
        self.assertTrue(rows[0]['dangerous_skip'])
        self.assertFalse(rows[0]['candidate_pass'])

    def test_conservative_rescue_not_dangerous(self):
        gt=[{'label':'EMAIL','role':'probe','bbox_pdf':[100,100,200,120]}]
        extra=[(98,98,202,123,'EMAIL','SYNTHETIC',None,'mock')]
        rows=classified(gt,[],extra,extra,False)
        self.assertTrue(rows[0]['rescue_by_3x'])
        self.assertFalse(rows[0]['dangerous_skip'])
        self.assertTrue(rows[0]['candidate_pass'])

    def test_wrong_label_not_cover_gt(self):
        self.assertEqual(coverage([10,10,30,30],[(10,10,30,30,'PERSON')],'EMAIL'),0)

    def test_fixture_geometry_and_no_text_layer(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'test.pdf'
            gt=build(path,count=3,seed=7,split='discovery')
            self.assertEqual(len(gt),3)
            with fitz.open(path) as f:
                self.assertEqual(len(f),3)
                self.assertTrue(all(not p.get_text('text').strip() for p in f))
            for g in gt:
                self.assertGreaterEqual(len(g['expected_text']),4)
                for r in g['expected_text']:
                    self.assertLess(r['bbox_pdf'][0],r['bbox_pdf'][2])
                    self.assertLess(r['bbox_pdf'][1],r['bbox_pdf'][3])
                    self.assertGreaterEqual(r['bbox_pdf'][0],0)
                    self.assertLessEqual(r['bbox_pdf'][2],WIDTH/2)
                    self.assertLessEqual(r['bbox_pdf'][3],HEIGHT/2)

    def test_reproducible_fixture(self):
        with tempfile.TemporaryDirectory() as td:
            p1,p2=Path(td)/'a.pdf',Path(td)/'b.pdf'
            a=build(p1,count=2,seed=1234,split='holdout')
            b=build(p2,count=2,seed=1234,split='holdout')
            self.assertEqual(a,b)
            with fitz.open(p1) as x,fitz.open(p2) as y:
                for u,v in zip(x,y):
                    self.assertEqual(u.get_pixmap().samples,v.get_pixmap().samples)

    def test_separate_holdout(self):
        im1,meta1=render(0,123,split='discovery')
        im2,meta2=render(0,123,split='holdout')
        self.assertNotEqual(im1.tobytes(),im2.tobytes())

    def test_verdict_no_rescue_not_success(self):
        groups={'discovery':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':0}},
                'holdout':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':0}}}
        self.assertEqual(verdict(groups),'NO_CONFIRMED_3X_RESCUES')

    def test_verdict_unsafe_overrides_gaps(self):
        groups={'discovery':{'status':'RECORDED','summary':{'dangerous_skips':1,'dual_gt_gaps':2,'rescues_in_probes':1}},
                'holdout':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':0}}}
        self.assertEqual(verdict(groups),'UNSAFE_SKIP_FOUND')

    def test_verdict_baseline_gaps(self):
        groups={'discovery':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':2,'rescues_in_probes':1}},
                'holdout':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':1}}}
        self.assertEqual(verdict(groups),'BASELINE_GROUND_TRUTH_GAPS')

    def test_verdict_rescues_both_shadows_only(self):
        groups={'discovery':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':2}},
                'holdout':{'status':'RECORDED','summary':{'dangerous_skips':0,'dual_gt_gaps':0,'rescues_in_probes':1}}}
        self.assertEqual(verdict(groups),'RESCUES_OBSERVED_SHADOW_ONLY')

    def test_summary_counts(self):
        row={'gt_count':4,'gt_probe_count':1,'zoom2_gt_pass':3,'dual_gt_pass':4,
           'candidate_gt_pass':3,'rescues_total':1,'rescues_in_probes':1,
           'dangerous_skips':1,'dual_gt_gaps':0,'would_skip_3x':True,
           'omitted_3x_s_estimate':10.5,'zoom2_s':6.2,'zoom3_s':10.5}
        s=summarize([row,row])
        self.assertEqual(s['dangerous_skips'],2)
        self.assertEqual(s['rescues_in_probes'],2)
        self.assertEqual(s['selective_3x_saved_s_estimate'],21.)

    def test_markdown_no_ocr_values(self):
        rep={'status':'NO_CONFIRMED_3X_RESCUES','groups':{'holdout':{'status':'RECORDED','summary':{'pages':1,'ground_truth_total':4,'zoom2_gt_pass':3,'dual_gt_pass':3,'rescues_in_probes':0,'dangerous_skips':0,'fallback_skipped_estimated':0},'cases':[]}}}
        md=render_markdown(rep)
        self.assertNotIn('demo@example.com',md)
        self.assertIn('NO_CONFIRMED_3X_RESCUES',md)

if __name__=='__main__':unittest.main(verbosity=2)
