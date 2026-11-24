"""Unit tests for the isolated V6.6.8 GUI safety trial, offline stdlib only."""
from __future__ import annotations
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock
import v668_gui as kit


def good(mode):
    trial = dict(mode=mode, safe=True, return_code=0, child_status='COMPLETE',
                 processing_s=180.0 if mode=='cpu' else 80.0,
                 text_regions_pass=27, face_map_boxes_pass=17,
                 pdf_structure_pass=True)
    if mode=='hybrid':
        trial['provider_evidence'] = {
            'sessions':{'det':1,'cls':1,'rec':1},
            'node_events':{'det_cuda':3040,'det_cpu':0,'cls_cpu':8702,
                           'cls_cuda':0,'rec_cpu':7410,'rec_cuda':0},
            'profiles_read':3}
    return trial


class Tests(unittest.TestCase):
    def test_known_names(self):
        self.assertEqual(kit.JSON_NAME, 'v668_gui_smoke_summary.json')
    def test_dest_guard(self):
        with TemporaryDirectory() as d:
            root=Path(d)
            with self.assertRaises(ValueError):kit.validate_destination(root,root)
            with self.assertRaises(ValueError):kit.validate_destination(root,root.parent)
            with self.assertRaises(ValueError):kit.validate_destination(root,root/'PII_V6676_Full_PDF_Hybrid_AB'/'x')
            self.assertEqual(kit.validate_destination(root,root/'result'),root/'result')
    def test_quantiles_empty(self):
        self.assertIsNone(kit.quantile([],.95))
    def test_quantiles(self):
        self.assertEqual(kit.quantile([0,4,12],.95),12)
    def test_redacted_only_whitelist(self):
        d=good('hybrid');d.update({'ocr_raw':'SECRET','file_path':'P:private','map':'SECRET'})
        record=kit.redacted_trial(d)
        self.assertFalse(any(x in str(record) for x in ('SECRET','P:private')))
        self.assertIn('provider_evidence',record)
    def test_cpu_no_providers(self):
        self.assertNotIn('provider_evidence',kit.redacted_trial(good('cpu')))
    def test_compilation(self):
        p={'status':'ISOLATED_GUI_AB_SAFE','pixel_identical':True,
           'cpu':kit.redacted_trial(good('cpu')),'hybrid':kit.redacted_trial(good('hybrid')),
           'worker_pair_elapsed_s':250.0}
        s=kit.compile_report(p,251,[2,3,4])
        self.assertEqual(s['comparison']['processing_improvement_pct'],55.56)
        self.assertTrue(s['not_production_gui'])
        self.assertEqual(s['gui']['max_schedule_delay_ms'],4)
    def test_gui_delay_review(self):
        p={'status':'ISOLATED_GUI_AB_SAFE','pixel_identical':True,'cpu':{},'hybrid':{}}
        self.assertEqual(kit.compile_report(p,1,[1,550])['gui']['responsiveness_review'],
                         'GUI_SCHEDULING_REVIEW')
    def test_fail_no_claim(self):
        p={'status':'SECURITY_OR_EXECUTION_FAILED','pixel_identical':False,
           'cpu':{},'hybrid':{}}
        self.assertIsNone(kit.compile_report(p,1,[2])['comparison']['processing_improvement_pct'])
    def test_report_no_extra(self):
        p={'status':'ISOLATED_GUI_AB_SAFE','pixel_identical':True,
           'cpu':kit.redacted_trial(good('cpu')),'hybrid':kit.redacted_trial(good('hybrid'))}
        summary=kit.compile_report(p,1,[1])
        with TemporaryDirectory() as d:
            kit.write_report(Path(d),summary)
            j=json.loads((Path(d)/kit.JSON_NAME).read_text(encoding='utf-8'))
            self.assertEqual(j['status'],'ISOLATED_GUI_AB_SAFE')
            self.assertIn('不是正式', (Path(d)/kit.MD_NAME).read_text(encoding='utf-8'))
    def test_pair_success_mock(self):
        b=Mock()
        b.run_trial.side_effect=lambda *args: good(args[8])
        b.strictly_identical_pages.return_value=True
        events=[]
        with TemporaryDirectory() as d:
            root=Path(d)
            out=kit.run_one_pair(b,root,root/'tool',('fixture','gate','gdir','expected','app','gpu'),
                                 events.append,60)
        self.assertEqual(out['status'],'ISOLATED_GUI_AB_SAFE')
        self.assertEqual([e['mode'] for e in events],['hybrid','cpu','pixel_check'])
        self.assertEqual(b.run_trial.call_count,2)
    def test_pair_stop_early_on_failure(self):
        b=Mock()
        bad=good('hybrid');bad['safe']=False
        b.run_trial.return_value=bad
        with TemporaryDirectory() as d:
            out=kit.run_one_pair(b,Path(d),Path(d)/'tool',
                                 ('fixture','gate','gdir','expected','app','gpu'),lambda x:None,60)
        self.assertEqual(out['status'],'SECURITY_OR_EXECUTION_FAILED')
        self.assertEqual(b.run_trial.call_count,1)
        b.strictly_identical_pages.assert_not_called()
    def test_pair_pixel_mismatch(self):
        b=Mock();b.run_trial.side_effect=lambda *args: good(args[8]);b.strictly_identical_pages.return_value=False
        with TemporaryDirectory() as d:
            out=kit.run_one_pair(b,Path(d),Path(d)/'tool',
                                 ('fixture','gate','gdir','expected','app','gpu'),lambda x:None,60)
        self.assertEqual(out['status'],'PDF_PIXEL_DIFFERENCE')
    def test_missing_kit(self):
        with TemporaryDirectory() as d:
            root=Path(d);kit_dir=root/'bad'
            with self.assertRaises(RuntimeError):kit.load_v6676(kit_dir,root,root/'x')

if __name__ == '__main__': unittest.main(verbosity=2)
