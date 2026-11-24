# -*- coding: utf-8 -*-
"""合成測試 PDF 專用：從單影像遮蔽 PDF + 私有 crop map 還原平面 PDF。

只處理本文專題的無旋轉、每頁一個全幅 raster image 的遮蔽 PDF。
不得用還原後的 PDF 對外公開。既有輸出不覆寫。
"""
from __future__ import annotations

import base64
import io
import math
import os
from pathlib import Path
import tempfile

import numpy as np
from PIL import Image
try:
    import pymupdf as fitz
except ImportError:
    import fitz


def _edge_extended(crop: Image.Image, pixels: int) -> Image.Image:
    if pixels == 0:
        return crop.copy()
    rgb = np.asarray(crop.convert("RGB"))
    return Image.fromarray(np.pad(rgb, ((pixels, pixels), (pixels, pixels), (0, 0)), mode="edge"))


def restore_pdf_flat(masked_path, output_path, entries, *, scale: float = 2.0,
                     border_px: int = 3) -> str:
    """逐頁恢復 crop，再將每頁輸出為單影像 PDF（取代 insert_image 疊圖）。

    僅在整份文件驗證、寫完後發布；不覆寫、不修改原稿與 map。
    2x 是舊版裁切影像的原生取樣倍率；邊緣延伸為視覺修復而非無損還原。
    """
    if not math.isfinite(scale) or scale <= 0 or not 0 <= border_px <= 12:
        raise ValueError("無效的還原參數")
    src_path = Path(masked_path).resolve(strict=True)
    dest_path = Path(output_path).absolute()
    if src_path == dest_path.resolve() or dest_path.exists() or dest_path.is_symlink():
        raise FileExistsError("還原輸出路徑與來源相同，或輸出已存在；拒絕覆寫")
    if not isinstance(entries, list) or not entries:
        raise ValueError("還原對照表沒有可用的裁切紀錄")
    if not dest_path.parent.is_dir():
        raise FileNotFoundError("還原目的資料夾不存在")

    by_page = {}
    with fitz.open(src_path) as src:
        if src.page_count == 0 or src.needs_pass:
            raise ValueError("遮蔽 PDF 不可讀或需要密碼")
        for entry in entries:
            if not isinstance(entry, dict) or not all(k in entry for k in ("page", "bbox", "crop_b64")):
                raise ValueError("還原對照表缺少必要欄位")
            page_num = entry["page"]
            if type(page_num) is not int or not 0 <= page_num < src.page_count:
                raise ValueError("還原對照表頁碼不正確")
            rect_data = entry["bbox"]
            if not isinstance(rect_data, (list, tuple)) or len(rect_data) != 4:
                raise ValueError("還原框座標格式錯誤")
            try:
                vals = [float(v) for v in rect_data]
            except (TypeError, ValueError) as exc:
                raise ValueError("還原框座標不可讀") from exc
            if not all(math.isfinite(v) for v in vals):
                raise ValueError("還原框座標不是有限數值")
            rect = fitz.Rect(*vals)
            if rect.is_empty or rect.is_infinite:
                raise ValueError("還原框大小無效")
            if not isinstance(entry["crop_b64"], str):
                raise ValueError("還原圖片資料格式錯誤")
            by_page.setdefault(page_num, []).append((rect, entry["crop_b64"]))

        tmp_path = None
        try:
            fd, name = tempfile.mkstemp(prefix=".pii_private_restored_", suffix=".pdf",
                                        dir=str(dest_path.parent))
            os.close(fd)
            tmp_path = Path(name)
            with fitz.open() as dst:
                for i, page in enumerate(src):
                    if page.rotation != 0 or page.get_text().strip() or page.get_links() or page.first_annot:
                        raise ValueError("來源 PDF 不是支援的無旋轉、單影像遮蔽頁面")
                    images = page.get_images(full=True)
                    if len(images) != 1:
                        raise ValueError("來源 PDF 每頁必須恰好只有一張影像")
                    xref = images[0][0]
                    rects = page.get_image_rects(xref)
                    if len(rects) != 1:
                        raise ValueError("來源 PDF 影像位置不唯一")
                    image_rect = rects[0]
                    if any(abs(a-b) > 0.2 for a,b in zip(image_rect, page.rect)):
                        raise ValueError("來源 PDF 的影像沒有鋪滿整頁")
                    original = Image.open(io.BytesIO(src.extract_image(xref)["image"]))
                    base = original.convert("RGB")
                    original.close()
                    pixels = (int(round(page.rect.width*scale)), int(round(page.rect.height*scale)))
                    canvas = base.resize(pixels, Image.Resampling.LANCZOS)
                    base.close()
                    try:
                        for rect, encoded_crop in by_page.get(i, []):
                            # 寬鬆允許裁切框超出頁面，但驗證必須有可見交集。
                            if not (rect.x0 < page.rect.x1 and rect.y0 < page.rect.y1
                                    and rect.x1 > page.rect.x0 and rect.y1 > page.rect.y0):
                                raise ValueError("還原框完全位於頁面之外")
                            raw = base64.b64decode(encoded_crop, validate=True)
                            with Image.open(io.BytesIO(raw)) as loaded:
                                crop = loaded.convert("RGB")
                            x0 = int(round(rect.x0*scale)); y0 = int(round(rect.y0*scale))
                            rw = int(round(rect.width*scale)); rh = int(round(rect.height*scale))
                            if min(rw, rh) <= 0 or abs(crop.width-rw) > 3 or abs(crop.height-rh) > 3:
                                crop.close()
                                raise ValueError("對照表裁圖與 PDF 座標倍率不一致")
                            if crop.size != (rw,rh):
                                resized = crop.resize((rw,rh), Image.Resampling.BILINEAR)
                                crop.close(); crop=resized
                            extended = _edge_extended(crop, border_px)
                            canvas.paste(extended, (x0-border_px, y0-border_px))
                            crop.close();extended.close()
                        buffer=io.BytesIO()
                        canvas.save(buffer, format="PNG")
                        new_page=dst.new_page(width=page.rect.width, height=page.rect.height)
                        new_page.insert_image(new_page.rect, stream=buffer.getvalue(), keep_proportion=False)
                    finally:
                        canvas.close()
                # 原始 PDF 只有單影像，沒有複製原始文件的頁面/元資料/附件。
                dst.save(str(tmp_path), garbage=4, deflate=True)
            with fitz.open(str(tmp_path)) as result:
                if result.page_count != src.page_count or result.embfile_count() != 0:
                    raise ValueError("還原 PDF 輸出驗證失敗")
                for page in result:
                    if page.get_text().strip() or len(page.get_images(full=True)) != 1:
                        raise ValueError("還原 PDF 輸出不是單影像頁面")
            # 不覆寫既有檔案。Windows NTFS 支援 hardlink；不支援即拒絕發布。
            os.link(str(tmp_path), str(dest_path))
        finally:
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass
    return str(dest_path)
