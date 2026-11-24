"""Fail-closed source patch for a *known-layout* pdf_secure_output.py.

Usage:
  python apply_export_v3.py --file pdf_secure_output.py --check
  python apply_export_v3.py --file pdf_secure_output.py --apply
  python apply_export_v3.py --file pdf_secure_output.py --restore

Never deploy a copied older pdf_secure_output.py over the user's working copy.
"""
from __future__ import annotations
import argparse
import ast
import hashlib
import json
from pathlib import Path
import shutil
import sys

MARKER = '# PII_PDF_EXPORT_V3_PROBE'
BACKUP_EXT = '.before_v3.bak'
STATE_EXT = '.v3_patch_state.json'


def substitute_once(source, before, after, label):
    n = source.count(before)
    if n != 1:
        raise ValueError(f'不支援目前程式結構：{label} 預期 1 個錨點，實際找到 {n} 個；未修改檔案。')
    return source.replace(before, after, 1)


def patch(source):
    if MARKER in source:
        raise ValueError('目前檔案已套用 V3，無須重複套用。')
    s = source
    s = substitute_once(s,
        'import tempfile\n',
        'import tempfile\n\n' + MARKER + '\nfrom pdf_export_probe_v3 import ExportProbe\n', 'import')
    s = substitute_once(s,
        '    page_sizes = []\n    try:\n',
        '    page_sizes = []\n' +
        '    _v3_probe = ExportProbe(page_count=len(masked_doc), dpi=dpi)\n' +
        '    _v3_success = False\n' +
        '    try:\n', 'probe initialize')
    s = substitute_once(s,
        '                pix = source_page.get_pixmap(\n'
        '                    dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False\n'
        '                )\n'
        '                target = fresh.new_page(width=visible.width, height=visible.height)\n'
        '                target.insert_image(target.rect, stream=pix.tobytes("png"), keep_proportion=False)\n'
        '                del pix\n',
        '                _t = _v3_probe.begin()\n'
        '                pix = source_page.get_pixmap(\n'
        '                    dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False\n'
        '                )\n'
        '                _v3_probe.end("page.render", _t, page=len(page_sizes))\n'
        '                _t = _v3_probe.begin()\n'
        '                _encoded_png = pix.tobytes("png")\n'
        '                _v3_probe.end("page.encode_png", _t, page=len(page_sizes))\n'
        '                _t = _v3_probe.begin()\n'
        '                target = fresh.new_page(width=visible.width, height=visible.height)\n'
        '                _v3_probe.end("page.new_page", _t, page=len(page_sizes))\n'
        '                _t = _v3_probe.begin()\n'
        '                target.insert_image(target.rect, stream=_encoded_png, keep_proportion=False)\n'
        '                _v3_probe.end("page.insert_image", _t, page=len(page_sizes))\n'
        '                del _encoded_png\n'
        '                del pix\n', 'render and encode')
    s = substitute_once(s,
        '            fresh.save(temporary_path, garbage=4, deflate=True, clean=True, incremental=False)\n',
        '            _t = _v3_probe.begin()\n'
        '            fresh.save(temporary_path, garbage=4, deflate=True, clean=True, incremental=False)\n'
        '            _v3_probe.end("pdf.save", _t)\n', 'save')
    s = substitute_once(s,
        '        _verify_secure_raster_pdf(temporary_path, page_sizes)\n'
        '        os.replace(temporary_path, output_path)\n'
        '    finally:\n'
        '        if os.path.exists(temporary_path):\n'
        '            os.remove(temporary_path)\n',
        '        _t = _v3_probe.begin()\n'
        '        _verify_secure_raster_pdf(temporary_path, page_sizes)\n'
        '        _v3_probe.end("pdf.verify", _t)\n'
        '        _t = _v3_probe.begin()\n'
        '        os.replace(temporary_path, output_path)\n'
        '        _v3_probe.end("pdf.atomic_replace", _t)\n'
        '        _v3_success = True\n'
        '    finally:\n'
        '        try:\n'
        '            if os.path.exists(temporary_path):\n'
        '                os.remove(temporary_path)\n'
        '        finally:\n'
        '            _v3_probe.finish(success=_v3_success)\n', 'verify and replace')
    ast.parse(s)
    return s


def hash_text(t):
    return hashlib.sha256(t.encode('utf-8')).hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--file', type=Path, required=True)
    opts = p.add_mutually_exclusive_group(required=True)
    opts.add_argument('--check', action='store_true')
    opts.add_argument('--apply', action='store_true')
    opts.add_argument('--restore', action='store_true')
    a = p.parse_args(argv)
    target = a.file.resolve()
    bak = Path(str(target) + BACKUP_EXT)
    state = Path(str(target) + STATE_EXT)
    if a.restore:
        if not (bak.is_file() and state.is_file()):
            p.error('找不到 V3 備份或 patch state，未修改檔案。')
        saved = json.loads(state.read_text(encoding='utf-8'))
        current = target.read_text(encoding='utf-8')
        original = bak.read_text(encoding='utf-8')
        if hash_text(current) != saved['patched_sha256'] or hash_text(original) != saved['original_sha256']:
            p.error('目前檔案與修補時的版本不同，禁止自動還原以避免覆蓋後續修改。')
        shutil.copy2(bak, target)
        print('[PASS] 已從備份恢復原始 pdf_secure_output.py；請確認功能。')
        return 0
    if not target.is_file():
        p.error(f'找不到檔案：{target}')
    original = target.read_text(encoding='utf-8-sig')
    patched = patch(original)
    if a.check:
        print('[PASS] V3 對目前 pdf_secure_output.py 的錨點比對成功，不會修改檔案。')
        return 0
    if bak.exists() or state.exists():
        p.error('發現先前的 V3 備份或狀態檔。為避免覆蓋，先檢查並手動處理。')
    helper = Path(__file__).with_name('pdf_export_probe_v3.py')
    if not helper.is_file():
        p.error('缺少 pdf_export_probe_v3.py')
    if not (target.parent / 'pdf_export_probe_v3.py').is_file():
        shutil.copy2(helper, target.parent / 'pdf_export_probe_v3.py')
    bak.write_text(original, encoding='utf-8')
    try:
        target.write_text(patched, encoding='utf-8')
        state.write_text(json.dumps({'original_sha256':hash_text(original),
                                     'patched_sha256':hash_text(patched)}, indent=2), encoding='utf-8')
    except Exception:
        shutil.copy2(bak, target)
        raise
    print(f'[PASS] 已套用 V3 並備份原檔至：{bak.name}')
    print('V3 量測預設關閉；請透過環境變數 PII_PDF_EXPORT_V3=1 啟用。')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except ValueError as e:
        print(f'[STOP] {e}', file=sys.stderr)
        sys.exit(1)
