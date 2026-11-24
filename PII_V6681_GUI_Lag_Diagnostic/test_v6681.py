# -*- coding: utf-8 -*-
"""Offline unit tests for V6.6.8.1. No model, source PDF, or PII is accessed."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import v6681_gui as d


class DiagnosticTests(unittest.TestCase):
    def test_quantile_empty(self): self.assertIsNone(d.quantile([], .95))
    def test_quantile_95(self): self.assertEqual(d.quantile([1, 2, 3, 4, 5], .95), 5.)
    def test_quantile_50(self): self.assertEqual(d.quantile([1, 2, 3], .5), 2.)
    def test_quantile_order_invariant(self): self.assertEqual(d.quantile([5,1,3], .5), 3.)
    def test_quantile_no_mutation(self):
        v=[3,1,2]; d.quantile(v,.5); self.assertEqual(v,[3,1,2])

    def test_heartbeat_empty(self):
        s=d.heartbeat_statistics([], 'hybrid', 1)
        self.assertIsNone(s['max_lag_ms'])
        self.assertEqual(s['sample_count'], 0)
    def test_heartbeat_spike_review(self):
        s=d.heartbeat_statistics([(1,'GPU_FULL_PDF',3),(2,'GPU_FULL_PDF',965.2)],'hybrid',2)
        self.assertEqual(s['lag_count_above_ms']['500'],1)
        self.assertEqual(s['responsiveness_review'],'GUI_SCHEDULING_REVIEW')
        self.assertEqual(s['largest_spikes'][0]['observed_stage'],'GPU_FULL_PDF')
    def test_heartbeat_no_review(self):
        s=d.heartbeat_statistics([(1,'GUI_IDLE',0),(2,'GUI_IDLE',12)],'idle',2)
        self.assertEqual(s['lag_count_above_ms']['500'],0)
        self.assertEqual(s['responsiveness_review'],'NO_OVER_500MS_SPIKE_OBSERVED')
    def test_heartbeat_invalid_stage_redacted(self):
        s=d.heartbeat_statistics([(1,'OCR_王小明',600)],'hybrid',1)
        self.assertEqual(s['largest_spikes'][0]['observed_stage'],'READY')
    def test_heartbeat_only_12_spikes(self):
        s=d.heartbeat_statistics([(i,'READY',900+i) for i in range(30)], 'hybrid', 30)
        self.assertEqual(len(s['largest_spikes']),12)
    def test_heartbeat_250_threshold(self):
        s=d.heartbeat_statistics([(1,'READY',250),(2,'READY',250.1)],'idle',2)
        self.assertEqual(s['lag_count_above_ms']['250'],1)

    def test_compile_idle(self):
        r=d.compile_report('idle',None,30,[],1)
        self.assertEqual(r['status'],'IDLE_GUI_MEASURED')
        self.assertIsNone(r['trial'])
    def test_compile_safe(self):
        r=d.compile_report('hybrid', {'safe':True},138,[],1)
        self.assertEqual(r['status'],'SINGLE_MODE_SYNTHETIC_SAFETY_PASS')
        self.assertFalse(r['pdf_pixel_equality_checked_in_this_run'])
    def test_compile_fail(self):
        r=d.compile_report('cpu', {'safe':False},181,[],1)
        self.assertEqual(r['status'],'SINGLE_MODE_EXECUTION_OR_SAFETY_FAILED')
    def test_invalid_mode(self):
        with self.assertRaises(ValueError):d.compile_report('real_pdf',None,1,[],1)
    def test_allowlist_no_pii(self):
        raw={'mode':'hybrid','safe':True,'OCR_TEXT':'王小明','document_path':'secret.pdf',
             'provider_evidence':{'sessions':{'det':1,'cls':1,'rec':1},
                                  'node_events':{'det_cuda':1,'cls_cpu':1,'rec_cpu':1},
                                  'profiles_read':3}}
        r=d.allowlisted_trial(raw)
        self.assertNotIn('OCR_TEXT',r)
        self.assertNotIn('document_path',r)
        self.assertEqual(r['provider_evidence']['sessions']['det'],1)
    def test_allowlist_cpu_no_provider(self):
        self.assertNotIn('provider_evidence',d.allowlisted_trial({'mode':'cpu','safe':True}))
    def test_execute_single_invalid_mode(self):
        with self.assertRaises(ValueError):d.execute_single(None,None,None,None,'real',1)
    def test_execute_single_mock_safe_and_auto_cleanup(self):
        mock=MagicMock()
        mock.run_trial.return_value={'mode':'hybrid','safe':True,'processing_s':77,
                                     'document_path':'SHOULD_NOT_APPEAR'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td); setup=('fixture','gate','dir','expected','app','gpu')
            result=d.execute_single(mock,root,'tool',setup,'hybrid',60)
            self.assertTrue(result['safe'])
            self.assertNotIn('document_path',result)
            target=mock.run_trial.call_args.args[-2]
            self.assertFalse(target.exists())
    def test_destination_inside(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            self.assertEqual(d.validate_destination(p,p/'results'),(p/'results').resolve())
    def test_destination_reject_root(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            with self.assertRaises(ValueError):d.validate_destination(p,p)
    def test_destination_reject_external(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)/'project';root.mkdir()
            with self.assertRaises(ValueError):d.validate_destination(root,Path(td)/'outside')
    def test_destination_reject_protected(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            with self.assertRaises(ValueError):d.validate_destination(p,p/'.venv'/'foo')
    def test_write_report_privacy_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)
            r=d.compile_report('hybrid',{'safe':True,'mode':'hybrid','processing_s':77},138,
                               [(1,'GPU_FULL_PDF',965)],1)
            a,b=d.write_report(p,r)
            self.assertTrue(a.is_file() and b.is_file())
            data=a.read_text(encoding='utf8')
            self.assertNotIn('PII_',data)
            self.assertNotIn('SHOULD_NOT_APPEAR',data)
            self.assertEqual(d.next_run_index(p),2)
            with self.assertRaises(FileExistsError):d.write_report(p,r)
    def test_run_index_empty(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(d.next_run_index(Path(td)),1)

if __name__=='__main__':
    unittest.main(verbosity=2)
