#!/usr/bin/env python3
# coding: utf-8
"""V6.6.1 runtime-only timing wrappers for the existing PDF OCR path.

No OCR text, image, crop, recognizer output, or PII values ever enter metrics.
This module is injected in a synthetic-fixture subprocess only.
"""
from __future__ import annotations

from contextvars import ContextVar
from functools import wraps
import time

_active = ContextVar('v661_active_ocr_collect', default=None)

PHASES = ('pdf_render', 'pixmap_to_pil', 'preprocess', 'ocr_backend', 'pii_boxes')


class OCRTiming:
    def __init__(self):
        self.collections = []
        self.scan_pages = []
        self.merges = []
        self.exports = []
        self.code_detect = []
        self.presidio_matches = []
        self._restorers = []

    @staticmethod
    def _elapsed(start):
        return time.perf_counter() - start

    def _patch(self, owner, name, fn):
        original = getattr(owner, name)
        setattr(owner, name, fn(original))
        self._restorers.append((owner, name, original))

    def _within_collect(self, phase):
        def wrap(original):
            @wraps(original)
            def measured(*args, **kwargs):
                record = _active.get()
                if record is None:
                    return original(*args, **kwargs)
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    record[phase] += self._elapsed(start)
                    record[phase + '_calls'] += 1
            return measured
        return wrap

    def _global_measure(self, collection):
        def wrap(original):
            @wraps(original)
            def measured(*args, **kwargs):
                start = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    collection.append(self._elapsed(start))
            return measured
        return wrap

    def _scan_measure(self):
        def wrap(original):
            @wraps(original)
            def measured(page, *args, **kwargs):
                start = time.perf_counter()
                try:
                    return original(page, *args, **kwargs)
                finally:
                    self.scan_pages.append({'page': int(page.number) + 1,
                                            'total_s': self._elapsed(start)})
            return measured
        return wrap

    def _collect_measure(self):
        def wrap(original):
            @wraps(original)
            def measured(page, zoom, *args, **kwargs):
                # Numeric page index is non-sensitive synthetic fixture metadata.
                record = {'page': int(page.number) + 1, 'zoom': float(zoom)}
                for k in PHASES:
                    record[k] = 0.0
                    record[k + '_calls'] = 0
                token = _active.set(record)
                start = time.perf_counter()
                try:
                    return original(page, zoom, *args, **kwargs)
                finally:
                    record['total_s'] = self._elapsed(start)
                    _active.reset(token)
                    self.collections.append(record)
            return measured
        return wrap

    def install(self, hm, fitz_module, image_module):
        """Fail closed if the expected stage boundaries cannot be instrumented."""
        required = ('_collect_pdf_ocr_boxes', '_mask_pdf_scanned_page',
                    'preprocess_for_ocr', 'ocr_words',
                    'find_sensitive_boxes_in_words',
                    '_merge_pdf_multizoom_boxes', 'save_secure_raster_pdf')
        missing = [n for n in required if not callable(getattr(hm, n, None))]
        if missing:
            raise RuntimeError('MISSING_TIMING_HOOKS: ' + ','.join(missing))
        if not callable(getattr(fitz_module.Page, 'get_pixmap', None)):
            raise RuntimeError('MISSING_GET_PIXMAP')
        if not callable(getattr(image_module, 'frombytes', None)):
            raise RuntimeError('MISSING_PIL_FROMBYTES')
        if not callable(getattr(hm.handler_code, 'detect', None)) or not callable(
            getattr(hm.handler_code.handler_text, 'presidio_matches', None)):
            raise RuntimeError('MISSING_DETECT_HOOKS')
        try:
            self._patch(hm, '_collect_pdf_ocr_boxes', self._collect_measure())
            self._patch(hm, '_mask_pdf_scanned_page', self._scan_measure())
            self._patch(fitz_module.Page, 'get_pixmap', self._within_collect('pdf_render'))
            self._patch(image_module, 'frombytes', self._within_collect('pixmap_to_pil'))
            self._patch(hm, 'preprocess_for_ocr', self._within_collect('preprocess'))
            self._patch(hm, 'ocr_words', self._within_collect('ocr_backend'))
            self._patch(hm, 'find_sensitive_boxes_in_words', self._within_collect('pii_boxes'))
            self._patch(hm, '_merge_pdf_multizoom_boxes', self._global_measure(self.merges))
            self._patch(hm, 'save_secure_raster_pdf', self._global_measure(self.exports))
            self._patch(hm.handler_code, 'detect', self._global_measure(self.code_detect))
            self._patch(hm.handler_code.handler_text, 'presidio_matches',
                        self._global_measure(self.presidio_matches))
        except Exception:
            self.restore()
            raise
        return self

    def restore(self):
        for owner, name, original in reversed(self._restorers):
            setattr(owner, name, original)
        self._restorers.clear()

    def summarize(self):
        rows = sorted(self.collections, key=lambda r: (r['page'], r['zoom']))
        summaries = {}
        for zoom in (2.0, 3.0):
            subset = [r for r in rows if abs(r['zoom'] - zoom) < 0.01]
            totals = {phase: round(sum(x[phase] for x in subset), 4) for phase in PHASES}
            rest = sum(r['total_s'] - sum(r[p] for p in PHASES) for r in subset)
            totals['other_collect'] = round(max(0.0, rest), 4)
            totals['collect_total'] = round(sum(r['total_s'] for r in subset), 4)
            totals['pages'] = len(subset)
            totals['phase_calls'] = {p: sum(r[p + '_calls'] for r in subset) for p in PHASES}
            summaries[str(int(zoom)) + 'x'] = totals
        per_page = []
        for r in rows:
            total = sum(r[p] for p in PHASES)
            per_page.append({
                'page': r['page'], 'zoom': r['zoom'],
                **{p + '_s': round(r[p], 4) for p in PHASES},
                'other_collect_s': round(max(0.0, r['total_s'] - total), 4),
                'collect_total_s': round(r['total_s'], 4),
            })
        return {'schema': 'pii_v661_ocr_profile_1',
                'collections': per_page, 'by_zoom': summaries,
                'scanned_pages': len(self.scan_pages),
                'scan_page_wall_s': round(sum(x['total_s'] for x in self.scan_pages), 4),
                'merge_calls': len(self.merges),
                'merge_wall_s': round(sum(self.merges), 4),
                'secure_export_calls': len(self.exports),
                'secure_export_wall_s': round(sum(self.exports), 4),
                'code_detect_calls': len(self.code_detect),
                'code_detect_wall_s': round(sum(self.code_detect), 4),
                'presidio_matches_calls': len(self.presidio_matches),
                'presidio_matches_wall_s': round(sum(self.presidio_matches), 4),
                'note': 'PII is part of pii_boxes, not additive. code_detect and presidio are nested diagnostics; do not add to totals.'}
