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
import re
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

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
    """
    OCR 影像前處理。

    實驗結果顯示，將圖片固定放大到 1600px
    並不會穩定提升 Windows OCR 的辨識準確率，
    部分情況反而會使 CER 上升。

    因此改用：
    1. 灰階化
    2. 對比增強
    3. Unsharp Mask 銳化

    目前不改變圖片尺寸，因此 scale 固定為 1.0，
    OCR bounding box 可直接對應原始圖片座標。
    """

    # 1. 灰階化
    processed = img.convert("L")

    # 2. 提升文字與背景的對比
    processed = ImageEnhance.Contrast(
        processed
    ).enhance(1.8)

    # 3. 強化文字邊緣
    processed = processed.filter(
        ImageFilter.UnsharpMask(
            radius=1.5,
            percent=150,
            threshold=2,
        )
    )

    # Windows OCR 最後仍使用 RGB 圖片
    processed = processed.convert("RGB")

    # 沒有 resize，因此 OCR 座標與原圖一致
    return processed, 1.0


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


def _build_compact_ocr_line(line_words: List[dict]):
    """
    建立不含 OCR word 人工空白的文字，
    並保留每個字串區段對應的原始 OCR word。

    主要用於補抓因 Windows OCR 中文分詞而無法匹配的 PII，
    例如台灣地址。
    """
    text = ""
    offsets = []

    for w in line_words:
        start = len(text)
        text += w["text"]
        offsets.append((start, len(text), w))

    return text, offsets


def _build_ip_normalized_ocr_line(line_words: List[dict]):
    """
    建立供 IP_ADDRESS 補抓使用的 OCR 文字。

    Windows OCR 有時會將 IPv4 的句點誤辨識成
    '·' 或 '丄'。這裡只建立 IP 專用的 normalization
    版本，不修改原始 OCR 文字，也不影響其他 PII 規則。

    每個 OCR word 的字串長度保持不變，
    因此 offsets 仍可正確對應 bounding box。
    """
    text = ""
    offsets = []

    for w in line_words:
        normalized_word = (
            w["text"]
            .replace("·", ".")
            .replace("丄", ".")
        )

        start = len(text)
        text += normalized_word
        offsets.append((start, len(text), w))

    return text, offsets


def _build_phone_normalized_ocr_line(line_words: List[dict]):
    """
    建立供 TW_PHONE 補抓使用的 OCR 文字。

    Windows OCR 在掃描 PDF / 低品質影像中，有時會將手機號碼中的
    半形連字號 '-' 誤辨識成中文字「一」或中點「·」。
    這裡只建立電話專用的 normalization 版本：
        一 -> -
        · -> -

    不修改原始 OCR 文字，也不影響其他 PII 規則。
    替換後字元長度不變，因此 offsets 仍可正確對應 bounding box。
    """
    text = ""
    offsets = []

    for w in line_words:
        normalized_word = (
            w["text"]
            .replace("一", "-")
            .replace("·", "-")
        )
        start = len(text)
        text += normalized_word
        offsets.append((start, len(text), w))

    return text, offsets



def _append_compact_span_box(boxes, offsets, text, start, end, label, scale=1.0, source="field_regex"):
    """把 compact OCR 文字中的指定 span 映射回實際 OCR word bbox。"""
    hit_words = [w for s, e, w in offsets if s < end and e > start]
    if not hit_words:
        return

    x0 = min(w["left"] for w in hit_words)
    y0 = min(w["top"] for w in hit_words)
    x1 = max(w["left"] + w["width"] for w in hit_words)
    y1 = max(w["top"] + w["height"] for w in hit_words)
    if scale != 1.0:
        x0 /= scale; y0 /= scale; x1 /= scale; y1 /= scale

    boxes.append((x0, y0, x1, y1, label, text[start:end], None, source))


def _add_field_aware_boxes(line_words, boxes, scale=1.0):
    """
    針對有明確欄位名稱的文件補完整 bbox。

    目的不是猜測 OCR 遺失字元，而是利用「出生日期為 / 住院期間為 / 開戶銀行為 /
    案件編號為 / 承辦單位於」等欄位名稱，將已 OCR 出來但被 NER 切碎的敏感值完整遮蔽。
    """
    text, offsets = _build_compact_ocr_line(line_words)

    # 圖片 OCR 欄位型 PII 補救。
    # 只在明確欄位名稱存在時擴張 bbox，避免把一般文字誤遮。

    # 姓名：Windows OCR 常把中文姓名拆成單字，NER 可能只抓到首尾字。
    m = re.search(r"姓名[:：]?[「\"']?(?P<value>[\u3400-\u9fff]{2,4})", text)
    if m:
        _append_compact_span_box(
            boxes, offsets, text,
            m.start("value"), m.end("value"),
            "PERSON", scale,
            source="field_regex_person",
        )

    # 地址：補救 detector 從第二個字才開始命中的情況。
    # 除了 regex span 外，再以明確「地址」欄位的 OCR word 邊界取值，
    # 避免低解析度時第一個地址字（例如「高」）落在既有 ADDRESS bbox 外。
    m = re.search(r"地址[:：]?[「\"']?(?P<value>[^，,。；;]{4,40})", text)
    if m:
        value = m.group("value")
        if any(token in value for token in ("市", "縣", "區", "鄉", "鎮", "路", "街", "號")):
            _append_compact_span_box(
                boxes, offsets, text,
                m.start("value"), m.end("value"),
                "ADDRESS", scale,
                source="field_regex_address",
            )

    # 低解析度欄位補救：只在同一 OCR 行明確出現「地址」時啟用。
    # 找到欄位名稱結束位置後，直接從第一個非冒號 word 開始建立 ADDRESS bbox。
    # 這裡不向左任意擴張，因此不會把「地址：」本身遮掉。
    field_end = None
    for i, (_s, _e, w) in enumerate(offsets):
        if w["text"] == "地址":
            field_end = i + 1
            break
        if i + 1 < len(offsets) and w["text"] == "地" and offsets[i + 1][2]["text"] == "址":
            field_end = i + 2
            break

    if field_end is not None:
        value_words = []
        for _s, _e, w in offsets[field_end:]:
            token = w["text"].strip()
            if token in {":", "："}:
                continue
            if not token:
                continue
            value_words.append(w)

        joined_value = "".join(w["text"] for w in value_words)
        if (
            len(joined_value) >= 4
            and any(token in joined_value for token in ("市", "縣", "區", "鄉", "鎮", "路", "街", "號"))
        ):
            x0 = min(w["left"] for w in value_words)
            y0 = min(w["top"] for w in value_words)
            x1 = max(w["left"] + w["width"] for w in value_words)
            y1 = max(w["top"] + w["height"] for w in value_words)

            # 低解析度 OCR 可能完全漏掉地址第一個字。
            # 例如原圖「地址：高雄市...」只辨識成「地址：雄市...」。
            # 此時不能重建遺失文字，但可以利用「欄位分隔符 -> 第一個值 word」
            # 的異常空隙，把可能漏掉的敏感字所在區域一併納入遮蔽。
            #
            # 正常字距約為單一中文字寬度的一小部分；若 gap 大於
            # 第一個已辨識值 word 寬度的 0.9 倍，視為可能漏掉一個字。
            # 左界只回補到分隔符右側，不會遮掉「地址」欄位名稱。
            first_value = value_words[0]
            separator_word = offsets[field_end - 1][2] if field_end > 0 else None

            # field_end 指向「地址」後；若下一個 OCR word 是冒號，使用冒號作為 separator。
            if field_end < len(offsets):
                maybe_sep = offsets[field_end][2]
                if maybe_sep["text"].strip() in {":", "："}:
                    separator_word = maybe_sep

            if separator_word is not None:
                sep_right = separator_word["left"] + separator_word["width"]
                gap = first_value["left"] - sep_right
                first_width = max(first_value["width"], 1.0)

                if gap >= first_width * 0.9:
                    # 留一點正常欄位間距，避免黑框直接貼住冒號。
                    normal_gap = min(first_width * 0.35, 6.0)
                    x0 = max(sep_right + normal_gap, 0.0)

            if scale != 1.0:
                x0 /= scale; y0 /= scale; x1 /= scale; y1 /= scale
            boxes.append((
                x0, y0, x1, y1,
                "ADDRESS", joined_value, None, "field_word_address",
            ))

    # IP：OCR 可能把 IPv4 點號讀成「· / 丄 / 工」甚至直接吞掉。
    # 不猜回遺失的點號；只在明確 IP 欄位中遮蔽 OCR 已辨識出的數字型值。
    m = re.search(r"(?i)(?:^|[^A-Za-z])IP[:：·.]?(?P<value>[0-9·.丄工]+)$", text)
    if m:
        value = m.group("value")
        digit_count = sum(ch.isdigit() for ch in value)
        if 7 <= digit_count <= 12:
            _append_compact_span_box(
                boxes, offsets, text,
                m.start("value"), m.end("value"),
                "IP_ADDRESS", scale,
                source="field_regex_ip",
            )

    # 完整日期：避免 Presidio 只回傳年份或日期的一小段。
    date_pat = r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日"

    # 出生日期：只取「出生日期為」後的完整日期。
    m = re.search(r"出生日期為[「\"']?(?P<value>" + date_pat + r")", text)
    if m:
        _append_compact_span_box(boxes, offsets, text, m.start("value"), m.end("value"),
                                 "DATE_OF_BIRTH", scale)

    # 住院期間：兩個日期分別建立 bbox，刻意保留中間的「至」。
    m = re.search(r"住院期間為[「\"']?(?P<start>" + date_pat + r")至(?P<end>" + date_pat + r")", text)
    if m:
        _append_compact_span_box(boxes, offsets, text, m.start("start"), m.end("start"),
                                 "DATE_TIME", scale)
        _append_compact_span_box(boxes, offsets, text, m.start("end"), m.end("end"),
                                 "DATE_TIME", scale)

    # 申請日期：同樣遮完整年月日。
    m = re.search(r"申請日期為[「\"']?(?P<value>" + date_pat + r")", text)
    if m:
        _append_compact_span_box(boxes, offsets, text, m.start("value"), m.end("value"),
                                 "DATE_TIME", scale)

    # 銀行名稱：依欄位邊界取值，不把所有 ORGANIZATION 一律視為敏感資訊。
    m = re.search(r"開戶銀行為(?P<value>[^，,。；;]{2,30}?分行)", text)
    if m:
        _append_compact_span_box(boxes, offsets, text, m.start("value"), m.end("value"),
                                 "BANK_NAME", scale)

    # 案件編號：欄位值整段遮蔽。即使 OCR 把最後一碼辨錯，也不讓 CLM/尾碼外露。
    m = re.search(
        r"(?:理賠)?案件編號為(?P<value>CLM[-一](?:19|20)\d{2}[-一]\d{6}[0-9丆])",
        text,
        re.IGNORECASE,
    )
    if m:
        _append_compact_span_box(boxes, offsets, text, m.start("value"), m.end("value"),
                                 "CASE_NUMBER", scale)

    # 承辦單位地址：只遮「於」後到「收件」前的地址，不把「承辦單位於」一起遮掉。
    m = re.search(r"承辦單位於(?P<value>.+?)(?=收件|並登錄|，|,|。)", text)
    if m and any(token in m.group("value") for token in ("市", "縣", "區", "鄉", "鎮", "路", "街", "號")):
        _append_compact_span_box(boxes, offsets, text, m.start("value"), m.end("value"),
                                 "ADDRESS", scale)


def _add_person_title_boxes(line_words, boxes, scale=1.0):
    """
    補抓「與特定 PERSON 緊鄰的稱謂」，而不是把所有 POSITION 都遮掉。

    例如：
      - 李佳蓉醫師      -> 補遮「醫師」
      - Dr. Chia-Jung Lee -> 補遮「Dr.」

    單獨出現的「主治醫師 / 醫師」不會因這個 helper 被遮蔽。
    """
    compact_text, offsets = _build_compact_ocr_line(line_words)
    if not compact_text:
        return

    # 先只用目前中文 NER 的 PERSON 結果判斷姓名位置。
    person_matches = [
        m for m in detect_matches(compact_text)
        if m[2] == "PERSON"
    ]

    title_spans = []
    for p_start, p_end, _label, _score, _source in person_matches:
        # 中文姓名後綴：PERSON + 醫師。只補「醫師」本身。
        suffix = re.match(r"醫師", compact_text[p_end:])
        if suffix:
            title_spans.append((p_end, p_end + suffix.end(), "PERSON_TITLE"))

        # 英文姓名前綴：Dr. + PERSON；允許 OCR 把句點漏掉。
        prefix_text = compact_text[:p_start]
        prefix = re.search(r"(?i)Dr\.?$", prefix_text)
        if prefix:
            title_spans.append((prefix.start(), prefix.end(), "PERSON_TITLE"))

    for start, end, label in title_spans:
        _append_compact_span_box(
            boxes, offsets, compact_text, start, end, label, scale,
            source="person_title_context",
        )

def _add_cross_line_field_boxes(grouped_lines, boxes, scale=1.0):
    """補抓被 OCR 換行切開的欄位值；每一行各自建立 bbox，避免遮到行間正常文字。"""
    date_pat = r"(?:19|20)\d{2}年\d{1,2}月\d{1,2}日"

    # 欄位名稱 + 允許跨行的值。CASE_NUMBER 使用固定格式，避免一路吃到申請日期/承辦單位。
    specs = [
        (r"出生日期為[「\"']?(?P<value>" + date_pat + r")", "DATE_OF_BIRTH"),
        (r"申請日期為[「\"']?(?P<value>" + date_pat + r")", "DATE_TIME"),
        (r"開戶銀行為(?P<value>[^，,。；;]{2,30}?分行)", "BANK_NAME"),
        (r"(?:理賠)?案件編號為(?P<value>CLM[-一](?:19|20)\d{2}[-一]\d{6}[0-9丆])", "CASE_NUMBER"),
        (r"承辦單位於(?P<value>.+?)(?=收件|並登錄|，|,|。)", "ADDRESS"),
    ]

    for i in range(len(grouped_lines) - 1):
        pair_words = grouped_lines[i] + grouped_lines[i + 1]
        text, offsets = _build_compact_ocr_line(pair_words)

        # 住院期間需要兩個日期分開遮，保留中間的「至」。
        stay = re.search(r"住院期間為[「\"']?(?P<start>" + date_pat + r")至(?P<end>" + date_pat + r")", text)
        matches = []
        if stay:
            matches.extend([
                (stay.start("start"), stay.end("start"), "DATE_TIME"),
                (stay.start("end"), stay.end("end"), "DATE_TIME"),
            ])

        for pattern, label in specs:
            m = re.search(pattern, text, re.IGNORECASE)
            if not m:
                continue
            value = m.group("value")
            if label == "ADDRESS" and not any(t in value for t in ("市", "縣", "區", "鄉", "鎮", "路", "街", "號")):
                continue
            matches.append((m.start("value"), m.end("value"), label))

        for start, end, label in matches:
            hit_offsets = [(s, e, w) for s, e, w in offsets if s < end and e > start]
            if not hit_offsets:
                continue
            hit_line_ids = {w["line_id"] for _, _, w in hit_offsets}
            # 這個 helper 只負責真正跨行的值；單行已由 _add_field_aware_boxes 處理。
            if len(hit_line_ids) < 2:
                continue

            for line_id in sorted(hit_line_ids):
                line_hits = [(s, e, w) for s, e, w in hit_offsets if w["line_id"] == line_id]
                if not line_hits:
                    continue
                hit_words = [w for _, _, w in line_hits]
                x0 = min(w["left"] for w in hit_words)
                y0 = min(w["top"] for w in hit_words)
                x1 = max(w["left"] + w["width"] for w in hit_words)
                y1 = max(w["top"] + w["height"] for w in hit_words)
                if scale != 1.0:
                    x0 /= scale; y0 /= scale; x1 /= scale; y1 /= scale

                part_start = max(start, min(s for s, _, _ in line_hits))
                part_end = min(end, max(e for _, e, _ in line_hits))
                boxes.append((x0, y0, x1, y1, label, text[part_start:part_end], None, "field_regex_crossline"))


def find_sensitive_boxes_in_words(words: List[dict], scale: float = 1.0):
    """回傳 [(x0, y0, x1, y1, label, matched_text, score, source), ...]（像素座標）
    scale：如果 words 是從 preprocess_for_ocr() 放大過的圖片跑 OCR 得到的，這裡要傳對應的放大倍率，
    才能把座標除回原圖尺寸；呼叫端不用另外處理，這裡回傳的座標已經是「原圖座標」。"""
    boxes = []
    grouped_lines = _group_lines(words)
    for line_words in grouped_lines:
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
        # Windows OCR 常將中文地址拆成多個 word。
        # 原本 line_text 會在人為重建時加入空白，
        # 例如：
        # 高 雄 市 測 試 區 測 試 路 123 號
        #
        # ADDRESS regex 需要連續文字，因此額外使用
        # compact line 補抓 ADDRESS。
        compact_text, compact_offsets = _build_compact_ocr_line(line_words)

        # 欄位感知補抓：補完整日期、銀行名稱、案件編號與承辦地址邊界。
        _add_field_aware_boxes(line_words, boxes, scale=scale)

        # 人物稱謂情境補抓：只遮與 PERSON 緊鄰的「醫師 / Dr.」，不恢復全域 POSITION 遮蔽。
        _add_person_title_boxes(line_words, boxes, scale=scale)

        for start, end, label, score, source in detect_matches(compact_text):

            # compact 版本目前只補抓 ADDRESS，
            # 避免改變其他既有 PII 的偵測行為。
            COMPACT_LABELS = {
                "ADDRESS",
                "LICENSE_NUMBER",
                "MEDICAL_RECORD_NUMBER",
                "INSURANCE_POLICY_NUMBER",
                "EMPLOYEE_ID",
                "CASE_NUMBER",
            }

            if label not in COMPACT_LABELS:
                continue

            hit_words = [
                w
                for s, e, w in compact_offsets
                if s < end and e > start
            ]

            if not hit_words:
                continue

            x0 = min(w["left"] for w in hit_words)
            y0 = min(w["top"] for w in hit_words)

            x1 = max(
                w["left"] + w["width"]
                for w in hit_words
            )

            y1 = max(
                w["top"] + w["height"]
                for w in hit_words
            )

            if scale != 1.0:
                x0 /= scale
                y0 /= scale
                x1 /= scale
                y1 /= scale

            boxes.append(
                (
                    x0,
                    y0,
                    x1,
                    y1,
                    label,
                    compact_text[start:end],
                    score,
                    source,
                )
            )

        # Windows OCR 有時會將 IPv4 的句點誤辨識成「·」或「丄」。
        # 使用 IP 專用 normalization 補抓 IP_ADDRESS，不影響其他 PII 規則。
        ip_text, ip_offsets = _build_ip_normalized_ocr_line(line_words)

        for start, end, label, score, source in detect_matches(ip_text):
            if label != "IP_ADDRESS":
                continue

            hit_words = [
                w
                for s, e, w in ip_offsets
                if s < end and e > start
            ]
            if not hit_words:
                continue

            x0 = min(w["left"] for w in hit_words)
            y0 = min(w["top"] for w in hit_words)
            x1 = max(w["left"] + w["width"] for w in hit_words)
            y1 = max(w["top"] + w["height"] for w in hit_words)

            if scale != 1.0:
                x0 /= scale
                y0 /= scale
                x1 /= scale
                y1 /= scale

            # 正常 OCR 文字可能已在第一輪被偵測到，避免加入重複 IP box。
            already_detected = any(
                existing[4] == "IP_ADDRESS"
                and abs(existing[0] - x0) < 1
                and abs(existing[1] - y0) < 1
                and abs(existing[2] - x1) < 1
                and abs(existing[3] - y1) < 1
                for existing in boxes
            )
            if already_detected:
                continue

            boxes.append(
                (
                    x0,
                    y0,
                    x1,
                    y1,
                    label,
                    ip_text[start:end],
                    score,
                    source,
                )
            )
        # 電話專用 OCR normalization：只修正已辨識出的分隔符，
        # 不猜測或補回 OCR 已遺失的數字。
        phone_text, phone_offsets = _build_phone_normalized_ocr_line(line_words)

        for start, end, label, score, source in detect_matches(phone_text):
            if label != "TW_PHONE":
                continue

            hit_words = [
                w for s, e, w in phone_offsets
                if s < end and e > start
            ]
            if not hit_words:
                continue

            x0 = min(w["left"] for w in hit_words)
            y0 = min(w["top"] for w in hit_words)
            x1 = max(w["left"] + w["width"] for w in hit_words)
            y1 = max(w["top"] + w["height"] for w in hit_words)

            if scale != 1.0:
                x0 /= scale
                y0 /= scale
                x1 /= scale
                y1 /= scale

            already_detected = any(
                existing[4] == "TW_PHONE"
                and abs(existing[0] - x0) < 1
                and abs(existing[1] - y0) < 1
                and abs(existing[2] - x1) < 1
                and abs(existing[3] - y1) < 1
                for existing in boxes
            )
            if already_detected:
                continue

            boxes.append(
                (
                    x0, y0, x1, y1,
                    label,
                    phone_text[start:end],
                    score,
                    source,
                )
            )


    # 欄位值本身若被 OCR 切成兩行，先以欄位語意補完整。
    _add_cross_line_field_boxes(grouped_lines, boxes, scale=scale)

    # 相鄰兩行的 PII 補抓。
    # Windows OCR 可能把 Email、身分證、帳號等切在換行處；單行偵測時會只抓到後半段。
    # 這裡把「相鄰兩行」各自 compact 後暫時串接，只允許格式明確的 PII 類別跨行。
    # 命中後仍依原本 line_id 分開建立 bbox，避免用一個大矩形遮住兩行之間的正常文字。
    CROSS_LINE_LABELS = {
        "EMAIL", "EMAIL_ADDRESS",
        "TW_ID",
        "TW_PHONE", "PHONE_NUMBER", "US_PHONE",
        "BANK_ACCOUNT",
        "LICENSE_NUMBER",
        "MEDICAL_RECORD_NUMBER",
        "INSURANCE_POLICY_NUMBER",
        "EMPLOYEE_ID",
        "CASE_NUMBER",
        "IP_ADDRESS",
    }

    for i in range(len(grouped_lines) - 1):
        pair_words = grouped_lines[i] + grouped_lines[i + 1]
        cross_text, cross_offsets = _build_compact_ocr_line(pair_words)

        for start, end, label, score, source in detect_matches(cross_text):
            if label not in CROSS_LINE_LABELS:
                continue

            hit_offsets = [
                (s, e, w)
                for s, e, w in cross_offsets
                if s < end and e > start
            ]
            if not hit_offsets:
                continue

            # 必須真的跨越兩個 OCR line，否則單行流程已經處理過。
            hit_line_ids = {w["line_id"] for _, _, w in hit_offsets}
            if len(hit_line_ids) < 2:
                continue

            # 同一個跨行 PII 分成每行各自的 bbox，避免遮到中間大片正常內容。
            for line_id in sorted(hit_line_ids):
                line_hits = [
                    (s, e, w)
                    for s, e, w in hit_offsets
                    if w["line_id"] == line_id
                ]
                if not line_hits:
                    continue

                hit_words = [w for _, _, w in line_hits]
                x0 = min(w["left"] for w in hit_words)
                y0 = min(w["top"] for w in hit_words)
                x1 = max(w["left"] + w["width"] for w in hit_words)
                y1 = max(w["top"] + w["height"] for w in hit_words)

                if scale != 1.0:
                    x0 /= scale
                    y0 /= scale
                    x1 /= scale
                    y1 /= scale

                # 該行若已被前面的單行流程以相同 label、相同位置遮蔽，就不重複加入。
                already_detected = any(
                    existing[4] == label
                    and abs(existing[0] - x0) < 1
                    and abs(existing[1] - y0) < 1
                    and abs(existing[2] - x1) < 1
                    and abs(existing[3] - y1) < 1
                    for existing in boxes
                )
                if already_detected:
                    continue

                # value 只記錄這一行實際被框到的片段；完整跨行內容仍由 detect_matches 判斷。
                part_start = max(start, min(s for s, _, _ in line_hits))
                part_end = min(end, max(e for _, e, _ in line_hits))
                part_value = cross_text[part_start:part_end]

                boxes.append((
                    x0, y0, x1, y1,
                    label,
                    part_value,
                    score,
                    source,
                ))

    # 最終輸出前做 bbox 去重。
    # 同一段 PII 可能被「一般行 / compact / normalization / 跨行」重複抓到；
    # 只移除空間上幾乎是同一個位置的重複框，不合併相鄰但不同列的跨行片段。
    boxes = _dedupe_sensitive_boxes(boxes)
    return boxes


def _box_overlap_ratio(box_a, box_b) -> float:
    """回傳兩框交集面積 / 較小框面積；用來判斷是否其實是同一段文字。"""
    ax0, ay0, ax1, ay1 = box_a[:4]
    bx0, by0, bx1, by1 = box_b[:4]

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0

    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    smaller = min(area_a, area_b)
    return intersection / smaller if smaller > 0 else 0.0


def _box_label_priority(label: str) -> int:
    """同一位置出現多種 label 時，格式明確的 PII 優先於泛用 NLP 類別。"""
    explicit = {
        "TW_ID", "EMAIL", "EMAIL_ADDRESS", "TW_PHONE", "IP_ADDRESS",
        "BANK_ACCOUNT", "LICENSE_NUMBER", "MEDICAL_RECORD_NUMBER",
        "INSURANCE_POLICY_NUMBER", "EMPLOYEE_ID", "CASE_NUMBER", "BANK_NAME", "DATE_OF_BIRTH",
    }
    if label in explicit:
        return 0
    if label in {"PERSON", "ADDRESS"}:
        return 1
    # PERSON_TITLE 是經「緊鄰 PERSON」情境確認後才補出的稱謂。
    # 必須優先於原始 POSITION，否則兩者 bbox 完全重疊時，
    # 去重會先留下 POSITION，接著政策過濾又把 POSITION 刪掉，
    # 最後就會出現「李佳蓉有遮、醫師沒遮」的情況。
    if label == "PERSON_TITLE":
        return 2
    if label in {"CREDENTIAL_LIKE", "PHONE_NUMBER", "US_PHONE"}:
        return 2
    return 3


def _dedupe_sensitive_boxes(boxes):
    """
    移除同位置重複 bbox。

    注意：這裡刻意不做全域 Sensitive-PII allowlist。
    DATE_TIME / ORGANIZATION / POSITION 是否屬於敏感資訊需要依上下文判斷；
    若直接依 label 全砍，可能讓出生日期、就醫日期等真正敏感內容外洩。
    因此這一版先做安全的 bbox 去重，再用 benchmark 決定是否需要情境式政策過濾。
    """
    ordered = sorted(
        boxes,
        key=lambda b: (
            _box_label_priority(b[4]),
            -(b[2] - b[0]) * (b[3] - b[1]),
            -(b[6] if b[6] is not None else 0),
        ),
    )

    kept = []
    for candidate in ordered:
        duplicate_index = None
        for i, existing in enumerate(kept):
            # 0.85：只把幾乎位於同一處的框視為重複。
            # 跨行 PII 的上下兩個片段 y 座標不同，不會在這裡被合併。
            if _box_overlap_ratio(candidate, existing) >= 0.85:
                duplicate_index = i
                break

        if duplicate_index is None:
            kept.append(candidate)
            continue

        existing = kept[duplicate_index]
        candidate_key = (
            _box_label_priority(candidate[4]),
            -(candidate[2] - candidate[0]) * (candidate[3] - candidate[1]),
            -(candidate[6] if candidate[6] is not None else 0),
        )
        existing_key = (
            _box_label_priority(existing[4]),
            -(existing[2] - existing[0]) * (existing[3] - existing[1]),
            -(existing[6] if existing[6] is not None else 0),
        )
        if candidate_key < existing_key:
            kept[duplicate_index] = candidate

    # 若欄位規則已抓到完整日期，移除與它重疊的泛用 DATE_TIME。
    # 例如「2026年2月14日至2026年2月20日」中的舊模型可能另抓「14日至2026」，
    # 若保留會把 Ground Truth 要留下的「至」一起遮掉。
    field_date_boxes = [
        b for b in kept
        if b[4] in {"DATE_OF_BIRTH", "DATE_TIME"}
        and b[7] in {"field_regex", "field_regex_crossline"}
    ]
    if field_date_boxes:
        cleaned = []
        for b in kept:
            if b[4] == "DATE_TIME" and b[7] not in {"field_regex", "field_regex_crossline"}:
                if any(_box_overlap_ratio(b, fb) > 0 for fb in field_date_boxes):
                    continue
            cleaned.append(b)
        kept = cleaned

    # 第二階段：媒體去識別化政策過濾。
    # POSITION（職稱）本身不因 NER 單獨命中就遮蔽。
    # ORGANIZATION 不再全域排除：銀行分行等欄位在本專題 Ground Truth 中屬於應遮資訊。
    # DATE_TIME 暫時保留：目前測試文件同時包含出生/住院/申請日期，
    # 在沒有可靠上下文分類前直接刪除會增加敏感日期外洩風險。
    excluded_labels = {"POSITION"}
    kept = [box for box in kept if box[4] not in excluded_labels]

    # OCR / NER 有時會把欄位名稱「IP」誤判成 ORGANIZATION。
    # 「IP」只是欄位標籤，不是個資值；只排除 exact value=IP，
    # 不全域排除 ORGANIZATION，避免影響銀行分行等真正需要遮蔽的欄位。
    kept = [
        box for box in kept
        if not (
            box[4] == "ORGANIZATION"
            and str(box[5]).strip().upper().rstrip(":：") in {"IP", "EMAIL", "TW"}
        )
    ]

    # 輸出順序依畫面位置排列，方便 terminal / map 檔閱讀。
    return sorted(kept, key=lambda b: (b[1], b[0]))


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
    """
    文字層 PDF：
    - 一般 PII 仍使用 detect_matches。
    - 對 Phone / Email / TW ID / IP 這類明確欄位，優先只遮 value，
      避免把 Email、TW、IP 等欄位名稱一起遮掉。
    """
    words = page.get_text("words")
    lines = {}
    for w in words:
        lines.setdefault((w[5], w[6]), []).append(w)

    field_value_patterns = [
        ("TW_PHONE", re.compile(r"(?i)\b(?:Phone|電話)\s*[:：]\s*(?P<value>0\d{1,2}-?\d{6,8}|09\d{2}-?\d{3}-?\d{3})")),
        ("EMAIL", re.compile(r"(?i)\bEmail\s*[:：]\s*(?P<value>[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})")),
        ("TW_ID", re.compile(r"(?i)\bTW\s*ID\s*[:：]\s*(?P<value>[A-Z][12]\d{8})")),
        ("IP_ADDRESS", re.compile(r"(?i)\bIP\s*[:：]\s*(?P<value>(?:\d{1,3}\.){3}\d{1,3})")),
    ]

    for line_words in lines.values():
        line_words.sort(key=lambda w: w[7])
        line_text, offsets = "", []
        for w in line_words:
            start = len(line_text)
            line_text += w[4]
            offsets.append((start, len(line_text), w))
            line_text += " "

        matches = list(detect_matches(line_text))

        # 明確欄位改成 value-only span；同 label 的一般 detector 結果移除，
        # 避免同一行又產生一個包含欄位名稱的較大黑框。
        for field_label, pattern in field_value_patterns:
            m = pattern.search(line_text)
            if not m:
                continue
            matches = [item for item in matches if item[2] != field_label]
            matches.append((
                m.start("value"),
                m.end("value"),
                field_label,
                None,
                "pdf_field_value",
            ))

        # PDF 文字層中這些 exact token 是欄位名稱，不是敏感值。
        field_tokens = {"PHONE", "EMAIL", "TW", "ID", "IP"}

        for start, end, label, score, source in matches:
            hit = [w for s, e, w in offsets if s < end and e > start]
            if not hit:
                continue

            # 防止 NER 把單獨欄位名稱誤判成 ORGANIZATION。
            if label == "ORGANIZATION":
                hit_text = "".join(w[4] for w in hit).strip().upper().rstrip(":：")
                if hit_text in field_tokens:
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


def _pdf_rects_overlap(rect_a, rect_b, threshold: float = 0.5) -> bool:
    """
    判斷兩個 PDF 座標框是否代表同一處 PII。

    multi-zoom OCR 對同一段文字產生的框通常只會有少量座標差異。
    這裡用「交集面積 / 較小框面積」判斷，而不是只看 label，
    因此同一頁出現多個電話、Email、地址時不會互相吃掉。
    """
    ax0, ay0, ax1, ay1 = rect_a
    bx0, by0, bx1, by1 = rect_b

    ix0 = max(ax0, bx0)
    iy0 = max(ay0, by0)
    ix1 = min(ax1, bx1)
    iy1 = min(ay1, by1)

    if ix1 <= ix0 or iy1 <= iy0:
        return False

    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    smaller_area = min(area_a, area_b)

    return smaller_area > 0 and intersection / smaller_area >= threshold


def _collect_pdf_ocr_boxes(page, zoom: float):
    """
    以指定 zoom 對掃描 PDF 頁面做一次 OCR。

    回傳：
      1. 轉回 PDF 座標系的 PII boxes
      2. 該 zoom render 出來的 PIL Image

    box 格式：
      (x0, y0, x1, y1, label, value, score, source)
    """
    matrix = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=matrix, annots=False)
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    processed, ocr_scale = preprocess_for_ocr(img)
    pdf_words = ocr_words(processed)
    pixel_boxes = find_sensitive_boxes_in_words(
        pdf_words,
        scale=ocr_scale,
    )

    # 掃描 PDF 欄位名稱保護：
    # OCR/英文 NER 偶爾會把純欄位名稱 "Email" 誤判成 PERSON，
    # 造成 Email: 左側標籤也被遮蔽。只排除「精確等於 Email」
    # 的 PERSON/ORGANIZATION 誤判；真正的 Email value 仍由 EMAIL 規則遮蔽。
    pixel_boxes = [
        box for box in pixel_boxes
        if not (
            box[4] in {"PERSON", "ORGANIZATION"}
            and str(box[5]).strip().upper().rstrip(":：") == "EMAIL"
        )
    ]

    # 掃描 PDF 專用 IPv4 fallback。
    # low-resolution 實測可能把 ".1." 整塊 OCR 成「工」：
    #   192.168.1.100 -> 192,168工100 / 192·168工100
    # 這時無法可靠還原遺失的 1，但去識別化只需要把整個 value 區域遮住。
    existing_ip = any(box[4] == "IP_ADDRESS" for box in pixel_boxes)
    if not existing_ip:
        for line_words in _group_lines(pdf_words):
            raw = "".join(w["text"] for w in line_words)
            normalized = (
                raw.replace("·", ".")
                   .replace("丄", ".")
                   .replace("工", ".")
                   .replace(",", ".")
                   .replace("，", ".")
                   .replace(" ", "")
            )

            valid_ip = None

            # 情況 A：正規化後仍可得到完整合法 IPv4。
            m = re.search(r"(?<!\d)(?P<ip>\d{1,3}(?:\.\d{1,3}){3})(?!\d)", normalized)
            if m:
                octets = m.group("ip").split(".")
                if all(0 <= int(part) <= 255 for part in octets):
                    valid_ip = m.group("ip")

            # 情況 B：low-resolution 把 ".1." 合併成「工」。
            # 只接受明確 IP-like 欄位：
            #   zoom 2 實測 prefix=""   -> :192,168工100
            #   zoom 3 實測 prefix="|P" -> |P:192·168工100
            if valid_ip is None and ":" in raw:
                prefix, value_part = raw.split(":", 1)
                prefix_norm = prefix.strip().upper().replace(" ", "")
                ip_like_prefix = prefix_norm in {"", "IP", "|P", "丨P", "1P"}

                allowed = set("0123456789.·丄工,，")
                digit_count = sum(ch.isdigit() for ch in value_part)
                separator_count = sum(ch in ".·丄工,，" for ch in value_part)

                if (
                    ip_like_prefix
                    and value_part
                    and all(ch in allowed for ch in value_part)
                    and 7 <= digit_count <= 12
                    and separator_count >= 2
                ):
                    valid_ip = value_part

            if valid_ip is None:
                continue

            # 只遮冒號後的 IP value，不遮 IP / |P / 冒號本身。
            colon_index = next(
                (i for i, w in enumerate(line_words) if w["text"].strip() in {":", "："}),
                -1,
            )
            value_words = line_words[colon_index + 1:] if colon_index >= 0 else line_words

            numeric_words = [
                w for w in value_words
                if any(ch.isdigit() for ch in w["text"])
                or w["text"] in {".", "·", "丄", "工", ",", "，"}
            ]
            if not numeric_words:
                continue

            x0 = min(w["left"] for w in numeric_words)
            y0 = min(w["top"] for w in numeric_words)
            x1 = max(w["left"] + w["width"] for w in numeric_words)
            y1 = max(w["top"] + w["height"] for w in numeric_words)
            if ocr_scale != 1.0:
                x0 /= ocr_scale; y0 /= ocr_scale
                x1 /= ocr_scale; y1 /= ocr_scale

            pixel_boxes.append((
                x0, y0, x1, y1,
                "IP_ADDRESS", valid_ip, None, "pdf_ip_fallback",
            ))
            break

    pdf_boxes = []
    for x0, y0, x1, y1, label, value, score, source in pixel_boxes:
        # padding 先在該 zoom 的像素座標加入，再除以 zoom 回到 PDF 座標。
        px0 = x0 - MASK_PADDING
        py0 = y0 - MASK_PADDING
        px1 = x1 + MASK_PADDING
        py1 = y1 + MASK_PADDING

        pdf_boxes.append((
            px0 / zoom,
            py0 / zoom,
            px1 / zoom,
            py1 / zoom,
            label,
            value,
            score,
            source,
        ))

    return pdf_boxes, img


def _merge_pdf_multizoom_boxes(primary_boxes, fallback_boxes):
    """
    合併 multi-zoom OCR 結果。

    primary (zoom=2.0) 全部保留；
    fallback (zoom=3.0) 只有在「同 label 且位置高度重疊」時才視為重複。
    因此不是 label 層級去重，同一頁可安全保留多筆相同類型 PII。
    """
    merged = list(primary_boxes)

    for candidate in fallback_boxes:
        candidate_rect = candidate[:4]
        candidate_label = candidate[4]

        duplicate = any(
            existing[4] == candidate_label
            and _pdf_rects_overlap(existing[:4], candidate_rect)
            for existing in merged
        )

        if not duplicate:
            merged.append(candidate)

    return merged


def _mask_pdf_scanned_page(page, entries, counters, zoom: float = 2.0):
    """
    掃描 PDF 使用 multi-zoom OCR：
      - zoom=2.0 作為主要辨識結果
      - zoom=3.0 作為 fallback
      - 兩次結果都先轉回 PDF 座標，再依 label + 位置重疊去重

    zoom 參數保留相容性；預設 2.0 為 primary，fallback 固定比實驗驗證過的 3.0。
    """
    primary_zoom = zoom
    fallback_zoom = 3.0

    primary_boxes, primary_img = _collect_pdf_ocr_boxes(page, primary_zoom)
    fallback_boxes, fallback_img = _collect_pdf_ocr_boxes(page, fallback_zoom)

    # 若呼叫端未來真的傳入 3.0，就不需要同一 zoom 重跑兩次。
    if abs(primary_zoom - fallback_zoom) < 1e-9:
        boxes = primary_boxes
    else:
        boxes = _merge_pdf_multizoom_boxes(primary_boxes, fallback_boxes)

    for x0, y0, x1, y1, label, value, score, source in boxes:
        rect = fitz.Rect(x0, y0, x1, y1)

        counters[label] = counters.get(label, 0) + 1

        # 對照表中的 crop 必須對應 PDF rect。
        # 統一使用 primary render 擷取，避免 fallback zoom 的像素座標混入。
        crop_box = (
            max(int(round(rect.x0 * primary_zoom)), 0),
            max(int(round(rect.y0 * primary_zoom)), 0),
            min(int(round(rect.x1 * primary_zoom)), primary_img.width),
            min(int(round(rect.y1 * primary_zoom)), primary_img.height),
        )
        crop = primary_img.crop(crop_box)
        buf = io.BytesIO()
        crop.save(buf, format="PNG")

        entries.append({
            "tag": f"{label}_{counters[label]}",
            "label": label,
            "value": value,
            "page": page.number,
            "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
            "score": score,
            "source": source,
            "crop_b64": base64.b64encode(buf.getvalue()).decode("ascii"),
        })
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