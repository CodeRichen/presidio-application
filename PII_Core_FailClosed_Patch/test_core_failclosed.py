"""Safe failure and batch semantics against the actual patched core.py.
All imported application modules, models, clipboard and media OCR are mocks.
No real user documents or OCR models are imported. Uses synthetic only.
"""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent


def load_core(mediapath):
    modules = {}
    for name in ('win32clipboard', 'win32gui', 'win32process', 'psutil', 'pyperclip',
                 'handler_text','handler_code','handler_media','handler_csv','entity_store','preprocess'):
        m = types.ModuleType(name)
        modules[name] = m
    m = modules['handler_media']
    m.DEFAULT_MAP_FILE = str(mediapath)
    m.detect_matches = lambda text: []
    m.MASK_COLOR = (0, 0, 0)
    modules['handler_csv'].TABLE_EXTENSIONS = {'.csv'}
    modules['handler_csv'].LAST_REPORT = []
    modules['entity_store'].SENSITIVE_FILE = 'dummy-list'
    modules['entity_store'].record_sensitive = lambda *a, **k: None
    modules['entity_store'].load_items = lambda: []
    modules['entity_store'].items_count = lambda: 0
    modules['preprocess'].APPLY_TO_MEDIA = False
    for key, val in modules.items():
        sys.modules[key] = val
    spec = importlib.util.spec_from_file_location('test_pii_core_patch', ROOT/'core.py')
    core = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(core)
    return core, modules


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pii_core_actual_test_')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.core, self.modules = load_core(self.root/'private'/'media_map.json')
        self.core.MAP_FILE = str(self.root/'private'/'text_map.txt')
        self.mod = self.modules['handler_media']
        self.events = []
        self.mode = 'success'

        def mask_image(src, out, map_file):
            self.events.append(('mask', src, out))
            img = Image.open(src).convert('RGB')
            if self.mode in ('raise','partial'):
                if self.mode == 'partial':
                    img.save(out)
                raise RuntimeError('synthetic failure (do not leak)')
            draw = ImageDraw.Draw(img)
            if self.mode != 'empty' and 'empty' not in Path(src).name:
                draw.rectangle([25,25,45,45] if self.mode == 'wrong_bbox' else [1,1,15,15],fill='black')
                entries = [{'value':'FAKE_NAME','label':'PERSON', 'bbox':[1,1,15,15], 'crop_b64':'FAKE_CROP'}]
            else:
                entries = []
            img.save(out)
            with open(map_file,'w', encoding='utf-8') as f:
                json.dump({Path(out).name:entries}, f)
            return entries
        self.mod.mask_image=mask_image

        def mask_pdf(src,out,map_file):
            import fitz
            if self.mode == 'raise':
                raise RuntimeError('synthetic pdf error')
            # Raster-only PDF, draw black rectangle directly on image; no source PDF reuse.
            outdoc=fitz.open()
            page=outdoc.new_page(width=160,height=160)
            page.draw_rect(fitz.Rect(1,1,70,70),fill=(0,0,0))
            if self.mode == 'invalidpdf':
                page.insert_text((15,15),'UNMASKED TEXT',fontsize=8)
            else:
                # require one image per page
                from io import BytesIO
                buf=BytesIO()
                Image.new('RGB',(80,80),'black').save(buf,format='PNG')
                page.insert_image(fitz.Rect(0,0,160,160),stream=buf.getvalue())
            outdoc.save(out)
            outdoc.close()
            entries=[{'value':'FAKE','label':'PERSON','bbox':[1,1,70,70], 'page':0}]
            with open(map_file,'w', encoding='utf-8') as f:
                json.dump({Path(out).name:entries},f)
            return entries
        self.mod.mask_pdf=mask_pdf

        def csv_mask(src,detect,tagger):
            if self.mode=='raise': raise RuntimeError('csv fail')
            out=Path(src).with_name(Path(src).stem+'_masked.csv')
            original=Path(src).read_text(encoding='utf-8')
            if 'FAKE_NAME' not in original: return None
            tag=tagger('PERSON','FAKE_NAME')
            out.write_text(original.replace('FAKE_NAME',tag),encoding='utf-8')
            self.modules['handler_csv'].LAST_REPORT=([] if self.mode == 'csv_no_report'
                                                  else [('column','PERSON','match')])
            return str(out)
        self.modules['handler_csv'].mask_file=csv_mask

        def fake_anonymize(text,mapping,is_code,save,record,source):
            if 'FAKE_NAME' not in text: return text,[]
            start=text.find('FAKE_NAME')
            mapping['<PERSON_1>']='FAKE_NAME'
            return text.replace('FAKE_NAME','<PERSON_1>'),[(start,start+9,'PERSON',1,'zh')]
        self.core._anonymize_full=fake_anonymize

    def image(self, name='photo.png'):
        p=self.root/name
        p.parent.mkdir(parents=True,exist_ok=True)
        Image.new('RGB',(80,60),'white').save(p)
        return p

    def test_media_success_with_private_map(self):
        p=self.image()
        original=p.read_bytes()
        out,entries=self.core._mask_media(p)
        self.assertTrue(Path(out).exists())
        self.assertEqual(len(entries),1)
        self.assertEqual(p.read_bytes(),original)
        media=json.loads(Path(self.mod.DEFAULT_MAP_FILE).read_text())
        self.assertEqual(list(media),['photo_masked.png'])
        self.assertEqual(media['photo_masked.png'][0]['value'],'FAKE_NAME')

    def test_media_failure_never_copies_original(self):
        p=self.image()
        self.mode='partial'
        with self.assertRaises(self.core.SafeOutputError):
            self.core._mask_media(p)
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertFalse(Path(self.mod.DEFAULT_MAP_FILE).exists())

    def test_empty_detection_publishes_nothing(self):
        p=self.image('empty.png')
        out,entries=self.core._mask_media(p)
        self.assertIsNone(out)
        self.assertEqual(entries,[])
        self.assertFalse((self.root/'empty_masked.png').exists())

    def test_preexisting_output_no_clobber(self):
        p=self.image()
        out=self.root/'photo_masked.png'
        out.write_bytes(b'RESERVE')
        with self.assertRaises(self.core.SafeOutputError):
            self.core._mask_media(p)
        self.assertEqual(out.read_bytes(),b'RESERVE')

    def test_batch_media_then_failed_text_rollback(self):
        p=self.image()
        bad=self.root/'bad.txt'
        bad.write_bytes(b'\xff\xfe\xfd\x00\x01')
        before=p.read_bytes()
        with self.assertRaises(self.core.SafeOutputError):
            self.core.dispatch_files([p,bad],{})
        self.assertEqual(p.read_bytes(),before)
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertFalse(Path(self.mod.DEFAULT_MAP_FILE).exists())

    def test_batch_publishes_only_after_all_processed(self):
        p=self.image()
        text=self.root/'letter.txt'
        text.write_text('My name is FAKE_NAME.', encoding='utf-8')
        mapping={}
        outputs=self.core.dispatch_files([p,text],mapping)
        self.assertEqual(len(outputs),2)
        self.assertEqual(mapping['<PERSON_1>'],'FAKE_NAME')
        self.assertEqual((self.root/'letter_masked.txt').read_text(),'My name is <PERSON_1>.')
        self.assertTrue(Path(self.mod.DEFAULT_MAP_FILE).exists())
        self.assertTrue(Path(self.core.MAP_FILE).exists())

    def test_batch_commit_second_failure_rolls_back_first(self):
        p=self.image()
        text=self.root/'x.txt';text.write_text('FAKE_NAME')
        link_orig=os.link
        calls=[]
        def fail_second(a,b,*args,**kw):
            calls.append(b)
            if len(calls)==2:raise OSError('inject second commit failure')
            return link_orig(a,b,*args,**kw)
        with patch.object(self.core.os,'link',side_effect=fail_second):
            with self.assertRaises(self.core.SafeOutputError):
                self.core.dispatch_files([p,text],{})
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertFalse((self.root/'x_masked.txt').exists())
        self.assertFalse(Path(self.mod.DEFAULT_MAP_FILE).exists())
        self.assertFalse(Path(self.core.MAP_FILE).exists())

    def test_map_write_failure_rolls_back_publication(self):
        p=self.image()
        old=self.root/'private'/'media_map.json'
        old.parent.mkdir()
        old.write_text('{"old": [1]}')
        original=old.read_bytes()
        with patch.object(self.core,'_atomic_private_write',side_effect=OSError('disk full')):
            with self.assertRaises(self.core.SafeOutputError):
                self.core.dispatch_files([p],{})
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertEqual(old.read_bytes(),original)

    def test_scan_only_masked_files_not_raw_tree(self):
        src=self.root/'input';src.mkdir()
        p=self.image('input/photo.png')
        (src/'yes.txt').write_text('FAKE_NAME')
        (src/'no.txt').write_text('plain text no pii')
        (src/'private.bin').write_bytes(b'SHOULD_NOT_BE_COPIED')
        (src/'.venv').mkdir()
        (src/'.venv'/'secret.txt').write_text('FAKE_NAME')
        output,results=self.core.scan_folder(src,{},recursive=True)
        out=Path(output)
        self.assertTrue((out/'yes.txt').exists())
        self.assertTrue((out/'photo.png').exists())
        self.assertFalse((out/'no.txt').exists())
        self.assertFalse((out/'private.bin').exists())
        self.assertFalse((out/'.venv').exists())
        self.assertFalse(any(x.name.startswith('.pii_private_stage_') for x in out.rglob('*')))
        self.assertEqual(len(results),3)
        self.assertEqual(sum(bool(r['output']) for r in results),2)
        self.assertTrue((src/'no.txt').exists())
        self.assertEqual((src/'yes.txt').read_text(),'FAKE_NAME')

    def test_scan_second_failure_never_publishes(self):
        src=self.root/'input';src.mkdir()
        (src/'a.txt').write_text('FAKE_NAME')
        (src/'b.txt').write_bytes(b'\xff\xfe\xfd\x00\x01')
        with self.assertRaises(self.core.SafeOutputError):
            self.core.scan_folder(src,{})
        self.assertFalse((self.root/'input_masked').exists())
        self.assertFalse(Path(self.core.MAP_FILE).exists())

    def test_scan_no_hits_returns_no_output_directory(self):
        src=self.root/'input';src.mkdir()
        (src/'a.txt').write_text('no PII')
        (src/'nested').mkdir()
        (src/'nested'/'empty.png').write_bytes(self.image('empty.png').read_bytes())
        out,results=self.core.scan_folder(src,{})
        self.assertEqual(Path(out),self.root/'input_masked')
        self.assertEqual(len(results),2)
        self.assertTrue(Path(out).is_dir())
        self.assertEqual([f for f in Path(out).rglob("*") if f.is_file()], [])

    def test_scan_table_isolation(self):
        src=self.root/'input';src.mkdir()
        f=src/'data.csv';f.write_text('name\nFAKE_NAME\n')
        out,results=self.core.scan_folder(src,{})
        self.assertEqual((Path(out)/'data.csv').read_text(),'name\n<PERSON_1>\n')
        self.assertEqual(f.read_text(),'name\nFAKE_NAME\n')
        self.assertFalse(any(x.name.startswith('.pii_private_stage_') for x in self.root.iterdir()))

    def test_pdf_structural_gate_accepts_raster_only_and_rejects_text_layer(self):
        import fitz
        p=self.root/'sample.pdf'
        doc=fitz.open(); doc.new_page(width=160,height=160);doc.save(str(p)); doc.close()
        out,entries=self.core._mask_media(p)
        self.assertEqual(len(entries),1)
        self.assertTrue(Path(out).is_file())
        p2=self.root/'other.pdf'; p2.write_bytes(p.read_bytes())
        self.mode='invalidpdf'
        with self.assertRaises(self.core.SafeOutputError):
            self.core._mask_media(p2)
        self.assertFalse((self.root/'other_masked.pdf').exists())


    def test_mask_bbox_must_be_blacked_out(self):
        p=self.image()
        self.mode='wrong_bbox'
        with self.assertRaises(self.core.SafeOutputError):
            self.core._mask_media(p)
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertFalse(Path(self.mod.DEFAULT_MAP_FILE).exists())

    def test_candidate_changed_after_stage_rejected(self):
        p=self.image()
        original=self.core._MaskedBatch.verify_sources
        def corrupt_candidate(batch):
            if batch.items:
                Path(batch.items[0][1]).write_bytes(b'TAMPERED_SYNTHETIC_OUTPUT')
            return original(batch)
        with patch.object(self.core._MaskedBatch,'verify_sources',new=corrupt_candidate):
            with self.assertRaises(self.core.SafeOutputError):
                self.core.dispatch_files([p],{})
        self.assertFalse((self.root/'photo_masked.png').exists())

    def test_table_without_masking_report_is_rejected(self):
        f=self.root/'list.csv';f.write_text('name\nFAKE_NAME\n')
        self.mode='csv_no_report'
        with self.assertRaises(self.core.SafeOutputError):
            self.core.dispatch_files([f],{})
        self.assertFalse((self.root/'list_masked.csv').exists())
        self.assertFalse(Path(self.core.MAP_FILE).exists())

    def test_scan_duplicate_media_basename_rejected(self):
        src=self.root/'input';src.mkdir()
        for sub in ('a','b'):
            self.image(f'input/{sub}/photo.png')
        with self.assertRaises(self.core.SafeOutputError):
            self.core.scan_folder(src,{})
        self.assertFalse((self.root/'input_masked').exists())

    def test_scan_no_source_symlink(self):
        src=self.root/'input';src.mkdir()
        p=self.image('outside.png')
        link=src/'alias.png'
        try:link.symlink_to(p)
        except OSError:self.skipTest('symlinks disabled')
        with self.assertRaises(self.core.SafeOutputError):
            self.core.scan_folder(src,{})
        self.assertFalse((self.root/'input_masked').exists())


    def test_second_map_update_failure_restores_first_map(self):
        p=self.image()
        txt=self.root/'notes.txt';txt.write_text('FAKE_NAME')
        text_map=Path(self.core.MAP_FILE)
        text_map.parent.mkdir(exist_ok=True)
        text_map.write_text('<OLD_1>\tSAFE_OLD_VALUE\n')
        before=text_map.read_bytes()
        origin=self.core._atomic_private_write
        def fail_media(path,data):
            if str(path) == str(self.mod.DEFAULT_MAP_FILE):
                raise OSError('synthetic second map write failure')
            return origin(path,data)
        with patch.object(self.core,'_atomic_private_write',side_effect=fail_media):
            with self.assertRaises(self.core.SafeOutputError):
                self.core.dispatch_files([p,txt],{})
        self.assertEqual(text_map.read_bytes(),before)
        self.assertFalse(Path(self.mod.DEFAULT_MAP_FILE).exists())
        self.assertFalse((self.root/'photo_masked.png').exists())
        self.assertFalse((self.root/'notes_masked.txt').exists())

    def test_no_hit_text_cannot_pollute_mapping(self):
        txt=self.root/'no.txt';txt.write_text('nothing sensitive')
        def buggy(text,mapping,*args):
            mapping['<PERSON_1>']='LEAKED_FAKE'
            return text,[]
        mapping={}
        with patch.object(self.core,'_anonymize_full',side_effect=buggy):
            outputs=self.core.dispatch_files([txt],mapping)
        self.assertEqual(outputs,[])
        self.assertEqual(mapping,{})
        self.assertFalse(Path(self.core.MAP_FILE).exists())
        self.assertFalse((self.root/'no_masked.txt').exists())

    def test_reject_symlink_input(self):
        p=self.image()
        q=self.root/'alias.png'
        try:q.symlink_to(p)
        except OSError:self.skipTest('platform does not permit symlinks')
        with self.assertRaises(self.core.SafeOutputError):
            self.core._mask_media(q)

    def test_no_raw_in_stage_after_fault(self):
        p=self.image()
        self.mode='raise'
        with self.assertRaises(self.core.SafeOutputError):
            self.core.dispatch_files([p],{})
        self.assertFalse(any(x.name.startswith('.pii_private_stage_') for x in self.root.rglob('*')))

if __name__=='__main__':
    unittest.main(verbosity=2)
