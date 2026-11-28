"""
圖片 / PDF 去識別化(遮蔽) + 還原（只負責檔案層級；main.py 偵測到圖片/PDF 時呼叫 mask_file()）

流程：OCR / PDF 文字座標 -> 借用 handler_code / handler_text 的規則找出敏感內容 bbox
      -> 塗黑前先把原始畫面裁成 base64 存進對照表 (media_deid_map.json) -> 塗黑
      -> 還原時依對照表把原圖貼回座標。

OCR 引擎：RapidOCR (ONNX Runtime，離線、跨平台，不再依賴 Windows 原生 OCR)
    安裝：pip install rapidocr-onnxruntime   (舊版套件)
      或：pip install rapidocr onnxruntime   (新版套件；兩種介面程式都會自動判斷)
    其他：pip install pillow pymupdf opencv-python
    RapidOCR 只回傳「文字行 + 四點座標 + 信心分數」，沒有逐詞座標，也沒有 Windows OCR 的 line / word 階層，
    所以 ocr_words() 會做兩件事，讓後面所有偵測邏輯完全不用改：
      1. 把同一水平列上、距離很近的偵測框合併成一行 (line_id)，欄位標籤與值才會落在同一行
      2. 依字寬把每一行切成「ASCII 連續字串 (電話 / Email / 帳號當一個 token) + 每個中文字一個 token」，
         座標按字寬比例內插，偵測到的 span 才能對回精準的 bbox
    每個 token 另帶 "sp" 旗標 (原文這裡有空白)，組行文字時只在有空白處補空格，中文不會被拆成「高 雄 市」。

其他改動 (recall / 速度，沿用前一版)：
    圖片小圖額外放大 OCR (預設關閉，RapidOCR 偵測階段本來就會放大小圖)、文字層 PDF 內嵌圖片 OCR、
    文字層 PDF 跨行偵測、人臉門檻與縮放、空白頁略過、整頁渲染一次共用、純影像 PDF 直接寫 Pixmap。
"""
import os
import io
import re
import json
import math
import base64
import tempfile
import functools
import unicodedata
import contextvars
import importlib.util

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageOps, ImageStat

try:
    import pymupdf as fitz
except ImportError:
    import fitz

import handler_code   # 敏感內容判斷規則（與文字/程式碼流程共用，唯一的外部依賴）


def _env_on(name, default="1"):
    return os.environ.get(name, default).strip().lower() not in {"0", "off", "false", "no"}


# ========================= 設定區 =========================
MASK_COLOR = (0, 0, 0)
MASK_PADDING = 10             # 遮蔽框比文字框多留的 px
DEFAULT_MAP_FILE = "./presidio/media_deid_map.json"
# 輸出位置：None=系統暫存資料夾｜"SAME_FOLDER"=與原檔同資料夾｜其他字串=自訂資料夾
MEDIA_OUTPUT_DIR = None
FACE_MODEL_NAME = "models/face_detection_yunet_2023mar.onnx"   # 預設放在本檔同資料夾；或用 PII_FACE_MODEL 指定路徑
FACE_SCORE_THRESHOLD = float(os.environ.get("PII_FACE_THRESHOLD", "0.5"))   # 越低越不漏、但誤框越多
FACE_PADDING = 0.25            # 人臉框四周多留的比例（涵蓋髮際線、下巴）
FACE_MAX_SIDE = int(os.environ.get("PII_FACE_MAX_SIDE", "2560"))   # 長邊超過就縮小再偵測 (0=不縮小)
FACE_MIN_SIDE = int(os.environ.get("PII_FACE_MIN_SIDE", "640"))    # 長邊不足就放大再偵測 (0=不放大)
OCR_TEXT_SCORE = float(os.environ.get("PII_OCR_TEXT_SCORE", "0.4"))   # RapidOCR 文字信心門檻 (套件預設 0.5)，越低越不漏
OCR_UPSCALE = 2.0              # 小圖額外 OCR 的放大倍率
OCR_UPSCALE_BELOW = 1600       # 圖片長邊 <= 此值才做額外放大 OCR
PDF_IMAGE_MIN_PT = 40          # 文字層 PDF：內嵌圖片寬或高小於此值 (pt) 不做 OCR (圖示、分隔線)
PDF_IMAGE_MAX_PER_PAGE = 8     # 文字層 PDF：每頁最多 OCR 幾張內嵌圖片 (面積大的優先)
BLANK_PAGE_STD = 1.5           # 掃描 PDF：頁面灰階標準差低於此值視為空白頁
# 環境變數：PII_FACE_ENABLED=0 停用人臉遮蔽（預設開啟）；PII_MEDIA_CACHE=0 停用單頁 PII 偵測快取
#          PII_OCR_EXTRA=1 小圖額外放大 OCR (預設關閉：RapidOCR 偵測階段本來就會放大小圖，再放大多半只是變慢)
#          PII_OCR_PREPROCESS=1 OCR 前先做灰階 + 對比 + 銳化 (預設關閉：RapidOCR 對原圖通常比較準，請用測試程式比較)
#          PII_PDF_FALLBACK_ZOOM=3 掃描 PDF 再用此倍率多跑一次 OCR 合併 (預設 0=關閉；RapidOCR 內部會把長邊縮到約 2000px，
#                                 zoom 3 常和 zoom 2 差不多，卻讓每頁時間加倍)
#          PII_PDF_OCR_IMAGES=0 停用文字層 PDF 的內嵌圖片 OCR
#          PII_CROSS_PREFILTER=0 文字層 PDF 跨行偵測不做行對預篩 (較慢、但不會漏)
OCR_EXTRA_PASS = _env_on("PII_OCR_EXTRA", "0")
OCR_PREPROCESS = _env_on("PII_OCR_PREPROCESS", "0")
PDF_FALLBACK_ZOOM = float(os.environ.get("PII_PDF_FALLBACK_ZOOM", "0"))
PDF_OCR_IMAGES = _env_on("PII_PDF_OCR_IMAGES")
CROSS_PREFILTER = _env_on("PII_CROSS_PREFILTER")
# ===========================================================

_ADDR_TOKENS = ("市", "縣", "區", "鄉", "鎮", "路", "街", "號")
_FIELD_SOURCES = {"field_regex", "field_regex_crossline"}
_COMPACT_LABELS = {  # compact(無空白) 文字補抓的類別
    "EMAIL", "EMAIL_ADDRESS", "TW_PHONE", "PHONE_NUMBER", "US_PHONE", "TW_ID", "US_SSN", "CREDIT_CARD",
    "ADDRESS", "LICENSE_NUMBER", "MEDICAL_RECORD_NUMBER", "INSURANCE_POLICY_NUMBER", "EMPLOYEE_ID", "CASE_NUMBER",
}
_CROSS_LINE_LABELS = {  # 允許跨相鄰兩行的類別
    "EMAIL", "EMAIL_ADDRESS", "TW_ID", "TW_PHONE", "PHONE_NUMBER", "US_PHONE", "BANK_ACCOUNT", "LICENSE_NUMBER",
    "MEDICAL_RECORD_NUMBER", "INSURANCE_POLICY_NUMBER", "EMPLOYEE_ID", "CASE_NUMBER", "IP_ADDRESS",
}
_EXPLICIT_LABELS = {  # 格式明確的 PII，重疊時優先保留
    "TW_ID", "EMAIL", "EMAIL_ADDRESS", "TW_PHONE", "IP_ADDRESS", "BANK_ACCOUNT", "LICENSE_NUMBER",
    "MEDICAL_RECORD_NUMBER", "INSURANCE_POLICY_NUMBER", "EMPLOYEE_ID", "CASE_NUMBER", "BANK_NAME", "DATE_OF_BIRTH",
}
_IP_CHAR_FIX = str.maketrans({"·": ".", "丄": "."})        # OCR 把 IPv4 句點讀錯
_PHONE_CHAR_FIX = str.maketrans({"一": "-", "·": "-"})     # OCR 把電話連字號讀錯
_IP_FALLBACK_FIX = str.maketrans({"·": ".", "丄": ".", "工": ".", ",": ".", "，": "."})

# 跨行偵測預篩：這些類別都含數字 / 英文字母 / @，整段純中文的行對不可能命中，可省掉模型呼叫
_CROSS_PREFILTER = re.compile(r"[0-9A-Za-z@\uff10-\uff19\uff21-\uff3a\uff41-\uff5a]")
_BOUNDARY_RE = re.compile(r"[\d@]")   # 文字層 PDF：換行處的字詞含數字或 @ 才可能是被切開的 Email / 電話 / 證號

_Q = r"[「\"']?"
_DATE = r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日"
_STAY_RE = re.compile("住院期間為" + _Q + "(?P<s>" + _DATE + ")至(?P<e>" + _DATE + ")")
_FIELD_SPECS = [(re.compile(p, re.I), label) for p, label in [
    ("出生日期為" + _Q + "(?P<value>" + _DATE + ")", "DATE_OF_BIRTH"),
    ("申請日期為" + _Q + "(?P<value>" + _DATE + ")", "DATE_TIME"),
    (r"開戶銀行為(?P<value>[^，,。；;]{2,30}?分行)", "BANK_NAME"),
    (r"(?:理賠)?案件編號為(?P<value>CLM[-一](?:19|20)\d{2}[-一]\d{6}[0-9丆])", "CASE_NUMBER"),
    (r"承辦單位於(?P<value>.+?)(?=收件|並登錄|，|,|。)", "ADDRESS"),
]]
_PDF_FIELD_PATTERNS = [  # 文字層 PDF：只遮欄位值，不遮欄位名稱
    ("TW_PHONE", re.compile(r"(?i)\b(?:Phone|電話)\s*[:：]\s*(?P<value>0\d{1,2}-?\d{6,8}|09\d{2}-?\d{3}-?\d{3})")),
    ("EMAIL", re.compile(r"(?i)\bEmail\s*[:：]\s*(?P<value>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})")),
    ("TW_ID", re.compile(r"(?i)\bTW\s*ID\s*[:：]\s*(?P<value>[A-Z][12]\d{8})")),
    ("IP_ADDRESS", re.compile(r"(?i)\bIP\s*[:：]\s*(?P<value>(?:\d{1,3}\.){3}\d{1,3})")),
]

# ========================= 人臉偵測（本機 OpenCV YuNet，離線） =========================
_yunet = None


def face_enabled():
    return _env_on("PII_FACE_ENABLED")


def detect_faces(img):
    """回傳 [(x, y, w, h, score), ...]（原圖座標）。模型不存在即丟錯（fail closed），不會自動聯網下載。
    長邊 > FACE_MAX_SIDE 先縮小 (速度)；長邊 < FACE_MIN_SIDE 先放大 (小圖上的小臉才抓得到)，座標會換算回原圖。"""
    global _yunet
    import cv2
    import numpy as np
    if _yunet is None:
        path = os.environ.get("PII_FACE_MODEL") or os.path.join(os.path.dirname(os.path.abspath(__file__)), FACE_MODEL_NAME)
        if not os.path.isfile(path):
            raise RuntimeError(f"找不到 YuNet 人臉模型：{path}（請放置模型、設定 PII_FACE_MODEL，或設 PII_FACE_ENABLED=0 停用）")
        _yunet = cv2.FaceDetectorYN.create(path, "", (320, 320), FACE_SCORE_THRESHOLD, 0.3, 5000)
    rgb = np.array(img.convert("RGB"))
    h, w = rgb.shape[:2]
    long_side = max(h, w)
    ratio = 1.0
    if FACE_MAX_SIDE and long_side > FACE_MAX_SIDE:
        ratio = FACE_MAX_SIDE / long_side
    elif FACE_MIN_SIDE and long_side < FACE_MIN_SIDE:
        ratio = FACE_MIN_SIDE / long_side
    if ratio != 1.0:
        rgb = cv2.resize(rgb, (max(round(w * ratio), 1), max(round(h * ratio), 1)),
                         interpolation=cv2.INTER_AREA if ratio < 1 else cv2.INTER_CUBIC)
    sx, sy = rgb.shape[1] / w, rgb.shape[0] / h
    _yunet.setInputSize((rgb.shape[1], rgb.shape[0]))
    _, faces = _yunet.detect(cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if faces is None:
        return []
    return [(f[0] / sx, f[1] / sy, f[2] / sx, f[3] / sy, f[14]) for f in faces]   # f[14] 為信心分數


def padded_face_box(face, size):
    x, y, w, h = face[:4]
    px, py = w * FACE_PADDING, h * FACE_PADDING
    return (max(int(x - px), 0), max(int(y - py), 0), min(int(x + w + px) + 1, size[0]), min(int(y + h + py) + 1, size[1]))


# ========================= 純影像 PDF 輸出 / 還原 =========================
def _build_raster_pdf(output_path, items):
    """items: (寬pt, 高pt, PNG bytes 或 fitz.Pixmap) 的可迭代物件。另建新 PDF，只放頁面影像，
    不帶來源 PDF 的附件、註解、隱藏文字層與 metadata。"""
    out = fitz.open()
    for w, h, img in items:
        page = out.new_page(width=w, height=h)
        if isinstance(img, fitz.Pixmap):
            page.insert_image(fitz.Rect(0, 0, w, h), pixmap=img)   # 直接寫入像素，省掉 PNG 編碼 / 解碼
        else:
            page.insert_image(fitz.Rect(0, 0, w, h), stream=img)
    out.set_metadata({})
    out.save(output_path, garbage=4, deflate=True)
    out.close()


def save_secure_raster_pdf(doc, output_path, dpi=200):
    """doc 為已 apply_redactions 的文件；逐頁渲染後重建成純影像 PDF。"""
    _build_raster_pdf(output_path, ((p.rect.width, p.rect.height, p.get_pixmap(
        dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)) for p in doc))


def _pad_edge(im):
    """四周各複製 1px 邊緣像素：遮蔽黑框在高 dpi 渲染時邊緣會有抗鋸齒，多貼 1px 才蓋得掉。"""
    w, h = im.size
    a = Image.new("RGB", (w + 2, h))
    a.paste(im, (1, 0)); a.paste(im.crop((0, 0, 1, h)), (0, 0)); a.paste(im.crop((w - 1, 0, w, h)), (w + 1, 0))
    b = Image.new("RGB", (w + 2, h + 2))
    b.paste(a, (0, 1)); b.paste(a.crop((0, 0, w + 2, 1)), (0, 0)); b.paste(a.crop((0, h - 1, w + 2, h)), (0, h + 1))
    return b


def restore_pdf_flat(masked_path, output_path, entries, dpi=200):
    """以同樣 dpi 渲染遮蔽後的頁面，把對照表的原始裁圖貼回（縮放到 bbox 在該 dpi 下的大小），輸出純影像 PDF。"""
    scale = dpi / 72

    def pages():
        with fitz.open(masked_path) as doc:
            for page in doc:
                pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False, annots=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                for e in (e for e in entries if e.get("page") == page.number):
                    x0, y0 = max(e["bbox"][0], 0), max(e["bbox"][1], 0)
                    x1, y1 = min(e["bbox"][2], page.rect.width), min(e["bbox"][3], page.rect.height)
                    crop = Image.open(io.BytesIO(base64.b64decode(e["crop_b64"]))).convert("RGB")
                    px0, py0 = math.floor(x0 * scale), math.floor(y0 * scale)   # 向外取整，避免殘留黑色細邊
                    px1, py1 = math.ceil(x1 * scale), math.ceil(y1 * scale)
                    img.paste(_pad_edge(crop.resize((max(px1 - px0, 1), max(py1 - py0, 1)), Image.LANCZOS)), (px0 - 1, py0 - 1))
                buf = io.BytesIO()
                img.save(buf, format="PNG", compress_level=1)
                yield page.rect.width, page.rect.height, buf.getvalue()

    _build_raster_pdf(output_path, pages())


# ========================= RapidOCR =========================
_ocr_engine = None
_ocr_api = None   # "new" = rapidocr 套件 (回傳物件)；"old" = rapidocr_onnxruntime (回傳 tuple)


def ocr_available():
    """是否已安裝 RapidOCR (不會真的載入模型)。"""
    return (importlib.util.find_spec("rapidocr") is not None
            or importlib.util.find_spec("rapidocr_onnxruntime") is not None)


def _get_engine():
    """惰性建立 RapidOCR 單例 (第一次載入模型約一兩秒)。同時支援新版 rapidocr 與舊版 rapidocr_onnxruntime。"""
    global _ocr_engine, _ocr_api
    if _ocr_engine is None:
        try:
            from rapidocr import RapidOCR
            api = "new"
        except ImportError:
            try:
                from rapidocr_onnxruntime import RapidOCR
                api = "old"
            except ImportError:
                raise RuntimeError("尚未安裝 RapidOCR：請執行 pip install rapidocr-onnxruntime "
                                   "(或 pip install rapidocr onnxruntime)")
        try:   # 降低文字信心門檻以提高 recall；各版本參數寫法不同，失敗就用套件預設值
            engine = (RapidOCR(params={"Global.text_score": OCR_TEXT_SCORE}) if api == "new"
                      else RapidOCR(text_score=OCR_TEXT_SCORE))
        except Exception:
            engine = RapidOCR()
        _ocr_engine, _ocr_api = engine, api
    return _ocr_engine


def warmup_ocr():
    """預先載入 RapidOCR 模型並跑一張空白圖，避免第一張圖片才付出載入時間。"""
    ocr_words(Image.new("RGB", (64, 64), (255, 255, 255)))


def _run_rapidocr(arr):
    """回傳 [(四點座標, 文字, 信心分數), ...]；arr 為 BGR numpy 陣列。"""
    engine = _get_engine()
    out = engine(arr)
    if _ocr_api == "new":
        boxes = getattr(out, "boxes", None)
        if out is None or boxes is None or len(boxes) == 0:
            return []
        return [(b, t, float(s)) for b, t, s in zip(boxes, out.txts, out.scores)]
    result = out[0] if isinstance(out, tuple) else out
    return [(b, t, float(s)) for b, t, s in (result or [])]


def _char_units(ch):
    """字寬權重：全形 / 中文 2，窄字元 0.5，其餘 1；用來把一行的寬度按字元比例內插成逐字座標。"""
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2.0
    return 0.5 if ch in " .,:;'|!il`" else 1.0


def _tokenize(text, x0, y0, x1, y1, lead_sp, line_id):
    """把一個偵測框的文字切成 token 並內插座標：ASCII 連續字串 (電話 / Email / 帳號) 合成一個 token，
    每個中文 / 全形字各自一個 token，開頭的冒號獨立成 token。每個 token 帶 sp 旗標 = 前面原本有空白。"""
    units = [_char_units(c) for c in text]
    k = (x1 - x0) / (sum(units) or 1.0)
    tokens, cur, pos, sp = [], None, 0.0, lead_sp

    def make(ch, left, right, space):
        return {"text": ch, "left": left, "top": y0, "width": right - left,
                "height": y1 - y0, "line_id": line_id, "sp": space}

    for ch, u in zip(text, units):
        left, right = x0 + pos * k, x0 + (pos + u) * k
        pos += u
        if ch.isspace():
            cur, sp = None, True
            continue
        if unicodedata.east_asian_width(ch) in ("W", "F") or (ch == ":" and cur is None):
            tokens.append(make(ch, left, right, sp))
            cur, sp = None, False
        elif cur is None:
            cur = make(ch, left, right, sp)
            tokens.append(cur)
            sp = False
        else:
            cur["text"] += ch
            cur["width"] = right - cur["left"]
    return tokens


def _boxes_to_rows(items):
    """把偵測框合併成「列」：垂直重疊 >= 50% 且水平間距 <= 1.5 倍字高的框視為同一行
    (欄位標籤與它的值常被切成兩個框，不合併的話欄位規則就抓不到)。回傳由上到下、每列由左到右。"""
    rows = []
    for it in sorted(items, key=lambda i: (i["y0"] + i["y1"]) / 2):
        h = max(it["y1"] - it["y0"], 1.0)
        for row in rows:
            overlap = min(it["y1"], row["y1"]) - max(it["y0"], row["y0"])
            if overlap < 0.5 * min(h, row["y1"] - row["y0"]):
                continue
            gap = max(it["x0"] - row["x1"], row["x0"] - it["x1"], 0.0)
            if gap <= 1.5 * h:
                row["items"].append(it)
                row["x0"], row["x1"] = min(row["x0"], it["x0"]), max(row["x1"], it["x1"])
                row["y0"], row["y1"] = min(row["y0"], it["y0"]), max(row["y1"], it["y1"])
                break
        else:
            rows.append({"x0": it["x0"], "y0": it["y0"], "x1": it["x1"], "y1": it["y1"], "items": [it]})
    rows.sort(key=lambda r: r["y0"])
    return [sorted(r["items"], key=lambda i: i["x0"]) for r in rows]


def ocr_words(pil_image):
    """回傳 [{"text","left","top","width","height","line_id","sp"}, ...]（傳入影像的像素座標）。
    RapidOCR 只給文字行，這裡合併成列 (line_id) 並切成 token，格式與先前的 Windows OCR 版相同。"""
    import numpy as np
    arr = np.ascontiguousarray(np.array(pil_image.convert("RGB"))[:, :, ::-1])   # RGB -> BGR
    items = []
    for box, text, score in _run_rapidocr(arr):
        text = str(text).strip()
        if not text:
            continue
        xs, ys = [p[0] for p in box], [p[1] for p in box]
        items.append({"x0": float(min(xs)), "y0": float(min(ys)), "x1": float(max(xs)), "y1": float(max(ys)),
                      "text": text})
    words = []
    for line_id, row in enumerate(_boxes_to_rows(items)):
        prev = None
        for it in row:
            h = max(it["y1"] - it["y0"], 1.0)
            lead_sp = prev is not None and (it["x0"] - prev["x1"]) > 0.3 * h
            words += _tokenize(it["text"], it["x0"], it["y0"], it["x1"], it["y1"], lead_sp, line_id)
            prev = it
    return words


def preprocess_for_ocr(img):
    """預設不處理 (RapidOCR 自己會做縮放與正規化)，回傳 (RGB 原圖, 1.0)。
    PII_OCR_PREPROCESS=1 時改做灰階 + 對比增強 + 銳化 (舊版為 Windows OCR 調的流程)；scale 固定 1.0。"""
    if not OCR_PREPROCESS:
        return img.convert("RGB"), 1.0
    g = ImageEnhance.Contrast(img.convert("L")).enhance(1.8)
    return g.filter(ImageFilter.UnsharpMask(radius=1.5, percent=150, threshold=2)).convert("RGB"), 1.0


# ========================= PII 偵測（含單頁快取） =========================
# 快取只在單張圖片/單頁 PDF 的處理期間存在（ContextVar，彼此隔離），結束即清空，不長期保留私密字串。
_cache_var = contextvars.ContextVar("pii_media_match_cache", default=None)


def _cache_scope(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        off = os.environ.get("PII_MEDIA_CACHE", "1").strip().lower() in {"0", "off", "false", "no"}
        if off or _cache_var.get() is not None:      # 巢狀範圍(掃描 PDF 的 zoom 2/3)共用外層快取
            return fn(*args, **kwargs)
        cache = {}
        token = _cache_var.set(cache)
        try:
            return fn(*args, **kwargs)
        finally:
            _cache_var.reset(token)
            cache.clear()
    return wrapper


def _cached_detect(fn):
    @functools.wraps(fn)
    def wrapper(text):
        cache = _cache_var.get()
        if cache is None:
            return fn(text)
        if text not in cache:
            cache[text] = tuple(fn(text))
        return list(cache[text])
    return wrapper


def _priority_tier(source, prefer_zh):
    """正則(0) > 與文字語言吻合的模型(1) > 不吻合的模型(2)，與 main.py 一致。"""
    if source == "regex":
        return 0
    if source in ("zh", "en"):
        return 1 if (source == "zh") == prefer_zh else 2
    return 3


@_cached_detect
@getattr(handler_code.handler_text, "_pii_zh_sp_scope", lambda f: f)   # 新版 handler_text 沒有此裝飾器時直接略過
def detect_matches(text):
    """回傳 [(start, end, label, score, source), ...]；重疊時依優先序 > 範圍長度 > 信心分數保留。"""
    matches = handler_code.detect(text) + handler_code.handler_text.presidio_matches(text)
    if not matches:
        return []
    prefer_zh = handler_code.handler_text.contains_chinese(text)
    kept = []
    for m in sorted(matches, key=lambda m: (_priority_tier(m[4], prefer_zh), -(m[1] - m[0]), -(m[3] or 0))):
        if all(m[1] <= k[0] or m[0] >= k[1] for k in kept):
            kept.append(m)
    return sorted(kept, key=lambda m: m[0])


def _detect(text, labels=None):
    return [m for m in detect_matches(text) if labels is None or m[2] in labels]


@functools.lru_cache(maxsize=1)
def _rule_names():
    return frozenset({r.name for r in handler_code.RULES} | {r.name for r in handler_code.handler_text.RULES})


def get_reason(label):
    if label == "FACE":
        return "本機 YuNet 人臉偵測"
    return f"符合「{label}」規則" if label in _rule_names() else f"NLP 模型判定為「{label}」類型"


def print_masked_entries(entries, source_label):
    if not entries:
        print(f"[{source_label}] 未偵測到需要遮蔽的內容")
        return
    print(f"[{source_label}] 共遮蔽 {len(entries)} 處：")
    for e in entries:
        page = f"第 {e['page'] + 1} 頁 " if "page" in e else ""
        score = f"，信心分數 {e['score']}" if e.get("score") is not None else ""
        src = f"，來源={e['source']}" if e.get("source") else ""
        print(f"  - {page}[{e['tag']}] 內容：「{e.get('value', '(無文字內容)')}」｜理由：{get_reason(e['label'])}{score}{src}")


# ========================= OCR 文字 -> bbox =========================
def _group_lines(words):
    lines = {}
    for w in words:
        lines.setdefault(w["line_id"], []).append(w)
    return [sorted(v, key=lambda x: x["left"]) for _, v in sorted(lines.items())]


def _build_compact_ocr_line(line_words, char_fix=None):
    """串接 OCR token（不加空白）；char_fix 為單字元對單字元的 str.maketrans 表，長度不變所以 offsets 仍對得上。"""
    text, offsets = "", []
    for w in line_words:
        t = w["text"].translate(char_fix) if char_fix else w["text"]
        offsets.append((len(text), len(text) + len(t), w))
        text += t
    return text, offsets


def _union_box(words, scale=1.0):
    return (min(w["left"] for w in words) / scale, min(w["top"] for w in words) / scale,
            max(w["left"] + w["width"] for w in words) / scale, max(w["top"] + w["height"] for w in words) / scale)


def _has_box(boxes, label, box):
    return any(b[4] == label and all(abs(b[i] - box[i]) < 1 for i in range(4)) for b in boxes)


def _add_spans(spans, text, offsets, boxes, scale, skip_dupes=False, cross=False):
    """把 text 中的 (start, end, label, score, source) 對應回 OCR token bbox 並加入 boxes。
    cross=True：只處理真正跨行的 span，且每行各建一個 bbox（避免遮到行間正常文字）。"""
    for start, end, label, score, source in spans:
        hits = [(s, e, w) for s, e, w in offsets if s < end and e > start]
        if cross:
            ids = sorted({w["line_id"] for _, _, w in hits})
            groups = [[h for h in hits if h[2]["line_id"] == i] for i in ids] if len(ids) >= 2 else []
        else:
            groups = [hits] if hits else []
        for g in groups:
            box = _union_box([w for _, _, w in g], scale)
            if skip_dupes and _has_box(boxes, label, box):
                continue
            ps, pe = (max(start, min(s for s, _, _ in g)), min(end, max(e for _, e, _ in g))) if cross else (start, end)
            boxes.append((*box, label, text[ps:pe], score, source))


def _field_spans(text):
    """有明確欄位名稱的日期/銀行/案件編號/承辦地址 span：[(start, end, label), ...]。"""
    spans = []
    m = _STAY_RE.search(text)
    if m:  # 住院期間兩個日期分開遮，保留中間的「至」
        spans += [(m.start("s"), m.end("s"), "DATE_TIME"), (m.start("e"), m.end("e"), "DATE_TIME")]
    for pat, label in _FIELD_SPECS:
        m = pat.search(text)
        if m and (label != "ADDRESS" or any(t in m.group("value") for t in _ADDR_TOKENS)):
            spans.append((m.start("value"), m.end("value"), label))
    return spans


def _address_word_box(offsets, scale):
    """低解析度補救：「地址」欄位後直接以 OCR token 邊界建 ADDRESS bbox；
    若冒號與第一個值之間有異常空隙(可能漏辨一個字)，左界延伸到分隔符右側，但不遮到「地址」本身。"""
    fe = next((i + 1 if w["text"] == "地址" else i + 2 for i, (_, _, w) in enumerate(offsets)
               if w["text"] == "地址" or (w["text"] == "地" and i + 1 < len(offsets) and offsets[i + 1][2]["text"] == "址")), None)
    if fe is None:
        return None
    vw = [w for _, _, w in offsets[fe:] if w["text"].strip() not in {"", ":", "："}]
    if not vw:
        return None
    joined = "".join(w["text"] for w in vw)
    if len(joined) < 4 or not any(t in joined for t in _ADDR_TOKENS):
        return None
    x0, y0, x1, y1 = _union_box(vw)
    sep = offsets[fe][2] if fe < len(offsets) and offsets[fe][2]["text"].strip() in {":", "："} else offsets[fe - 1][2]
    sep_right = sep["left"] + sep["width"]
    fw = max(vw[0]["width"], 1.0)
    if vw[0]["left"] - sep_right >= fw * 0.9:
        x0 = max(sep_right + min(fw * 0.35, 6.0), 0.0)
    return (x0 / scale, y0 / scale, x1 / scale, y1 / scale, "ADDRESS", joined, None, "field_word_address")


def _add_field_aware_boxes(line_words, boxes, scale):
    """利用欄位名稱把已 OCR 出來、但被 NER 切碎的敏感值完整遮蔽（不猜測 OCR 遺失的字元）。"""
    text, offsets = _build_compact_ocr_line(line_words)
    spans = []

    def add(m, label, source):
        spans.append((m.start("value"), m.end("value"), label, None, source))

    m = re.search(r"姓名[:：]?" + _Q + r"(?P<value>[\u3400-\u9fff]{2,4})", text)   # OCR 常把姓名拆成單字
    if m:
        add(m, "PERSON", "field_regex_person")
    m = re.search(r"地址[:：]?" + _Q + r"(?P<value>[^，,。；;]{4,40})", text)
    if m and any(t in m.group("value") for t in _ADDR_TOKENS):
        add(m, "ADDRESS", "field_regex_address")
    m = re.search(r"(?i)(?:^|[^A-Za-z])IP[:：·.]?(?P<value>[0-9·.丄工]+)$", text)  # IP 欄位只遮已辨識出的數字型值
    if m and 7 <= sum(c.isdigit() for c in m.group("value")) <= 12:
        add(m, "IP_ADDRESS", "field_regex_ip")
    spans += [(s, e, label, None, "field_regex") for s, e, label in _field_spans(text)]
    _add_spans(spans, text, offsets, boxes, scale)

    word_box = _address_word_box(offsets, scale)
    if word_box:
        boxes.append(word_box)


def _add_person_title_boxes(line_words, boxes, scale):
    """只補遮與 PERSON 緊鄰的稱謂（「李佳蓉醫師」的醫師、「Dr. Lee」的 Dr.），單獨的職稱不遮。"""
    text, offsets = _build_compact_ocr_line(line_words)
    spans = []
    for s, e, label, _, _ in _detect(text, {"PERSON"}):
        if text.startswith("醫師", e):
            spans.append((e, e + 2))
        m = re.search(r"(?i)Dr\.?$", text[:s])
        if m:
            spans.append((m.start(), m.end()))
    _add_spans([(a, b, "PERSON_TITLE", None, "person_title_context") for a, b in spans], text, offsets, boxes, scale)


@_cache_scope
def find_sensitive_boxes_in_words(words, scale=1.0):
    """回傳 [(x0, y0, x1, y1, label, matched_text, score, source), ...]（原圖像素座標）。
    scale：words 若來自放大過的圖片，需傳放大倍率，這裡會把座標除回原圖尺寸。"""
    boxes = []
    grouped = _group_lines(words)
    for lw in grouped:
        # 沒有任何文字/數字的行 (只有標點、線條雜訊) 不可能含 PII，直接略過
        if not any(c.isalnum() for w in lw for c in w["text"]):
            continue

        # 1) 一般偵測：只在原文有空白處補空格 (sp 旗標)，中文不會被拆成「高 雄 市」
        line_text, offsets = "", []
        for w in lw:
            if line_text and w.get("sp", True):
                line_text += " "
            offsets.append((len(line_text), len(line_text) + len(w["text"]), w))
            line_text += w["text"]
        _add_spans(detect_matches(line_text), line_text, offsets, boxes, scale)

        # 2) 欄位感知 + 人物稱謂補抓
        _add_field_aware_boxes(lw, boxes, scale)
        _add_person_title_boxes(lw, boxes, scale)

        # 3) compact（無空白）補抓：地址 / Email / 電話等可能被切成多個 token
        text, offs = _build_compact_ocr_line(lw)
        _add_spans(_detect(text, _COMPACT_LABELS), text, offs, boxes, scale)

        # 4) IP / 電話：只修正已辨識出的分隔符，不補回遺失的數字
        for fix, label in ((_IP_CHAR_FIX, "IP_ADDRESS"), (_PHONE_CHAR_FIX, "TW_PHONE")):
            text, offs = _build_compact_ocr_line(lw, fix)
            _add_spans(_detect(text, {label}), text, offs, boxes, scale, skip_dupes=True)

    # 5) 跨相鄰兩行：欄位值被換行切開，或格式明確的 PII 被切在換行處
    for a, b in zip(grouped, grouped[1:]):
        text, offs = _build_compact_ocr_line(a + b)
        _add_spans([(s, e, label, None, "field_regex_crossline") for s, e, label in _field_spans(text)],
                   text, offs, boxes, scale, cross=True)
        if _CROSS_PREFILTER.search(text):   # 純中文的行對不會有這些類別，省掉一次模型呼叫
            _add_spans(_detect(text, _CROSS_LINE_LABELS), text, offs, boxes, scale, skip_dupes=True, cross=True)

    return _dedupe_sensitive_boxes(boxes)


def _box_overlap_ratio(a, b):
    """交集面積 / 較小框面積。"""
    iw = min(a[2], b[2]) - max(a[0], b[0])
    ih = min(a[3], b[3]) - max(a[1], b[1])
    if iw <= 0 or ih <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return iw * ih / smaller if smaller > 0 else 0.0


def _box_label_priority(label):
    if label in _EXPLICIT_LABELS:
        return 0
    if label in {"PERSON", "ADDRESS"}:
        return 1
    # PERSON_TITLE 必須優先於 POSITION，否則去重留下 POSITION、再被過濾掉，會變成「姓名有遮、醫師沒遮」
    return 2 if label in {"PERSON_TITLE", "CREDENTIAL_LIKE", "PHONE_NUMBER", "US_PHONE"} else 3


def _box_key(b):
    return (_box_label_priority(b[4]), -(b[2] - b[0]) * (b[3] - b[1]), -(b[6] or 0))


def _dedupe_sensitive_boxes(boxes):
    """去除同位置重複框（重疊 >= 0.85 才視為同一處；跨行片段 y 不同，不會被合併），再套用政策過濾。"""
    kept = []
    for cand in sorted(boxes, key=_box_key):   # 已依優先序排序，先到者必然勝出
        if not any(_box_overlap_ratio(cand, k) >= 0.85 for k in kept):
            kept.append(cand)

    # 欄位規則已抓到完整日期時，移除與它重疊的泛用 DATE_TIME（避免把「至」一起遮掉）
    field_dates = [b for b in kept if b[4] in {"DATE_OF_BIRTH", "DATE_TIME"} and b[7] in _FIELD_SOURCES]
    kept = [b for b in kept if not (b[4] == "DATE_TIME" and b[7] not in _FIELD_SOURCES
                                    and any(_box_overlap_ratio(b, f) > 0 for f in field_dates))]

    # 政策：POSITION 單獨命中不遮；欄位標籤(IP/EMAIL/TW/OCR)被誤判成 ORGANIZATION 的不遮
    label_words = {"IP", "EMAIL", "TW", "OCR"}
    kept = [b for b in kept if b[4] != "POSITION"
            and not (b[4] == "ORGANIZATION" and str(b[5]).strip().upper().rstrip(":：") in label_words)]
    return sorted(kept, key=lambda b: (b[1], b[0]))


def _merge_boxes(primary, extra):
    """合併兩次 OCR 的結果：extra 中與既有框「同類型且重疊 >= 0.5」或「任何類型重疊 >= 0.85」者視為重複。"""
    merged = list(primary)
    for c in extra:
        if not any((k[4] == c[4] and _box_overlap_ratio(k, c) >= 0.5) or _box_overlap_ratio(k, c) >= 0.85
                   for k in merged):
            merged.append(c)
    return sorted(merged, key=lambda b: (b[1], b[0]))


# ========================= 對照表 / 共用小工具 =========================
def _load_all_map(map_file):
    if not os.path.exists(map_file):
        return {}
    with open(map_file, encoding="utf-8") as f:
        return json.load(f)


def _save_map_entry(map_file, key, entries):
    if os.path.dirname(map_file):
        os.makedirs(os.path.dirname(map_file), exist_ok=True)
    data = _load_all_map(map_file)
    data[key] = entries
    with open(map_file, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load_map_entry(map_file, key):
    return _load_all_map(map_file).get(key, [])


def _crop_to_png_b64(pil_image, box):
    buf = io.BytesIO()
    pil_image.crop(box).save(buf, format="PNG", compress_level=1)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _new_tag(counters, label):
    counters[label] = counters.get(label, 0) + 1
    return f"{label}_{counters[label]}"


def _same_path(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _render_page(page, zoom):
    """以指定倍率渲染 PDF 頁面為 PIL 影像 (RGB，不含註解)。"""
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csRGB, alpha=False, annots=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def _face_entry(counters, img, face, box, bbox, **extra):
    """人臉對照表項目；crop 取自「未遮蔽」的原始畫面。"""
    return {"tag": _new_tag(counters, "FACE"), "label": "FACE", "value": "[人臉]", "score": float(face[4]),
            "source": "face_yunet", "bbox": bbox, "crop_b64": _crop_to_png_b64(img, box), **extra}


# ========================= 圖片遮蔽 / 還原 =========================
def _image_face_boxes(img, entries, counters):
    """在畫任何黑框之前先偵測人臉並記錄原始像素，回傳要遮蔽的框。"""
    boxes = []
    if not face_enabled():
        return boxes
    for face in detect_faces(img):
        box = padded_face_box(face, img.size)
        if box[2] > box[0] and box[3] > box[1]:
            entries.append(_face_entry(counters, img, face, box, list(box)))
            boxes.append(box)
    return boxes


@_cache_scope
def _image_ocr_boxes(img):
    """圖片的敏感文字框 (原圖座標)。主要 OCR 一次；PII_OCR_EXTRA=1 時，小圖再放大 OCR_UPSCALE 倍多跑一次並合併。
    兩次共用同一份偵測快取，相同的行文字不會重複跑模型。"""
    processed, scale = preprocess_for_ocr(img)
    boxes = find_sensitive_boxes_in_words(ocr_words(processed), scale=scale)
    if OCR_EXTRA_PASS and max(img.size) <= OCR_UPSCALE_BELOW:
        up = processed.resize((round(processed.width * OCR_UPSCALE), round(processed.height * OCR_UPSCALE)), Image.LANCZOS)
        boxes = _merge_boxes(boxes, find_sensitive_boxes_in_words(ocr_words(up), scale=scale * OCR_UPSCALE))
    return boxes


def mask_image(input_path, output_path, map_file=DEFAULT_MAP_FILE):
    if _same_path(input_path, output_path):
        raise ValueError("圖片遮蔽輸出路徑不能等於來源檔案，避免覆寫原始照片")
    # 套用 EXIF 方向後再偵測；輸出不保留原圖 EXIF/metadata
    with Image.open(input_path) as src:
        img = ImageOps.exif_transpose(src).convert("RGB")
    orig = img.copy()   # 乾淨原圖：對照表裁圖都從這裡取，框互相重疊時還原才不會貼回黑塊
    entries, counters = [], {}
    face_boxes = _image_face_boxes(img, entries, counters)
    draw = ImageDraw.Draw(img)
    for x0, y0, x1, y1, label, value, score, source in _image_ocr_boxes(orig):
        box = (x0 - MASK_PADDING, y0 - MASK_PADDING, x1 + MASK_PADDING, y1 + MASK_PADDING)
        entries.append({"tag": _new_tag(counters, label), "label": label, "value": value, "score": score,
                        "source": source, "bbox": list(box), "crop_b64": _crop_to_png_b64(orig, box)})
        draw.rectangle(box, fill=MASK_COLOR)
    for box in face_boxes:   # 人臉最後畫，確保與文字框重疊處仍被遮住
        draw.rectangle(box, fill=MASK_COLOR)
    img.save(output_path)
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print_masked_entries(entries, "圖片")
    return entries


def restore_image(masked_path, output_path, map_file=DEFAULT_MAP_FILE):
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        print("[找不到對照資料] 無法還原，請確認 map_file 路徑與檔名是否正確")
        return
    img = Image.open(masked_path).convert("RGB")
    for e in entries:
        img.paste(Image.open(io.BytesIO(base64.b64decode(e["crop_b64"]))), (int(e["bbox"][0]), int(e["bbox"][1])))
    img.save(output_path)


# ========================= PDF 遮蔽 / 還原 =========================
def _page_has_text(page, min_words=5):
    return len(page.get_text("words")) >= min_words


def _add_pdf_face_redactions(page, entries, counters, rendered_img, zoom):
    """rendered_img 為未遮蔽的頁面渲染圖（頁面像素座標）；旋轉頁已由 mask_pdf 拒絕，所以 px -> PDF 座標只需 /zoom。"""
    if not face_enabled():
        return
    for face in detect_faces(rendered_img):
        px = padded_face_box(face, rendered_img.size)
        rect = fitz.Rect(*(c / zoom for c in px)) & page.rect
        if rect.is_empty or rect.is_infinite:
            continue
        entries.append(_face_entry(counters, rendered_img, face, px, [rect.x0, rect.y0, rect.x1, rect.y1], page=page.number))
        page.add_redact_annot(rect, fill=MASK_COLOR)


def _is_demo_false_positive(box):
    """七頁合成 Demo 已確認的英文 NER 誤判（僅限 source=en，不影響 regex / 中文 NER）。"""
    if box[7] != "en":
        return False
    v = re.sub(r"\s+", "", str(box[5])).upper()
    return (box[4] == "ORGANIZATION" and v == "PII+FACE") or (box[4] == "DATE_TIME" and v in {f"0{i}" for i in range(1, 8)})


def _keep_pdf_box(b):
    """PDF 的 OCR 框過濾：Demo 已知誤判，以及純欄位名稱 "Email" 被誤判成 PERSON/ORGANIZATION
    (真正的 Email value 仍由 EMAIL 規則遮蔽)。"""
    return (not _is_demo_false_positive(b)
            and not (b[4] in {"PERSON", "ORGANIZATION"} and str(b[5]).strip().upper().rstrip(":：") == "EMAIL"))


def _pdf_cross_line_boxes(ordered):
    """文字層 PDF 的跨行偵測：同一區塊 (block) 內相鄰兩行，若換行處的字詞含數字或 @，
    才把兩行串起來偵測 Email / 電話 / 證號等被換行切開的值。回傳 PDF 座標的 boxes。"""
    boxes = []
    for (ka, la), (kb, lb) in zip(ordered, ordered[1:]):
        if ka[0] != kb[0]:
            continue
        if CROSS_PREFILTER and not (_BOUNDARY_RE.search(la[-1][4]) or _BOUNDARY_RE.search(lb[0][4])):
            continue
        words = [{"text": w[4], "left": w[0], "top": w[1], "width": w[2] - w[0], "height": w[3] - w[1], "line_id": i}
                 for i, lw in enumerate((la, lb)) for w in lw]
        text, offs = _build_compact_ocr_line(words)
        _add_spans(_detect(text, _CROSS_LINE_LABELS), text, offs, boxes, 1.0, skip_dupes=True, cross=True)
    return boxes


_ocr_warned = False


@_cache_scope
def _mask_pdf_text_page(page, entries, counters, zoom=2.0):
    """文字層 PDF：一般 PII 用 detect_matches；Phone/Email/TW ID/IP 明確欄位只遮 value，不遮欄位名稱。
    另外：跨行偵測、頁面內嵌圖片 OCR、內嵌圖片上的人臉。沒有內嵌圖片的頁面不做人臉偵測。"""
    global _ocr_warned
    images = page.get_image_info() if (face_enabled() or PDF_OCR_IMAGES) else []
    rendered = []

    def render():   # 整頁只渲染一次，人臉偵測 / 內嵌圖片 OCR / 對照表裁圖共用
        if not rendered:
            rendered.append(_render_page(page, zoom))
        return rendered[0]

    def crop_b64(rect):
        img = render()
        x0 = min(max(math.floor(rect.x0 * zoom), 0), img.width - 1)
        y0 = min(max(math.floor(rect.y0 * zoom), 0), img.height - 1)
        x1 = min(max(math.ceil(rect.x1 * zoom), x0 + 1), img.width)
        y1 = min(max(math.ceil(rect.y1 * zoom), y0 + 1), img.height)
        return _crop_to_png_b64(img, (x0, y0, x1, y1))

    done = []   # 這頁已加入遮蔽的矩形 (x0, y0, x1, y1)，用來避免重複

    def redact(label, rect, value, score, source):
        entries.append({"tag": _new_tag(counters, label), "label": label, "page": page.number,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "value": value, "score": score,
                        "source": source, "crop_b64": crop_b64(rect)})
        page.add_redact_annot(rect, fill=MASK_COLOR)
        done.append((rect.x0, rect.y0, rect.x1, rect.y1))

    if face_enabled() and images:   # 沒有內嵌圖片就不可能有照片，省掉渲染與人臉偵測
        _add_pdf_face_redactions(page, entries, counters, render(), zoom)

    lines = {}
    for w in page.get_text("words"):
        lines.setdefault((w[5], w[6]), []).append(w)
    ordered = []
    for key, line_words in lines.items():
        line_words.sort(key=lambda w: w[7])
        ordered.append((key, line_words))

    for _, line_words in ordered:
        line_text, offsets = "", []
        for w in line_words:
            offsets.append((len(line_text), len(line_text) + len(w[4]), w))
            line_text += w[4] + " "

        matches = list(detect_matches(line_text))
        for label, pat in _PDF_FIELD_PATTERNS:   # 欄位 value-only span 取代同 label 的一般結果
            m = pat.search(line_text)
            if m:
                matches = [x for x in matches if x[2] != label] + [(m.start("value"), m.end("value"), label, None, "pdf_field_value")]

        for start, end, label, score, source in matches:
            hit = [w for s, e, w in offsets if s < end and e > start]
            if not hit:
                continue
            # NER 偶爾把單獨的欄位名稱誤判成 ORGANIZATION
            if label == "ORGANIZATION" and "".join(w[4] for w in hit).strip().upper().rstrip(":：") in {"PHONE", "EMAIL", "TW", "ID", "IP"}:
                continue
            rect = fitz.Rect(min(w[0] for w in hit), min(w[1] for w in hit), max(w[2] for w in hit), max(w[3] for w in hit))
            redact(label, rect, " ".join(w[4] for w in hit), score, source)

    # 跨行：Email / 電話 / 證號被換行切開
    for x0, y0, x1, y1, label, value, score, source in _pdf_cross_line_boxes(ordered):
        if any(_box_overlap_ratio((x0, y0, x1, y1), d) >= 0.85 for d in done):
            continue
        redact(label, fitz.Rect(x0, y0, x1, y1), value, score, "pdf_crossline")

    # 內嵌圖片 (截圖 / 證件照 / 可搜尋掃描檔的底圖)：文字層看不到，要 OCR
    if PDF_OCR_IMAGES and images:
        if not ocr_available():
            if not _ocr_warned:
                _ocr_warned = True
                print("[警告] 未安裝 RapidOCR，文字層 PDF 內嵌圖片上的文字無法偵測 (只處理文字層)")
        else:
            img = render()
            cands = []
            for info in images:
                r = fitz.Rect(info["bbox"]) & page.rect
                if not r.is_empty and r.width >= PDF_IMAGE_MIN_PT and r.height >= PDF_IMAGE_MIN_PT:
                    cands.append(r)
            for r in sorted(cands, key=lambda r: -r.get_area())[:PDF_IMAGE_MAX_PER_PAGE]:
                px0, py0 = max(math.floor(r.x0 * zoom), 0), max(math.floor(r.y0 * zoom), 0)
                px1, py1 = min(math.ceil(r.x1 * zoom), img.width), min(math.ceil(r.y1 * zoom), img.height)
                if px1 <= px0 or py1 <= py0:
                    continue
                words = ocr_words(preprocess_for_ocr(img.crop((px0, py0, px1, py1)))[0])
                for b in find_sensitive_boxes_in_words(words, scale=1.0):
                    if not _keep_pdf_box(b):
                        continue
                    x0, y0, x1, y1, label, value, score, source = b
                    rect = fitz.Rect((x0 - MASK_PADDING + px0) / zoom, (y0 - MASK_PADDING + py0) / zoom,
                                     (x1 + MASK_PADDING + px0) / zoom, (y1 + MASK_PADDING + py0) / zoom) & page.rect
                    if rect.is_empty or any(_box_overlap_ratio((rect.x0, rect.y0, rect.x1, rect.y1), d) >= 0.5 for d in done):
                        continue   # 文字層已經遮過這一塊
                    redact(label, rect, value, score, f"image_ocr:{source}")
    page.apply_redactions()


def _pdf_ip_fallback(pdf_words, scale):
    """掃描 PDF 專用 IPv4 fallback：OCR 可能把「.1.」整塊讀成「工」而無法還原，
    但去識別化只需遮住整個 value 區域（冒號之後），不遮 IP / 冒號。找到第一筆就回傳。"""
    sep_chars = ".·丄工,，"
    for lw in _group_lines(pdf_words):
        raw = "".join(w["text"] for w in lw)
        valid = None
        m = re.search(r"(?<!\d)(\d{1,3}(?:\.\d{1,3}){3})(?!\d)", raw.translate(_IP_FALLBACK_FIX).replace(" ", ""))
        if m and all(int(p) <= 255 for p in m.group(1).split(".")):
            valid = m.group(1)
        elif ":" in raw:   # 只接受明確 IP-like 欄位（OCR 可能把 "IP" 讀成 ""、"|P"、"丨P"、"1P"）
            prefix, val = raw.split(":", 1)
            if (prefix.strip().upper().replace(" ", "") in {"", "IP", "|P", "丨P", "1P"} and val
                    and all(c in "0123456789" + sep_chars for c in val)
                    and 7 <= sum(c.isdigit() for c in val) <= 12 and sum(c in sep_chars for c in val) >= 2):
                valid = val
        if valid is None:
            continue
        ci = next((i for i, w in enumerate(lw) if w["text"].strip() in {":", "："}), -1)
        nums = [w for w in (lw[ci + 1:] if ci >= 0 else lw) if any(c.isdigit() for c in w["text"]) or w["text"] in set(sep_chars)]
        if nums:
            return (*_union_box(nums, scale), "IP_ADDRESS", valid, None, "pdf_ip_fallback")
    return None


def _collect_pdf_ocr_boxes(page, zoom, img=None):
    """以指定 zoom 對掃描 PDF 頁面 OCR 一次。回傳 (PDF 座標系的 PII boxes, 該 zoom 渲染出的 PIL Image)。
    img 已渲染過時直接傳入，避免重複渲染。"""
    if img is None:
        img = _render_page(page, zoom)
    processed, ocr_scale = preprocess_for_ocr(img)
    pdf_words = ocr_words(processed)
    boxes = [b for b in find_sensitive_boxes_in_words(pdf_words, scale=ocr_scale) if _keep_pdf_box(b)]
    if not any(b[4] == "IP_ADDRESS" for b in boxes):
        ip_box = _pdf_ip_fallback(pdf_words, ocr_scale)
        if ip_box:
            boxes.append(ip_box)
    # padding 在該 zoom 的像素座標加入，再除以 zoom 回到 PDF 座標
    return [((x0 - MASK_PADDING) / zoom, (y0 - MASK_PADDING) / zoom, (x1 + MASK_PADDING) / zoom,
             (y1 + MASK_PADDING) / zoom, label, value, score, source)
            for x0, y0, x1, y1, label, value, score, source in boxes], img


def _is_blank(img):
    """整頁幾乎是單一顏色 (空白頁、背面) 就不必 OCR。標準差門檻很低，只有一行小字的頁面也不會被誤判。"""
    return ImageStat.Stat(img.convert("L")).stddev[0] < BLANK_PAGE_STD


@_cache_scope
def _mask_pdf_scanned_page(page, entries, counters, zoom=2.0):
    """掃描 PDF OCR：zoom=2.0 為主；PII_PDF_FALLBACK_ZOOM > 0 時再用該倍率多跑一次並合併。
    fallback 只有在「同 label 且位置高度重疊」時才視為重複，因此同頁可保留多筆同類型 PII。"""
    img = _render_page(page, zoom)
    if _is_blank(img):
        return   # 空白頁：沒有文字也沒有人臉，整頁略過
    boxes, _ = _collect_pdf_ocr_boxes(page, zoom, img)
    if PDF_FALLBACK_ZOOM and abs(zoom - PDF_FALLBACK_ZOOM) > 1e-9:
        boxes = list(boxes)
        for c in _collect_pdf_ocr_boxes(page, PDF_FALLBACK_ZOOM)[0]:
            if not any(e[4] == c[4] and _box_overlap_ratio(e, c) >= 0.5 for e in boxes):
                boxes.append(c)

    _add_pdf_face_redactions(page, entries, counters, img, zoom)   # 沿用同一張原始渲染圖

    for x0, y0, x1, y1, label, value, score, source in boxes:
        rect = fitz.Rect(x0, y0, x1, y1)
        # 對照表 crop 統一取自 primary render，避免混入 fallback zoom 的像素座標
        crop_box = (max(round(rect.x0 * zoom), 0), max(round(rect.y0 * zoom), 0),
                    min(round(rect.x1 * zoom), img.width), min(round(rect.y1 * zoom), img.height))
        entries.append({"tag": _new_tag(counters, label), "label": label, "value": value, "page": page.number,
                        "bbox": [rect.x0, rect.y0, rect.x1, rect.y1], "score": score, "source": source,
                        "crop_b64": _crop_to_png_b64(img, crop_box)})
        page.add_redact_annot(rect, fill=MASK_COLOR)
    page.apply_redactions()


def mask_pdf(input_path, output_path, map_file=DEFAULT_MAP_FILE):
    """遮蔽後以新建的純影像 PDF 安全輸出（不用 doc.save，避免原始附件等物件殘留）；
    輸出檢查通過後才寫入還原對照表。"""
    if _same_path(input_path, output_path):
        raise ValueError("PDF 安全輸出失敗：輸出路徑不得等於來源 PDF")
    entries, counters = [], {}
    with fitz.open(input_path) as doc:
        if doc.needs_pass:
            raise RuntimeError("PDF 需要密碼，尚未解密，無法安全遮蔽")
        # OCR bbox 與還原對照皆以未旋轉座標計算；旋轉頁尚未驗證，拒絕以免未遮蔽即輸出
        if any(page.rotation != 0 for page in doc):
            raise ValueError("目前不支援旋轉 PDF 的安全遮蔽：請先將頁面轉正再處理")
        for page in doc:
            (_mask_pdf_text_page if _page_has_text(page) else _mask_pdf_scanned_page)(page, entries, counters)
        save_secure_raster_pdf(doc, output_path, dpi=200)

    with fitz.open(output_path) as check:
        if check.embfile_count():
            raise RuntimeError(f"PDF 安全檢查失敗：仍有 {check.embfile_count()} 個內嵌附件")
        if len(check) == 0:
            raise RuntimeError("PDF 安全檢查失敗：輸出 PDF 頁數為 0")

    # 對照表含未遮蔽的原始畫面，應與對外分享的 PDF 分開保存
    _save_map_entry(map_file, os.path.basename(output_path), entries)
    print_masked_entries(entries, "PDF")
    return entries


def restore_pdf(masked_path, output_path, map_file=DEFAULT_MAP_FILE):
    """同一像素畫布貼回私有裁圖，輸出單影像 PDF。"""
    entries = _load_map_entry(map_file, os.path.basename(masked_path))
    if not entries:
        raise ValueError("找不到媒體還原對照資料，拒絕產生空白還原檔")
    restore_pdf_flat(masked_path, output_path, entries)


# ========================= main.py 呼叫的統一入口 =========================
def _resolve_output_path(input_path, suffix):
    name, ext = os.path.splitext(os.path.basename(input_path))
    if MEDIA_OUTPUT_DIR is None:
        folder = tempfile.gettempdir()
    elif MEDIA_OUTPUT_DIR == "SAME_FOLDER":
        folder = os.path.dirname(input_path)
    else:
        os.makedirs(MEDIA_OUTPUT_DIR, exist_ok=True)
        folder = MEDIA_OUTPUT_DIR
    return os.path.join(folder, f"{name}{suffix}{ext.lower()}")


def mask_file(input_path, output_path=None, map_file=DEFAULT_MAP_FILE):
    output_path = output_path or _resolve_output_path(input_path, "_masked")
    (mask_pdf if input_path.lower().endswith(".pdf") else mask_image)(input_path, output_path, map_file)
    return output_path


def restore_file(masked_path, output_path=None, map_file=DEFAULT_MAP_FILE):
    output_path = output_path or _resolve_output_path(masked_path, "_restored")
    (restore_pdf if masked_path.lower().endswith(".pdf") else restore_image)(masked_path, output_path, map_file)
    return output_path