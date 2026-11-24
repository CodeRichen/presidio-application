from pathlib import Path
import contextlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parent

class TestUITimer(unittest.TestCase):
    def test_actual_file_callback_gets_timed_without_real_tkinter(self):
        with tempfile.TemporaryDirectory() as t:
            project=Path(t)/'proj';project.mkdir()
            (project/'ui.py').write_text('# stub only',encoding='utf-8')
            (project/'core.py').write_text('# stub only',encoding='utf-8')
            report=Path(t)/'ui.jsonl'
            media=types.ModuleType('handler_media')
            media.ocr_words=lambda a: ['mock words']
            media.find_sensitive_boxes_in_words=lambda a: [(1,2,3,4)]
            media._save_map_entry=lambda *a:None
            media.mask_image=lambda *a:None
            media.mask_pdf=lambda *a:None
            fake_ui=types.ModuleType('ui')
            class FakeApp:
                def __init__(self):
                    self.busy=False
                    self.files=['sample.png']
                    self.ready=True
                def _start(self,func,msg,on_done):
                    if msg=='處理檔案中…':
                        self.busy=True
                        value=func()
                        self.busy=False
                        on_done(value)
                def run_files(self):
                    def job():
                        media.ocr_words(None)
                        media.find_sensitive_boxes_in_words(None)
                        return ['sample_masked.png']
                    return self._start(job,'處理檔案中…',lambda _:None)
                def mainloop(self):
                    self.run_files()
            fake_ui.App=FakeApp
            spec=importlib.util.spec_from_file_location('test_launcher',ROOT/'launch_ui_timed.py')
            mod=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cwd=Path.cwd()
            try:
                with patch.dict(sys.modules,{'handler_media':media,'ui':fake_ui}):
                    self.assertEqual(0,mod.main(['--project',str(project),'--report',str(report)]))
            finally:
                os.chdir(cwd)
            data=json.loads(report.read_text(encoding='utf-8').strip())
            self.assertEqual(data['status'],'OK')
            self.assertEqual(data['file_types'],{'.png':1})
            self.assertEqual(data['outputs'],1)
            self.assertEqual(data['phases']['ocr']['calls'],1)
            self.assertEqual(data['phases']['pii']['calls'],1)
            self.assertGreaterEqual(data['click_to_ui_s'],data['worker_s'])
            self.assertNotIn('sample_masked',report.read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
