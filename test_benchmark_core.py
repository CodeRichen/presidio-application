"""Pure unit tests; run without NER models or OCR engines."""
import tempfile
import unittest
from pathlib import Path
from PIL import Image, ImageDraw
from benchmark_lib import compact,ocr_joined_text,mask_from_boxes, fraction_covered,union_masks,score_one_case

class ScoringTests(unittest.TestCase):
    def test_normalized_email(self):
        self.assertEqual(compact("ＤＥＭＯ＠ＥＸＡＭＰＬＥ．ＣＯＭ"),"demo@example.com")
    def test_not_silent_phone_substitution(self):
        self.assertNotEqual(compact("0912-345一678"),compact("0912-345-678"))
    def test_join_ocr_words(self):
        words=[{"line_id":0,"text":"林"},{"line_id":0,"text":"家宇"},
               {"line_id":1,"text":"demo@"},{"line_id":1,"text":"example.com"}]
        self.assertEqual(ocr_joined_text(words),"林家宇demo@example.com")
    def test_correct_bbox_covers_target(self):
        m=Image.new("L",(100,70),0)
        ImageDraw.Draw(m).rectangle((10,10,30,20),fill=255)
        red=mask_from_boxes((100,70),[(9,9,31,21)],0)
        self.assertEqual(fraction_covered(m,red),1.0)
    def test_unrelated_bbox_no_cover(self):
        m=Image.new("L",(100,70),0)
        ImageDraw.Draw(m).rectangle((10,10,30,20),fill=255)
        red=mask_from_boxes((100,70),[(45,30,60,50)],0)
        self.assertEqual(fraction_covered(m,red),0.0)
    def test_union_complementary(self):
        m=Image.new("L",(100,40),0)
        ImageDraw.Draw(m).rectangle((10,10,49,20),fill=255)
        a=mask_from_boxes((100,40),[(10,10,30,21)])
        b=mask_from_boxes((100,40),[(30,10,50,21)])
        self.assertLess(fraction_covered(m,a),1)
        self.assertLess(fraction_covered(m,b),1)
        self.assertEqual(fraction_covered(m,union_masks(a,b)),1)
    def test_score_case_using_fake_words_and_boxes(self):
        with tempfile.TemporaryDirectory() as directory:
            p=Path(directory)
            Image.new("L",(100,50),0).save(p/"safe.png")
            m=Image.new("L",(100,50),0)
            ImageDraw.Draw(m).rectangle((20,20,45,35),fill=255)
            m.save(p/"person.png")
            case={"targets":[{"id":"p","type":"PERSON","text":"林家宇","mask_path":"person.png"}],"safe_mask":"safe.png"}
            words=[{"line_id":0,"text":"林家宇"}]
            result=score_one_case(Image.new("RGB",(100,50)),case,p,words,[(18,18,47,37,"PERSON","林家宇",None,"regex")])
            self.assertTrue(result['targets'][0]['fully_covered'])
            self.assertTrue(result['targets'][0]['text_found'])

if __name__=="__main__":
    unittest.main()

# Manifest checks: verify that the shipped data is complete without loading any OCR model.
class DatasetManifestTests(unittest.TestCase):
    def test_manifest_36_images_144_targets(self):
        import json
        from collections import Counter
        path=Path(__file__).parent/'data'
        d=json.loads((path/'labels.json').read_text(encoding='utf-8'))
        self.assertTrue(d['synthetic_only'])
        self.assertEqual(len(d['cases']),36)
        self.assertEqual(sum(map(lambda c:len(c['targets']),d['cases'])),144)
        self.assertEqual(set(Counter(c['condition'] for c in d['cases']).values()),{4})
        for c in d['cases']:
            self.assertTrue((path/c['image']).is_file())
            self.assertTrue((path/c['safe_mask']).is_file())
            self.assertEqual(len(c['targets']),4)
            self.assertEqual(sorted(t['type'] for t in c['targets']),['EMAIL','PERSON','PERSON','TW_PHONE'])
            for t in c['targets']:
                self.assertTrue((path/t['mask_path']).is_file())
                self.assertGreater(t['bbox'][2]-t['bbox'][0],0)
                self.assertGreater(t['bbox'][3]-t['bbox'][1],0)

    def test_mask_and_image_size_match(self):
        import json
        path=Path(__file__).parent/'data'
        d=json.loads((path/'labels.json').read_text(encoding='utf-8'))
        for c in d['cases']:
            with Image.open(path/c['image']) as im:
                for t in c['targets']:
                    with Image.open(path/t['mask_path']) as mask:
                        self.assertEqual(im.size,mask.size)
                        self.assertIsNotNone(mask.getbbox())

    def test_whole_image_mask_is_not_mislabeled(self):
        import json
        path=Path(__file__).parent/'data'
        d=json.loads((path/'labels.json').read_text(encoding='utf-8'))
        for c in d['cases']:
            with Image.open(path/c['image']) as original:
                w,h=original.size
            for t in c['targets']:
                x0,y0,x1,y1=t['bbox']
                self.assertGreaterEqual(x0,0)
                self.assertGreaterEqual(y0,0)
                self.assertLessEqual(x1,w)
                self.assertLessEqual(y1,h)
                self.assertLess((x1-x0)*(y1-y0),w*h/3)
