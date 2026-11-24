r"""End-to-end local test for handler_media.mask_pdf against a synthetic scanned PDF.

Usage (from the presidio_app_allen project root, with this script and sample files there):
  .\.venv\Scripts\python.exe run_scanned_pdf_test.py --engine rapid
  .\.venv\Scripts\python.exe run_scanned_pdf_test.py --engine windows

This is a diagnostic tool, not a certification that a PDF is safe to share.
"""
from __future__ import annotations

import argparse
import contextlib
from collections import Counter, defaultdict
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import traceback


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1048576), b''):
            h.update(chunk)
    return h.hexdigest()


def raster_only(doc) -> bool:
    return all(not page.get_text().strip() for page in doc)


def render_pages(doc, prefix: str, folder: Path, fitz):
    from PIL import Image
    output = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False, annots=False)
        path = folder / f'{prefix}_page_{i+1:02d}.png'
        pix.save(str(path))
        output.append(Image.open(path).convert('RGB'))
    return output


def original_image_fingerprints(doc):
    """Hash decoded pixels so re-encoded unredacted copies are still detected."""
    from PIL import Image
    found = set()
    for xref in range(1, doc.xref_length()):
        try:
            item = doc.extract_image(xref)
            if not item or not item.get('image'):
                continue
            with Image.open(io.BytesIO(item['image'])) as im:
                rgb = im.convert('RGB')
                found.add((rgb.size, hashlib.sha256(rgb.tobytes()).hexdigest()))
        except Exception:
            continue
    return found


def create_preview(imgs_a, imgs_b, dest: Path):
    from PIL import Image, ImageDraw
    thumbs=[]
    for i,(a,b) in enumerate(zip(imgs_a, imgs_b),1):
        target_w=560
        h=round(a.height * target_w / a.width)
        row=Image.new('RGB',(target_w*2+30,h+66),(237,240,243))
        d=ImageDraw.Draw(row)
        d.text((14,12),f'Page {i} - ORIGINAL',fill=(35,45,57))
        d.text((target_w+24,12),f'Page {i} - MASKED',fill=(35,45,57))
        row.paste(a.resize((target_w,h)),(10,55))
        row.paste(b.resize((target_w,h)),(target_w+20,55))
        thumbs.append(row)
    if not thumbs:
        return
    sheet=Image.new('RGB',(max(im.width for im in thumbs),sum(im.height for im in thumbs)+18*(len(thumbs)-1)),(237,240,243))
    y=0
    for im in thumbs:
        sheet.paste(im,(0,y));y+=im.height+18
    sheet.save(dest)


def black_fraction(img, rect_pt):
    # PDF pages are rendered at 2 pixels per PDF point in render_pages().
    x0,y0,x1,y1=(float(c)*2.0 for c in rect_pt)
    x0=max(0,int(x0));y0=max(0,int(y0));x1=min(img.width,int(x1+0.9999));y1=min(img.height,int(y1+0.9999))
    if x1<=x0 or y1<=y0:
        return 0.0
    crop=img.crop((x0,y0,x1,y1)).convert('RGB')
    data = crop.tobytes()
    covered = sum(1 for k in range(0, len(data), 3) if max(data[k:k+3]) <= 45)
    return covered / (len(data) // 3)


def main():
    parser=argparse.ArgumentParser(description='OCR scanned PDF redaction integration test')
    parser.add_argument('--engine',choices=('rapid','windows'),default='rapid')
    base=Path(__file__).resolve().parent
    parser.add_argument('--input',type=Path,default=base/'synthetic_scanned_pii_3pages.pdf')
    parser.add_argument('--manifest',type=Path,default=base/'synthetic_scanned_pii_ground_truth.json')
    parser.add_argument('--output-dir',type=Path,default=base/'pdf_test_results')
    args=parser.parse_args()
    src=args.input.resolve();outdir=args.output_dir.resolve()
    if not src.is_file():
        parser.error(f'Missing PDF: {src}')
    outdir.mkdir(parents=True,exist_ok=True)
    dst=outdir / f'{src.stem}_masked_{args.engine}.pdf'
    if src==dst:
        parser.error('Output path must differ from source path')
    map_file=outdir / f'media_test_map_{args.engine}.json'
    log_file=outdir / f'pdf_model_log_{args.engine}.txt'
    old_hash=sha256(src)
    os.environ['PII_OCR_BACKEND']=args.engine
    os.environ.setdefault('PII_NER_BACKEND','ac')
    os.environ.setdefault('PII_C_LONG_MODE','chunk')

    # Main project must be in sys.path; don't assume the script was copied to root.
    sys.path.insert(0,str(Path.cwd()))
    try:
        import fitz
        import handler_media
    except Exception as exc:
        print('ERROR: Please run from the project root with the project .venv:',exc)
        return 2

    print('OCR backend:',os.environ['PII_OCR_BACKEND'])
    print('Source PDF:',src)
    print('Output PDF:',dst)
    print('Running existing handler_media.mask_pdf()...')
    try:
        with log_file.open('w',encoding='utf-8') as log, contextlib.redirect_stdout(log):
            entries=handler_media.mask_pdf(str(src),str(dst),str(map_file)) or []
    except Exception as exc:
        print('MASK FAILURE:',str(exc))
        print('Inspect model log:',log_file)
        return 2

    if sha256(src)!=old_hash:
        print('FAIL: source PDF was modified')
        return 2
    original=fitz.open(str(src));masked=fitz.open(str(dst))
    print(f'Pages: source={len(original)}, output={len(masked)}')
    if len(original)!=len(masked):
        print('FAIL: page count changed')
        return 2
    print('Input PDF has no text layer:',raster_only(original))
    print('Output PDF native text layer empty:',raster_only(masked))
    images_original=original_image_fingerprints(original)
    images_masked=original_image_fingerprints(masked)
    leaks=images_original.intersection(images_masked)
    if leaks:
        print('CRITICAL: original image pixels still embedded in output PDF:',len(leaks))
        print('Do not distribute this output; check PyMuPDF image redaction and garbage collection.')
    else:
        print('No identical complete original embedded images found (not a comprehensive PDF security audit).')

    src_imgs=render_pages(original,'source',outdir,fitz)
    dst_imgs=render_pages(masked,'masked',outdir,fitz)
    create_preview(src_imgs,dst_imgs,outdir/'side_by_side.png')
    by_page=defaultdict(list)
    for e in entries:
        by_page[int(e.get('page',0))].append(e)
    report={'engine':args.engine,'source':str(src),'output':str(dst),'source_sha256':old_hash,
            'page_count':len(masked),'entries_total':len(entries),
            'source_unchanged':True,'identical_original_embedded_images':len(leaks),'pages':[]}
    manifest=json.loads(args.manifest.read_text(encoding='utf-8')) if args.manifest.is_file() else None
    for i in range(len(masked)):
        actual=Counter(e.get('label','?') for e in by_page[i])
        item={'page':i+1,'detected_labels':dict(actual),'redaction_entries':len(by_page[i])}
        print(f'Page {i+1}: {len(by_page[i])} redaction entries | {dict(actual)}')
        if manifest and i<len(manifest.get('pages',[])):
            gt=manifest['pages'][i]['expected']
            checked=[]
            for ent in gt:
                frac=round(black_fraction(dst_imgs[i],ent['bbox_pt']),3)
                checked.append({'label':ent['label'],'text':ent['text'],'black_coverage':frac,
                                'visual_box_coverage':'LIKELY_COVERED' if frac>=0.65 else 'CHECK_MANUALLY'})
                print(f'    {ent["label"]:<11} {ent["text"]:<22} black coverage={frac:.1%}',
                      'OK' if frac>=0.65 else 'CHECK')
            item['expected_regions']=checked
        report['pages'].append(item)
    (outdir/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Preview:',outdir/'side_by_side.png')
    print('Detailed report:',outdir/'report.json')
    print('NOTE: Black coverage measures synthetic ground-truth regions only; it is not proof of secure redaction.')
    print('NOTE: media_test_map stores original image crops. Keep it private and delete after testing.')
    original.close();masked.close()
    return 1 if leaks else 0


if __name__=='__main__':
    raise SystemExit(main())
