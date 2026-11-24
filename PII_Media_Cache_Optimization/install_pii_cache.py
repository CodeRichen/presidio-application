"""Safely add page/image scoped detect_matches memoization to handler_media.py.

Default is a dry-run.  Does not change Regex, NER, OCR or masking decisions.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import sys
import uuid

BEGIN = "# BEGIN PII_MEDIA_SCOPED_CACHE_PATCH_V1"
END = "# END PII_MEDIA_SCOPED_CACHE_PATCH_V1"

SUPPORT = '''# BEGIN PII_MEDIA_SCOPED_CACHE_PATCH_V1
# OCR PII: cache ONLY identical input strings during a single image/PDF page.
# ContextVar keeps parallel calls isolated; do not retain private strings globally.
import contextvars as _pii_cache_contextvars
import functools as _pii_cache_functools
import time as _pii_cache_time

_pii_media_cache_var = _pii_cache_contextvars.ContextVar(
    "pii_media_match_cache_v1", default=None
)
_pii_media_stats_var = _pii_cache_contextvars.ContextVar(
    "pii_media_cache_stats_v1", default=None
)


def _pii_media_cache_enabled():
    return os.environ.get("PII_MEDIA_CACHE", "1").strip().lower() not in {
        "0", "off", "false", "no"
    }


def _pii_media_cache_scope(function):
    @_pii_cache_functools.wraps(function)
    def wrapper(*args, **kwargs):
        # Nested scopes (scanned PDF zoom 2 + zoom 3) share the outer cache.
        if not _pii_media_cache_enabled() or _pii_media_cache_var.get() is not None:
            return function(*args, **kwargs)
        cache = {}
        stats = {"hits": 0, "misses": 0, "model_seconds": 0.0}
        cache_token = _pii_media_cache_var.set(cache)
        stats_token = _pii_media_stats_var.set(stats)
        try:
            return function(*args, **kwargs)
        finally:
            # Clear even if OCR / PII detection raises an exception.
            _pii_media_stats_var.reset(stats_token)
            _pii_media_cache_var.reset(cache_token)
            cache.clear()
            if os.environ.get("PII_MEDIA_CACHE_STATS", "0") == "1":
                print(
                    "[PII cache] "
                    f"{function.__name__}: hits={stats['hits']}, "
                    f"misses={stats['misses']}, "
                    f"saved_inference_calls={stats['hits']}, "
                    f"uncached_detect_seconds={stats['model_seconds']:.3f}"
                )
    return wrapper


def _pii_media_cached_detect(function):
    @_pii_cache_functools.wraps(function)
    def wrapper(text, *args, **kwargs):
        cache = _pii_media_cache_var.get()
        # Preserve legacy behavior outside image/page scopes and for extensions.
        if cache is None or args or kwargs:
            return function(text, *args, **kwargs)
        try:
            result = cache[text]
        except KeyError:
            started = _pii_cache_time.perf_counter()
            result = function(text)
            elapsed = _pii_cache_time.perf_counter() - started
            # Immutable snapshot; return independent lists to callers.
            cache[text] = tuple(result)
            stats = _pii_media_stats_var.get()
            if stats is not None:
                stats["misses"] += 1
                stats["model_seconds"] += elapsed
            return list(result)
        else:
            stats = _pii_media_stats_var.get()
            if stats is not None:
                stats["hits"] += 1
            return list(result)
    return wrapper
# END PII_MEDIA_SCOPED_CACHE_PATCH_V1

'''

SCOPE_NAMES = [
    'find_sensitive_boxes_in_words',
    '_mask_pdf_text_page',
    '_mask_pdf_scanned_page',
]


def checksum(blob: bytes) -> str:
    return hashlib.sha256(blob).hexdigest()


def patch_source(source: str) -> str:
    if BEGIN in source:
        if END not in source:
            raise RuntimeError('偵測到不完整的舊補丁；請手動檢查')
        return source
    tree = ast.parse(source)
    target_nodes = {
        n.name: n for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = ['detect_matches', 'find_sensitive_boxes_in_words']
    for name in required:
        if name not in target_nodes:
            raise RuntimeError(f'找不到原始頂層函式 {name}，不自動修改')
    if not any(isinstance(n, ast.Import) and any(a.name == 'os' for a in n.names) for n in tree.body):
        raise RuntimeError('handler_media.py 沒有 import os；補丁不適用')

    # Add decorators immediately before existing decorator lists, without touching
    # the actual function body. Use AST line offsets instead of regex guessing.
    lines = source.splitlines(keepends=True)
    insertions = {}
    for name in ['detect_matches'] + [n for n in SCOPE_NAMES if n in target_nodes]:
        node = target_nodes[name]
        if isinstance(node, ast.AsyncFunctionDef):
            raise RuntimeError(f'{name} 已變成 async 函式，暫不自動修改')
        first_line = min([node.lineno] + [d.lineno for d in node.decorator_list])
        decorator = '@_pii_media_cached_detect\n' if name == 'detect_matches' else '@_pii_media_cache_scope\n'
        insertions[first_line - 1] = insertions.get(first_line - 1, '') + decorator

    for offset in sorted(insertions, reverse=True):
        lines.insert(offset, insertions[offset])
    modified = ''.join(lines)
    # Install helper immediately before decorated detect_matches.
    anchor = '@_pii_media_cached_detect\n'
    if modified.count(anchor) != 1:
        raise RuntimeError('detect_matches decorator insertion error')
    modified = modified.replace(anchor, SUPPORT + anchor, 1)
    ast.parse(modified)
    for name in required:
        if f'def {name}(' not in modified:
            raise RuntimeError(f'Missing function after patch: {name}')
    return modified


def cli(argv=None):
    parser = argparse.ArgumentParser(description='Scoped PII memoization installer (dry-run by default)')
    parser.add_argument('--project', type=Path, default=Path('.'), help='presidio_app_allen 專案根目錄')
    parser.add_argument('--apply', action='store_true', help='確認後正式安裝（會自動備份）')
    parser.add_argument('--restore', action='store_true', help='僅當原檔未被改動時還原上一次備份')
    args = parser.parse_args(argv)
    project = args.project.resolve()
    target = project / 'handler_media.py'
    if not target.exists():
        parser.error(f'找不到 {target}；請指定 --project')
    backup_dir = project / '.pii_media_backups'
    manifest_file = backup_dir / 'latest_cache_patch.json'

    if args.restore:
        if not manifest_file.is_file():
            parser.error('找不到此補丁的備份紀錄，無法自動還原')
        record = json.loads(manifest_file.read_text(encoding='utf-8'))
        original = Path(record['backup'])
        current_sha = checksum(target.read_bytes())
        if current_sha != record['patched_sha256']:
            parser.error('現在的 handler_media.py 已被再次修改；為避免覆蓋，拒絕自動還原')
        if not original.is_file() or checksum(original.read_bytes()) != record['original_sha256']:
            parser.error('備份遺失或雜湊不符，拒絕還原')
        shutil.copy2(original, target)
        manifest_file.unlink()
        print('已還原:', target)
        return

    raw = target.read_bytes()
    source = raw.decode('utf-8-sig')
    modified = patch_source(source)
    if modified == source:
        print('已存在完整 PII 暫存補丁，沒有重複修改。')
        return
    print('原檔:', target)
    print('差異: 新增單次圖片/頁面生命週期 cache、3 個 scope decorator、1 個 detect decorator')
    print('不修改: OCR / NER / Regex / bbox 去重 / PDF 遮蔽正文')
    if not args.apply:
        print('目前僅預覽。正式套用請加 --apply')
        return
    backup_dir.mkdir(parents=True, exist_ok=True)
    suffix = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:6]
    backup = backup_dir / f'handler_media_{suffix}.py'
    backup.write_bytes(raw)
    # Preserve BOM if original file used UTF-8 BOM.
    new_raw = ('\ufeff' if raw.startswith(b'\xef\xbb\xbf') else '').encode('utf-8') + modified.encode('utf-8')
    tmp = target.with_suffix('.py.pii_tmp')
    tmp.write_bytes(new_raw)
    tmp.replace(target)
    manifest_file.write_text(json.dumps({
        'backup': str(backup),
        'original_sha256': checksum(raw),
        'patched_sha256': checksum(new_raw),
    }, ensure_ascii=False, indent=2), encoding='utf-8')
    print('完成套用:', target)
    print('原檔備份:', backup)
    print('回復指令: python install_pii_cache.py --project . --restore')

if __name__ == '__main__':
    cli()
