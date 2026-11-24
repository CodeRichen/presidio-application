r"""Launch the existing PII GUI with the seven-card synthetic benchmark profile.

TEST-ONLY: this activates a fixture-specific false-positive whitelist for the
entire lifetime of this UI process. Do not use it for real/private documents.
Run from the project's .venv Python:
    .\.venv\Scripts\python.exe .\launch_ui_synthetic_test.py
"""
from __future__ import annotations

import os
from pathlib import Path
import runpy
import sys


def main() -> None:
    root = Path(__file__).resolve().parent
    ui_path = root / "ui.py"
    if not ui_path.is_file():
        raise FileNotFoundError(f"ui.py 不存在: {ui_path}")
    if not (root / "media_fp_guard.py").is_file():
        raise FileNotFoundError("找不到 media_fp_guard.py，請先安裝 v2 過濾模組")
    # Must be set BEFORE ui.py imports core -> handler_media.
    os.environ.update({
        "PII_MEDIA_FP_PROFILE": "synthetic_face_test",
        "PII_MEDIA_FP_REPORT": "1",
        "PII_OCR_BACKEND": "rapid",
        "PII_NER_BACKEND": "ac",
        "PII_MEDIA_CACHE": "1",
        "PII_ZH_SINGLE_PASS": "1",
        "PII_FACE_ENABLED": "1",
    })
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    print("[合成測試模式] 已啟用：僅供七張合成圖片／七頁掃描 PDF 回歸測試")
    print("[提醒] 此模式會排除部分固定測試文字，請勿處理真實個資文件")
    print(f"[進入 UI] {ui_path}")
    runpy.run_path(str(ui_path), run_name="__main__")


if __name__ == "__main__":
    main()
