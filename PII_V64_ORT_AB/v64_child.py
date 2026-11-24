"""Isolated seven-page synthetic fixture runner. Discards application logs.

The only persistent child output is a small no-PII session count JSON.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

FIXTURE_SHA256='df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def sha256_file(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def main(argv=None):
    ap=argparse.ArgumentParser()
    ap.add_argument('--project-root', required=True)
    ap.add_argument('--source', required=True)
    ap.add_argument('--expected', required=True)
    ap.add_argument('--results-dir', required=True)
    ap.add_argument('--runner', required=True)
    ap.add_argument('--threads', type=int, default=0)
    args=ap.parse_args(argv)
    root=Path(args.project_root).resolve()
    source=Path(args.source).resolve()
    out=Path(args.results_dir).resolve()
    if sha256_file(source)!=FIXTURE_SHA256:
        raise RuntimeError('synthetic fixture SHA mismatch; refusing to process')
    if not str(Path(args.runner).resolve()).startswith(str(root)+os.sep):
        raise RuntimeError('runner must be inside the project root')
    if args.threads not in (0,1,2,4,6,8,12,16):
        raise ValueError('unsupported experimental trial value')
    # No PII contents are ever added to these reports.
    from v64_runtime import install_ort_thread_trial
    stats,undo=install_ort_thread_trial(args.threads if args.threads else None)
    out.mkdir(parents=True,exist_ok=True)
    child_report=out/'v64_sessions.json'
    code=0
    try:
        sys.path.insert(0,str(root))
        sys.path.insert(0,str(Path(args.runner).resolve().parent))
        sys.argv=[str(Path(args.runner).resolve()),'--source',str(source),
                  '--expected',str(Path(args.expected).resolve()),'--results-dir',str(out)]
        try:
            runpy.run_path(str(Path(args.runner).resolve()),run_name='__main__')
        except SystemExit as e:
            code=int(e.code or 0) if isinstance(e.code,int) or e.code is None else 1
    finally:
        undo()
        child_report.write_text(json.dumps({
            'trial_threads':args.threads,
            'ort_session_calls':stats.attempted,
            'ort_sessions_tuned':stats.sessions_tuned,
            'preexisting_session_options':stats.existing_options,
        },indent=2),encoding='utf-8')
    return code

if __name__=='__main__':
    raise SystemExit(main())
