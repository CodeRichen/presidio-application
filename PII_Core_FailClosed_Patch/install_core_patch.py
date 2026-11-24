# -*- coding: utf-8 -*-
"""Windows-safe apply helper. Does NOT import the PII application or touch data.

Default: read-only preflight. --apply: back up original and atomically replace
core.py only if its AST matches the source the patch was authored against.
"""
import argparse
import ast
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

EXPECTED_SOURCE_AST_SHA256 = "3e50de4862705561f837bffb70a4dcf425c834561e3c40f024825127ba235a34"
EXPECTED_PATCH_SHA256 = "3953b4ad859cf77911af15dc7041341ac69e7e3c00fc96b94963465ceb9ad522"


def ast_sha(text):
    tree = ast.parse(text)
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode('utf-8')).hexdigest()


def main():
    ap=argparse.ArgumentParser(description='檢查並選擇性安裝 core.py Fail-Closed 修正')
    ap.add_argument('--project', type=Path, default=Path.cwd(), help='presidio_app_allen 專案根目錄')
    ap.add_argument('--apply', action='store_true', help='執行前必須先關閉 GUI；建立備份後原子安裝')
    args=ap.parse_args()
    src=Path(__file__).resolve().parent/'core.py'
    dst=args.project.expanduser().resolve()/'core.py'
    if not src.is_file() or not dst.is_file() or dst.is_symlink():
        raise SystemExit('拒絕：找不到 core.py 或目標是符號連結')
    payload=src.read_bytes()
    if hashlib.sha256(payload).hexdigest() != EXPECTED_PATCH_SHA256:
        raise SystemExit('拒絕：修正檔校驗不符')
    try:
        orig=dst.read_bytes()
        digest=ast_sha(orig.decode('utf-8-sig'))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise SystemExit('拒絕：正式 core.py 格式異常') from exc
    if digest != EXPECTED_SOURCE_AST_SHA256:
        if hashlib.sha256(orig).hexdigest() == hashlib.sha256(payload).hexdigest():
            print('已是相同修正版；不需要安裝')
            return
        raise SystemExit('拒絕：現有 core.py 與先前上傳版本不同，必須重新審查後再合併')
    if not args.apply:
        print('SOURCE_MATCH: True')
        print('PATCH_INTEGRITY: True')
        print('MODE: CHECK_ONLY（未修改任何檔案）')
        return
    timestamp=datetime.now().strftime('%Y%m%d_%H%M%S')
    backup=dst.with_name(dst.name+'.backup_'+timestamp)
    suffix=0
    while backup.exists():
        suffix+=1
        backup=dst.with_name(dst.name+'.backup_'+timestamp+'_'+str(suffix))
    shutil.copy2(dst,backup)
    try:
        fd,temp=tempfile.mkstemp(prefix='.pii_core_install_',suffix='.py',dir=str(dst.parent))
        with os.fdopen(fd,'wb') as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp,dst)
        temp=None
    finally:
        if temp and os.path.exists(temp):os.unlink(temp)
    print('INSTALL_STATUS: PATCHED_CORE_BACKUP_CREATED')
    print('ORIGINAL_BACKUP:',backup.name)
    print('仍需以合成 PDF 做完整 Windows OCR、PDF 安全與 GUI 驗收；此訊息不代表允許使用真實 PII。')

if __name__=='__main__':
    main()
