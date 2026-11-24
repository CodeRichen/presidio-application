#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Check/fix optional handler_media.mask_image return value for core.dispatch_files.

Read-only by default. --apply creates an immutable backup; no other logic changed.
"""
from __future__ import annotations
import argparse
import ast
from pathlib import Path
import shutil


def analyze(text):
    tree=ast.parse(text)
    matches=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name=='mask_image']
    if len(matches)!=1:raise ValueError('找不到唯一的頂層 mask_image()')
    fn=matches[0]
    final=fn.body[-1]
    if isinstance(final,ast.Return) and isinstance(final.value,ast.Name) and final.value.id=='entries':
        return 'OK',None
    if any(isinstance(node,ast.Return) for node in ast.walk(fn)):
        return 'MANUAL','函式已包含其他 return，拒絕自動修改'
    if not any(isinstance(node,ast.Name) and node.id=='entries' and isinstance(node.ctx,ast.Store) for node in ast.walk(fn)):
        return 'MANUAL','函式沒有 entries 變數，無法自動修改'
    return 'NEEDS_FIX',fn.end_lineno


def patched(text):
    status,pos=analyze(text)
    if status=='MANUAL':return status,pos
    if status!='NEEDS_FIX':return status,text
    lines=text.splitlines(keepends=True)
    newline='\r\n' if '\r\n' in text else '\n'
    lines.insert(pos, '    return entries'+newline)
    updated=''.join(lines)
    ast.parse(updated)
    return status,updated


def main(argv=None):
    ap=argparse.ArgumentParser(description='修正 UI 處理圖片後沒有回傳 entries 的相容問題')
    ap.add_argument('--project',type=Path,default=Path.cwd())
    ap.add_argument('--apply',action='store_true')
    ap.add_argument('--restore',action='store_true')
    args=ap.parse_args(argv)
    dest=args.project.resolve()/'handler_media.py'
    if not dest.is_file():ap.error('找不到 handler_media.py')
    backup=dest.with_name('handler_media.py.pre_ui_return_fix.bak')
    if args.restore:
        if not backup.is_file():ap.error('找不到先前備份')
        shutil.copy2(backup,dest)
        print('已還原:',dest)
        return 0
    text=dest.read_text(encoding='utf-8')
    status,result=patched(text)
    if status=='OK':
        print('[OK] mask_image() 最後已經 return entries，無需修正')
        return 0
    if status=='MANUAL':
        print('[需人工檢查] ',result)
        return 1
    print('[發現] 目前 mask_image() 不回傳 entries，core.dispatch_files 可能因此丟棄遮蔽圖片')
    if not args.apply:
        print('[預覽] 若想安全修正，關閉 UI 後加入 --apply；會先備份 handler_media.py')
        return 0
    if backup.exists():
        ap.error(f'已有備份：{backup}，為避免覆寫而停止')
    backup.write_text(text,encoding='utf-8')
    dest.write_text(result,encoding='utf-8')
    print('[完成] 僅在 mask_image() 最後補上 return entries')
    print('[備份]',backup)
    return 0

if __name__=='__main__':raise SystemExit(main())
