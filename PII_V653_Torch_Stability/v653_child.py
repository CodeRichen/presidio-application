#!/usr/bin/env python3
# coding: utf-8
"""V6.5.3: two complete, independent-output passes in one model process.
Fixed verified synthetic fixture only; never writes OCR inputs or private map to report.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import statistics
import sys
import time
from pathlib import Path

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'
THREAD_CHOICES = (0, 1, 2, 4, 6, 8, 12, 16)


def sha256_file(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def run_two_passes(root, source, runner, expected, dest, threads):
    setup_started = time.perf_counter()
    if sha256_file(source) != FIXTURE_SHA256:
        raise RuntimeError('Source is not the approved synthetic 7-page fixture')
    if root not in runner.parents:
        raise RuntimeError('Fixture runner is not within the project root')
    if runner.name != 'run_7page_pdf_test.py':
        raise RuntimeError('Unexpected fixture runner')
    import torch
    before = int(torch.get_num_threads())
    if threads:
        torch.set_num_threads(threads)
    effective = int(torch.get_num_threads())
    if threads and effective != threads:
        raise RuntimeError('Torch intra-op thread setting not applied')

    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runner.parent))
    import handler_code
    import run_7page_pdf_test
    original = handler_code.detect
    pre_run_setup_s = round(time.perf_counter() - setup_started, 4)
    current_times = []

    @functools.wraps(original)
    def timed_detect(*args, **kwargs):
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            current_times.append(time.perf_counter() - start)

    handler_code.detect = timed_detect
    passes = {}
    try:
        for label in ('first', 'second'):
            current_times = []
            outdir = dest / label
            outdir.mkdir(parents=True, exist_ok=False)
            start = time.perf_counter()
            # Each pass gets fresh output and private map. Models in this Python
            # process stay initialized; per-call PII media cache remains scoped.
            run_7page_pdf_test.run(source, expected, outdir)
            complete_wall = time.perf_counter() - start
            audit_path = outdir / 'pdf_face_7page_audit.json'
            audit = json.loads(audit_path.read_text(encoding='utf-8'))
            passes[label] = {
                'fixture_processing_s': round(float(audit['processing_seconds']), 4),
                'fixture_plus_audit_s': round(complete_wall, 4),
                'code_detect_calls': len(current_times),
                'code_detect_wall_s': round(sum(current_times), 4),
                'code_detect_median_ms': round(1000 * statistics.median(current_times), 4)
                    if current_times else None,
            }
    finally:
        handler_code.detect = original

    return {'schema':'pii_v653_child_1', 'trial_threads':threads,
            'torch_before': before, 'torch_effective': effective,
            'pre_run_setup_s':pre_run_setup_s, 'passes':passes}


def main(argv=None):
    p = argparse.ArgumentParser()
    for key in ('project-root', 'source', 'runner', 'expected', 'results-dir'):
        p.add_argument('--' + key, required=True)
    p.add_argument('--threads', type=int, required=True, choices=THREAD_CHOICES)
    a = p.parse_args(argv)
    root, source, runner, expected, dest = [Path(v).resolve() for v in (
        a.project_root, a.source, a.runner, a.expected, a.results_dir)]
    if dest.exists() and any(dest.iterdir()):
        raise RuntimeError('Results directory must start empty')
    dest.mkdir(parents=True, exist_ok=True)
    stats = run_two_passes(root, source, runner, expected, dest, a.threads)
    (dest / 'v653_child_stats.json').write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
