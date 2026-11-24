"""Install the pinned OpenCV YuNet 2023mar ONNX model once; offline inference thereafter.

Usage:
    python setup_face_model.py
    python setup_face_model.py --from-file path/to/face_detection_yunet_2023mar.onnx
    python setup_face_model.py --verify-only

Uses an official OpenCV Hugging Face mirror rather than raw.githubusercontent.com,
which may return a Git LFS pointer instead of the actual ONNX file.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen

MODEL_NAME = "face_detection_yunet_2023mar.onnx"
TARGET = Path(__file__).resolve().parent / "models" / MODEL_NAME
EXPECTED_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
SOURCES = (
    "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
    "https://huggingface.co/opencv/opencv_zoo/resolve/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
)
MAX_DOWNLOAD = 5_000_000


def validate_model_bytes(data: bytes, expected_sha256: str = EXPECTED_SHA256) -> None:
    if data.lstrip().startswith(b"version https://git-lfs.github.com/spec/v1"):
        raise ValueError("收到 Git LFS 指標，並非 ONNX 模型")
    if data.lstrip().lower().startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ValueError("收到網頁（HTML/XML），並非 ONNX 模型")
    if len(data) < 100_000 or len(data) > MAX_DOWNLOAD:
        raise ValueError(f"模型大小異常：{len(data):,} bytes")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"模型 SHA-256 不符：{actual}（預期 {expected_sha256}）")


def download_bytes(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 PII-YuNet-Installer/2.0"})
    with urlopen(req, timeout=60) as response:
        payload = response.read(MAX_DOWNLOAD + 1)
    return payload


def install_bytes(data: bytes, target: Path = TARGET) -> None:
    validate_model_bytes(data)
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("找不到 OpenCV，先執行：.\\.venv\\Scripts\\python.exe -m pip install opencv-python") from exc
    if not hasattr(cv2, "FaceDetectorYN"):
        raise RuntimeError("OpenCV 缺少 FaceDetectorYN，請更新 opencv-python (>=4.8,<5)")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".yunet_", suffix=".onnx", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        try:
            cv2.FaceDetectorYN.create(model=tmp, config="", input_size=(320, 320))
        except Exception as exc:
            raise RuntimeError(f"OpenCV 無法載入 YuNet 模型：{exc}") from exc
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def main() -> None:
    parser = argparse.ArgumentParser(description="Install a verified OpenCV YuNet face detector model")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--from-file", type=Path, help="從已手動下載的 ONNX 檔案安裝（不連線）")
    group.add_argument("--verify-only", action="store_true", help="檢查已安裝模型，不連線")
    args = parser.parse_args()

    if TARGET.exists():
        try:
            validate_model_bytes(TARGET.read_bytes())
            if args.from_file is None:
                import cv2
                cv2.FaceDetectorYN.create(model=str(TARGET), config="", input_size=(320, 320))
                print(f"[模型有效] {TARGET}")
                print(f"SHA-256: {EXPECTED_SHA256}")
                return
        except Exception as exc:
            print(f"[現有檔案無效] {exc}")
            if args.verify_only:
                raise SystemExit(1)
    elif args.verify_only:
        raise SystemExit(f"[找不到模型] {TARGET}")

    if args.from_file:
        data = args.from_file.read_bytes()
        install_bytes(data)
        print(f"[從本機檔案安裝完成] {TARGET}")
        return

    failures = []
    for url in SOURCES:
        print(f"[嘗試下載] {url}")
        try:
            data = download_bytes(url)
            install_bytes(data)
            print(f"[安裝成功] {TARGET}")
            print(f"SHA-256: {EXPECTED_SHA256}")
            print("往後人臉偵測會直接讀取本機模型，不需要連網。")
            return
        except Exception as exc:
            failures.append(f"- {url}: {exc}")
            print(f"[下載來源失敗] {exc}")

    raise SystemExit(
        "所有自動下載來源都失敗。請從 OpenCV 官方 Hugging Face 頁面手動下載 ONNX，"
        "再使用 --from-file 安裝。\n" + "\n".join(failures)
    )


if __name__ == "__main__":
    main()
