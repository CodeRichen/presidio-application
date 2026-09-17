"""
圖片 / PDF 去識別化(遮蔽) + 還原
====================================
只負責「檔案層級」的處理：main.py 偵測到複製的是圖片/PDF 檔案時呼叫 mask_file()。

流程：
    1) OCR / PDF 文字座標找出敏感內容的位置 (bounding box)（敏感與否的判斷邏輯借用 handler_code.detect
       +handler_text.presidio_matches，跟文字/程式碼那邊共用同一套規則，不用重複維護）
    2) 塗黑該區域
    3) 塗黑前，先把該區域「原始畫面」裁切下來存成 base64，寫進對照表 (media_deid_map.json)
    4) 還原時，依對照表把原始畫面貼回對應座標

【OCR 引擎：Windows 原生 OCR (winrt.windows.media.ocr)】
Windows 原生 OCR 的 recognize_async() 是 async API，但這支程式其餘部分(main.py 的 pynput 監聽迴圈、
json_test.py 的測試流程)都是同步的，所以這裡用 asyncio.run() 在 ocr_words() 內部各自建立/結束一個
事件迴圈去跑辨識，呼叫端(find_sensitive_boxes_in_words/mask_image/mask_pdf...)完全不用改成 async、
不用互相 await。(如果是在 Jupyter Notebook 裡，本身就跑在事件迴圈中，直接 await 呼叫即可，不需要
asyncio.run()；但這裡是給一般 .py 同步流程用，所以採用 asyncio.run() 的寫法。)

另外，Windows OCR 的辨識結果 (OcrResult) 本身就有 result.lines -> line.words 這種階層結構(每個
OcrWord 有 bounding_rect 座標)，比 Tesseract 更完整，也不用像 EasyOCR 那樣自己用座標去猜分行——
ocr_words() 直接把每個詞屬於第幾行記在 "line_id" 欄位，_group_lines() 依這個欄位分組即可。
Windows OCR 的 API 沒有提供逐字/逐詞的信心分數，所以這裡的 word dict 沒有 "conf" 欄位。

安裝：
    pip install pillow pymupdf
    (winrt 相關套件請沿用你原本能跑通這份範例程式碼時安裝的版本，一般是
     winrt-Windows.Media.Ocr / winrt-Windows.Graphics.Imaging / winrt-Windows.Storage.Streams /
     winrt-Windows.Globalization 這幾個命名空間套件；只能在 Windows 上執行。
     另外要確認系統「設定 > 時間與語言 > 語言與地區」有安裝對應語言的「光學字元辨識」選用功能，
     否則 OcrEngine.try_create_from_language() 會拿不到引擎。)
"""
import os
import io
import base64
import json
import tempfile
import asyncio
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw

try:
    import winrt.windows.media.ocr as ocr
    import winrt.windows.graphics.imaging as imaging
    import winrt.windows.storage.streams as streams
    import winrt.windows.globalization as globalization
except ImportError:
    ocr = imaging = streams = globalization = None  # 沒裝 winrt 時，_get_engine() 會丟出清楚的錯誤訊息

try:
    import pymupdf as fitz  # 新版套件名稱
except ImportError:
    import fitz  # 舊版 PyMuPDF 相容寫法

import handler_code  # 敏感內容判斷邏輯直接借用程式碼/個資規則，不重複定義一套

# ========================= 設定區 =========================
OCR_LANG_TAG = "zh-Hant-TW"   # 優先嘗試載入的語言；系統沒裝這個語言的 OCR 選用功能時，自動退回系統預設語言

MASK_COLOR = (0, 0, 0)        # 遮蔽色塊顏色
MASK_PADDING = 2              # 遮蔽框比偵測到的文字框多留幾個 px，避免邊緣殘留
DEFAULT_MAP_FILE = "./presidio/media_deid_map.json"

MIN_OCR_WIDTH = 1600           # OCR 前處理：圖片寬度小於這個值就放大，小字體/低解析度圖片辨識率會差很多

# 遮蔽/還原後的檔案要存在哪裡，改這個變數就好：
#   None          -> 系統暫存資料夾 (預設；不會弄髒原始檔案所在的資料夾，反正結果都會放回剪貼簿)
#   "SAME_FOLDER" -> 跟原始檔案同一個資料夾 (檔名加 _masked / _restored)
#   其他字串       -> 自訂資料夾路徑，統一集中存放，不存在會自動建立
MEDIA_OUTPUT_DIR = None
# ===========================================================

_ocr_engine = None  # 惰性初始化：第一次真的要 OCR 才建立引擎，main.py import 這支檔案時不會卡住


def _get_engine():
    """建立/回傳 Windows OCR 引擎單例。這段邏輯跟你提供的範例程式碼一致：
    優先嘗試 OCR_LANG_TAG，系統沒裝該語言的 OCR 選用功能就退回使用者設定檔裡的預設語言。"""
    global _ocr_engine
    if _ocr_engine is None:
        if ocr is None:
            raise RuntimeError(
                "尚未安裝 winrt 相關套件 (winrt.windows.media.ocr 等)，"
                "請安裝跟你原本範例程式碼相同版本的 winrt-Windows.* 套件，且只能在 Windows 上執行"
            )
        lang = globalization.Language(OCR_LANG_TAG)
        if ocr.OcrEngine.is_language_supported(lang):
            _ocr_engine = ocr.OcrEngine.try_create_from_language(lang)
            print(f"已成功載入：{OCR_LANG_TAG} OCR 引擎")
        else:
            _ocr_engine = ocr.OcrEngine.try_create_from_user_profile_languages()
            if _ocr_engine is not None:
                print(f"{OCR_LANG_TAG} 不受支援，載入系統預設 OCR 引擎：{_ocr_engine.recognizer_language.language_tag}")
        if _ocr_engine is None:
            raise RuntimeError(
                "無法建立 Windows OCR 引擎，請確認「設定 > 時間與語言 > 語言與地區」"
                "已安裝對應語言的「光學字元辨識」選用功能"
            )
    return _ocr_engine


async def _recognize_bytes_async(img_bytes: bytes):
    """把記憶體中的圖片位元組丟給 Windows OCR 引擎辨識，回傳 OcrResult。
    寫法跟你提供的 run_win_ocr_on_file() 一致，只是輸入從「檔案路徑」改成「位元組」，
    這樣不管圖片是從硬碟讀的、還是 PDF 頁面轉出來的 pixmap、或是前處理後的暫存圖片，
    都不用先落地成檔案才能餵給 OCR。"""
    stream = streams.InMemoryRandomAccessStream()
    writer = streams.DataWriter(stream)
    writer.write_bytes(img_bytes)

    await writer.store_async()
    await writer.flush_async()
    writer.detach_stream()
    stream.seek(0) if hasattr(stream, 'seek') else setattr(stream, 'position', 0)

    decoder = await imaging.BitmapDecoder.create_async(stream)
    software_bitmap = await decoder.get_software_bitmap_async()
    return await _get_engine().recognize_async(software_bitmap)


def get_reason(label: str) -> str:
    return f"符合「{label}」規則" if label in {r.name for r in handler_code.RULES} | {r.name for r in handler_code.handler_text.RULES} \
        else f"NLP 模型判定為「{label}」類型"


def print_masked_entries(entries: list, source_label: str):
    if not entries:
        print(f"[{source_label}] 未偵測到需要遮蔽的內容")
        return
    print(f"[{source_label}] 共遮蔽 {len(entries)} 處：")
    for e in entries:
        page_info = f"第 {e['page'] + 1} 頁 " if "page" in e else ""
        value = e.get("value", "(無法取得文字內容，僅有畫面截圖)")
        score = e.get("score")
        source = e.get("source")
        score_info = f"，信心分數 {score}" if score is not None else ""
        source_info = f"，來源={source}" if source else ""
        print(f"  - {page_info}[{e['tag']}] 內容：「{value}」｜理由：{get_reason(e['label'])}{score_info}{source_info}")


def _priority_tier(source: str, prefer_zh: bool) -> int:
    """跟 main.py 的 _priority_tier 邏輯保持一致，數字越小越優先：
        0 -> 正則規則 (source == "regex")
        1 -> 跟這行文字語言吻合的模型 (文字含中文 -> zh 優先，否則 en 優先)
        2 -> 跟這行文字語言不吻合的模型"""
    if source == "regex":
        return 0
    if source == "zh":
        return 1 if prefer_zh else 2
    if source == "en":
        return 2 if prefer_zh else 1
    return 3


def _merge_overlaps(matches, prefer_zh: bool):
    """matches 是 (start, end, label, score, source) 的 5 元素 tuple。
    重疊處理優先序跟 main.py 一致：正則規則 > 語言吻合的模型 > 語言不吻合的模型，
    同優先序再比範圍長度、最後比信心分數。保留與淘汰(含信心分數)都印出來方便除錯。"""
    ordered = sorted(
        matches,
        key=lambda m: (
            _priority_tier(m[4], prefer_zh),
            -(m[1] - m[0]),
            -(m[3] if m[3] is not None else 0),
        ),
    )
    kept = []
    for start, end, label, score, source in ordered:
        overlapped = any(not (end <= k[0] or start >= k[1]) for k in kept)
        status = "淘汰(重疊)" if overlapped else "保留"
        score_info = f"{score}" if score is not None else "None(規則比對)"
        print(f"    [{status}] {label:<22} [{start:>4}:{end:<4}] 來源={source:<5} 信心分數={score_info}")
        if not overlapped:
            kept.append((start, end, label, score, source))
    return sorted(kept, key=lambda m: m[0])


def detect_matches(text: str) -> List[Tuple[int, int, str, Optional[float], str]]:
    """借用 handler_code 的正則規則，再加上 handler_text 的 Presidio/中文 NER 模型一起判斷，
    重疊的結果依「規則 > 語言吻合的模型 > 語言不吻合的模型」的優先序合併(見 _merge_overlaps)"""
    matches = handler_code.detect(text) + handler_code.handler_text.presidio_matches(text)
    if not matches:
        return []
    prefer_zh = handler_code.handler_text.contains_chinese(text)
    return _merge_overlaps(matches, prefer_zh)


# ========== OCR 前處理 ==========
def preprocess_for_ocr(img: Image.Image) -> Tuple[Image.Image, float]:
    """Windows 原生 OCR 前的影像前處理：
    Windows OCR 引擎本身也是端對端模型，不太需要像 Tesseract 那樣額外二值化/去雜訊，
    所以這裡只保留「太小的圖放大到至少 MIN_OCR_WIDTH 寬」這一步(小字體/低解析度截圖仍然是
    常見的辨識率殺手)，圖片維持原本的彩色，交給 OCR 引擎自己處理。
    回傳 (前處理後的圖片, scale)，scale 是實際放大的倍率（沒放大就是 1.0）。
    呼叫端要記得：偵測到的座標是在「前處理後的圖片」上，貼回原圖/PDF 前要除以這個 scale 換算回去。
    """
    scale = max(1.0, MIN_OCR_WIDTH / img.width)
    if scale > 1.0:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img, scale


# ========== OCR 共用工具 ==========
def ocr_words(pil_image: Image.Image) -> List[dict]:
    """呼叫 Windows 原生 OCR，回傳跟其他引擎版本相容的格式：
    [{"text", "left", "top", "width", "height", "line_id"}, ...]（像素座標）。
    line_id 是 Windows OCR 原生就有的「這個詞屬於第幾行」資訊 (result.lines 的索引)，
    比自己用座標去猜分行準確可靠，_group_lines() 直接依這個欄位分組即可。"""
    buf = io.BytesIO()
    pil_image.convert("RGB").save(buf, format="PNG")
    result = asyncio.run(_recognize_bytes_async(buf.getvalue()))

    words = []
    for line_id, line in enumerate(result.lines):
        for w in line.words:
            text = w.text.strip()
            if not text:
                continue
            rect = w.bounding_rect
            words.append({"text": text, "left": rect.x, "top": rect.y,
                           "width": rect.width, "height": rect.height, "line_id": line_id})
    return words


def _group_lines(words: List[dict]) -> List[List[dict]]:
    """Windows OCR 本身就有「這個詞屬於第幾行」的資訊 (ocr_words() 填進 line_id)，
    這裡只要照 line_id 分組、行內再依 x 座標排序即可。"""
    lines = {}
    for w in words:
        lines.setdefault(w["line_id"], []).append(w)
    return [sorted(v, key=lambda x: x["left"]) for _, v in sorted(lines.items())]


def find_sensitive_boxes_in_words(words: List[dict], scale: float = 1.0):
    """回傳 [(x0, y0, x1, y1, label, matched_text, score, source), ...]（像素座標）
    scale：如果 words 是從 preprocess_for_ocr() 放大過的圖片跑 OCR 得到的，這裡要傳對應的放大倍率，
    才能把座標除回原圖尺寸；呼叫端不用另外處理，這裡回傳的座標已經是「原圖座標」。"""
    boxes = []
    for line_words in _group_lines(words):
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w["text"]
            offsets.append((start, len(line_text), w))
            line_text += " "
        for start, end, label, score, source in detect_matches(line_text):
            hit_words = [w for s, e, w in offsets if s < end and e > start]
            if not hit_words:
                continue
            x0 = min(w["left"] for w in hit_words); y0 = min(w["top"] for w in hit_words)
            x1 = max(w["left"] + w["width"] for w in hit_words); y1 = max(w["top"] + w["height"] for w in hit_words)
            if scale != 1.0:
                x0, y0, x1, y1 = x0 / scale, y0 / scale, x1 / scale, y1 / scale
            boxes.append((x0, y0, x1, y1, label, line_text[start:end], score, source))
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
    processed, scale = preprocess_for_ocr(img)
    boxes = find_sensitive_boxes_in_words(ocr_words(processed), scale=scale)

    entries, counters = [], {}
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1, label, value, score, source in boxes:
        box = (x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING)
        counters[label] = counters.get(label, 0) + 1
        entries.append({"tag": f"{label}_{counters[label]}", "label": label, "value": value, "score": score,
                         "source": source, "bbox": list(box), "crop_b64": _crop_to_png_b64(img, box)})
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

        for start, end, label, score, source in detect_matches(line_text):
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
                             "source": source,
                             "crop_b64": base64.b64encode(crop_pix.tobytes("png")).decode("ascii")})
            page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def _mask_pdf_scanned_page(page, entries, counters, zoom: float = 2.0):
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, annots=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    processed, ocr_scale = preprocess_for_ocr(img)
    # find_sensitive_boxes_in_words 已經把座標除回 ocr_scale，回傳的是「img」(zoom 轉出來的原圖) 座標系，
    # 所以下面把座標除以 zoom 轉回 PDF 座標時，跟原本沒有前處理的邏輯完全一樣，不用再額外處理 ocr_scale
    boxes = find_sensitive_boxes_in_words(ocr_words(processed), scale=ocr_scale)

    for x0, y0, x1, y1, label, value, score, source in boxes:
        px0, py0, px1, py1 = x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING
        rect = fitz.Rect(px0 / zoom, py0 / zoom, px1 / zoom, py1 / zoom)

        counters[label] = counters.get(label, 0) + 1
        crop = img.crop((max(px0, 0), max(py0, 0), px1, py1))
        buf = io.BytesIO(); crop.save(buf, format="PNG")
        entries.append({"tag": f"{label}_{counters[label]}", "label": label, "value": value, "page": page.number,
                         "bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "score": score, "source": source,
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