"""Optional and privacy-minimizing timings for secure PDF raster export.

Set PII_PDF_EXPORT_V3=1 to record stage timings. No file paths, text,
page pixels, source filenames, crops or restore-map values are recorded.
Diagnostic failures never bypass/disable the original PDF security checks.
"""
from __future__ import annotations
import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class ExportProbe:
    def __init__(self, page_count: int, dpi: int):
        self.enabled = os.environ.get('PII_PDF_EXPORT_V3', '0').strip() == '1'
        self.steps = []
        self.page_count = page_count
        self.dpi = dpi
        self.started_mono = time.perf_counter()
        self.started_epoch_ms = round(time.time() * 1000, 2)

    def begin(self):
        return (time.perf_counter(), time.time()) if self.enabled else None

    def end(self, stage: str, start, *, page: int | None = None):
        if start is None:
            return
        ended_mono = time.perf_counter()
        ended_epoch = time.time()
        entry = {
            'stage': stage,
            'duration_ms': round((ended_mono - start[0]) * 1000, 3),
            'start_epoch_ms': round(start[1] * 1000, 2),
            'end_epoch_ms': round(ended_epoch * 1000, 2),
        }
        if page is not None:
            entry['page'] = page
        self.steps.append(entry)

    def finish(self, *, success: bool):
        if not self.enabled:
            return
        try:
            # Diagnostic output is intentionally independent of the masked PDF.
            # Never include input/output file names or OCR/PII content.
            parent = Path(os.environ.get('PII_PDF_EXPORT_V3_DIR', 'gui_v3_export_results'))
            parent.mkdir(parents=True, exist_ok=True)
            when = datetime.now(timezone.utc)
            filename = f'pdf_v3_{when:%Y%m%d_%H%M%S}_{os.getpid()}_{time.time_ns() % 1000000000:09d}.json'
            data = {
                'schema_version': 'pdf_secure_export_v3_1',
                'status': 'OK' if success else 'ERROR',
                'page_count': self.page_count,
                'dpi': self.dpi,
                'started_epoch_ms': self.started_epoch_ms,
                'ended_epoch_ms': round(time.time() * 1000, 2),
                'total_s': round(time.perf_counter() - self.started_mono, 4),
                'steps': self.steps,
                'note': 'Timings only; no PII, paths, rendered content or restore-map values recorded.',
            }
            fd, temp_path = tempfile.mkstemp(prefix='.tmp_pdf_v3_', suffix='.json', dir=str(parent))
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(temp_path, str(parent / filename))
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        except Exception:
            # Measurement failure must not alter a securely produced PDF.
            pass
