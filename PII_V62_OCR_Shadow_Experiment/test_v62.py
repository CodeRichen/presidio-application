import json
from pathlib import Path
import tempfile
import unittest
from v62_core import Features,decide,union_area,coverage,audit_gt,metrics_from_words
from v62_fixture import build
from v62_experiment import summarize_cases,sha256

class CoreTests(unittest.TestCase):
    def test_policy_conservative_small(self):
        skip,reasons=decide(Features(20,4.5,3,75,24))
        self.assertFalse(skip)
        self.assertIn('small_or_missing_2x_words', reasons)
    def test_policy_conservative_no_words(self):
        self.assertFalse(decide(Features(0,0,0,20,2))[0])
    def test_policy_risky_low_contrast(self):
        self.assertFalse(decide(Features(20,12,3,10,26))[0])
    def test_policy_eligible_experiment(self):
        self.assertTrue(decide(Features(21,15,4,70,28))[0])
    def test_area_overlap(self):
        self.assertAlmostEqual(union_area([(0,0,7,10),(4,0,10,10)],(0,0,10,10)),100)
    def test_label_matters(self):
        self.assertEqual(coverage((0,0,10,10),[(0,0,10,10,'PHONE')],'PERSON'),0)
    def test_gt_union(self):
        gt=[{'label':'EMAIL','bbox_pdf':[0,0,10,10]}]
        b=[(0,0,5,10,'EMAIL'),(5,0,10,10,'EMAIL')]
        self.assertEqual(audit_gt(gt,b)['pass_count'],1)
    def test_word_stats_no_pii(self):
        self.assertEqual(metrics_from_words([{'height':24,'text':'SECRET'}],2.),(1,12.))
    def test_fixture_raster_pdf(self):
        import fitz
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'synthetic.pdf'
            cases=build(p)
            self.assertEqual(len(cases),8)
            self.assertEqual(sum(len(q['expected_text']) for q in cases),30)
            with fitz.open(str(p)) as f:
                self.assertEqual(len(f),8)
                self.assertTrue(all(not page.get_text('text').strip() and len(page.get_images(full=True))==1 for page in f))
                self.assertGreater(len(sha256(p)),50)
    def test_child_mocked_end_to_end(self):
        import hashlib,sys,types
        from unittest.mock import patch
        from PIL import Image
        import v62_child
        fake=types.ModuleType('handler_media')
        fake._pii_media_cache_scope=lambda fn: fn
        fake.ocr_words=lambda image: [{'text':'synthetic', 'height':26} for _ in range(10)]
        def collect(page,zoom):
            fake.ocr_words(None)
            return [], Image.new('RGB',(100,80),'white')
        fake._collect_pdf_ocr_boxes=collect
        fake._merge_pdf_multizoom_boxes=lambda a,b:a+b
        with tempfile.TemporaryDirectory() as d:
            fixture=Path(d)/'synthetic.pdf'
            gt=build(fixture)
            target=Path(d)/'gt.json';target.write_text(json.dumps(gt,ensure_ascii=False),encoding='utf8')
            report=Path(d)/'out.json'
            args=['v62_child.py','--root',d,'--fixture',str(fixture),'--gt',str(target),
                  '--sha256',hashlib.sha256(fixture.read_bytes()).hexdigest(),'--report',str(report)]
            with patch.dict(sys.modules, {'handler_media':fake}):
                with patch.object(sys,'argv',args):
                    v62_child.main()
            data=json.loads(report.read_text(encoding='utf8'))
            self.assertEqual(len(data['cases']),8)
            self.assertEqual(data['cases'][0]['ground_truth_count'],3)
            self.assertNotIn('example.com',report.read_text(encoding='utf8'))

    def test_summary(self):
        row={'ground_truth_count':3,'dual':{'pass_count':3},'zoom2':{'pass_count':2},
             'selective_shadow':{'pass_count':3},'would_skip_3x':False,'3x_extra_boxes':1,
             'zoom2_s':1,'zoom3_s':2,'omitted_3x_s_estimate':0,'new_miss_against_dual':0}
        self.assertEqual(summarize_cases([row])['dual_gt_pass'],3)

if __name__=='__main__':unittest.main(verbosity=2)
