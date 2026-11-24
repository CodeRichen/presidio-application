#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline, no-model, no-network unit tests for V6673."""
from __future__ import annotations
import io
import json
from pathlib import Path
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from v6673_adapter_probe import compare_without_text, mismatch_kind,provider_gate
from v6673_diagnose import (only_allowlisted_child_data, run_child, status,
                            write_reports,validate)
from v667_bridge import strict_words_equal,send_frame,read_frame
from v667_gpu_worker import identify_model,force_cpu


def word(text='ABC',line=0,left=1):
    return {'text':text,'left':left,'top':2.,'width':3.,'height':4.,'line_id':line}

class Characterization(unittest.TestCase):
    def test_exact(self):
        x=compare_without_text([word()],[word()])
        self.assertTrue(x['exact_adapter_equal'])
        self.assertEqual(x['hypothesis'],'NO_DIFFERENCE_IN_THIS_PAIR')
    def test_split_words_same_stream(self):
        a=[word('SYN'),word('THETIC')];b=[word('SYNTHETIC')]
        x=compare_without_text(a,b)
        self.assertEqual(x['mismatch_kind'],'WORD_COUNT_DIFFERENCE')
        self.assertTrue(x['ordered_text_equal_after_whitespace_removal'])
        self.assertEqual(x['cpu']['word_count'],2)
        self.assertEqual(x['hybrid']['word_count'],1)
        self.assertEqual(x['hypothesis'],'POSSIBLE_SEGMENTATION_OR_LINE_GROUPING_DIFFERENCE')
    def test_word_counts_differ_and_text_differs(self):
        x=compare_without_text([word('ABC'),word('DEF')],[word('ABC')])
        self.assertFalse(x['ordered_text_equal_after_whitespace_removal'])
        self.assertIn('CONFIG_OR_FILTERING',x['hypothesis'])
    def test_internal_whitespace_compacted(self):
        x=compare_without_text([word('A  B')],[word('A'),word('B')])
        self.assertEqual(x['cpu']['words_with_internal_whitespace'],1)
        self.assertTrue(x['ordered_text_equal_after_whitespace_removal'])
    def test_order_changed_is_not_equal(self):
        x=compare_without_text([word('AB'),word('CD')],[word('CD'),word('AB')])
        self.assertFalse(x['ordered_text_equal_after_whitespace_removal'])
    def test_different_line_id(self):
        x=compare_without_text([word('XYZ')],[word('XYZ',line=1)])
        self.assertEqual(x['mismatch_kind'],'LINE_ID_DIFFERENCE')
        self.assertEqual(x['cpu']['line_id_groups'],1)
    def test_different_box(self):
        x=compare_without_text([word('XYZ')],[word('XYZ',left=100)])
        self.assertEqual(x['mismatch_kind'],'BOX_COORDINATE_DIFFERENCE')
        self.assertTrue(x['ordered_text_equal_after_whitespace_removal'])
    def test_different_text(self):
        self.assertEqual(mismatch_kind([word('abc')],[word('xyz')]),'OCR_TEXT_DIFFERENCE')
    def test_missing_schema(self):
        x=compare_without_text([{'text':'TOP SECRET'}],[word()])
        self.assertFalse(x['cpu_schema_ok'])
        self.assertNotIn('cpu',x)
    def test_invalid_adapter(self):
        x=compare_without_text('SECRET',[word()])
        self.assertEqual(x['mismatch_kind'],'INVALID_ADAPTER_TYPE')
    def test_empty_lists(self):
        x=compare_without_text([],[])
        self.assertEqual(x['mismatch_kind'],'EMPTY_OCR_OUTPUT')
        self.assertFalse(x['exact_adapter_equal'])
    def test_no_text_in_returned_dict(self):
        x=compare_without_text([word('SENSITIVE_TOKEN_123')],[word('OTHER_STRING')])
        self.assertNotIn('SENSITIVE_TOKEN_123',json.dumps(x))
        self.assertNotIn('OTHER_STRING',json.dumps(x))
        self.assertNotIn('left',json.dumps(x))
    def test_bool_not_valid_coordinate(self):
        x=word();x['top']=True
        self.assertFalse(compare_without_text([x],[word()])['cpu_schema_ok'])

class ProviderTests(unittest.TestCase):
    good={'sessions':{'det':1,'cls':1,'rec':1},
          'profiles_read':3,
          'node_events':{'det_cuda':1,'cls_cpu':5,'rec_cpu':6}}
    def test_good(self):self.assertTrue(provider_gate(self.good))
    def test_cls_cuda_rejected(self):
        x={'sessions':self.good['sessions'],'profiles_read':3,
           'node_events':dict(self.good['node_events'],cls_cuda=1)}
        self.assertFalse(provider_gate(x))
    def test_incomplete(self):
        self.assertFalse(provider_gate({'sessions':{'det':1},'profiles_read':1}))
    def test_classify_status_good(self):
        self.assertEqual(status({'status':'ADAPTER_PREFLIGHT_OK','return_code':0,
                                 'pairs_equal':2,'provider_route_verified':True}),
                         'ADAPTER_PREFLIGHT_OK')
    def test_classify_status_mismatch(self):
        self.assertEqual(status({'status':'OCR_ADAPTER_MISMATCH_CHARACTERIZED'}),
                         'OCR_ADAPTER_MISMATCH_CHARACTERIZED')
    def test_invalid_exit(self):
        self.assertEqual(status({'status':'ADAPTER_PREFLIGHT_OK','return_code':2,
                                 'pairs_equal':2,'provider_route_verified':True}),
                         'DIAGNOSTIC_FAILED')

class Reports(unittest.TestCase):
    def test_only_allowlist(self):
        raw={'schema':'pii_v6673_adapter_probe_v1','status':'OCR_ADAPTER_MISMATCH_CHARACTERIZED',
             'raw_ocr_text':'SENSITIVE_TOKEN_123',
             'pairs':[{'zoom':2,'raw_text':'SENSITIVE_TOKEN_123',
                       'mismatch_kind':'WORD_COUNT_DIFFERENCE',
                       'cpu':{'word_count':20,'word_dump':'SENSITIVE_TOKEN_123'},
                       'hybrid':{'word_count':10},
                       'hypothesis':'POSSIBLE_SEGMENTATION_OR_LINE_GROUPING_DIFFERENCE'}]}
        out=only_allowlisted_child_data(raw)
        self.assertNotIn('SENSITIVE_TOKEN_123',json.dumps(out))
        self.assertEqual(out['pairs'][0]['cpu']['word_count'],20)
    def test_invalid_schema(self):
        out=only_allowlisted_child_data({'schema':'wrong','raw':'secret'})
        self.assertEqual(out,{'status':'INVALID_CHILD_SCHEMA'})
    def test_md_json_no_private_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            result={'status':'OCR_ADAPTER_MISMATCH_CHARACTERIZED',
                    'probe':{'stage':'COMPLETE','pairs_checked':1,'pairs_equal':0,
                             'pairs':[{'zoom':2,'mismatch_kind':'WORD_COUNT_DIFFERENCE',
                                      'cpu':{'word_count':7,'line_id_groups':4},
                                      'hybrid':{'word_count':4,'line_id_groups':4},
                                      'ordered_text_equal_after_whitespace_removal':True,
                                      'hypothesis':'POSSIBLE_SEGMENTATION_OR_LINE_GROUPING_DIFFERENCE'}]}}
            write_reports(Path(tmp),result)
            x=(Path(tmp)/'v6673_adapter_diagnosis_summary.md').read_text(encoding='utf8')
            self.assertIn('7 | 4',x)
            self.assertNotIn('SECRET_RAW',x)
    def test_timeout_no_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('subprocess.run',side_effect=subprocess.TimeoutExpired('x',30)):
                got=run_child(Path('python'),Path(tmp),Path(tmp),Path('gpu'),Path(tmp)/'none',30)
            self.assertEqual(got['status'],'PROBE_TIMEOUT')
    def test_spawn_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch('subprocess.run',side_effect=OSError('safe')):
                got=run_child(Path('python'),Path(tmp),Path(tmp),Path('gpu'),Path(tmp)/'none',30)
            self.assertEqual(got['return_code'],'SPAWN_ERROR')
    def test_verify_sha_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            tool=root/'PII_V6673_Adapter_Diagnostics';tool.mkdir()
            with self.assertRaises(RuntimeError):validate(root,tool)
    def test_bridge_protocol(self):
        data=io.BytesIO()
        send_frame(data,b'synthetic')
        data.seek(0)
        self.assertEqual(read_frame(data),b'synthetic')
    def test_strict_equality_never_loosened(self):
        self.assertFalse(strict_words_equal([word('SYN'),word('THETIC')],[word('SYNTHETIC')]))
    def test_worker_model_routing(self):
        self.assertEqual(identify_model('model_det.onnx'),'det')
        self.assertEqual(identify_model('model_rec.onnx'),'rec')
        self.assertEqual(force_cpu((),{})[1]['providers'],['CPUExecutionProvider'])

if __name__=='__main__': unittest.main(verbosity=2)
