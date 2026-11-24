"""Offline face detection for PII redaction (OpenCV YuNet, CPU-only).

No cloud APIs, no identity recognition, no network requests, no on-disk face cache.
The ONNX model must be installed manually at models/face_detection_yunet_2023mar.onnx
(or set PII_FACE_MODEL to an absolute pathname).
"""
from __future__ import annotations

import os
from pathlib import Path
import threading
from typing import List, Tuple

from PIL import Image

FaceBox = Tuple[float, float, float, float, float]  # x0,y0,x1,y1,confidence (pixels)
_LOCAL = threading.local()


def face_enabled() -> bool:
    """Enabled by default. Set PII_FACE_ENABLED=0 to explicitly disable."""
    return os.getenv("PII_FACE_ENABLED", "1").strip().lower() not in {"0", "off", "false", "no"}


def _model_path() -> Path:
    chosen = os.getenv("PII_FACE_MODEL", "").strip()
    return Path(chosen).expanduser() if chosen else Path(__file__).resolve().parent / "models" / "face_detection_yunet_2023mar.onnx"


def _get_detector(cv, *, path: Path, score_threshold: float):
    """Thread-local lazy model, reused on subsequent images/pages."""
    identity = (str(path.resolve()), float(score_threshold))
    if getattr(_LOCAL, "identity", None) != identity:
        if not path.is_file():
            raise FileNotFoundError(
                "離線人臉偵測模型不存在：" + str(path) + "。請依整合說明下載 YuNet ONNX 到 models/，"
                "或設定 PII_FACE_MODEL。為避免漏遮，本次處理已中止。"
            )
        if not hasattr(cv, "FaceDetectorYN"):
            raise RuntimeError("OpenCV 版本過舊：需要具有 FaceDetectorYN 的 opencv-python")
        try:
            model = cv.FaceDetectorYN.create(
                model=str(path), config="", input_size=(320, 320),
                score_threshold=score_threshold, nms_threshold=0.3, top_k=5000,
                backend_id=cv.dnn.DNN_BACKEND_OPENCV, target_id=cv.dnn.DNN_TARGET_CPU,
            )
        except Exception as exc:
            raise RuntimeError(f"YuNet 模型載入失敗（路徑：{path}）：{exc}") from exc
        _LOCAL.detector = model
        _LOCAL.identity = identity
    return _LOCAL.detector


def detect_faces(pil_image: Image.Image, *, threshold: float = 0.60, max_side: int = 1280) -> List[FaceBox]:
    """Detect faces locally and return bounding boxes in *original image* pixels.

    Each returned box encloses the whole detected face; callers should expand
    a margin and mask it fully opaque. This function never saves any image.
    """
    if not (0 < threshold < 1):
        raise ValueError("threshold 必須介於 0 與 1 之間")
    if max_side < 320:
        raise ValueError("max_side 不得小於 320")
    try:
        import cv2 as cv
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("尚未安裝離線人臉偵測套件：.venv\\Scripts\\python.exe -m pip install opencv-python") from exc

    width, height = pil_image.size
    if width < 1 or height < 1:
        raise ValueError("無效的圖片大小")
    # Reduce large PDFs to keep inference responsive. Very small images are
    # scaled up because tiny inputs perform poorly in YuNet.
    scale = max(320.0 / max(width, height), min(1.0, max_side / float(max(width, height))))
    scaled_w = max(1, round(width * scale))
    scaled_h = max(1, round(height * scale))
    rgb = pil_image.convert("RGB")
    if (scaled_w, scaled_h) != (width, height):
        rgb = rgb.resize((scaled_w, scaled_h), Image.Resampling.BILINEAR)
    bgr = cv.cvtColor(np.asarray(rgb), cv.COLOR_RGB2BGR)

    detector = _get_detector(cv, path=_model_path(), score_threshold=threshold)
    try:
        detector.setInputSize((scaled_w, scaled_h))
        _status, rows = detector.detect(bgr)
    except Exception as exc:
        raise RuntimeError(f"YuNet 離線人臉偵測失敗：{exc}") from exc
    if rows is None:
        return []

    result: List[FaceBox] = []
    for item in rows:
        x, y, w, h = map(float, item[:4])
        score = float(item[14])
        if score < threshold or w <= 0 or h <= 0:
            continue
        x0 = max(0.0, min(float(width), x / scale))
        y0 = max(0.0, min(float(height), y / scale))
        x1 = max(0.0, min(float(width), (x + w) / scale))
        y1 = max(0.0, min(float(height), (y + h) / scale))
        if x1 > x0 and y1 > y0:
            result.append((x0, y0, x1, y1, score))
    return result


def padded_face_box(face: FaceBox, image_size: tuple[int, int], *, padding: float = 0.24):
    """Return integer pixel bounds with 24%-of-face margin, clipped to image."""
    x0, y0, x1, y1, _score = face
    width, height = image_size
    dx, dy = (x1 - x0) * padding, (y1 - y0) * padding
    import math
    return (
        max(0, math.floor(x0 - dx)),
        max(0, math.floor(y0 - dy)),
        min(width, math.ceil(x1 + dx)),
        min(height, math.ceil(y1 + dy)),
    )
