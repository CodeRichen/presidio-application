"""只使用標準函式庫進行靜態驗證；不 import 正式 core、OCR 或 GUI。"""
import ast
import hashlib
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parent
BASE=(ROOT/'ui_original_reference.txt').read_text('utf-8')
DEMO=(ROOT/'ui_demo.py').read_text('utf-8')
BM=ast.parse(BASE)
DM=ast.parse(DEMO)
BAPP=next(x for x in BM.body if isinstance(x,ast.ClassDef) and x.name=='App')
DAPP=next(x for x in DM.body if isinstance(x,ast.ClassDef) and x.name=='App')
BMETHOD={x.name:x for x in BAPP.body if isinstance(x,ast.FunctionDef)}
DMETHOD={x.name:x for x in DAPP.body if isinstance(x,ast.FunctionDef)}

class DemoThemeStaticTests(unittest.TestCase):
    def test_source_complete(self):
        self.assertIn('def run_files(self):',DEMO)
    def test_original_app_methods_retained(self):
        self.assertTrue(set(BMETHOD)<=set(DMETHOD))
    def test_business_handlers_unchanged(self):
        for name in ('_start','_poll','run_text','run_files','run_scan','_set_busy','_append_log_batch',
                     'add_files','copy_outputs','_on_close','refresh_map_view','clear_map','read_clipboard_text'):
            with self.subTest(method=name):
                self.assertEqual(ast.dump(BMETHOD[name],include_attributes=False),
                                 ast.dump(DMETHOD[name],include_attributes=False))
    def test_imports_preserved(self):
        self.assertTrue(all(x in DEMO for x in ('import app_env','import core','import entity_store',
                                                'import hotkey_service','import preprocess')))
    def test_theme_is_local(self):
        self.assertIn('def apply_demo_theme(root):',DEMO)
        self.assertIn('st.configure("Primary.TButton"',DEMO)
    def test_homepage(self):
        self.assertIn('def _build_overview(self, notebook):',DEMO)
        self.assertIn('self._demo_pages["file"] = f',DEMO)
    def test_original_threading_lock(self):
        self.assertIn('hotkey_service.WORK_LOCK',DEMO)
    def test_original_log_queue(self):
        self.assertIn('GUI_LOG_POLL_MAX_ITEMS = 250',DEMO)
    def test_original_default_mask(self):
        self.assertIn('self.text_action_var = tk.StringVar(value="mask")',DEMO)
    def test_original_core_entry(self):
        self.assertIn('return core.dispatch_files(paths, self.mapping)',DEMO)
    def test_no_gpu_integration(self):
        self.assertNotIn('v6676_gpu_worker',DEMO)
    def test_no_secure_output_claim(self):
        self.assertNotIn('正式 PDF 安全驗收通過',DEMO)
    def test_footer_visible_order(self):
        self.assertIn('footer.pack(side="bottom", fill="x")',DEMO)
        self.assertIn('lg.pack(side="bottom", fill="x"',DEMO)
    def test_launcher_is_no_overwrite(self):
        l=(ROOT/'launch_ui_demo.py').read_text('utf-8')
        self.assertIn('runpy.run_path',l)
        self.assertNotIn('copyfile(',l)
        self.assertNotIn('unlink(',l)
    def test_source_sha256(self):
        data=__import__('json').loads((ROOT/'source_hash.json').read_text('utf-8'))
        self.assertEqual(hashlib.sha256(BASE.encode()).hexdigest(),data['source_utf8_normalized_sha256'])

if __name__=='__main__':
    unittest.main(verbosity=2)
