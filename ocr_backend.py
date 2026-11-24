"""handler_media OCR engine adapter: Windows OCR or local RapidOCR.

The public output schema matches handler_media._group_lines() expectations:
    {text, left, top, width, height, line_id, conf?}

Switch DEFAULT_OCR_BACKEND below, or set PII_OCR_BACKEND=windows|rapid.
RapidOCR is imported lazily and never uses remote URLs here.
"""
from __future__ import annotations

import math
import os
import re
import threading
from typing import Any, Callable

# Choose 'windows' for the original behavior; change to 'rapid' to enable
# RapidOCR on startup without any PowerShell environment commands.
DEFAULT_OCR_BACKEND = "rapid"

_RAPID = None
_RAPID_LOCK = threading.Lock()


def selected_backend() -> str:
    name = os.environ.get("PII_OCR_BACKEND", DEFAULT_OCR_BACKEND).strip().lower()
    if name not in {"windows", "rapid"}:
        raise ValueError(
            f"PII_OCR_BACKEND={name!r} 不支援；請使用 windows 或 rapid。"
        )
    return name


def ocr_words_with_backend(image: Any, windows_words: Callable[[Any], list[dict]]) -> list[dict]:
    """Used by the new handler_media.ocr_words() wrapper."""
    mode = selected_backend()
    if mode == "windows":
        return windows_words(image)
    return rapid_ocr_words(image)


def _rapid_engine():
    global _RAPID
    if _RAPID is not None:
        return _RAPID
    with _RAPID_LOCK:
        if _RAPID is None:
            try:
                from rapidocr import RapidOCR
            except ImportError as exc:
                raise RuntimeError(
                    "尚未安裝 RapidOCR。請在專案 .venv 執行："
                    ".\\.venv\\Scripts\\python.exe -m pip install rapidocr onnxruntime"
                ) from exc
            # RAPID_SPEED_PROFILE_BEGIN
            DEFAULT_RAPID_CPU_THREADS = 4
            _threads = int(os.environ.get('PII_RAPID_CPU_THREADS', str(DEFAULT_RAPID_CPU_THREADS)))
            if _threads not in (0, 2, 4, 6, 8):
                raise ValueError('PII_RAPID_CPU_THREADS 只支援 0、2、4、6、8')
            _params = ({'EngineConfig.onnxruntime.intra_op_num_threads': _threads}
                       if _threads else None)
            _RAPID = RapidOCR(params=_params)
            # RAPID_SPEED_PROFILE_END

    return _RAPID


def _rect_from_polygon(polygon: Any, image_width: int, image_height: int) -> tuple[int, int, int, int]:
    """A conservative axis-aligned rectangle clipped to the input image."""
    try:
        points = list(polygon)
        if len(points) < 4:
            raise ValueError("polygon requires at least four points")
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        if not all(math.isfinite(v) for v in (*xs, *ys)):
            raise ValueError("nonfinite polygon coordinates")
        x0, y0 = math.floor(min(xs)), math.floor(min(ys))
        x1, y1 = math.ceil(max(xs)), math.ceil(max(ys))
        x0 = max(0, min(image_width, x0))
        y0 = max(0, min(image_height, y0))
        x1 = max(0, min(image_width, x1))
        y1 = max(0, min(image_height, y1))
        if x1 <= x0 or y1 <= y0:
            raise ValueError("empty polygon after clipping")
        return x0, y0, x1, y1
    except Exception as exc:
        raise ValueError(f"OCR 回傳無效文字座標：{exc}") from exc


def _make_word(text: str, polygon: Any, line_id: int, image_width: int, image_height: int,
               confidence: Any = None) -> dict:
    x0, y0, x1, y1 = _rect_from_polygon(polygon, image_width, image_height)
    output = {
        "text": str(text), "left": x0, "top": y0,
        "width": x1-x0, "height": y1-y0, "line_id": line_id,
    }
    if confidence is not None:
        try:
            output["conf"] = float(confidence)
        except (TypeError, ValueError):
            pass
    return output


def _compact(value: Any) -> str:
    return re.sub(r"\s+", "", str(value))


def rapid_result_to_words(result: Any, image_width: int, image_height: int) -> list[dict]:
    """Convert RapidOCROutput to the existing handler_media word-level schema.

    Prefer word_results where their concatenated text matches the recognized
    line. Otherwise return the full-line box rather than risk under-redaction.
    """
    texts = getattr(result, "txts", None)
    boxes = getattr(result, "boxes", None)
    if texts is None and boxes is None:
        return []
    if texts is None or boxes is None:
        raise RuntimeError("RapidOCR 結果缺少文字或座標，無法安全遮蔽")
    if len(texts) != len(boxes):
        raise RuntimeError("RapidOCR 文字與座標數量不同，無法安全遮蔽")

    scores = getattr(result, "scores", None)
    word_lines = getattr(result, "word_results", None)
    output = []
    for line_id, (line_text, line_box) in enumerate(zip(texts, boxes)):
        line_text = str(line_text).strip()
        if not line_text:
            continue
        score = scores[line_id] if scores is not None and line_id < len(scores) else None

        # Check line box validity even if word boxes are available; this avoids
        # producing coordinates where the underlying OCR result is inconsistent.
        whole = _make_word(line_text, line_box, line_id, image_width, image_height, score)
        items = word_lines[line_id] if word_lines is not None and line_id < len(word_lines) else None
        if not items:
            output.append(whole)
            continue

        proposed = []
        raw_words = []
        try:
            for entry in items:
                wtext, wscore, wbox = entry
                if not str(wtext).strip():
                    continue
                proposed.append(_make_word(wtext, wbox, line_id, image_width, image_height, wscore))
                raw_words.append(str(wtext))
        except (ValueError, TypeError, IndexError):
            # A broken word box must not accidentally expose part of the line.
            proposed = []
        if proposed and _compact("".join(raw_words)) == _compact(line_text):
            # Word boxes are returned in reading order. Keep the same order
            # to preserve index-to-coordinate correspondence for PII spans.
            output.extend(proposed)
        else:
            output.append(whole)
    return output


def rapid_ocr_words(image: Any) -> list[dict]:
    """Perform local RapidOCR on a PIL image and return handler_media words."""
    image = image.convert("RGB")
    engine = _rapid_engine()
    try:
        result = engine(image, return_word_box=True)
    except TypeError as exc:
        # No silent downgrade to older APIs. A supported version is required.
        raise RuntimeError("目前 RapidOCR 不支援 return_word_box；請更新 rapidocr") from exc
    try:
        return rapid_result_to_words(result, image.width, image.height)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("RapidOCR 文字座標轉換失敗，為避免漏遮蔽已停止輸出") from exc
