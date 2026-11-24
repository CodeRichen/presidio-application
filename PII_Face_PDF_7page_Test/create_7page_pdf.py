#!/usr/bin/env python3
# coding: utf-8
"""Create raster-page PDF from the seven supplied synthetic PII+face PNGs.
The optional synthetic attachment is a canary to verify secure PDF rebuilding.
"""
import argparse
import io
import json
from pathlib import Path
import zipfile
import fitz
from PIL import Image


def run(assets, expected, output, attachment=True):
    fixture = json.loads(Path(expected).read_text(encoding='utf-8'))
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f'Output already exists: {output} (move it before rerunning)')
    assets = Path(assets)
    zf = zipfile.ZipFile(assets) if assets.is_file() else None
    try:
        with fitz.open() as doc:
            for i, sample in enumerate(fixture['samples'], 1):
                filename = sample['filename']
                raw = zf.read(filename) if zf else (assets / filename).read_bytes()
                with Image.open(io.BytesIO(raw)) as im:
                    size = im.size
                if list(size) != sample['size_px']:
                    raise ValueError(f'Image size mismatch: {filename}: {size} != {sample["size_px"]}')
                # 842pt wide; height preserves the source PNG's aspect ratio.
                w_pt = 842.0
                h_pt = w_pt * size[1] / size[0]
                page = doc.new_page(width=w_pt, height=h_pt)
                page.insert_image(page.rect, stream=raw, keep_proportion=False)
                assert not page.get_text().strip(), f'Unexpected PDF text layer on page {i}'
            if attachment:
                doc.embfile_add('synthetic_attachment_canary.txt',
                    b'SYNTHETIC-TEST-ONLY\nDO_NOT_COPY_SOURCE_PDF_ATTACHMENTS\n')
            doc.set_metadata({})
            doc.save(str(output), garbage=4, deflate=True, incremental=False)
    finally:
        if zf is not None:
            zf.close()
    with fitz.open(output) as check:
        assert len(check) == len(fixture['samples']) == 7
        assert sum(len(p.get_text()) for p in check) == 0
        assert check.embfile_count() == (1 if attachment else 0)
        assert all(len(p.get_images(full=True)) >= 1 for p in check)
    print(f'CREATED: {output}')
    print('PAGES: 7 | PAGE_TEXT: 0 | SOURCE_ATTACHMENT:', int(attachment))
    print('NOTE: Input PDF deliberately contains synthetic sensitive-looking text *in images*; do not share it as redacted output.')
    return output


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--assets', default='PII_Face_7_整合測試圖片.zip', help='Original seven-image ZIP or PNG directory')
    p.add_argument('--expected', default='expected_text_and_faces.json')
    p.add_argument('--output', default='pii_face_7page_scan_source.pdf')
    p.add_argument('--without-attachment', action='store_true')
    a = p.parse_args()
    run(a.assets, a.expected, a.output, not a.without_attachment)
