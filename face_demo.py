"""Quick OFFLINE face-only smoke test. Does not load OCR or Presidio.

Usage: python face_demo.py path/to/photo.jpg --output path/to/face_masked.png
"""
from __future__ import annotations
import argparse
from pathlib import Path
from PIL import Image, ImageDraw, ImageOps
from face_detector import detect_faces, padded_face_box


def main():
    p = argparse.ArgumentParser(description="Offline YuNet face redaction demo")
    p.add_argument("image", help="來源 JPG/PNG")
    p.add_argument("--output", help="輸出 PNG；預設為 <filename>_faces_masked.png")
    args = p.parse_args()
    source = Path(args.image).resolve()
    if source.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("請輸入 JPG 或 PNG")
    output = Path(args.output).resolve() if args.output else source.with_name(source.stem + "_faces_masked.png")
    if source == output:
        raise ValueError("不可覆寫來源圖片")
    with Image.open(source) as im:
        rgb = ImageOps.exif_transpose(im).convert("RGB")
    faces = detect_faces(rgb)
    pen = ImageDraw.Draw(rgb)
    for face in faces:
        pen.rectangle(padded_face_box(face, rgb.size), fill="black")
    rgb.save(output)
    print(f"已遮蔽 {len(faces)} 張人臉，輸出：{output}")
    if not faces:
        print("警告：沒有偵測到人臉，不代表圖片一定沒有可辨識人臉。請人工檢查。")


if __name__ == "__main__":
    main()
