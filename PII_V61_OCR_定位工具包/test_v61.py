# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parent))
from v61_geometry import (rect_area,intersection_area,overlap_fraction_of_smaller,
                          normalized_box,expected_rect_to_pdf,safe_category,
                          LABELS,SOURCES,describe_added_boxes)
import v61_locate as cli


class V61Tests(unittest.TestCase):
    def setUp(self):
        self.sample={'size_px':[2000,1000], 'expected_text':[{'label':'PERSON','bbox_px':[1000,200,1200,250]}]}

    def test_area_and_intersection(self):
        self.assertEqual(rect_area((0,0,10,10)),100)
        self.assertEqual(intersection_area((0,0,10,10),(5,5,15,15)),25)

    def test_overlap_fraction_of_smaller(self):
        self.assertAlmostEqual(overlap_fraction_of_smaller((0,0,10,10),(5,5,10,10)),1)
        self.assertEqual(overlap_fraction_of_smaller((0,0,1,1),(5,5,10,10)),0)

    def test_normalized_percent(self):
        self.assertEqual(normalized_box((50,10,100,20),200,100),[25.,10.,50.,20.])

    def test_expected_coordinates(self):
        self.assertEqual(expected_rect_to_pdf((1000,200,1200,250),2000,1000,1000,500),(500,100,600,125))

    def test_safe_categories(self):
        self.assertEqual(safe_category('EMAIL',LABELS),'EMAIL')
        self.assertEqual(safe_category('demo@example.org',SOURCES),'OTHER')
        self.assertEqual(safe_category('secret marker',LABELS),'OTHER')

    def test_diagnostic_does_not_emit_raw_values(self):
        secret='SUPER_FAKE_PII_SHOULD_NOT_LEAK'
        b=(500,100,600,125,'PERSON',secret,0.94,'regex')
        rows=describe_added_boxes([], [b],self.sample,1000,500)
        x=json.dumps(rows)
        self.assertNotIn(secret,x)
        self.assertEqual(rows[0]['label'],'PERSON')
        self.assertEqual(rows[0]['overlap_known_text_label'],'PERSON')
        self.assertEqual(rows[0]['bbox_percent_xyxy'],[50.,20.,60.,25.])

    def test_outside_ground_truth_is_review_not_safe(self):
        b=(100,100,150,125,'EMAIL','secret@example.org',None,'weird unsafe source')
        rows=describe_added_boxes([], [b],self.sample,1000,500)
        self.assertIsNone(rows[0]['overlap_known_text_label'])
        self.assertEqual(rows[0]['source_type'],'OTHER')
        self.assertEqual(rows[0]['review'],'MANUAL_REVIEW_REQUIRED')

    def test_markdown_safe_and_explicit_manual_review(self):
        report={'status':'MANUAL_REVIEW_REQUIRED','primary_boxes':10,'fallback_boxes':11,
                'additional_count':1,'count_consistent_with_v6':True,
                'primary_ocr_s':1.2,'fallback_ocr_s':3.4,
                'additional_3x_boxes':describe_added_boxes([],[(1,2,3,4,'PERSON','hidden private',None,'regex')],self.sample,1000,500)}
        md=cli.markdown(report)
        self.assertNotIn('hidden private',md)
        self.assertIn('MANUAL_REVIEW_REQUIRED',md)

    def test_fixture_hash_rejects_wrong_data(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td); d=p/'PII_Face_PDF_7page_Test'; d.mkdir()
            (d/'pii_face_7page_scan_source.pdf').write_bytes(b'not a real fixture')
            with self.assertRaises(ValueError): cli.setup(p)

    def test_empty_and_invalid_box(self):
        self.assertEqual(describe_added_boxes([],[],self.sample,1000,500),[])
        with self.assertRaises(ValueError):
            describe_added_boxes([],[(0,0,0,0,'PERSON','hidden',None,'regex')],self.sample,1000,500)

if __name__=='__main__':unittest.main(verbosity=2)
