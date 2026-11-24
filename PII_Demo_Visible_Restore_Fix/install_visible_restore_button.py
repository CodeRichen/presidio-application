# -*- coding: utf-8 -*-
"""Safely install a visible Restore PDF/Image button into the exact supplied Demo UI.

Defaults to read-only. --apply verifies source and patch SHA256, backs up and
atomically replaces ONLY PII_UI_Demo_Refresh/ui_demo.py. No import of project.
"""
from __future__ import annotations
import argparse
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from datetime import datetime
import ast

SOURCE_SHA256 = 'eec9245926c65732c5cdcc2edfe1faf176ed485bee32c7893db9835015e40a90'
PATCH_SHA256 = '41a26b69b2f149684ad31538070f7923ba7a086efe4f1cd3edb0ae85f24f8f54'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description='安全加入美化版 UI 的可見媒體還原按鈕')
    parser.add_argument('--project', type=Path, default=Path.cwd(), help='專案 presidio_app_allen 根目錄')
    parser.add_argument('--check', action='store_true', help='只檢查，不修改（預設）')
    parser.add_argument('--apply', action='store_true', help='備份並套用')
    args = parser.parse_args()
    if args.check and args.apply:
        parser.error('--check 與 --apply 不能同時使用')

    root = args.project.expanduser().resolve()
    dst = root / 'PII_UI_Demo_Refresh' / 'ui_demo.py'
    src = Path(__file__).resolve().parent / 'ui_demo.py'
    media = root / 'handler_media.py'
    seam = root / 'seamless_pdf_restore.py'

    for path in (dst, src, media, seam):
        if not path.is_file() or path.is_symlink():
            raise SystemExit('REFUSED: 找不到必要檔案或發現符號連結：' + path.name)
    if 'from seamless_pdf_restore import restore_pdf_flat' not in media.read_text(encoding='utf-8-sig'):
        raise SystemExit('REFUSED: handler_media.py 尚未啟用無接縫 restore_pdf；請先完成前次後端整合。')
    patch = src.read_bytes()
    if sha(patch) != PATCH_SHA256:
        raise SystemExit('REFUSED: 補丁內容驗證失敗')
    try:
        ast.parse(patch.decode('utf-8-sig'))
        before = dst.read_bytes()
        ast.parse(before.decode('utf-8-sig'))
    except (UnicodeError, SyntaxError) as exc:
        raise SystemExit('REFUSED: ui_demo.py 格式或語法異常') from exc

    current = sha(before)
    if current == PATCH_SHA256:
        print('STATUS: ALREADY_INSTALLED (no changes)')
        return
    if current != SOURCE_SHA256:
        raise SystemExit('REFUSED: 本機 ui_demo.py 與此次上傳的版本不同；未修改任何檔案。請重新提供最新 ui_demo.py。')
    print('SOURCE_MATCH: True')
    print('PATCH_INTEGRITY: True')
    if not args.apply:
        print('STATUS: CHECK_PASS (read only)')
        return

    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    backup = dst.with_name(dst.name + '.backup_' + stamp)
    if backup.exists():
        raise SystemExit('REFUSED: 備份目標已存在')
    shutil.copy2(dst, backup)
    temp = None
    try:
        if sha(dst.read_bytes()) != SOURCE_SHA256:
            raise RuntimeError('安裝期間來源已改變')
        fd, temp = tempfile.mkstemp(prefix='.ui_demo_visible_restore_', suffix='.py', dir=str(dst.parent))
        with os.fdopen(fd,'wb') as f:
            f.write(patch)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, dst)
        temp = None
        if sha(dst.read_bytes()) != PATCH_SHA256:
            raise RuntimeError('安裝後校驗失敗')
    except Exception as exc:
        # 若異常時已替換，從備份還原原本功能。
        if sha(dst.read_bytes()) != SOURCE_SHA256:
            shutil.copy2(backup, dst)
        raise SystemExit('REFUSED / ROLLED_BACK: ' + type(exc).__name__) from exc
    finally:
        if temp is not None and os.path.exists(temp):
            os.unlink(temp)

    print('STATUS: PATCH_INSTALLED')
    print('BACKUP:', backup.name)
    print('VISIBLE_BUTTON: 檔案 / PDF 分頁上方，開始處理檔案按鈕左側')
    print('注意：關閉舊 Demo UI 再重新啟動，且需在清除 media map 前還原。')


if __name__ == '__main__':
    main()
