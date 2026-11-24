# -*- coding: utf-8 -*-
"""Strict SHA-256 guarded installer for the specific PII core.py pair.

Default mode: read-only. No application modules imported.
--apply: only after both exact hashes match, create exclusive backup,
then atomically replace the root core.py with the verified patch.

This does NOT validate real OCR/NER/PDF behavior. Run regression tests afterward.
"""

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from datetime import datetime

# SHA-256 from the user's current local core.py and its attached exact copy.
EXPECTED_CURRENT_SHA256 = "a409847a891a64967ccec95865e6b27771dadcf818f16ee3a8a624c109f3c2de"
# SHA-256 from the existing installer's trusted patch check and the user's prior Get-FileHash.
EXPECTED_PATCH_SHA256 = "3953b4ad859cf77911af15dc7041341ac69e7e3c00fc96b94963465ceb9ad522"


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fail-Closed core.py installation with exact SHA256 preflight")
    parser.add_argument("--project", type=Path, default=Path.cwd(), help="Path to presidio_app_allen root")
    parser.add_argument("--apply", action="store_true", help="Install verified patch after stopping all GUIs")
    args = parser.parse_args(argv)

    project = args.project.expanduser().resolve()
    current = project / "core.py"
    patch = project / "PII_Core_FailClosed_Patch" / "core.py"

    if any(not p.is_file() or p.is_symlink() for p in (current, patch)):
        raise SystemExit("REFUSED: Missing core.py/patch file, or symlink detected. No files changed.")

    current_bytes = current.read_bytes()
    patch_bytes = patch.read_bytes()
    current_hash = digest_bytes(current_bytes)
    patch_hash = digest_bytes(patch_bytes)

    print("CURRENT_SHA256:", current_hash)
    print("PATCH_SHA256:  ", patch_hash)
    if patch_hash != EXPECTED_PATCH_SHA256:
        raise SystemExit("REFUSED: Patch SHA256 differs from the original verified patch. No files changed.")

    try:
        compile(patch_bytes.decode("utf-8-sig"), str(patch), "exec")
    except (UnicodeDecodeError, SyntaxError) as e:
        raise SystemExit("REFUSED: Patch is not valid Python source. No files changed.") from e

    if current_hash == EXPECTED_PATCH_SHA256:
        print("STATUS: ALREADY_PATCHED. No files changed.")
        return

    if current_hash != EXPECTED_CURRENT_SHA256:
        raise SystemExit("REFUSED: Current core.py differs from the reviewed original. No files changed.")

    if not args.apply:
        print("STATUS: CHECK_PASS (SHA256 verified; no files changed)")
        print("To install, close all GUI/Python processes using core.py and rerun with --apply.")
        return

    # Verify once more before backup and replacement; fail rather than overwrite unexpected changes.
    if digest_bytes(current.read_bytes()) != EXPECTED_CURRENT_SHA256:
        raise SystemExit("REFUSED: core.py changed after preflight. No files changed.")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    counter = 0
    while True:
        suffix = f"_{counter}" if counter else ""
        backup = project / f"core.py.backup_{stamp}{suffix}"
        try:
            with backup.open("xb") as f:
                f.write(current_bytes)
                f.flush()
                os.fsync(f.fileno())
            break
        except FileExistsError:
            counter += 1

    # The backup was created exclusively, without clobbering previous backups.
    temporary = None
    try:
        if digest_bytes(current.read_bytes()) != EXPECTED_CURRENT_SHA256:
            raise RuntimeError("core.py changed during backup; leaving source unchanged")
        fd, temporary = tempfile.mkstemp(prefix=".pii_core_verified_", suffix=".py", dir=str(project))
        with os.fdopen(fd, "wb") as f:
            f.write(patch_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, current)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)

    print("STATUS: PATCH_INSTALLED")
    print("BACKUP:", backup.name)
    print("Reminder: Run synthetic unit tests and the full OCR/face/PDF GUI regression before handling real PII.")


if __name__ == "__main__":
    main()
