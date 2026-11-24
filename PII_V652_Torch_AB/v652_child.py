#!/usr/bin/env python3
# coding: utf-8
"""Isolated 7-page synthetic end-to-end runner. No original text in saved metrics."""
from __future__ import annotations
import argparse, functools, hashlib, json, os, runpy, statistics, sys, time
from pathlib import Path

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def hash_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''): h.update(b)
    return h.hexdigest()

def main():
    ap = argparse.ArgumentParser()
    for arg in ('project-root', 'source', 'expected', 'runner', 'results-dir'):
        ap.add_argument('--' + arg, required=True)
    ap.add_argument('--threads', type=int, choices=[0,1,2,4,6,8,12,16], required=True)
    a = ap.parse_args()
    root, source, runner, dest = (Path(p).resolve() for p in (a.project_root, a.source, a.runner, a.results_dir))
    if hash_file(source) != FIXTURE_SHA256: raise RuntimeError('Not the approved synthetic PDF')
    if root not in runner.parents: raise RuntimeError('Runner must be inside project root')
    import torch
    initial_threads = torch.get_num_threads()
    if a.threads:
        # Change one variable only: PyTorch intra-op threads. Inter-op settings are untouched.
        torch.set_num_threads(a.threads)
    effective_threads = torch.get_num_threads()
    if a.threads and effective_threads != a.threads:
        raise RuntimeError('Torch thread setting did not take effect')
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runner.parent))
    import handler_code
    original = handler_code.detect
    timing = []
    @functools.wraps(original)
    def timed_detect(*args, **kwargs):
        start = time.perf_counter()
        try:
            return original(*args, **kwargs)
        finally:
            timing.append(time.perf_counter() - start)
    handler_code.detect = timed_detect
    dest.mkdir(parents=True, exist_ok=True)
    rc = 0
    try:
        sys.argv = [str(runner), '--source', str(source), '--expected', str(Path(a.expected).resolve()), '--results-dir', str(dest)]
        try:
            runpy.run_path(str(runner), run_name='__main__')
        except SystemExit as e:
            rc = int(e.code or 0) if isinstance(e.code, int) or e.code is None else 1
    finally:
        handler_code.detect = original
        report = {'trial_threads':a.threads, 'torch_before':initial_threads,
                  'torch_effective':effective_threads, 'code_detect_calls':len(timing),
                  'code_detect_wall_s':round(sum(timing), 4),
                  'code_detect_median_ms':round(1000*statistics.median(timing), 4) if timing else None}
        (dest/'v652_torch_stats.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    return rc

if __name__ == '__main__':
    raise SystemExit(main())
