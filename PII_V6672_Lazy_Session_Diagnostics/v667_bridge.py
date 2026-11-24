#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""In-memory, length-prefixed OCR IPC. Only approved synthetic fixture in V667.
Worker and parent never write raw OCR text to reports/logs/disk.
"""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

MAX_REQ = 48 * 1024 * 1024
MAX_RESP = 8 * 1024 * 1024


def read_exact(pipe, n):
    pieces = []
    left = n
    while left:
        part = pipe.read(left)
        if not part:
            raise RuntimeError('WORKER_PROTOCOL_EOF')
        pieces.append(part)
        left -= len(part)
    return b''.join(pieces)


def send_frame(pipe, payload, max_size=MAX_REQ):
    if len(payload) > max_size:
        raise RuntimeError('WORKER_FRAME_TOO_LARGE')
    pipe.write(struct.pack('!I', len(payload)))
    pipe.write(payload)
    pipe.flush()


def read_frame(pipe, max_size=MAX_RESP):
    size, = struct.unpack('!I', read_exact(pipe, 4))
    if size <= 0 or size > max_size:
        raise RuntimeError('WORKER_FRAME_INVALID')
    return read_exact(pipe, size)


def strict_words_equal(a, b):
    """Exact ordered adapter agreement; never persist contents or hashes."""
    keys = {'text', 'left', 'top', 'width', 'height', 'line_id'}
    if not isinstance(a, list) or not isinstance(b, list) or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if not isinstance(x, dict) or not isinstance(y, dict):
            return False
        if set(x) != keys or set(y) != keys:
            return False
        if x != y:
            return False
    return bool(a)


class HybridOCRBridge:
    def __init__(self, gpu_python: Path, worker: Path, root: Path):
        self.child = subprocess.Popen(
            [str(gpu_python), '-u', str(worker)], cwd=str(root),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.ocr_calls = 0
        self.ocr_wall_s = 0.0
        try:
            ready = json.loads(read_frame(self.child.stdout))
            if ready.get('status') != 'READY' or ready.get('mode') != 'hybrid':
                raise RuntimeError('WORKER_NOT_READY')
        except BaseException:
            self.close(force=True)
            raise

    def ocr_words(self, pil_img):
        # Transport is a private pipe; the image and OCR text are never saved.
        start = time.perf_counter()
        from PIL import Image
        buf = io.BytesIO()
        pil_img.convert('RGB').save(buf, format='PNG')
        send_frame(self.child.stdin, buf.getvalue())
        answer = json.loads(read_frame(self.child.stdout))
        if answer.get('status') != 'OK' or not isinstance(answer.get('words'), list):
            raise RuntimeError('GPU_WORKER_OCR_FAILED')
        words = answer['words']
        self.ocr_calls += 1
        self.ocr_wall_s += time.perf_counter() - start
        return words

    def finish(self):
        send_frame(self.child.stdin, b'!DONE')
        report = json.loads(read_frame(self.child.stdout))
        if report.get('status') != 'SUMMARY':
            raise RuntimeError('MISSING_WORKER_PROFILE')
        self.child.stdin.close()
        self.child.wait(timeout=90)
        if self.child.returncode != 0:
            raise RuntimeError('GPU_WORKER_EXIT_FAILURE')
        return report['provider_evidence']

    def close(self, force=False):
        if self.child.poll() is None:
            self.child.kill()
        try:
            self.child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (self.child.stdin, self.child.stdout):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass
