#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V6.6.9.4 *synthetic-mock-only* core-layer transaction preview.

Never use this module as a replacement for core.py. It refuses real PDF input;
all output lives in a self-deleting temporary directory. No OCR, GUI, GPU, PDF
security library, or application module is imported. The audit is injected by
this *test harness*, and is NOT a real security audit.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import tempfile
from typing import Callable

SOURCE_MAGIC = b'%V6694_SYNTHETIC_FIXTURE\n'
MASKED_MAGIC = b'%V6694_SYNTHETIC_MASKED\n'


class SafetyReject(RuntimeError):
    """Status code only: never include untrusted file paths or OCR/PII."""


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


@dataclass(frozen=True)
class SyntheticBatchResult:
    count: int
    # These are temporary paths for in-process assertion ONLY. Never log them.
    files: tuple[Path, ...]


class SyntheticCorePreview:
    """An all-or-nothing batch publication *mock*, not a production publisher.

    - Only exact allowlisted fake sources with SOURCE_MAGIC are permitted.
    - The complete batch is processed and audited before any publication.
    - Independent caller-provided `audit` must return literal True.
    - The commit uses hard links that fail if a destination already exists.
    - On *any* failure, remove only the links created by this transaction.
    - Output is confined to an auto-deleted TemporaryDirectory.
    - No real OCR, PDF output, PII, or GUI code can be invoked by this preview.

    The processor is assumed non-malicious and must never alter input sources;
    a malicious processor can violate that assumption. Source checks detect,
    but do not reverse, a malicious write to a source.
    """
    def __init__(self, allowlist: dict[Path, str]):
        if not allowlist or len(allowlist) > 5:
            raise SafetyReject('BAD_ALLOWLIST')
        self.allowlist = {Path(k).absolute(): v for k, v in allowlist.items()}
        if len(self.allowlist) != len(allowlist):
            raise SafetyReject('DUPLICATE_ALLOWLIST')
        self._temp: tempfile.TemporaryDirectory | None = None
        self.base: Path | None = None
        self.published: Path | None = None

    def __enter__(self) -> 'SyntheticCorePreview':
        self._temp = tempfile.TemporaryDirectory(prefix='pii_v6694_synthetic_')
        self.base = Path(self._temp.name).resolve(strict=True)
        self.published = self.base / 'published'
        self.published.mkdir(mode=0o700)
        return self

    def __exit__(self, *_exc) -> None:
        if self._temp is not None:
            self._temp.cleanup()
        self._temp, self.base, self.published = None, None, None

    def _check_source(self, source: Path) -> None:
        if source.is_symlink() or not source.is_file():
            raise SafetyReject('BAD_SOURCE_TYPE')
        abs_source = source.absolute()
        if abs_source not in self.allowlist:
            raise SafetyReject('SOURCE_NOT_ALLOWLISTED')
        if source.resolve(strict=True) != abs_source.resolve(strict=True):
            raise SafetyReject('SOURCE_PATH_NOT_STABLE')
        if digest(source) != self.allowlist[abs_source]:
            raise SafetyReject('SOURCE_DIGEST_MISMATCH')
        with source.open('rb') as f:
            if f.read(len(SOURCE_MAGIC)) != SOURCE_MAGIC:
                raise SafetyReject('REAL_DOCUMENT_REJECTED')

    @staticmethod
    def _check_candidate(candidate: Path, stage: Path) -> None:
        if candidate.is_symlink() or not candidate.is_file():
            raise SafetyReject('BAD_CANDIDATE_TYPE')
        if candidate.resolve(strict=True).parent != stage:
            raise SafetyReject('CANDIDATE_ESCAPED_STAGE')
        if candidate.stat().st_size <= 0:
            raise SafetyReject('EMPTY_CANDIDATE')

    def files_now(self) -> tuple[Path, ...]:
        assert self.published is not None
        return tuple(sorted(self.published.iterdir()))

    def run_batch(self, sources: list[Path], processor: Callable[[Path, Path, int], None],
                  audit: Callable[[Path, int], bool], *, commit_fail_after: int | None = None,
                  before_commit: Callable[[], None] | None = None) -> SyntheticBatchResult:
        if self.base is None or self.published is None:
            raise SafetyReject('CONTEXT_REQUIRED')
        if not (1 <= len(sources) <= 5) or len({Path(p).absolute() for p in sources}) != len(sources):
            raise SafetyReject('BAD_BATCH')
        sources = [Path(p) for p in sources]
        for source in sources:
            self._check_source(source)  # Reject entire batch before executing anything.
        if self.published.is_symlink():
            raise SafetyReject('PUBLISH_ROOT_SYMLINK')
        if self.files_now():
            raise SafetyReject('PUBLICATION_AREA_NOT_EMPTY')
        stages = self.base / 'stage'
        stages.mkdir(mode=0o700)
        candidates: list[Path] = []
        candidate_digests: list[str] = []
        committed: list[Path] = []
        try:
            for i, source in enumerate(sources):
                candidate = stages / f'candidate_{i:02}.pdf'
                # The *only* file-writing operation given to the processor is
                # a private path under this automatically deleted stage.
                processor(source, candidate, i)
                self._check_candidate(candidate, stages)
                if audit(candidate, i) is not True:
                    raise SafetyReject('INDEPENDENT_AUDIT_REJECTED')
                candidates.append(candidate)
                candidate_digests.append(digest(candidate))
                self._check_source(source)  # Detect writes to source during processor/audit.
            # Source and candidate verification again immediately before publish.
            for source in sources:
                self._check_source(source)
            if before_commit is not None:
                before_commit()  # Test injection only.
            for source in sources:
                self._check_source(source)
            for i, candidate in enumerate(candidates):
                self._check_candidate(candidate, stages)
                if digest(candidate) != candidate_digests[i]:
                    raise SafetyReject('CANDIDATE_CHANGED_AFTER_AUDIT')
                if commit_fail_after is not None and i >= commit_fail_after:
                    raise SafetyReject('INJECTED_COMMIT_FAILURE')
                dest = self.published / f'approved_{i:02}.pdf'
                if dest.exists() or dest.is_symlink():
                    raise SafetyReject('PUBLICATION_COLLISION')
                # Hard link is atomic and non-overwriting; never use os.replace
                # because overwriting a prior approved result is unacceptable.
                os.link(candidate, dest, follow_symlinks=False)
                committed.append(dest)
            # Recheck sources after commit, rollback on any evidence of mutation.
            for source in sources:
                self._check_source(source)
            return SyntheticBatchResult(len(committed), tuple(committed))
        except BaseException:
            # Remove only paths WE successfully created. Existing files untouched.
            for dest in reversed(committed):
                try:
                    dest.unlink()
                except OSError:
                    # Rollback failure must never be reported as a success.
                    raise SafetyReject('ROLLBACK_FAILED') from None
            raise
        finally:
            # Scratch files are synthetic only; no sensitive input lives here.
            for candidate in candidates:
                try:
                    if candidate.is_file() or candidate.is_symlink():
                        candidate.unlink()
                except OSError:
                    pass
            for p in stages.iterdir():
                if p.is_file() or p.is_symlink():
                    try:
                        p.unlink()
                    except OSError:
                        pass
            try:
                stages.rmdir()
            except OSError:
                pass
