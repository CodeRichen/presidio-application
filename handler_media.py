"""
圖片 / PDF 去識別化(遮蔽) + 還原
====================================
只負責「檔案層級」的處理：main.py 偵測到複製的是圖片/PDF 檔案時呼叫 mask_file()。

流程：
    1) OCR / PDF 文字座標找出敏感內容的位置 (bounding box)（敏感與否的判斷邏輯借用 handler_code.detect，
       跟文字/程式碼那邊共用同一套正則，不用重複維護）
    2) 塗黑該區域
    3) 塗黑前，先把該區域「原始畫面」裁切下來存成 base64，寫進對照表 (media_deid_map.json)
    4) 還原時，依對照表把原始畫面貼回對應座標

安裝：
    pip install pillow pytesseract pymupdf
    另外需要安裝 Tesseract OCR 主程式 (非 pip 套件)：
        Windows: https://github.com/UB-Mannheim/tesseract/wiki (安裝時記得勾選 Chinese-Traditional)
        macOS  : brew install tesseract tesseract-lang
        Linux  : sudo apt install tesseract-ocr tesseract-ocr-chi-tra
    Windows 上若找不到 tesseract 執行檔，改下面 TESSERACT_CMD 指到安裝路徑。
"""
import os
import io
import base64
import json
import tempfile
from typing import List, Tuple

from PIL import Image, ImageDraw
import pytesseract

try:
    import pymupdf as fitz  # 新版套件名稱
except ImportError:
    import fitz  # 舊版 PyMuPDF 相容寫法

import handler_code  # 敏感內容判斷邏輯直接借用程式碼/個資規則，不重複定義一套

# ========================= 設定區 =========================
TESSERACT_CMD = r"C:\Program Files\Tesseract-OCR\tesseract.exe"  # 沒加進 PATH 才需要設定
if os.path.exists(TESSERACT_CMD):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

OCR_LANG = "chi_tra+eng"      # Tesseract 語言包，依需求調整，例如只用 "eng"
MASK_COLOR = (0, 0, 0)        # 遮蔽色塊顏色
MASK_PADDING = 2              # 遮蔽框比偵測到的文字框多留幾個 px，避免邊緣殘留
DEFAULT_MAP_FILE = "./presidio/media_deid_map.json"

# 遮蔽/還原後的檔案要存在哪裡，改這個變數就好：
#   None          -> 系統暫存資料夾 (預設；不會弄髒原始檔案所在的資料夾，反正結果都會放回剪貼簿)
#   "SAME_FOLDER" -> 跟原始檔案同一個資料夾 (檔名加 _masked / _restored)
#   其他字串       -> 自訂資料夾路徑，統一集中存放，不存在會自動建立
MEDIA_OUTPUT_DIR = None
# ===========================================================


def _resolve_output_path(input_path: str, suffix: str) -> str:
    ext = os.path.splitext(input_path)[1].lower()
    name = os.path.splitext(os.path.basename(input_path))[0]
    if MEDIA_OUTPUT_DIR is None:
        folder = tempfile.gettempdir()
    elif MEDIA_OUTPUT_DIR == "SAME_FOLDER":
        folder = os.path.dirname(input_path)
    else:
        os.makedirs(MEDIA_OUTPUT_DIR, exist_ok=True)
        folder = MEDIA_OUTPUT_DIR
    return os.path.join(folder, f"{name}{suffix}{ext}")


def get_reason(label: str) -> str:
    return f"符合「{label}」規則" if label in {r.name for r in handler_code.RULES} | {r.name for r in handler_code.handler_text.RULES} \
        else f"Presidio NLP 模型判定為「{label}」類型"


def print_masked_entries(entries: list, source_label: str):
    if not entries:
        print(f"[{source_label}] 未偵測到需要遮蔽的內容")
        return
    print(f"[{source_label}] 共遮蔽 {len(entries)} 處：")
    for e in entries:
        page_info = f"第 {e['page'] + 1} 頁 " if "page" in e else ""
        value = e.get("value", "(無法取得文字內容，僅有畫面截圖)")
        score = e.get("score")
        score_info = f"，信心分數 {score}" if score is not None else ""
        print(f"  - {page_info}[{e['tag']}] 內容：「{value}」｜理由：{get_reason(e['label'])}{score_info}")


def _merge_overlaps(matches):
    """matches 是 (start, end, label, score) 的 4 元素 tuple，重疊時只留較長的那個"""
    matches = sorted(matches, key=lambda m: (m[0], -(m[1] - m[0])))
    merged, last_end = [], -1
    for m in matches:
        start, end = m[0], m[1]
        if start >= last_end:
            merged.append(m)
            last_end = end
    return merged


def detect_matches(text: str) -> List[Tuple[int, int, str, float]]:
    """借用 handler_code 的正則規則，再加上 handler_text 的 Presidio 模型一起判斷，重疊的結果合併"""
    matches = handler_code.detect(text) + handler_code.handler_text.presidio_matches(text)
    return _merge_overlaps(matches)


# ========== OCR 共用工具 ==========
def ocr_words(pil_image: Image.Image) -> List[dict]:
    data = pytesseract.image_to_data(pil_image, lang=OCR_LANG, output_type=pytesseract.Output.DICT)
    words = []
    for i in range(len(data["text"])):
        txt = data["text"][i].strip()
        try:
            conf = int(float(data["conf"][i]))
        except (ValueError, TypeError):
            conf = -1
        if not txt or conf < 0:
            continue
        words.append({
            "text": txt, "left": data["left"][i], "top": data["top"][i],
            "width": data["width"][i], "height": data["height"][i],
            "block": data["block_num"][i], "par": data["par_num"][i],
            "line": data["line_num"][i], "word_num": data["word_num"][i],
        })
    return words


def _group_lines(words: List[dict]) -> List[List[dict]]:
    lines = {}
    for w in words:
        lines.setdefault((w["block"], w["par"], w["line"]), []).append(w)
    return [sorted(v, key=lambda w: w["word_num"]) for v in lines.values()]


def find_sensitive_boxes_in_words(words: List[dict]):
    """回傳 [(x0, y0, x1, y1, label, matched_text, score), ...]（像素座標）"""
    boxes = []
    for line_words in _group_lines(words):
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w["text"]
            offsets.append((start, len(line_text), w))
            line_text += " "
        for start, end, label, score in detect_matches(line_text):
            hit_words = [w for s, e, w in offsets if s < end and e > start]
            if not hit_words:
                continue
            x0 = min(w["left"] for w in hit_words); y0 = min(w["top"] for w in hit_words)
            x1 = max(w["left"] + w["width"] for w in hit_words); y1 = max(w["top"] + w["height"] for w in hit_words)
            boxes.append((x0, y0, x1, y1, label, line_text[start:end], score))
    return boxes


# ========== 對照表讀寫 ==========
def _load_all_map(map_file: str) -> dict:
    if not os.path.exists(map_file):
        return {}
    with open(map_file, encoding="utf-8") as f:
        return json.load(f)



def _save_map_entry(map_file: str, key: str, entries: list):
    folder = os.path.dirname(map_file)
    if folder:
        os.makedirs(folder, exist_ok=True)
    data = _load_all_map(map_file)
    data[key] = entries
    with open(map_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_map_entry(map_file: str, key: str) -> list:
    return _load_all_map(map_file).get(key, [])


# ========== 圖片遮蔽 / 還原 ==========
def _crop_to_png_b64(pil_image: Image.Image, box) -> str:
    buf = io.BytesIO()
    pil_image.crop(box).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def mask_image(input_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    img = Image.open(input_path).convert("RGB")
    boxes = find_sensitive_boxes_in_words(ocr_words(img))

    entries, counters = [], {}
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1, label, value, score in boxes:
        box = (x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING)
        counters[label] = counters.get(label, 0) + 1
        entries.append({"tag": f"{label}_{counters[label]}", "label": label, "value": value, "score": score,
                         "bbox": list(box), "crop_b64": _crop_to_png_b64(img, box)})
        draw.rectangle(box, fill=MASK_COLOR)

    img.save(output_path)
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print(f"[圖片遮蔽完成] {input_path} -> {output_path}")
    print_masked_entries(entries, "圖片")
    return entries


def restore_image(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        print("[找不到對照資料] 無法還原，請確認 map_file 路徑與檔名是否正確")
        return
    img = Image.open(masked_path).convert("RGB")
    for e in entries:
        crop = Image.open(io.BytesIO(base64.b64decode(e["crop_b64"])))
        x0, y0, _, _ = e["bbox"]
        img.paste(crop, (int(x0), int(y0)))
    img.save(output_path)
    print(f"[圖片還原完成] {masked_path} -> {output_path}，共還原 {len(entries)} 處")


# ========== PDF 遮蔽 / 還原 ==========
def _page_has_text(page: "fitz.Page", min_words: int = 5) -> bool:
    return len(page.get_text("words")) >= min_words


def _mask_pdf_text_page(page, entries, counters):
    words = page.get_text("words")
    lines = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    for line_words in lines.values():
        line_words.sort(key=lambda w: w[7])
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w[4]
            offsets.append((start, len(line_text), w))
            line_text += " "

        for start, end, label, score in detect_matches(line_text):
            hit = [w for s, e, w in offsets if s < end and e > start]
            if not hit:
                continue
            x0 = min(w[0] for w in hit); y0 = min(w[1] for w in hit)
            x1 = max(w[2] for w in hit); y1 = max(w[3] for w in hit)
            rect = fitz.Rect(x0, y0, x1, y1)

            counters[label] = counters.get(label, 0) + 1
            crop_pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2), annots=False)
            entries.append({"tag": f"{label}_{counters[label]}", "label": label, "page": page.number,
                             "bbox": [x0, y0, x1, y1], "value": " ".join(w[4] for w in hit), "score": score,
                             "crop_b64": base64.b64encode(crop_pix.tobytes("png")).decode("ascii")})
            page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def _mask_pdf_scanned_page(page, entries, counters, zoom: float = 2.0):
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, annots=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    boxes = find_sensitive_boxes_in_words(ocr_words(img))

    for x0, y0, x1, y1, label, value, score in boxes:
        px0, py0, px1, py1 = x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING
        rect = fitz.Rect(px0 / zoom, py0 / zoom, px1 / zoom, py1 / zoom)

        counters[label] = counters.get(label, 0) + 1
        crop = img.crop((max(px0, 0), max(py0, 0), px1, py1))
        buf = io.BytesIO(); crop.save(buf, format="PNG")
        entries.append({"tag": f"{label}_{counters[label]}", "label": label, "value": value, "page": page.number,
                         "bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "score": score,
                         "crop_b64": base64.b64encode(buf.getvalue()).decode("ascii")})
        page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def mask_pdf(input_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    doc = fitz.open(input_path)
    entries, counters = [], {}
    for page in doc:
        (_mask_pdf_text_page if _page_has_text(page) else _mask_pdf_scanned_page)(page, entries, counters)
    doc.save(output_path)
    doc.close()
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print(f"[PDF 遮蔽完成] {input_path} -> {output_path}")
    print_masked_entries(entries, "PDF")
    return entries


def restore_pdf(masked_path: str, output_path: str, map_file: str = DEFAULT_MAP_FILE):
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        print("[找不到對照資料] 無法還原，請確認 map_file 路徑與檔名是否正確")
        return
    doc = fitz.open(masked_path)
    for e in entries:
        page = doc[e["page"]]
        page.insert_image(fitz.Rect(*e["bbox"]), stream=base64.b64decode(e["crop_b64"]))
    doc.save(output_path)
    doc.close()
    print(f"[PDF 還原完成] {masked_path} -> {output_path}，共還原 {len(entries)} 處")


# ========== main.py 呼叫的統一入口：依副檔名自動決定要走圖片還是 PDF 流程 ==========
def mask_file(input_path: str, output_path: str = None, map_file: str = DEFAULT_MAP_FILE) -> str:
    ext = os.path.splitext(input_path)[1].lower()
    output_path = output_path or _resolve_output_path(input_path, "_masked")
    (mask_pdf if ext == ".pdf" else mask_image)(input_path, output_path, map_file)
    return output_path


def restore_file(masked_path: str, output_path: str = None, map_file: str = DEFAULT_MAP_FILE) -> str:
    ext = os.path.splitext(masked_path)[1].lower()
    output_path = output_path or _resolve_output_path(masked_path, "_restored")
    (restore_pdf if ext == ".pdf" else restore_image)(masked_path, output_path, map_file)
    return output_path
