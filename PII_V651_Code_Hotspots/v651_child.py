"""Run only pinned synthetic PDF in an isolated subprocess with in-RAM profiling."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import runpy
import sys

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def main(argv=None):
    p = argparse.ArgumentParser()
    for flag in ('root', 'source', 'runner', 'expected', 'out'):
        p.add_argument('--' + flag, required=True)
    p.add_argument('--profile-every', type=int, default=4)
    p.add_argument('--max-profiled-calls', type=int, default=80)
    a = p.parse_args(argv)
    root = Path(a.root).resolve()
    source = Path(a.source).resolve()
    runner = Path(a.runner).resolve()
    expected = Path(a.expected).resolve()
    out = Path(a.out).resolve()
    if not (root / 'handler_media.py').is_file() or sha256(source) != FIXTURE_SHA256:
        raise RuntimeError('wrong project or fixed synthetic input')
    if runner != root / 'PII_V5_Security_Acceptance' / 'run_7page_pdf_test.py':
        raise RuntimeError('unexpected runner')
    if expected != root / 'PII_V5_Security_Acceptance' / 'expected_text_and_faces.json':
        raise RuntimeError('unexpected ground truth')
    out.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(runner.parent))
    import handler_media
    if Path(handler_media.__file__).resolve() != root / 'handler_media.py':
        raise RuntimeError('handler_media loaded outside project root')
    from v651_runtime import CodeHotspots
    hot = CodeHotspots(handler_media.handler_code, a.profile_every, a.max_profiled_calls).install()
    exit_code = 0
    try:
        sys.argv = [str(runner), '--source', str(source), '--expected', str(expected), '--results-dir', str(out)]
        try:
            runpy.run_path(str(runner), run_name='__main__')
        except SystemExit as e:
            exit_code = int(e.code or 0) if e.code is None or isinstance(e.code, int) else 1
    finally:
        hot.restore()
        (out / 'v651_hotspots_privatefree.json').write_text(
            json.dumps(hot.safe_summary(), ensure_ascii=False, indent=2), encoding='utf-8')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
