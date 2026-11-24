#!/usr/bin/env python3
# coding: utf-8
"""V5 security acceptance: re-run unchanged synthetic 7-page test, baseline vs V5.

Never collects original PII/crops in its permanent reports. Private maps and
intermediate PDFs are created only within a temporary directory and removed.
This is NOT a general security certification.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time

try:
    import pymupdf as fitz
except ImportError:
    import fitz

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
EXPECTED_TEXT_REGIONS = 27
EXPECTED_FACE_MAP_REGIONS = 17


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def check_pdf(path: Path, expected_pages: int = 7) -> dict:
    """Verify structural properties of a freshly reconstructed raster-only PDF."""
    result = {'pages': 0, 'attachments': 0, 'text_chars': 0,
              'annotations': 0, 'links': 0, 'widgets': 0,
              'images_per_page': [], 'has_forms': False, 'eof_markers': 0}
    with fitz.open(str(path)) as doc:
        if doc.needs_pass or not doc.is_pdf:
            raise ValueError('輸出文件格式或加密狀態異常')
        result['pages'] = len(doc)
        result['attachments'] = doc.embfile_count()
        result['has_forms'] = bool(doc.is_form_pdf)
        for page in doc:
            result['text_chars'] += len(page.get_text('text'))
            result['annotations'] += len(list(page.annots() or []))
            result['links'] += len(page.get_links())
            result['widgets'] += len(list(page.widgets() or []))
            result['images_per_page'].append(len(page.get_images(full=True)))
    with open(path, 'rb') as f:
        # Synthetic test fixture: 20 MiB, bounded and fine for streaming count.
        tail = b''
        count = 0
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            data = tail + block
            count += data.count(b'%%EOF')
            tail = data[-4:]
        result['eof_markers'] = count
    result['pass'] = (result['pages'] == expected_pages
        and result['attachments'] == 0 and result['text_chars'] == 0
        and result['annotations'] == 0 and result['links'] == 0
        and result['widgets'] == 0 and not result['has_forms']
        and result['images_per_page'] == [1] * expected_pages
        and result['eof_markers'] == 1)
    return result


def compare_page_pixels(base_path: Path, v5_path: Path, dpi: int = 150) -> dict:
    """Compare rendered visible RGB page pixels; no pixel data is written out."""
    matches = []
    with fitz.open(str(base_path)) as a, fitz.open(str(v5_path)) as b:
        if len(a) != len(b):
            return {'pages_compared': 0, 'identical_pages': 0, 'all_identical': False}
        for pa, pb in zip(a, b):
            if tuple(pa.rect) != tuple(pb.rect):
                matches.append(False)
                continue
            xa = pa.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB, annots=False)
            xb = pb.get_pixmap(dpi=dpi, alpha=False, colorspace=fitz.csRGB, annots=False)
            matches.append(xa.width == xb.width and xa.height == xb.height
                and xa.samples == xb.samples)
            del xa, xb
    return {'pages_compared': len(matches), 'identical_pages': sum(matches),
            'all_identical': bool(matches) and all(matches)}


def audit_status(path: Path) -> dict:
    report = json.loads(path.read_text(encoding='utf-8'))
    summ = report.get('summary', {})
    structure = report.get('structure', {})
    rows = report.get('scenarios', [])
    valid = (
        report.get('status') == 'PASS_AUTOMATED'
        and report.get('automated_structure_and_text_checks') == 'PASS'
        and summ.get('text_regions') == EXPECTED_TEXT_REGIONS
        and summ.get('text_regions_pass_98pct') == EXPECTED_TEXT_REGIONS
        and summ.get('face_map_regions_checked') == EXPECTED_FACE_MAP_REGIONS
        and summ.get('face_map_regions_pass_98pct') == EXPECTED_FACE_MAP_REGIONS
        and summ.get('face_counts_needing_review') == []
        and len(rows) == 7
        and structure.get('source_attachments') == 1
        and structure.get('output_attachments') == 0
        and structure.get('source_pages') == 7
        and structure.get('output_pages') == 7
        and structure.get('output_text_chars') == 0
        and structure.get('output_annotations') == 0
        and structure.get('output_eof_markers') == 1
    )
    return {
        'automated_pass': bool(valid),
        'text_regions_pass': summ.get('text_regions_pass_98pct'),
        'text_regions_expected': summ.get('text_regions'),
        'face_map_boxes_pass': summ.get('face_map_regions_pass_98pct'),
        'face_map_boxes_checked': summ.get('face_map_regions_checked'),
        'face_counts_needing_review': summ.get('face_counts_needing_review'),
        'per_page_text_pass': [r.get('text_pass_98pct') for r in rows],
        'per_page_face_map_counts': [r.get('detected_faces_from_map') for r in rows],
        'automated_status': report.get('status'),
        'seconds': report.get('processing_seconds'),
    }


def check_setup(root: Path, source: Path, toolkit: Path) -> None:
    if not (root / 'handler_media.py').is_file():
        raise FileNotFoundError('專案根目錄找不到 handler_media.py')
    secure = root / 'pdf_secure_output.py'
    if not secure.is_file() or '# BEGIN PII_PDF_V5_PROCESS_ISOLATION' not in secure.read_text(encoding='utf-8'):
        raise RuntimeError('未偵測到已安裝 V5 的 pdf_secure_output.py')
    for filename in ('pdf_secure_v5.py','pdf_secure_v5_worker.py'):
        if not (root / filename).is_file():
            raise FileNotFoundError(f'專案缺少 V5 子程序模組：{filename}')
    if not (toolkit / 'run_7page_pdf_test.py').is_file() or not (toolkit / 'audit_7page_pdf.py').is_file():
        raise RuntimeError('測試包內缺少七頁整合測試程式')
    if not source.is_file():
        raise FileNotFoundError('找不到原始七頁合成 PDF')
    if sha256_file(source) != FIXTURE_SHA256:
        raise ValueError('輸入檔案與原始七頁合成測試 PDF 的 SHA-256 不同；為避免誤處理真實個資，已中止')


def run_one(mode: str, root: Path, source: Path, expected: Path, toolkit: Path, scratch: Path,
            timeout: int) -> dict:
    outdir = scratch / mode
    outdir.mkdir()
    env = os.environ.copy()
    env.update({
        'PII_PDF_EXPORT_V5': '1' if mode == 'v5' else '0',
        'PII_PDF_EXPORT_V3': '0',
        'PII_OCR_BACKEND': 'rapid',
        'PII_NER_BACKEND': 'ac',
        'PII_FACE_ENABLED': '1',
        'PII_MEDIA_FP_PROFILE': 'synthetic_face_test',
        'PII_MEDIA_FP_REPORT': '0',
        'PII_MEDIA_CACHE': '1',
        'PII_ZH_SINGLE_PASS': '1',
    })
    cmd = [sys.executable, str(toolkit / 'run_7page_pdf_test.py'),
           '--source', str(source), '--expected', str(expected), '--results-dir', str(outdir)]
    start = time.perf_counter()
    # Do not save stdout/stderr: handler modules may print masked values and private paths.
    try:
        p = subprocess.run(cmd, cwd=str(root), env=env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return {'execution_ok': False, 'reason': 'timeout', 'elapsed_s': round(time.perf_counter()-start, 3)}
    result = {'execution_ok': p.returncode == 0,
              'return_code': p.returncode,
              'elapsed_s': round(time.perf_counter()-start, 3)}
    output = outdir / 'pii_face_7page_masked.pdf'
    audit = outdir / 'pdf_face_7page_audit.json'
    if p.returncode != 0 or not output.is_file() or not audit.is_file():
        result['reason'] = 'execution_or_output_missing'
        return result
    try:
        result['ground_truth'] = audit_status(audit)
        result['pdf_structure'] = check_pdf(output)
        result['pdf_bytes'] = output.stat().st_size
    except Exception as e:
        result['execution_ok'] = False
        result['reason'] = f'verification_error_{type(e).__name__}'
    return result


def write_report(destination: Path, report: dict) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    (destination / 'v5_security_acceptance.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    a, b = report.get('baseline',{}), report.get('v5',{})
    px = report.get('pixel_comparison',{})
    lines = [
        '# V5 PDF 安全回歸驗收報告', '',
        f"- 自動驗收結果：**{report['status']}**",
        '- 本測試僅使用固定 SHA-256 的七頁合成 PDF，不包含原文、個資值或私有還原裁圖',
        '- 人臉數量為原始資料集的目視估計，不是經驗證的人臉 Ground Truth', '',
        '| 指標 | V5 關閉 | V5 啟用 |', '|---|---:|---:|',
        f"| 運行成功 | {a.get('execution_ok')} | {b.get('execution_ok')} |",
        f"| 文字 PII 遮蔽 >=98% | {a.get('ground_truth',{}).get('text_regions_pass', '—')}/27 | {b.get('ground_truth',{}).get('text_regions_pass','—')}/27 |",
        f"| FACE-map 框遮蔽 >=98% | {a.get('ground_truth',{}).get('face_map_boxes_pass','—')}/17 | {b.get('ground_truth',{}).get('face_map_boxes_pass','—')}/17 |",
        f"| PDF 結構 | {a.get('pdf_structure',{}).get('pass','—')} | {b.get('pdf_structure',{}).get('pass','—')} |",
        f"| 處理秒數 (含初始化) | {a.get('elapsed_s','—')} | {b.get('elapsed_s','—')} |",
        f"| PDF 位元組 | {a.get('pdf_bytes','—')} | {b.get('pdf_bytes','—')} |",
        '',
        f"- 前後頁面像素一致：{px.get('identical_pages','—')}/{px.get('pages_compared','—')} 頁",
        '- **仍須逐頁人工確認小臉、側臉、遮擋臉與誤遮蔽**。',
        '- 自動 PASS 不代表能保證所有真實文件的 PII 辨識率。',
        '',
    ]
    (destination / 'v5_security_acceptance.md').write_text('\n'.join(lines), encoding='utf-8')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description='七頁合成 PDF：原版與 V5 的安全 A/B 回歸測試')
    ap.add_argument('--project-root', default='.', help='presidio_app_allen 專案根目錄')
    ap.add_argument('--source', default=r'PII_Face_PDF_7page_Test/pii_face_7page_scan_source.pdf')
    ap.add_argument('--results-dir', default='v5_security_acceptance_results')
    ap.add_argument('--timeout', type=int, default=1200, help='每次處理的最長秒數')
    ap.add_argument('--keep-v5-pdf', action='store_true', help='僅在通過自動驗收後，保留合成 PDF 供逐頁目視檢查')
    args = ap.parse_args(argv)
    root = Path(args.project_root).resolve()
    source = (root / args.source).resolve()
    dest = (root / args.results_dir).resolve()
    toolkit = Path(__file__).resolve().parent
    report = {'schema': 'v5_security_acceptance_1', 'status': 'FAIL',
              'fixture_sha256_verified': False,
              'baseline': {}, 'v5': {}, 'pixel_comparison': {},
              'manual_face_review_required': True,
              'note': 'Only known synthetic PDF; temporary private maps and masked PDFs deleted on exit.'}
    try:
        check_setup(root, source, toolkit)
        expected = toolkit / 'expected_text_and_faces.json'
        fixture = json.loads(expected.read_text(encoding='utf-8'))
        if len(fixture['samples']) != 7 or sum(len(s['expected_text']) for s in fixture['samples']) != 27:
            raise ValueError('內附 Ground Truth 異常')
        report['fixture_sha256_verified'] = True
        with tempfile.TemporaryDirectory(prefix='pii_v5_gate_') as tempdir:
            scratch = Path(tempdir)
            for mode in ('baseline', 'v5'):
                print(f'[V5 安全驗收] 開始 {mode} 模式；處理輸出與私有 map 僅存暫存資料夾')
                report[mode] = run_one(mode, root, source, expected, toolkit, scratch, args.timeout)
                if not (report[mode].get('execution_ok')
                    and report[mode].get('ground_truth',{}).get('automated_pass')
                    and report[mode].get('pdf_structure',{}).get('pass')):
                    report['status'] = 'FAIL'
                    break
            else:
                report['pixel_comparison'] = compare_page_pixels(
                    scratch/'baseline'/'pii_face_7page_masked.pdf',
                    scratch/'v5'/'pii_face_7page_masked.pdf', dpi=150)
                report['status'] = 'PASS_AUTOMATED' if report['pixel_comparison']['all_identical'] else 'FAIL'
                if report['status'] == 'PASS_AUTOMATED' and args.keep_v5_pdf:
                    dest.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(scratch/'v5'/'pii_face_7page_masked.pdf',
                                 dest/'v5_masked_for_manual_review.pdf')
                    report['manual_review_pdf_saved'] = True
    except Exception as exc:
        report['error_type'] = type(exc).__name__
        report['error_message'] = '測試準備或執行失敗；請確認 V5 安裝、固定的合成測試檔與專案相依套件'
    finally:
        write_report(dest, report)
        print(f"[V5 安全驗收] {report['status']}")
        print(f'[V5 安全驗收] 報告：{dest / "v5_security_acceptance.json"}')
        print('[隱私] 請勿上傳任何 PRIVATE_restore_map_DO_NOT_SHARE.json')
    return 0 if report['status'] == 'PASS_AUTOMATED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
