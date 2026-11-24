#!/usr/bin/env python3
"""Run fixed synthetic masking in a separate process; never forward app logs."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', required=True)
    ap.add_argument('--source', required=True)
    ap.add_argument('--expected', required=True)
    ap.add_argument('--runner', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--cache', choices=['on', 'off'], required=True)
    a = ap.parse_args(argv)
    root, source, runner, expected = [Path(x).resolve() for x in (a.root, a.source, a.runner, a.expected)]
    out = Path(a.out).resolve()
    if digest(source) != FIXTURE_SHA256:
        raise RuntimeError('not the pinned synthetic fixture')
    if runner.parent != root / 'PII_V5_Security_Acceptance':
        raise RuntimeError('runner must be the V5 security acceptance runner')
    if runner.name != 'run_7page_pdf_test.py' or expected.name != 'expected_text_and_faces.json':
        raise RuntimeError('unexpected fixture runner or ground truth')
    os.environ['PII_MEDIA_CACHE'] = '1' if a.cache == 'on' else '0'
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runner.parent))
    import handler_media
    if Path(handler_media.__file__).resolve() != root / 'handler_media.py':
        raise RuntimeError('handler_media not loaded from project root')
    from v65_profile_runtime import Profiler
    profiler = Profiler(handler_media)
    code = 0
    profiler.install()
    try:
        sys.argv = [str(runner), '--source', str(source), '--expected', str(expected), '--results-dir', str(out)]
        try:
            runpy.run_path(str(runner), run_name='__main__')
        except SystemExit as e:
            code = int(e.code or 0) if (e.code is None or isinstance(e.code, int)) else 1
    finally:
        profiler.restore()
        safe = profiler.safe_summary()
        safe['cache_trial'] = a.cache
        safe['production_code_changed'] = False
        (out / 'v65_profile.json').write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding='utf-8')
        profiler.discard_sensitive()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
