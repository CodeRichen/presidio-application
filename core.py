"""
core.py - main.py / ui.py / hotkey_service.py 共用的核心
====================================================
放：設定、對照表、敏感清單、標籤遮蔽/還原、重疊處理、檔案分派 (code/text/media/field/csv)、
    資料夾掃描、剪貼簿讀寫工具、模型預熱。
log 一律用 print；ui.py 會把 sys.stdout 接到畫面上的記錄區。

名單 (敏感清單 / 黑名單 / 安全清單) 的存取與管理在 entity_store.py，main.py 與 ui.py 都直接用它；
這裡只負責在偵測時套用：
    敏感清單 -> 當成「正則規則」(source="regex"，優先序最高) 自動遮蔽 (USE_KNOWN_ITEMS = False 可關閉)
    安全清單 -> 偵測結果若剛好等於清單裡的值就略過，不遮蔽
    黑名單   -> 遮蔽時不會再把這些值自動記進敏感清單
這些名單在 ./presidio/real/，程式結束時【不會】刪除 (對照表 clipboard_map.txt 才會在結束時刪除)。

前處理 (preprocess.py)：文字 / 程式碼 / 表格儲存格 / 圖片與 PDF 的 OCR 文字送模型前，統一先做
    全形轉半形 -> 隱形字元 -> 網頁標籤 -> 簡繁統一 -> 換行與空白。偵測用前處理後的文字，
    結果再換算回【原文座標】，所以遮蔽只會動被偵測到的那一段，其餘原樣保留。
"""
import os
import io
import re
import sys
import time
import shutil
import struct
import contextlib
import hashlib
import json
import tempfile
from collections import Counter

import win32clipboard
import win32gui
import win32process
import psutil
import pyperclip

import handler_text
import handler_code
import handler_media
import handler_csv
import entity_store
import preprocess

try:
    import handler_field  # 選用：提供 detect(text) -> [(start, end, label, score, source)]
except ImportError:
    handler_field = None

# ========================= 設定區 =========================
CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs",
                   ".php", ".rb", ".sh", ".sql", ".json", ".yml", ".yaml"}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp", ".pdf"}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".log"}
TABLE_EXTENSIONS = handler_csv.TABLE_EXTENSIONS

MAP_FILE = "./presidio/real/clipboard_map.txt"            # 結束時刪除
SENSITIVE_FILE = entity_store.SENSITIVE_FILE   # 敏感清單 (名單都不會被自動刪除，見 entity_store.py)

USE_KNOWN_ITEMS = True    # 偵測時是否讀取敏感清單並自動遮蔽

# 資料夾掃描：僅在暫存目錄產生真正遮蔽的檔案，整批驗證後一次發布。
# 未命中的原稿不會複製；原資料夾保持不動。
SCAN_OUTPUT_SUFFIX = "_masked"
SCAN_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}   # 略過這些資料夾，不複製
# 偵測類型名稱含這些關鍵字就視為「高風險」(憑證/金融/證號)，其餘有命中的算「中風險」
HIGH_RISK_KEYWORDS = ("KEY", "JWT", "PASSWORD", "PASSWD", "SECRET", "TOKEN",
                      "CARD", "NATIONAL_ID", "SSN", "PRIVATE")


def heuristic_is_code(text: str) -> bool:
    signals = re.findall(
        r"[{};]|=>|\bdef \b|\bclass \b|\bimport \b|#include|\bfunction\b|\bconst \b|\bvar \b|\breturn \b",
        text,
    )
    return len(signals) >= 3


CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
    # "qwen": your_qwen_is_code_function,
}
TEXT_CLASSIFY_MODE = "heuristic"
# ==========================================================

TAG_RE = re.compile(r"<([A-Z_]+)_(\d+)>")


def is_code_text(text: str) -> bool:
    return CLASSIFY_MODELS[TEXT_CLASSIFY_MODE](text)


# ---------- 對照表 ----------
def load_map():
    mapping = {}
    try:
        with open(MAP_FILE, encoding="utf-8") as f:
            for line in f:
                tag, _, val = line.rstrip("\n").partition("\t")
                if tag:
                    mapping[tag] = val
    except FileNotFoundError:
        pass
    return mapping


def save_map(mapping):
    folder = os.path.dirname(MAP_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        for tag, val in mapping.items():
            f.write(f"{tag}\t{val}\n")


def clear_map(mapping):
    mapping.clear()
    try:
        os.remove(MAP_FILE)
    except FileNotFoundError:
        pass


def cleanup_map_files():
    """結束時刪除對照表，不留明碼敏感資訊在硬碟上。
    注意：敏感清單 (SENSITIVE_FILE) 是刻意保留的，不在這裡刪除。"""
    for f in (MAP_FILE, handler_media.DEFAULT_MAP_FILE):
        try:
            os.remove(f)
            print(f"[清理] 已刪除 {f}")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[警告] 刪除 {f} 失敗：{e}")


# ---------- 名單 (實作在 entity_store.py) ----------
record_sensitive = entity_store.record_sensitive
load_sensitive_items = entity_store.load_items
sensitive_count = entity_store.items_count


def remove_sensitive_items(values):
    """相容舊名稱：刪除 = 從敏感清單移除並加入黑名單"""
    return entity_store.block_items(values)


def detect_known(text):
    """把敏感清單當成一條正則規則：[(start, end, label, None, "regex")]"""
    return entity_store.detect_known(text) if USE_KNOWN_ITEMS else []


# ---------- 重疊處理 ----------
def _priority_tier(source: str, prefer_zh: bool) -> int:
    if source == "regex":
        return 0
    if source == "zh":
        return 1 if prefer_zh else 2
    if source == "en":
        return 2 if prefer_zh else 1
    return 3


def merge_overlaps(matches, prefer_zh: bool):
    """優先序：正則 (含敏感清單) > (依整段語言) 吻合的模型 > 不吻合的模型；同層範圍長者優先，再比信心分數。"""
    ordered = sorted(
        matches,
        key=lambda m: (
            _priority_tier(m[4], prefer_zh),
            -(m[1] - m[0]),
            -(m[3] if m[3] is not None else 0),
        ),
    )
    kept = []
    print(f"  重疊處理優先序：規則 > {'中文' if prefer_zh else '英文'}模型 > {'英文' if prefer_zh else '中文'}模型")
    for start, end, label, score, source in ordered:
        overlapped = any(not (end <= k[0] or start >= k[1]) for k in kept)
        status = "淘汰(重疊)" if overlapped else "保留"
        score_info = f"{score}" if score is not None else "None(規則比對)"
        print(f"    [{status}] {label:<22} [{start:>4}:{end:<4}] 來源={source:<5} 信心分數={score_info}")
        if not overlapped:
            kept.append((start, end, label, score, source))
    return sorted(kept, key=lambda m: m[0])


# ---------- 遮蔽 / 還原 ----------
def _detect_models(text, is_code):
    """規則 + 模型 (handler_text / handler_code / handler_field)；座標是傳進來的 text 的位置"""
    matches = list(handler_code.detect(text) if is_code else handler_text.detect(text))
    if handler_field is not None:
        matches += list(handler_field.detect(text))
    return matches


def detect_entities(text, is_code=None):
    """前處理 -> 規則與模型 -> 座標換算回原文 -> 手動清單 -> 安全清單 -> 重疊處理。
    回傳 (is_code, 保留下來的偵測結果 [(start, end, label, score, source)])，座標一律是【原文】的位置。"""
    if is_code is None:
        is_code = is_code_text(text)

    # 1. 前處理後的文字送模型 (程式碼不做網頁標籤刪除)，結果換算回原文
    norm = preprocess.preprocess(text, html=not is_code)
    matches = norm.map_matches(_detect_models(norm.text, is_code))
    if matches is None:                       # 偵測結果的格式認不得 -> 退回用原文偵測，不給錯誤座標
        matches = _detect_models(text, is_code)

    # 2. 手動清單的值是「原文裡的寫法」，直接比對原文 (known_entities.txt、敏感清單)
    fn = getattr(handler_text, "detect_known_entities", None)
    if fn:
        matches += fn(text)
    known = detect_known(text)
    matches += known
    matches = list(dict.fromkeys(tuple(m) for m in matches))      # 完全相同的候選只留一筆

    # 3. 安全清單：偵測到的值剛好等於清單裡的值就略過
    safe = entity_store.safe_values()
    if safe and matches:
        kept = [m for m in matches if text[m[0]:m[1]].strip() not in safe]
        if len(kept) != len(matches):
            print(f"[安全清單] 略過 {len(matches) - len(kept)} 筆已標為安全的值")
        matches = kept
    if not matches:
        return is_code, []

    prefer_zh = handler_text.contains_chinese(text)
    known_info = f"，其中敏感清單命中 {len(known)} 筆" if known else ""
    pre_info = f"；前處理 {len(text)} -> {len(norm.text)} 字元，座標已換算回原文" if not norm.identity else ""
    print(f"[偵測] 共 {len(matches)} 筆候選 (含正則規則與模型{known_info}{pre_info})：")
    return is_code, merge_overlaps(matches, prefer_zh)


def _anonymize_full(text, mapping, is_code, save, record, source):
    """回傳 (遮蔽後文字或 None, 保留下來的偵測結果)"""
    is_code, matches = detect_entities(text, is_code)
    if not matches:
        return None, []

    reverse = {v: k for k, v in mapping.items()}
    counters = {}
    for tag in mapping:
        m = TAG_RE.match(tag)
        if m:
            t, i = m.groups()
            counters[t] = max(counters.get(t, 0), int(i))
    # 預留原文中已存在的標籤編號
    # 即使 mapping 是空的，也不能重複使用 <PERSON_1>
    for m in TAG_RE.finditer(text):
        entity_type, number = m.groups()
        counters[entity_type] = max(
            counters.get(entity_type, 0),
            int(number)
        )
    out = text
    for start, end, etype, score, src in sorted(matches, key=lambda m: m[0], reverse=True):
        val = text[start:end]
        tag = reverse.get(val)
        if not tag:
            counters[etype] = counters.get(etype, 0) + 1
            tag = f"<{etype}_{counters[etype]}>"
            mapping[tag] = val
            reverse[val] = tag
        kind = "程式碼" if is_code else "一般文字"
        score_info = f", 信心分數 {score}" if score is not None else ""
        print(f"[遮蔽] {val!r} -> {tag}  (理由: {etype}, 判斷為{kind}{score_info}, 來源={src})")
        out = out[:start] + tag + out[end:]

    if record:
        record_sensitive([(text[s:e], label) for s, e, label, *_ in matches], source)
    if save:
        save_map(mapping)
    return out, matches


def anonymize_text(text, mapping, is_code=None, save=True, record=None, source=""):
    """is_code=None 才會用分類器猜；檔案處理一律明確傳入。
    save   : 是否寫對照表檔
    record : 是否把偵測到的值記錄進敏感清單 (None = 跟著 save；預熱 / 測試 / 暫時偵測都不會記錄)"""
    if record is None:
        record = save
    out, _ = _anonymize_full(text, mapping, is_code, save, record, source)
    return out


def deanonymize_text(text, mapping):
    return TAG_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


def process_text(text, mapping, action="auto", is_code=None):
    """
    action: "auto" (含標籤就還原、否則遮蔽) | "mask" | "restore"
    回傳處理後文字；沒有任何變動時回傳 None。
    """
    if action == "auto":
        action = "restore" if TAG_RE.search(text) else "mask"
    if action == "restore":
        print("[分類] 還原模式")
        result = deanonymize_text(text, mapping)
    else:
        result = anonymize_text(text, mapping, is_code=is_code)
    return result if result and result != text else None


def detect_cell(text):
    """給 handler_csv 判斷「這格有沒有個資 / 什麼類型」：用暫時的空對照表、不記錄清單、訊息靜音"""
    with contextlib.redirect_stdout(io.StringIO()):
        return anonymize_text(text, {}, is_code=False, save=False, record=False)


def make_tagger(mapping, source=""):
    """回傳 tag_value(etype, val)：同值同標籤，寫入共用 mapping (給 handler_csv 用)。
    實際貼上標籤的值會暫存，呼叫 tag_value.flush() 時一次記錄進敏感清單。"""
    state = {"n": -1, "reverse": {}, "counters": {}}
    pending = {}

    def refresh():
        state["reverse"] = {v: k for k, v in mapping.items()}
        counters = {}
        for tag in mapping:
            m = TAG_RE.match(tag)
            if m:
                t, i = m.groups()
                counters[t] = max(counters.get(t, 0), int(i))
        state["counters"] = counters
        state["n"] = len(mapping)

    def tag_value(etype, val):
        pending.setdefault(val, etype)
        if state["n"] != len(mapping):
            refresh()
        tag = state["reverse"].get(val)
        if tag:
            return tag
        c = state["counters"]
        c[etype] = c.get(etype, 0) + 1
        tag = f"<{etype}_{c[etype]}>"
        mapping[tag] = val
        state["reverse"][val] = tag
        state["n"] = len(mapping)
        return tag

    def flush():
        record_sensitive([(v, t) for v, t in pending.items()], source)
        pending.clear()

    tag_value.flush = flush
    return tag_value


# ---------- V6.6.9.5: 交易式檔案輸出保護（不含通用 PII 召回率證明） ----------
class SafeOutputError(RuntimeError):
    """對外只提供不含個資與絕對路徑的安全失敗訊息。"""


def _safe_source(path):
    path = os.path.abspath(os.fspath(path))
    if not os.path.isfile(path) or os.path.islink(path):
        raise SafeOutputError("輸入不是一般檔案或包含檔案符號連結")
    return path


def _file_digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _assert_safe_candidate(source, candidate, *, media=False):
    if os.path.islink(candidate) or not os.path.isfile(candidate) or os.path.getsize(candidate) == 0:
        raise SafeOutputError("輸出不存在、為空或是不允許的符號連結")
    if os.path.samefile(source, candidate) or _file_digest(source) == _file_digest(candidate):
        raise SafeOutputError("疑似未遮蔽原稿，不允許發布")
    if not media:
        return
    ext = os.path.splitext(source)[1].lower()
    if ext == ".pdf":
        # 檢查 PDF 可讀、頁數相同、已重新建立為每頁單一平面影像，
        # 且不存在可抽取文字、附件、註解、連結、表單。
        # 這不能證明「所有個資」都被模型找出，仍需獨立 GT 驗收。
        try:
            import fitz
            with fitz.open(source) as orig, fitz.open(candidate) as doc:
                if len(doc) == 0 or len(doc) != len(orig) or doc.needs_pass:
                    raise SafeOutputError("PDF 頁數、加密或開啟狀態異常")
                if doc.embfile_count() != 0 or doc.is_form_pdf:
                    raise SafeOutputError("PDF 含附件或表單")
                for page in doc:
                    if page.get_text().strip() or len(page.get_images(full=True)) != 1:
                        raise SafeOutputError("PDF 存在文字層或不是單影像頁面")
                    if page.get_links() or page.first_annot or page.first_widget:
                        raise SafeOutputError("PDF 存在連結、註解或表單元件")
        except SafeOutputError:
            raise
        except Exception as exc:
            raise SafeOutputError("PDF 輸出結構檢查失敗") from exc
    else:
        try:
            from PIL import Image
            with Image.open(candidate) as im:
                im.verify()
            with Image.open(candidate) as im, Image.open(source) as orig:
                if im.size != orig.size:
                    raise SafeOutputError("遮蔽圖片尺寸與原稿不符")
        except SafeOutputError:
            raise
        except Exception as exc:
            raise SafeOutputError("遮蔽圖片驗證失敗") from exc


def _verify_mask_coverage(candidate, entries, *, is_pdf):
    """機械性檢查：回報的每個 PII bbox，在實際輸出中需為黑色。

    不等於自動找出未知 PII 的完整召回率；搭配人工 GT 測試使用。
    """
    import math
    from PIL import Image
    if not entries:
        raise SafeOutputError("沒有可驗證的遮蔽框")
    cached_pages = {}
    try:
        if is_pdf:
            import fitz
            doc = fitz.open(candidate)
        else:
            doc = None
        try:
            for e in entries:
                box = e.get("bbox") if isinstance(e, dict) else None
                if not isinstance(box, (list, tuple)) or len(box) != 4:
                    raise SafeOutputError("缺少遮蔽框座標，不能驗證已遮蔽")
                try:
                    coords = [float(v) for v in box]
                except (ValueError, TypeError) as exc:
                    raise SafeOutputError("遮蔽框座標不合法") from exc
                if not all(math.isfinite(v) for v in coords):
                    raise SafeOutputError("遮蔽框座標非有限數字")
                x0,y0,x1,y1 = coords
                if x1 <= x0 or y1 <= y0:
                    raise SafeOutputError("遮蔽框大小不合法")
                page = int(e.get("page", 0)) if is_pdf else 0
                if page not in cached_pages:
                    if is_pdf:
                        if page < 0 or page >= len(doc):
                            raise SafeOutputError("遮蔽框 PDF 頁碼不合法")
                        pix = doc[page].get_pixmap(matrix=fitz.Matrix(2,2), alpha=False)
                        im = Image.frombytes("RGB", (pix.width,pix.height),pix.samples)
                        ratio=2
                    else:
                        im=Image.open(candidate).convert("RGB")
                        ratio=1
                    cached_pages[page]=(im,ratio)
                im,ratio=cached_pages[page]
                x0=max(0, int(x0*ratio));y0=max(0,int(y0*ratio))
                x1=min(im.width,int(math.ceil(x1*ratio)))
                y1=min(im.height,int(math.ceil(y1*ratio)))
                if x1 <= x0 or y1 <= y0:
                    raise SafeOutputError("遮蔽框不在輸出影像內")
                # 避免邊緣抗鋸齒；在 bbox 中心 5x5 網格抽樣。
                n=0;black=0
                for yi in range(5):
                    y=min(y1-1,max(y0, int(y0+(yi+0.5)*(y1-y0)/5)))
                    for xi in range(5):
                        x=min(x1-1,max(x0, int(x0+(xi+0.5)*(x1-x0)/5)))
                        rgb=im.getpixel((x,y))
                        n+=1
                        if max(rgb[:3]) <= 55: black+=1
                if black/n < 0.8:
                    raise SafeOutputError("輸出遮蔽框未達黑色覆蓋門檻")
        finally:
            if doc is not None: doc.close()
            for image,_ in cached_pages.values(): image.close()
    except SafeOutputError:
        raise
    except Exception as exc:
        raise SafeOutputError("實際遮蔽框像素驗證失敗") from exc


def _private_temp(parent):
    os.makedirs(parent, exist_ok=True)
    result = tempfile.mkdtemp(prefix=".pii_private_stage_", dir=parent)
    try:
        os.chmod(result, 0o700)
    except OSError:
        pass
    return result


def _atomic_private_write(path, data):
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".pii_map_", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "wb") as f:
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


def _snapshot_map(path):
    if not os.path.isfile(path):
        return None
    with open(path, "rb") as f:
        return f.read()


def _restore_map(path, original):
    if original is None:
        if os.path.lexists(path):
            os.unlink(path)
    else:
        _atomic_private_write(path, original)


class _MaskedBatch:
    """先全部處理、檢查，最後發布；原檔永不作為成功輸出。"""

    def __init__(self, mapping):
        self.original_mapping = mapping
        self.working_mapping = dict(mapping)
        self.items = []  # (source, candidate, destination, source_hash, candidate_hash)
        self.tempdirs = []
        self.media_records = {}
        self.records = []
        self.taggers = []
        self.created = []

    def stage_dir(self, parent):
        temp = _private_temp(parent)
        self.tempdirs.append(temp)
        return temp

    def add(self, source, candidate, destination, source_digest=None):
        source = _safe_source(source)
        destination = os.path.abspath(destination)
        if os.path.lexists(destination):
            raise SafeOutputError("輸出檔已存在，拒絕覆蓋原有檔案")
        if os.path.normcase(source) == os.path.normcase(destination):
            raise SafeOutputError("來源檔與輸出檔不可相同")
        if any(os.path.normcase(item[2]) == os.path.normcase(destination) for item in self.items):
            raise SafeOutputError("批次輸出路徑重複，拒絕發布")
        self.items.append((source, candidate, destination, source_digest or _file_digest(source),
                           _file_digest(candidate)))

    def stage_media(self, source, candidate, destination):
        source = _safe_source(source)
        source_digest = _file_digest(source)
        os.makedirs(os.path.dirname(candidate), exist_ok=True)
        media_private = self.stage_dir(os.path.dirname(os.path.abspath(source)))
        private_map = os.path.join(media_private, "isolated_media_map.json")
        func = handler_media.mask_pdf if source.lower().endswith(".pdf") else handler_media.mask_image
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                entries = list(func(source, candidate, private_map) or [])
        except Exception as exc:
            raise SafeOutputError("圖片／PDF 遮蔽執行失敗") from exc
        if not entries:
            # 即使 handler_media 產生未遮蔽的複本，也絕對不會進入發布清單。
            if os.path.lexists(candidate):
                os.unlink(candidate)
            return []
        _assert_safe_candidate(source, candidate, media=True)
        _verify_mask_coverage(candidate, entries, is_pdf=source.lower().endswith(".pdf"))
        try:
            with open(private_map, encoding="utf-8") as f:
                saved = json.load(f)
            key = os.path.basename(candidate)
            if not isinstance(saved, dict) or saved.get(key) != entries:
                raise SafeOutputError("隔離還原對照表缺失或內容不一致")
        except (OSError, ValueError, TypeError) as exc:
            raise SafeOutputError("隔離還原對照表驗證失敗") from exc
        if key in self.media_records:
            raise SafeOutputError("批次檔名重複，還原對照表無法區分")
        self.media_records[key] = entries
        self.records.extend((e.get("value"), e.get("label")) for e in entries)
        self.add(source, candidate, destination, source_digest)
        return entries

    def stage_text(self, source, candidate, destination, *, is_code=False, source_name=""):
        source = _safe_source(source)
        source_digest = _file_digest(source)
        content, enc = _read_text_file(source)
        if content is None:
            raise SafeOutputError("文字編碼無法辨識，拒絕略過")
        # 遮蔽前後 PII 值不寫入 UI log；正式敏感清單等成功後才更新。
        mapping_before = dict(self.working_mapping)
        with contextlib.redirect_stdout(io.StringIO()):
            masked, matches = _anonymize_full(content, self.working_mapping, is_code,
                                              False, False, source_name)
        if not matches:
            self.working_mapping.clear()
            self.working_mapping.update(mapping_before)
            return []
        if not isinstance(masked, str) or masked == content:
            raise SafeOutputError("文字處理沒有產生有效遮蔽結果")
        os.makedirs(os.path.dirname(candidate), exist_ok=True)
        with open(candidate, "wb") as f:
            f.write(masked.encode(enc))
        _assert_safe_candidate(source, candidate)
        self.records.extend((content[s:e], label) for s, e, label, *_ in matches)
        self.add(source, candidate, destination, source_digest)
        return matches

    def stage_table(self, source, candidate, destination, *, source_name=""):
        source = _safe_source(source)
        source_digest = _file_digest(source)
        private = self.stage_dir(os.path.dirname(os.path.abspath(source)))
        private_input = os.path.join(private, os.path.basename(source))
        shutil.copy2(source, private_input)  # 僅存在私有暫存目錄，不進入發布資料夾。
        mapping_before = dict(self.working_mapping)
        tagger = make_tagger(self.working_mapping, source_name)
        try:
            handler_csv.LAST_REPORT = []
            with contextlib.redirect_stdout(io.StringIO()):
                result = handler_csv.mask_file(private_input, detect_cell, tagger)
        except Exception as exc:
            raise SafeOutputError("表格遮蔽執行失敗") from exc
        if not result:
            self.working_mapping.clear()
            self.working_mapping.update(mapping_before)
            return []
        report = list(getattr(handler_csv, "LAST_REPORT", []))
        if not report:
            raise SafeOutputError("表格引擎輸出沒有有效遮蔽報告，拒絕發布")
        result = os.path.abspath(result)
        if (os.path.dirname(result) != os.path.abspath(private)
                or os.path.islink(result) or not os.path.isfile(result)):
            raise SafeOutputError("表格引擎輸出了不受信任的路徑")
        os.makedirs(os.path.dirname(candidate), exist_ok=True)
        os.replace(result, candidate)
        _assert_safe_candidate(source, candidate)
        self.add(source, candidate, destination, source_digest)
        self.taggers.append(tagger)
        return report

    def verify_sources(self):
        for source, candidate, destination, digest, candidate_digest in self.items:
            if not os.path.isfile(source) or os.path.islink(source) or _file_digest(source) != digest:
                raise SafeOutputError("處理期間來源檔案已變更，拒絕發布")
            published_by_us = any(
                destination == created or (
                    os.path.isdir(created) and
                    os.path.commonpath((created, destination)) == created
                ) for created in self.created
            )
            verified = (candidate if os.path.isfile(candidate) else
                        destination if published_by_us else None)
            if (not verified or os.path.islink(verified) or not os.path.isfile(verified)
                    or _file_digest(verified) != candidate_digest):
                raise SafeOutputError("暫存或已發布輸出被變更，拒絕發布")

    def persist_maps(self):
        tpath, mpath = os.path.abspath(MAP_FILE), os.path.abspath(handler_media.DEFAULT_MAP_FILE)
        old_text, old_media = _snapshot_map(tpath), _snapshot_map(mpath)
        changed = []
        try:
            if self.working_mapping != self.original_mapping:
                content = "".join(f"{k}\t{v}\n" for k, v in self.working_mapping.items())
                _atomic_private_write(tpath, content.encode("utf-8"))
                changed.append((tpath, old_text))
            if self.media_records:
                current = json.loads(old_media.decode("utf-8")) if old_media else {}
                if not isinstance(current, dict):
                    raise SafeOutputError("還原對照表格式錯誤")
                current.update(self.media_records)
                _atomic_private_write(mpath, json.dumps(current, ensure_ascii=False, indent=2).encode("utf-8"))
                changed.append((mpath, old_media))
        except Exception as exc:
            for path, old in reversed(changed):
                _restore_map(path, old)
            raise SafeOutputError("無法安全更新對照表，已撤銷輸出") from exc
        return changed

    def finish(self, publish):
        """publish() 發布輸出；失敗時撤銷該批次建立的成果及私有對照表。"""
        self.verify_sources()
        changed_maps = []
        try:
            publish()
            self.verify_sources()
            changed_maps = self.persist_maps()
            self.original_mapping.clear()
            self.original_mapping.update(self.working_mapping)
        except Exception:
            # 僅刪除本次確實建立的公開輸出，不碰任何原有檔案。
            for created in reversed(self.created):
                try:
                    if os.path.isdir(created) and not os.path.islink(created):
                        shutil.rmtree(created)
                    elif os.path.lexists(created):
                        os.unlink(created)
                except OSError:
                    pass
            for path, old in reversed(changed_maps):
                _restore_map(path, old)
            raise
        # 敏感清單是輔助記錄；與對外輸出解耦，失敗也不得記錄原文或曝露。
        try:
            if self.records:
                with contextlib.redirect_stdout(io.StringIO()):
                    record_sensitive(self.records, "安全批次")
            for tagger in self.taggers:
                with contextlib.redirect_stdout(io.StringIO()):
                    tagger.flush()
        except Exception:
            print("[警告] 敏感清單更新失敗；遮蔽檔案已完成，請另行檢查名單")

    def cleanup(self):
        for directory in reversed(self.tempdirs):
            if os.path.isdir(directory):
                shutil.rmtree(directory)


def _mask_media(path, out=None):
    """單檔安全入口。失敗或零命中不輸出，絕不複製原始內容。"""
    source = _safe_source(path)
    ext = os.path.splitext(source)[1].lower()
    destination = (os.path.abspath(out) if out is not None else
                   os.path.splitext(source)[0] + "_masked" + ext)
    batch = _MaskedBatch({})
    stage = batch.stage_dir(os.path.dirname(destination))
    candidate = os.path.join(stage, os.path.basename(destination))
    try:
        entries = batch.stage_media(source, candidate, destination)
        if entries:
            def publish():
                if os.path.lexists(destination):
                    raise SafeOutputError("輸出已存在")
                os.link(candidate, destination)  # 原子 no-clobber；不支援則拒絕輸出
                batch.created.append(destination)
            batch.finish(publish)
            return destination, entries
        return None, []
    finally:
        batch.cleanup()


def dispatch_files(paths, mapping) -> list:
    """完整批次成功才公布輸出；單檔處理例外直接終止並撤銷本次成果。"""
    batch = _MaskedBatch(mapping)
    produced = []
    try:
        for path in paths:
            source = _safe_source(path)
            ext = os.path.splitext(source)[1].lower()
            if ext not in (MEDIA_EXTENSIONS | TABLE_EXTENSIONS | CODE_EXTENSIONS | TEXT_FILE_EXTENSIONS):
                print("[略過] 不支援的副檔名（未複製任何資料）")
                continue
            destination = os.path.splitext(source)[0] + "_masked" + ext
            stage = batch.stage_dir(os.path.dirname(source))
            candidate = os.path.join(stage, os.path.basename(destination))
            if ext in MEDIA_EXTENSIONS:
                hits = batch.stage_media(source, candidate, destination)
            elif ext in TABLE_EXTENSIONS:
                hits = batch.stage_table(source, candidate, destination, source_name=os.path.basename(source))
            else:
                hits = batch.stage_text(source, candidate, destination, is_code=(ext in CODE_EXTENSIONS),
                                        source_name=os.path.basename(source))
            if hits:
                produced.append(destination)
        if not produced:
            return []
        def publish():
            for _, candidate, destination, _, _ in batch.items:
                if os.path.lexists(destination):
                    raise SafeOutputError("輸出已存在，不會覆蓋使用者原有檔案")
                os.link(candidate, destination)
                batch.created.append(destination)
        batch.finish(publish)
        print(f"[完成] 批次成功，產生 {len(produced)} 份遮蔽結果")
        return produced
    except Exception as exc:
        print("[失敗] 批次已中止，不會回報部分成功的結果")
        raise SafeOutputError("檔案處理失敗，已拒絕發布本批次成果") from exc
    finally:
        batch.cleanup()

# ---------- 資料夾掃描 ----------
_OUT_DIR_RE = re.compile(re.escape(SCAN_OUTPUT_SUFFIX) + r"(_\d+)?$")


def _risk_of(labels):
    if not labels:
        return "無"
    if any(k in str(l).upper() for l in labels for k in HIGH_RISK_KEYWORDS):
        return "高"
    return "中"


def make_output_dir(folder):
    """同層、名稱不同的輸出資料夾路徑：<原名>_masked，已存在就 _masked_2、_masked_3…"""
    folder = os.path.abspath(folder)
    parent, name = os.path.split(folder)
    if not name:
        raise ValueError("不能掃描磁碟根目錄 (找不到同層可以建立輸出資料夾的位置)")
    if _OUT_DIR_RE.search(name):
        raise ValueError(f"「{name}」看起來已經是遮蔽後的輸出資料夾，請選擇原始資料夾")
    cand, n = os.path.join(parent, name + SCAN_OUTPUT_SUFFIX), 2
    while os.path.exists(cand):
        cand = os.path.join(parent, f"{name}{SCAN_OUTPUT_SUFFIX}_{n}")
        n += 1
    return cand


def list_scan_files(folder, recursive=True):
    """列出資料夾內可處理的檔案 (略過 SCAN_SKIP_DIRS、舊的 *_masked 檔與先前產生的輸出資料夾)"""
    supported = CODE_EXTENSIONS | MEDIA_EXTENSIONS | TEXT_FILE_EXTENSIONS | TABLE_EXTENSIONS
    paths = []
    if recursive:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS and not _OUT_DIR_RE.search(d)]
            paths += [os.path.join(root, n) for n in names]
    else:
        paths = [os.path.join(folder, n) for n in os.listdir(folder)
                 if os.path.isfile(os.path.join(folder, n))]
    out = []
    for p in sorted(paths):
        stem, ext = os.path.splitext(os.path.basename(p))
        if not stem.endswith("_masked") and ext.lower() in supported:
            out.append(p)
    return out


def _read_text_file(path):
    """讀文字檔並記住編碼，之後寫回同一種編碼 (換行符號也原樣保留)。無法判斷編碼回傳 (None, None)"""
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    for enc in ("utf-8", "cp950", "big5"):     # 同 notebook 的編碼讀取順序 (BOM 已在上面處理)
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return None, None


def _scan_one(src, dst, mapping):
    """相容單檔介面：dst 只在確實產生遮蔽時建立；不會複製未遮蔽原稿。"""
    source = _safe_source(src)
    ext = os.path.splitext(source)[1].lower()
    name = os.path.basename(source)
    res = {"path": source, "output": None, "kind": "", "hits": 0,
           "labels": {}, "error": None, "risk": "無"}
    batch = _MaskedBatch(mapping)
    temp = batch.stage_dir(os.path.dirname(os.path.abspath(dst)))
    candidate = os.path.join(temp, name)
    try:
        if ext in MEDIA_EXTENSIONS:
            res["kind"] = "圖片/PDF"
            hits = batch.stage_media(source, candidate, dst)
            counts = Counter(e.get("label") or "UNKNOWN" for e in hits)
        elif ext in TABLE_EXTENSIONS:
            res["kind"] = "表格"
            hits = batch.stage_table(source, candidate, dst, source_name=name)
            counts = Counter((r[1] if len(r) > 1 else "UNKNOWN") for r in hits)
        else:
            res["kind"] = "程式碼" if ext in CODE_EXTENSIONS else "文字"
            hits = batch.stage_text(source, candidate, dst, is_code=(ext in CODE_EXTENSIONS), source_name=name)
            counts = Counter(m[2] for m in hits)
        if batch.items:
            def publish():
                if os.path.lexists(dst):
                    raise SafeOutputError("目標檔已存在")
                os.link(candidate, dst)
                batch.created.append(dst)
            batch.finish(publish)
            res.update(output=dst, hits=len(hits), labels=dict(counts), risk=_risk_of(counts))
        return res
    finally:
        batch.cleanup()


def scan_folder(folder, mapping, recursive=True, progress=None):
    """先在不公開的暫存目錄生成 *僅遮蔽* 成果，全部成功才發布。

    未命中的檔案、未知格式、跳過的資料夾不會複製到輸出資料夾。
    這是安全設計上的刻意行為變更：無命中 ≠ 已證明原檔安全。
    """
    folder = os.path.abspath(folder)
    if not os.path.isdir(folder) or os.path.islink(folder):
        raise SafeOutputError("來源資料夾不存在或不可使用")
    dest = make_output_dir(folder)
    files = list_scan_files(folder, recursive)
    batch = _MaskedBatch(mapping)
    staged_dir = batch.stage_dir(os.path.dirname(dest))
    results = []
    print(f"[掃描] 待檢查的受支援檔案：{len(files)} 個；只會發布已遮蔽結果")
    try:
        for i, source in enumerate(files, 1):
            source = _safe_source(source)
            if progress:
                progress(i, len(files), source)
            rel = os.path.relpath(source, folder)
            if rel.startswith(".." + os.sep) or rel == ".." or os.path.isabs(rel):
                raise SafeOutputError("不允許跨出來源資料夾的路徑")
            candidate = os.path.join(staged_dir, rel)
            destination = os.path.join(dest, rel)
            ext = os.path.splitext(source)[1].lower()
            name = os.path.basename(source)
            res = {"path": source, "output": None, "kind": "", "hits": 0,
                   "labels": {}, "error": None, "risk": "無"}
            if ext in MEDIA_EXTENSIONS:
                res["kind"] = "圖片/PDF"
                entries = batch.stage_media(source, candidate, destination)
                counts = Counter(e.get("label") or "UNKNOWN" for e in entries)
                hits = len(entries)
            elif ext in TABLE_EXTENSIONS:
                res["kind"] = "表格"
                report = batch.stage_table(source, candidate, destination, source_name=name)
                counts = Counter(r[1] for r in report if len(r) > 1)
                hits = len(report)
            else:
                res["kind"] = "程式碼" if ext in CODE_EXTENSIONS else "文字"
                matches = batch.stage_text(source, candidate, destination,
                                           is_code=(ext in CODE_EXTENSIONS), source_name=name)
                counts = Counter(m[2] for m in matches)
                hits = len(matches)
            if os.path.isfile(candidate) and any(it[1] == candidate for it in batch.items):
                res.update(output=destination, hits=hits, labels=dict(counts), risk=_risk_of(counts))
            results.append(res)
        # 即使未偵測到 PII，也發布不含原始資料的空結果目錄，維持 GUI 回傳格式。
        def publish():
            # 同一檔案系統內的整目錄 rename：不會暴露一半完成的輸出。
            if os.path.lexists(dest):
                raise SafeOutputError("輸出資料夾已存在，拒絕覆蓋")
            os.rename(staged_dir, dest)
            batch.created.append(dest)
            batch.tempdirs.remove(staged_dir)
        batch.finish(publish)
        print(f"[完成] 掃描與發布完成，已遮蔽檔案數：{sum(bool(r['output']) for r in results)}")
        order = {"高": 0, "中": 1, "無": 2}
        results.sort(key=lambda r: (order.get(r["risk"], 9), r["path"]))
        return dest, results
    except Exception as exc:
        print("[失敗] 資料夾掃描中止；未發布不完整的資料夾")
        raise SafeOutputError("資料夾掃描失敗，已拒絕發布本批次成果") from exc
    finally:
        batch.cleanup()

def format_scan_report(results, dest=None):
    """把掃描結果整理成文字，給 log / 對話框用"""
    danger = [r for r in results if r["output"]]
    errors = [r for r in results if r["error"]]
    lines = [f"共掃描 {len(results)} 個檔案：危險 {len(danger)} 個、安全 {len(results) - len(danger) - len(errors)} 個"
             + (f"、處理失敗 {len(errors)} 個" if errors else "")]
    if dest:
        lines.append(f"遮蔽後的資料夾：{dest}")
    for r in danger:
        lines.append(f"  [{r['risk']}] {r['path']}  ({r['kind']}，{r['hits']} 筆：{r['labels']})")
    for r in errors:
        lines.append(f"  [錯誤] {r['path']}：{r['error']}")
    return "\n".join(lines)


# ---------- 視窗 / 剪貼簿 ----------
def get_active_window_info():
    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return "未知視窗", "未知程式"
    title = win32gui.GetWindowText(hwnd)
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        proc_name = psutil.Process(pid).name()
    except Exception:
        proc_name = "未知程式"
    return title, proc_name


def _paste_with_retry(retries: int = 5, delay: float = 0.05) -> str:
    last_err = None
    for _ in range(retries):
        try:
            return pyperclip.paste()
        except pyperclip.PyperclipWindowsException as e:
            last_err = e
            time.sleep(delay)
    print(f"[警告] 讀取剪貼簿失敗，已重試 {retries} 次仍失敗：{last_err}")
    return ""


def get_clipboard_content():
    """回傳 ("FILES", [路徑...]) 或 ("TEXT", 文字)"""
    copied_files = []
    for _ in range(5):  # 剪貼簿可能被短暫佔用，重試
        try:
            win32clipboard.OpenClipboard()
        except Exception:
            time.sleep(0.05)
            continue
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
                data = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
                if data:
                    copied_files = list(data)
        except Exception:
            pass
        finally:
            try:
                win32clipboard.CloseClipboard()
            except Exception:
                pass
        break
    if copied_files:
        return "FILES", copied_files
    return "TEXT", _paste_with_retry()


def copy_files_to_clipboard(paths):
    """效果等同在檔案總管複製這些檔案"""
    file_list = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    dropfiles_header = struct.pack("Iiiii", 20, 0, 0, 0, 1)
    data = dropfiles_header + file_list.encode("utf-16le")
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, data)
    finally:
        win32clipboard.CloseClipboard()


def copy_text_to_clipboard(text):
    pyperclip.copy(text)


def clipboard_seq():
    return win32clipboard.GetClipboardSequenceNumber()


def wait_clipboard_change(old_seq, timeout=1.0):
    """等剪貼簿內容真的被更新 (序號改變)，比固定 sleep 可靠"""
    end = time.time() + timeout
    while time.time() < end:
        if win32clipboard.GetClipboardSequenceNumber() != old_seq:
            time.sleep(0.05)  # 讓寫入的程式把剪貼簿釋放
            return True
        time.sleep(0.02)
    return False


# ---------- 預熱 ----------
def warmup():
    """啟動時先跑一次，讓 Presidio / NER 模型提前載入，第一次複製才不會卡幾秒 (不會記錄進敏感清單)"""
    sample = "王小明的 Email 是 test@example.com，電話 0912-345-678，API_KEY=sk-1234567890abcdef"
    print("[預熱] 載入模型中…")
    t0 = time.time()
    errors = []
    with contextlib.redirect_stdout(io.StringIO()):  # 預熱過程不印偵測細節
        for is_code in (False, True):
            try:
                anonymize_text(sample, {}, is_code=is_code, save=False, record=False)
            except Exception as e:
                errors.append(f"[警告] 預熱失敗 (is_code={is_code})：{e}")
    for msg in errors:
        print(msg)
    print(f"[預熱] 完成，耗時 {time.time() - t0:.1f} 秒")
    for line in getattr(handler_text, "MODEL_STATUS", []):   # 哪些模型啟用/停用 (停用 = 該偵測功能沒有作用)
        print(line)


# ---------- 圖片 / PDF：OCR 文字送模型前也做前處理 ----------
def _install_media_preprocess():
    """把 handler_media.detect_matches 包一層 (它在 handler_media 內部是用模組層級名稱呼叫的，所以 mask_image / mask_pdf 也會走到)：
        OCR 文字 -> 前處理 -> 原本的偵測 -> 座標換算回 OCR 文字的位置 (之後對應回像素框的邏輯完全不用改)
    另外補上敏感清單 / known_entities.txt / 安全清單 (圖片/PDF 原本不會套用)；
    偵測結果不是 5 元素 tuple (start, end, label, score, source) 時只做前處理，不補清單。
    預處理結果認不得時退回用原文偵測，不會給出錯誤座標。"""
    orig = getattr(handler_media, "detect_matches", None)
    if orig is None:
        print("[前處理] 找不到 handler_media.detect_matches，圖片/PDF 未套用前處理")
        return
    if getattr(orig, "_preprocessed", False):
        return

    def detect_matches(text, *args, **kwargs):
        if not isinstance(text, str):
            return orig(text, *args, **kwargs)
        res = None
        if preprocess.APPLY_TO_MEDIA:
            norm = preprocess.preprocess(text, html=False)      # OCR 文字不做網頁標籤刪除
            if not norm.identity:
                res = norm.map_matches(orig(norm.text, *args, **kwargs))
        if res is None:
            res = list(orig(text, *args, **kwargs))
        if all(isinstance(m, tuple) and len(m) == 5 for m in res):
            extra = []
            fn = getattr(handler_text, "detect_known_entities", None)
            if fn:
                extra += fn(text)
            extra += detect_known(text)
            res = list(dict.fromkeys(res + [tuple(m) for m in extra]))
            safe = entity_store.safe_values()
            if safe:
                res = [m for m in res if text[m[0]:m[1]].strip() not in safe]
        return res

    detect_matches._preprocessed = True
    detect_matches.__wrapped__ = orig
    handler_media.detect_matches = detect_matches


_install_media_preprocess()