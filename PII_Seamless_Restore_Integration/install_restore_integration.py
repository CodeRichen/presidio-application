# -*- coding: utf-8 -*-
"""Safe installer: patch ONLY handler_media.restore_pdf and add a UI restore entry.

Default --check is read-only. --apply creates backups, validates preconditions and
atomically replaces the two Python files. Leaves core.py, OCR, NER, masking logic alone.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime

MARKER = "# SEAMLESS_RESTORE_UI_V1"
START = "def restore_pdf(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):"
RESTORE_FUNC = '''def restore_pdf(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    """還原遮蔽 PDF：同一像素畫布貼回私有裁圖並輸出單影像 PDF。"""
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        raise ValueError("找不到媒體還原對照資料，拒絕產生空白還原檔")
    from seamless_pdf_restore import restore_pdf_flat
    restore_pdf_flat(masked_path, output_path, entries)
    print(f"[PDF 還原完成] 已還原 {len(entries)} 處並輸出單影像 PDF（私有文件）")
'''
UI_HOOK = '''\n\n# SEAMLESS_RESTORE_UI_V1
from ui_demo_media_restore import patch_demo_app as _install_seamless_restore_for_demo
_install_seamless_restore_for_demo(App)
\n'''


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _patch_media(source: str) -> str:
    tree=ast.parse(source)
    funcs=[x for x in tree.body if isinstance(x,ast.FunctionDef) and x.name=="restore_pdf"]
    if len(funcs)!=1:
        raise RuntimeError("handler_media.py 找不到唯一 restore_pdf；停止安裝")
    fn=funcs[0]
    old='\n'.join(source.splitlines()[fn.lineno-1:fn.end_lineno])
    if 'from seamless_pdf_restore import restore_pdf_flat' in old:
        return source
    if 'page.insert_image(' not in old or '_load_map_entry' not in old:
        raise RuntimeError("restore_pdf 不是已知的舊版疊圖方式；停止安裝，避免覆寫其他修正")
    lines=source.splitlines(keepends=True)
    lines[fn.lineno-1:fn.end_lineno]=[RESTORE_FUNC+'\n']
    return ''.join(lines)


def _patch_demo(source: str) -> str:
    if MARKER in source:
        return source
    tree=ast.parse(source)
    apps=[x for x in tree.body if isinstance(x,ast.ClassDef) and x.name=="App"]
    if len(apps)!=1:
        raise RuntimeError("美化版 ui_demo.py 沒有唯一 class App，拒絕推測修改。請提供該檔原始碼。")
    main_guards=[]
    for n in tree.body:
        if isinstance(n,ast.If):
            snippet=ast.get_source_segment(source,n.test) or ''
            if "__main__" in snippet and "__name__" in snippet:
                main_guards.append(n)
    if len(main_guards)!=1 or main_guards[0].lineno <= apps[0].end_lineno:
        raise RuntimeError("找不到 App 定義後的唯一 __main__ 入口，拒絕修改 UI。")
    lines=source.splitlines(keepends=True)
    lines.insert(main_guards[0].lineno-1,UI_HOOK)
    return ''.join(lines)


def _atomic_replace(path, contents):
    fd,name=tempfile.mkstemp(dir=str(path.parent),prefix='.seamless_apply_', suffix='.py')
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(contents)
            f.flush();os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name):os.unlink(name)


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--project',type=Path,default=Path.cwd())
    group=ap.add_mutually_exclusive_group()
    group.add_argument('--check',action='store_true',help='只檢查，不修改任何檔案（預設）')
    group.add_argument('--apply',action='store_true',help='關閉 Demo UI 後才執行：備份、安裝')
    args=ap.parse_args()
    project=args.project.resolve(); here=Path(__file__).resolve().parent
    media=project/'handler_media.py'; demo=project/'PII_UI_Demo_Refresh'/'ui_demo.py'
    modules=[(here/'seamless_pdf_restore.py',project/'seamless_pdf_restore.py'),
             (here/'ui_demo_media_restore.py',project/'ui_demo_media_restore.py')]
    for p in (media,demo):
        if not p.is_file() or p.is_symlink():
            raise SystemExit(f'REFUSED: 找不到正式原始檔或是符號連結：{p.name}')
    original={p:p.read_bytes() for p in (media,demo)}
    try:
        changed={media:_patch_media(original[media].decode('utf-8-sig')).encode('utf-8'),
                 demo:_patch_demo(original[demo].decode('utf-8-sig')).encode('utf-8')}
        for path,blob in changed.items():ast.parse(blob.decode('utf-8'), filename=str(path))
        for src,dst in modules:
            if not src.is_file():raise RuntimeError('安裝包缺少 '+src.name)
            ast.parse(src.read_text('utf-8'),filename=str(src))
            if dst.exists() and _sha(dst.read_bytes())!=_sha(src.read_bytes()):
                raise RuntimeError('已存在不同版本的 '+dst.name+'，停止覆寫')
    except (UnicodeError,SyntaxError,RuntimeError) as exc:
        raise SystemExit('REFUSED: '+str(exc))
    print('MODE: '+('APPLY' if args.apply else 'CHECK_ONLY'))
    print('handler_media.py old SHA256:',_sha(original[media]))
    print('ui_demo.py old SHA256:',_sha(original[demo]))
    print('PATCH_MEDIA:',changed[media]!=original[media])
    print('PATCH_DEMO_UI:',changed[demo]!=original[demo])
    if not args.apply:
        print('STATUS: CHECK_PASS（未修改檔案）')
        return
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    backups=[]; installed_modules=[]; modified=[]
    try:
        for p,contents in changed.items():
            if contents==original[p]:continue
            backup=p.with_name(p.name+'.backup_'+stamp)
            if backup.exists():raise RuntimeError('備份名稱已存在，拒絕安裝')
            shutil.copy2(p,backup);backups.append((p,backup))
        for src,dst in modules:
            if not dst.exists():
                shutil.copy2(src,dst)
                installed_modules.append(dst)
        for p,contents in changed.items():
            if contents==original[p]:continue
            if _sha(p.read_bytes())!=_sha(original[p]):
                raise RuntimeError('檔案安裝期間被其他程序變更')
            _atomic_replace(p,contents);modified.append(p)
    except Exception as exc:
        for p in reversed(modified):
            _atomic_replace(p,original[p])
        for p in installed_modules:
            try:p.unlink()
            except OSError:pass
        raise SystemExit('REFUSED / ROLLED_BACK: '+str(exc))
    print('STATUS: PATCH_INSTALLED')
    for p,backup in backups:print('BACKUP:',str(backup))
    print('注意：還原檔含完整人臉/個資，僅能本機私有保存；重啟 Demo UI 後驗收。')

if __name__=='__main__':main()
