"""PDF/image structural smoke checks; never treats this as proof of PII removal."""
from __future__ import annotations
import argparse
import json
from pathlib import Path


def check_file(path: Path, expect_pages=None):
    rec = {'file': path.name, 'exists': path.exists(), 'type': path.suffix.lower(), 'checks': {}}
    if not path.is_file():
        rec['status'] = 'FAIL'
        rec['error_type'] = 'FileNotFoundError'
        return rec
    try:
        rec['bytes'] = path.stat().st_size
        if path.suffix.lower() == '.pdf':
            import pymupdf as fitz
            with fitz.open(path) as d:
                rec['pages'] = len(d)
                rec['attachments'] = d.embfile_count()
                rec['text_chars'] = sum(len(page.get_text()) for page in d)
                rec['annotations'] = sum(len(list(page.annots() or [])) for page in d)
                rec['links'] = sum(len(page.get_links()) for page in d)
            rec['eof_markers'] = path.read_bytes().count(b'%%EOF')
            rec['checks'] = {'nonzero_pages': rec['pages'] > 0,
                             'expected_pages': expect_pages is None or rec['pages'] == expect_pages,
                             'no_attachments': rec['attachments'] == 0,
                             'no_text_layer': rec['text_chars'] == 0,
                             'no_annotations': rec['annotations'] == 0,
                             'no_links': rec['links'] == 0,
                             'single_eof_marker': rec['eof_markers'] == 1}
        elif path.suffix.lower() in ('.png', '.jpg', '.jpeg'):
            from PIL import Image
            with Image.open(path) as im:
                im.verify()
            with Image.open(path) as im:
                rec['dimensions'] = list(im.size)
            rec['checks'] = {'readable_image': True}
        else:
            rec['status'] = 'SKIP'
            rec['checks'] = {}
            return rec
    except Exception as e:
        rec['status'] = 'FAIL'
        rec['error_type'] = type(e).__name__
        return rec
    rec['status'] = 'PASS' if all(rec['checks'].values()) else 'REVIEW'
    return rec


def main():
    p = argparse.ArgumentParser(description='檢查 GUI 輸出的 PDF/圖片可讀性和 PDF 基本結構')
    p.add_argument('files', nargs='+', type=Path)
    p.add_argument('--expect-pages', type=int, help='PDF 預期頁數，例如 7')
    p.add_argument('--json-out', type=Path, default=Path('gui_output_structure_audit.json'))
    a = p.parse_args()
    result = [check_file(f, a.expect_pages) for f in a.files]
    a.json_out.parent.mkdir(parents=True, exist_ok=True)
    a.json_out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    for r in result:
        print(f"{r['file']}: {r['status']} {r['checks']}")
    print(f'結果：{a.json_out}')
    if any(r['status'] in ('FAIL', 'REVIEW') for r in result):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
