#!/usr/bin/env python3
# coding: utf-8
"""Run one V5 fixture pass with temporary OCR timing wrappers; no private logs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

from v66_runtime import OCRTiming

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as file:
        for block in iter(lambda: file.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def run_once(root, source, runner, expected, output):
    if sha256_file(source) != FIXTURE_SHA256:
        raise RuntimeError('NOT_APPROVED_SYNTHETIC_FIXTURE')
    if runner.name != 'run_7page_pdf_test.py' or runner.parent.name != 'PII_V5_Security_Acceptance':
        raise RuntimeError('NOT_APPROVED_RUNNER')
    if root not in runner.parents or root not in source.parents or root not in expected.parents:
        raise RuntimeError('PATH_OUTSIDE_PROJECT')
    if output.exists() and any(output.iterdir()):
        raise RuntimeError('CHILD_OUTPUT_NOT_EMPTY')
    output.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runner.parent))
    setup_start = time.perf_counter()
    import handler_media as hm
    import run_7page_pdf_test
    setup_s = time.perf_counter() - setup_start
    import PIL.Image as image_module

    timer = OCRTiming().install(hm, hm.fitz, image_module)
    try:
        wall_start = time.perf_counter()
        run_7page_pdf_test.run(source, expected, output)
        with_audit_s = time.perf_counter() - wall_start
    finally:
        timer.restore()
    summary = timer.summarize()
    summary['pre_run_setup_s'] = round(setup_s, 4)
    summary['fixture_plus_audit_s'] = round(with_audit_s, 4)
    audit = json.loads((output/'pdf_face_7page_audit.json').read_text(encoding='utf-8'))
    summary['fixture_processing_s'] = round(float(audit['processing_seconds']), 4)
    # This child writes metrics only. Output PDF/private map remain in parent temp dir.
    (output/'v66_child_metrics.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return summary


def main(argv=None):
    p = argparse.ArgumentParser()
    for key in ('project-root', 'source', 'runner', 'expected', 'results-dir'):
        p.add_argument('--' + key, required=True)
    a = p.parse_args(argv)
    root, source, runner, expected, output = [Path(v).resolve() for v in (
        a.project_root, a.source, a.runner, a.expected, a.results_dir)]
    run_once(root, source, runner, expected, output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
