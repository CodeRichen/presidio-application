#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Conservative PDF redaction audit. This is NOT a security certification.

Does not modify its inputs. No known secret strings are written to JSON.
Exit: 0 no detected blockers (may still be REVIEW); 2 detected leak/failure; 3 input/tool error.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
from pathlib import Path
import re
import sys

try:
    import pymupdf as fitz
except ImportError:
    import fitz
from PIL import Image


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalized(text: str) -> str:
    return re.sub(r"[\s\u200b\u200c\u200d]+", "", text).casefold()


def image_fingerprints(doc):
    """Search ALL xrefs, including orphaned/unreferenced images, not only page.get_images()."""
    result = {}
    for xref in range(1, doc.xref_length()):
        try:
            obj = doc.extract_image(xref)
            if not obj or not obj.get('image'):
                continue
            with Image.open(io.BytesIO(obj['image'])) as im:
                im.load()
                rgb = im.convert('RGB')
                finger = (rgb.width, rgb.height, digest(rgb.tobytes()))
            result.setdefault(finger, []).append(xref)
        except (ValueError, RuntimeError, OSError, TypeError):
            pass
    return result


def decoded_stream_leaks(doc, secrets):
    """Best-effort search of plain/decoded xref data, not substitute for embedded OCR."""
    hits = set()
    for xref in range(1, doc.xref_length()):
        try:
            data = doc.xref_stream(xref)
        except (ValueError, RuntimeError):
            continue
        if not data:
            continue
        for index, secret in enumerate(secrets):
            for encoding in ('utf-8', 'utf-16-be', 'utf-16-le'):
                if secret.encode(encoding) in data:
                    hits.add((index, xref))
                    break
    return hits


def render_black_coverage(page, bbox_pt, zoom=2):
    """Share of ground-truth rectangle that renders near-black. Conservative for our fixture."""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=False, colorspace=fitz.csRGB)
    with Image.frombytes('RGB', (pix.width, pix.height), pix.samples) as im:
        x0, y0, x1, y1 = bbox_pt
        x0 = max(0, int(x0 * zoom)); y0 = max(0, int(y0 * zoom))
        x1 = min(im.width, int(x1 * zoom + .9999)); y1 = min(im.height, int(y1 * zoom + .9999))
        if x1 <= x0 or y1 <= y0: return 0.0
        crop = im.crop((x0, y0, x1, y1))
        data = crop.tobytes()
        black = sum(1 for i in range(0,len(data),3) if max(data[i:i+3]) <= 45)
        return black / (len(data)//3)


def audit(source: Path, output: Path, *, manifest=None, explicit_secrets=(), coverage_threshold=.98, ocr_images=False):
    findings=[]
    def add(status, code, detail, **info):
        findings.append(dict(status=status, code=code, detail=detail, **info))

    source_bytes=source.read_bytes()
    output_bytes=output.read_bytes()
    if digest(source_bytes) == digest(output_bytes):
        add('FAIL','OUTPUT_IDENTICAL_TO_INPUT','輸出與原始 PDF 位元組完全相同')
    with fitz.open(str(source)) as src, fitz.open(str(output)) as out:
        if src.page_count != out.page_count:
            add('FAIL','PAGE_COUNT_CHANGED','頁數不同',source_pages=src.page_count, output_pages=out.page_count)
        else:
            add('PASS','PAGE_COUNT','頁數一致',pages=out.page_count)
        if out.is_encrypted:
            add('REVIEW','ENCRYPTED','輸出 PDF 加密，須另行人工檢查')
        if out.is_repaired:
            add('REVIEW','REPAIRED','PDF 由解析器修復，需另行確認解析結果')
        if output_bytes.count(b'%%EOF') > 1:
            add('REVIEW','MULTIPLE_REVISIONS','偵測到多個 PDF EOF 標記，可能含舊版增量更新',count=output_bytes.count(b'%%EOF'))
        else:
            add('PASS','SINGLE_REVISION','未偵測到多份 PDF EOF 修訂尾標')

        texts=[p.get_text('text', sort=True) for p in out]
        text_total=sum(len(t.strip()) for t in texts)
        add('INFO','TEXT_LAYER','輸出可抽取文字字元數',characters=text_total)

        secrets=[]
        if manifest:
            for page in manifest.get('pages',[]):
                for ent in page.get('expected',[]):
                    text=ent.get('text')
                    if isinstance(text,str) and text:
                        secrets.append(text)
        secrets.extend(str(s) for s in explicit_secrets if str(s))
        secrets=list(dict.fromkeys(secrets))

        remaining=[]
        for idx,secret in enumerate(secrets):
            if any(normalized(secret) in normalized(t) for t in texts):
                remaining.append(idx)
        if remaining:
            add('FAIL','EXTRACTABLE_TEXT_LEAK','原始敏感文字仍可從 PDF 文字層抽取',secret_indices=remaining)
        elif secrets:
            add('PASS','NO_KNOWN_TEXT_LEAK','未在文字層找到指定的敏感字串',secret_count=len(secrets))
        else:
            add('REVIEW','NO_SECRET_MANIFEST','沒有標註／敏感字串，無法執行已知敏感文字檢查')

        raw_hits=[]
        for idx,secret in enumerate(secrets):
            if any(secret.encode(enc) in output_bytes for enc in ('utf-8','utf-16-be','utf-16-le')):
                raw_hits.append(idx)
        if raw_hits:
            add('FAIL','RAW_PDF_SECRET','原始敏感文字在 PDF 原始位元組中仍可找到',secret_indices=raw_hits)
        elif secrets:
            add('PASS','RAW_PDF_CHECK','輸出原始位元組沒有明碼敏感字串')

        xref_hits=decoded_stream_leaks(out,secrets)
        if xref_hits:
            add('FAIL','DECODED_STREAM_SECRET','解碼 PDF 底層資料流仍找到敏感字串',secret_indices=sorted({x for x,_ in xref_hits}),xref_count=len({x for _,x in xref_hits}))
        elif secrets:
            add('PASS','DECODED_STREAM_CHECK','未在可解碼的 PDF 底層資料流找到明碼敏感字串')

        src_images=image_fingerprints(src)
        dst_images=image_fingerprints(out)
        reused=src_images.keys() & dst_images.keys()
        reused_xrefs=sum(len(dst_images[key]) for key in reused)
        if reused:
            # Exact image reuse does not PROVE secrets inside; still a high-priority block for synthetic redaction tests.
            add('FAIL','ORIGINAL_IMAGE_RECOVERABLE','輸出 PDF 的底層物件可提取到與原檔解碼像素完全相同的圖片；可能包含遮蔽前個資',image_groups=len(reused),output_xrefs=reused_xrefs)
        else:
            add('PASS','NO_EXACT_ORIGINAL_IMAGE','未從輸出 PDF 物件提取到解碼像素完全相同的原始圖片',source_image_groups=len(src_images),output_image_groups=len(dst_images))

        annots=0
        for page in out:
            for ann in page.annots() or []:
                annots+=1
        if annots:
            add('REVIEW','ANNOTATIONS_PRESENT','輸出含註解物件，需檢查是否僅以外觀遮住底層內容',count=annots)
        else:
            add('PASS','NO_ANNOTATIONS','輸出沒有 PDF 註解物件')

        attachments=0
        try:
            attachments=out.embfile_count()
        except (AttributeError,RuntimeError,ValueError):
            pass
        if attachments:
            add('REVIEW','ATTACHMENTS_PRESENT','輸出仍含嵌入附件，可能帶有原始資料',count=attachments)
            attachment_hits = []
            identical_attachments = 0
            try:
                source_attachments = {n: digest(src.embfile_get(n)) for n in src.embfile_names()}
                for name in out.embfile_names():
                    data = out.embfile_get(name)
                    if source_attachments.get(name) == digest(data):
                        identical_attachments += 1
                    for idx,secret in enumerate(secrets):
                        if any(secret.encode(enc) in data for enc in ('utf-8','utf-16-be','utf-16-le')):
                            attachment_hits.append(idx)
                if attachment_hits:
                    add('FAIL','ATTACHMENT_SECRET','嵌入附件仍包含敏感文字',secret_indices=sorted(set(attachment_hits)))
                if identical_attachments:
                    add('REVIEW','UNCHANGED_ATTACHMENTS','與原始 PDF 完全相同的附件可能含有敏感副本',count=identical_attachments)
            except (ValueError,RuntimeError) as e:
                add('REVIEW','ATTACHMENT_UNREADABLE','有附件無法完整檢查',error_type=type(e).__name__)
        else:
            add('PASS','NO_ATTACHMENTS','未找到 PDF 嵌入附件')

        if manifest and src.page_count==out.page_count:
            regions=[]
            for i,pg in enumerate(manifest.get('pages',[])):
                if i>=out.page_count:break
                for expected in pg.get('expected',[]):
                    if 'bbox_pt' not in expected:continue
                    pct=render_black_coverage(out[i],expected['bbox_pt'])
                    regions.append(pct)
            missed=[i for i,v in enumerate(regions) if v < coverage_threshold]
            if missed:
                add('FAIL','VISUAL_COVERAGE','部分標註區域沒有達到黑色覆蓋門檻',region_count=len(regions),uncovered_count=len(missed),indices=missed,threshold=coverage_threshold)
            else:
                add('PASS','VISUAL_COVERAGE','合成標註區域均達到黑色覆蓋門檻',region_count=len(regions),threshold=coverage_threshold)
        else:
            add('REVIEW','NO_VISUAL_GROUND_TRUTH','沒有適用的影像遮蔽座標標註，請人工檢查渲染畫面')

        if ocr_images:
            try:
                import handler_media
                checked=0; leaked=[]
                for finger,xrefs in dst_images.items():
                    xref=xrefs[0]
                    obj=out.extract_image(xref)
                    if not obj:continue
                    with Image.open(io.BytesIO(obj['image'])) as im:
                        words=handler_media.ocr_words(im.convert('RGB'))
                    ocr_text=''.join(w.get('text','') for w in words)
                    checked+=1
                    for i,s in enumerate(secrets):
                        if normalized(s) in normalized(ocr_text):leaked.append(i)
                if leaked:
                    add('FAIL','EMBEDDED_IMAGE_OCR_LEAK','OCR 在 PDF 底層可提取圖片中辨識出原始敏感字串',secret_indices=sorted(set(leaked)),images_checked=checked)
                else:
                    add('REVIEW','EMBEDDED_IMAGE_OCR_CLEAR','未於可提取圖片中 OCR 命中原始字串；OCR 仍可能漏判',images_checked=checked)
            except Exception as e:
                add('REVIEW','EMBEDDED_OCR_UNAVAILABLE','圖片物件 OCR 檢查無法執行',error_type=type(e).__name__)
        else:
            add('REVIEW','EMBEDDED_OCR_NOT_RUN','未啟用提取圖片的二次 OCR；無法排除重編碼影像藏有個資')
    statuses=Counter(f['status'] for f in findings)
    return {'status': 'FAIL' if statuses['FAIL'] else 'REVIEW' if statuses['REVIEW'] else 'PASS',
            'source_sha256':digest(source_bytes),'output_sha256':digest(output_bytes),
            'finding_counts':dict(statuses),'findings':findings,
            'disclaimer':'此工具是最佳努力的回歸檢查，不能保證所有 PDF 內部物件或影像均無可恢復個資。'}


def main(argv=None):
    ap=argparse.ArgumentParser(description='PDF 遮蔽安全檢查（離線／不修改 PDF）')
    ap.add_argument('--source',required=True,type=Path)
    ap.add_argument('--output',required=True,type=Path)
    ap.add_argument('--ground-truth',type=Path)
    ap.add_argument('--secret',action='append',default=[],help='已知敏感文字（不會寫進報告）')
    ap.add_argument('--ocr-images',action='store_true',help='嘗試以目前 OCR 引擎辨識 PDF 底層可提取圖片')
    ap.add_argument('--project',type=Path,default=Path.cwd(),help='啟用 --ocr-images 時的專案根目錄')
    ap.add_argument('--min-black',type=float,default=.98)
    ap.add_argument('--report',type=Path,default=Path('pdf_security_report.json'))
    args=ap.parse_args(argv)
    if not args.source.is_file() or not args.output.is_file():ap.error('找不到來源或輸出 PDF')
    if args.source.resolve()==args.output.resolve():ap.error('來源及輸出不能相同')
    if not (0<args.min_black<=1):ap.error('--min-black 必須在 (0,1]')
    try:
        if args.ocr_images:
            sys.path.insert(0,str(args.project.resolve()))
        manifest=json.loads(args.ground_truth.read_text(encoding='utf-8')) if args.ground_truth else None
        result=audit(args.source,args.output,manifest=manifest,explicit_secrets=args.secret,
                     coverage_threshold=args.min_black,ocr_images=args.ocr_images)
    except Exception as e:
        print(f'[ERROR] 無法完成稽核：{type(e).__name__}: {e}',file=sys.stderr)
        return 3
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print('PDF security audit:',result['status'])
    for f in result['findings']:
        print(f"  [{f['status']}] {f['code']}: {f['detail']}")
    print('報告：',args.report)
    print('注意：沒有任何自動化檢查能單獨證明 PDF 已不可恢復原始資訊。')
    return 2 if result['status']=='FAIL' else 0

if __name__=='__main__':raise SystemExit(main())
