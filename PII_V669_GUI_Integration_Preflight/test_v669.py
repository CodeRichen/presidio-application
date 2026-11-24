#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pure, dependency-free tests for V6.6.9 source-only scanner."""
from __future__ import annotations
import ast
import json
from pathlib import Path
import tempfile
import unittest
import v669_preflight as d

UI = '''
import threading
class App:
    def run_files(self):
        return core.dispatch_files(paths, self.mapping)
    def _start(self):
        with hotkey_service.WORK_LOCK:
            t = threading.Thread(target=worker)
        self.q.put(('done', None, None))
    def _poll(self):
        if what == 'done':
            pass
'''
CORE = '''
def dispatch_files(paths, mapping):
    try:
        handler_media.mask_file('synthetic.pdf')
    except Exception:
        continue
'''
MEDIA = '''
def mask_pdf(): pass
def mask_image(): pass
def mask_file(): pass
def ocr_words():
    return ocr_words_with_backend(a,b)
'''
BACKEND = '''
def rapid_ocr_words():
    result = engine(image, return_word_box=True)
    return rapid_result_to_words(result, 1, 1)
'''
ALL_EXIST = {k: True for k in (
    'v6676_benchmark_present','v6676_gpu_worker_present',
    'v5_acceptance_present','synthetic_fixture_present',
    'windows_env_present','windows_gpu_env_present')}


def trees(ui=UI,core=CORE,media=MEDIA,backend=BACKEND):
    return {'ui.py':ast.parse(ui),'core.py':ast.parse(core),
            'handler_media.py':ast.parse(media),'ocr_backend.py':ast.parse(backend)}


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.good = d.scan_trees(trees(), ALL_EXIST)

    def test_all_required_in_synthetic_tree(self):
        self.assertTrue(all(self.good[k] for k in d.REQUIRED))

    def test_good_status(self):
        self.assertEqual(d.status_of(self.good, []),'STATIC_HOOKS_READY_FOR_REVIEW')

    def test_missing_file_status(self):
        self.assertEqual(d.status_of(self.good, ['MISSING_CORE_PY']),'SOURCE_FILES_MISSING_OR_UNREADABLE')

    def test_missing_hook_status(self):
        vals = self.good.copy();vals['ui_run_files'] = False
        self.assertEqual(d.status_of(vals, []),'SOURCE_SIGNATURE_CHANGED_REVIEW_REQUIRED')

    def test_ui_class(self):
        self.assertTrue(self.good['ui_app_class'])

    def test_ui_dispatch(self):
        self.assertTrue(self.good['ui_dispatch_files_call'])

    def test_background_thread(self):
        self.assertTrue(self.good['ui_background_thread'])

    def test_hotkey_lock(self):
        self.assertTrue(self.good['ui_hotkey_lock'])

    def test_queue_done(self):
        self.assertTrue(self.good['ui_done_queue'])

    def test_core_dispatch(self):
        self.assertTrue(self.good['core_dispatch_files'])

    def test_core_mask(self):
        self.assertTrue(self.good['core_media_entry_call'])

    def test_exception_suppression_review(self):
        self.assertTrue(self.good['core_media_exception_continues'])

    def test_no_suppression_review_without_except(self):
        t=trees(core='def dispatch_files():\n    return handler_media.mask_file(1)')
        self.assertFalse(d.scan_trees(t,ALL_EXIST)['core_media_exception_continues'])

    def test_risk_flag_not_gate(self):
        self.assertNotIn('core_media_exception_continues',d.REQUIRED)

    def test_ocr_wordbox(self):
        self.assertTrue(self.good['ocr_backend_wordbox_requested'])

    def test_ocr_adapter(self):
        self.assertTrue(self.good['ocr_backend_adapter_converter'])

    def test_wordbox_false_rejected(self):
        t=trees(backend=BACKEND.replace('return_word_box=True','return_word_box=False'))
        self.assertFalse(d.scan_trees(t,ALL_EXIST)['ocr_backend_wordbox_requested'])

    def test_missing_gpu_tool(self):
        exist=ALL_EXIST.copy(); exist['v6676_gpu_worker_present']=False
        self.assertFalse(d.scan_trees(trees(),exist)['v6676_gpu_worker_present'])

    def test_constant_unknown_not_reported(self):
        self.assertEqual(set(self.good), set(d.KEYS))

    def test_relative_contained_results(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();kit=root/d.KIT;kit.mkdir()
            output=d.validate_destination(root,root/'results',kit)
            self.assertEqual(output,(root/'results').resolve())

    def test_reject_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp).resolve();kit=root/d.KIT;kit.mkdir()
            with self.assertRaises(ValueError):d.validate_destination(root,root,kit)

    def test_reject_external(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'app';root.mkdir();kit=root/d.KIT;kit.mkdir()
            with self.assertRaises(ValueError):d.validate_destination(root,Path(temp)/'outside',kit)

    def test_reject_venv(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);kit=root/d.KIT;kit.mkdir()
            with self.assertRaises(ValueError):d.validate_destination(root,root/'.venv'/'results',kit)

    def test_reject_kit(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);kit=root/d.KIT;kit.mkdir()
            with self.assertRaises(ValueError):d.validate_destination(root,kit/'result',kit)

    def test_report_does_not_leak_string(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp)/'reports'
            report={'schema':d.SCHEMA,'status':'STATIC_HOOKS_READY_FOR_REVIEW',
                    'checks':self.good,'failed_required_check_count':0,
                    'review_flags':{'core_may_suppress_media_exceptions':True}}
            d.write_report(out,report)
            content=(out/'v669_source_preflight.md').read_text(encoding='utf-8')
            self.assertNotIn('synthetic.pdf',content)
            self.assertNotIn(str(Path(temp)),content)
            self.assertIn('core_may_suppress_media_exceptions',str(report['review_flags']))

    def test_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            out=Path(temp);(out/'present.txt').write_text('original')
            with self.assertRaises(FileExistsError):
                d.write_report(out,{'status':'X','checks':{},'review_flags':{},'failed_required_check_count':0})
            self.assertEqual((out/'present.txt').read_text(),'original')

    def test_source_missing_reports_only_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            report=d.generate_report(Path(temp))
            self.assertEqual(report['status'],'SOURCE_FILES_MISSING_OR_UNREADABLE')
            self.assertFalse(report['project_code_imported'])
            self.assertTrue(all('MISSING_' in s for s in report['source_issues']))

    def test_unexpected_file_does_not_execute(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            marker=root/'SHOULD_NOT_EXIST'
            (root/'ui.py').write_text(f'open({str(marker)!r}, "w").write("x")')
            d.generate_report(root)
            self.assertFalse(marker.exists())

    def test_read_invalid_syntax_sanitized(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); (root/'ui.py').write_text('SECRET_DO_NOT_REPORT = (!',encoding='utf-8')
            report=d.generate_report(root)
            self.assertTrue(any('UNREADABLE_UI_PY' == s for s in report['source_issues']))
            self.assertNotIn('SECRET_DO_NOT_REPORT',json.dumps(report))

    def test_avoid_pii_in_report_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            report=d.generate_report(Path(temp))
            self.assertNotIn('ocr_text',report)
            self.assertNotIn('private_map',report)
            self.assertNotIn('paths',report)


if __name__ == '__main__':
    unittest.main(verbosity=2)
