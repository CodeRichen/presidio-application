"""Run only verified synthetic seven-page fixture in a private subprocess."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import sys

FIXTURE_SHA256 = 'df9362afb18880000ce85290e9055dd798126c16351c5cbd306e4e9f4b160b49'

def checksum(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()

def main():
    p=argparse.ArgumentParser()
    for arg in ('project-root','source','expected','runner','results-dir','mode'):
        p.add_argument('--'+arg, required=True)
    a=p.parse_args()
    root=Path(a.project_root).resolve()
    source=Path(a.source).resolve()
    runner=Path(a.runner).resolve()
    out=Path(a.results_dir).resolve()
    if checksum(source)!=FIXTURE_SHA256: raise RuntimeError('FIXTURE_SHA_MISMATCH')
    if root not in runner.parents: raise RuntimeError('RUNNER_OUTSIDE_PROJECT')
    if a.mode not in ('cpu','cuda'): raise RuntimeError('BAD_MODE')
    out.mkdir(parents=True, exist_ok=True)
    import onnxruntime as ort
    if a.mode == 'cuda':
        # Official ORT guidance: preload compatible CUDA/cuDNN DLLs from the
        # installed environment when available; never install dependencies.
        preload = getattr(ort, 'preload_dlls', None)
        if callable(preload):
            preload()
        else:
            import torch  # may provide compatible CUDA DLLs on Windows
    from v662_runtime import ProviderTrial
    trial=ProviderTrial(ort, a.mode, out).install()
    code=0
    try:
        sys.path.insert(0,str(root))
        sys.path.insert(0,str(runner.parent))
        sys.argv=[str(runner),'--source',str(source),
                  '--expected',str(Path(a.expected).resolve()),
                  '--results-dir',str(out)]
        try:
            runpy.run_path(str(runner),run_name='__main__')
        except SystemExit as ex:
            code = int(ex.code or 0) if isinstance(ex.code,int) or ex.code is None else 1
    finally:
        stats=trial.finish()
        (out/'v662_provider_counts.json').write_text(json.dumps(stats,indent=2), encoding='utf-8')
    return code

if __name__=='__main__':
    raise SystemExit(main())
