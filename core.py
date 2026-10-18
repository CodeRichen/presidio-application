"""
core.py - main.py / ui.py / hotkey_service.py 共用的核心
====================================================
放：設定、對照表、敏感清單、標籤遮蔽/還原、重疊處理、檔案分派 (code/text/media/field/csv)、
    資料夾掃描、剪貼簿讀寫工具、模型預熱。
log 一律用 print；ui.py 會把 sys.stdout 接到畫面上的記錄區。

敏感清單 (sensitive_items.json)：
    每次遮蔽時偵測到的敏感值 (值、類型、次數、來源檔案) 都會記錄到 ./presidio/real/，
    程式結束時【不會】刪除 (對照表 clipboard_map.txt 才會在結束時刪除)。
    之後只要文字 / 程式碼 / 表格儲存格出現清單裡的值，會當成「正則規則」(source="regex",
    優先序最高) 自動遮蔽。USE_KNOWN_ITEMS = False 可關閉自動遮蔽。
"""
import os
import io
import re
import sys
import json
import time
import struct
import threading
import contextlib
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
SENSITIVE_FILE = "./presidio/real/sensitive_items.json"   # 敏感清單：不會被自動刪除

USE_KNOWN_ITEMS = True    # 偵測時是否讀取敏感清單並自動遮蔽
KNOWN_MIN_LEN = 3         # 太短的值 (例如 2 個字) 不記錄、不自動遮蔽，避免誤傷一般文字

# 資料夾掃描
SCAN_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
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


# ---------- 敏感清單 ----------
_items_lock = threading.RLock()
_items = {"mtime": None, "data": {}, "regex": None}   # data: 值 -> 記錄


def _clean_label(label):
    label = re.sub(r"[^A-Z_]", "_", str(label or "SENSITIVE").upper()).strip("_")
    return label or "SENSITIVE"


def _file_mtime():
    try:
        return os.path.getmtime(SENSITIVE_FILE)
    except OSError:
        return None


def _sync_items():
    """檔案有變動 (例如另一個程式寫入) 就重新載入。呼叫端需持有 _items_lock。"""
    mt = _file_mtime()
    if mt == _items["mtime"]:
        return
    data = {}
    if mt is not None:
        try:
            with open(SENSITIVE_FILE, encoding="utf-8") as f:
                for rec in json.load(f):
                    if rec.get("value"):
                        data[rec["value"]] = rec
        except Exception as e:
            print(f"[警告] 讀取敏感清單失敗：{e}")
    _items.update(mtime=mt, data=data, regex=None)


def _save_items():
    """呼叫端需持有 _items_lock。先寫暫存檔再取代，避免寫到一半被讀到。"""
    folder = os.path.dirname(SENSITIVE_FILE)
    if folder:
        os.makedirs(folder, exist_ok=True)
    tmp = SENSITIVE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(list(_items["data"].values()), f, ensure_ascii=False, indent=2)
    os.replace(tmp, SENSITIVE_FILE)
    _items["mtime"] = _file_mtime()
    _items["regex"] = None


def record_sensitive(entries, source=""):
    """entries: [(值, 類型), ...]。新值會加入清單，已存在的累計次數。回傳新增筆數。"""
    entries = list(entries)
    if not entries:
        return 0
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    added = 0
    with _items_lock:
        _sync_items()
        data = _items["data"]
        touched = False
        for value, label in entries:
            value = (value or "").strip()
            if len(value) < KNOWN_MIN_LEN:
                continue
            touched = True
            rec = data.get(value)
            if rec is None:
                data[value] = {"value": value, "label": _clean_label(label), "count": 1,
                               "first_seen": now, "last_seen": now,
                               "sources": [source] if source else []}
                added += 1
            else:
                rec["count"] = rec.get("count", 0) + 1
                rec["last_seen"] = now
                if source and source not in rec.setdefault("sources", []):
                    rec["sources"] = (rec["sources"] + [source])[-5:]
        if touched:
            try:
                _save_items()
            except OSError as e:
                print(f"[警告] 儲存敏感清單失敗：{e}")
    if added:
        print(f"[敏感清單] 新增 {added} 筆 -> {SENSITIVE_FILE}")
    return added


def load_sensitive_items():
    """回傳清單記錄 (最近出現的在前)，給 UI 顯示"""
    with _items_lock:
        _sync_items()
        return sorted((dict(r) for r in _items["data"].values()),
                      key=lambda r: r.get("last_seen", ""), reverse=True)


def sensitive_count():
    with _items_lock:
        _sync_items()
        return len(_items["data"])


def remove_sensitive_items(values):
    """使用者手動從清單移除 (例如誤判的詞)。回傳移除筆數。"""
    with _items_lock:
        _sync_items()
        n = 0
        for v in values:
            if _items["data"].pop(v, None) is not None:
                n += 1
        if n:
            _save_items()
        return n


def _known_regex():
    """把清單裡所有值編成一個正則 (長的優先)。呼叫端需持有 _items_lock。"""
    if _items["regex"] is None:
        parts = []
        for v in sorted(_items["data"], key=len, reverse=True):
            p = re.escape(v)
            if re.match(r"[A-Za-z0-9]", v[0]):
                p = r"(?<![A-Za-z0-9])" + p      # 英數開頭：前面不能緊接英數，避免命中字的一部分
            if re.match(r"[A-Za-z0-9]", v[-1]):
                p += r"(?![A-Za-z0-9])"
            parts.append(p)
        _items["regex"] = re.compile("|".join(parts)) if parts else False
    return _items["regex"] or None


def detect_known(text):
    """把敏感清單當成一條正則規則：回傳 [(start, end, label, None, "regex")]"""
    if not USE_KNOWN_ITEMS:
        return []
    with _items_lock:
        _sync_items()
        rx = _known_regex()
        if not rx:
            return []
        data = _items["data"]
        return [(m.start(), m.end(), data[m.group(0)]["label"] if m.group(0) in data else "SENSITIVE",
                 None, "regex")
                for m in rx.finditer(text)]


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
def _anonymize_full(text, mapping, is_code, save, record, source):
    """回傳 (遮蔽後文字或 None, 保留下來的偵測結果)"""
    if is_code is None:
        is_code = is_code_text(text)
    matches = list(handler_code.detect(text) if is_code else handler_text.detect(text))
    if handler_field is not None:
        matches += list(handler_field.detect(text))
    if is_code:   # 程式碼路徑不一定經過 handler_text.detect()，這裡補上手動維護的已知實體清單
        fn = getattr(handler_text, "detect_known_entities", None)
        if fn:
            matches += fn(text)
    known = detect_known(text)
    matches += known
    if not matches:
        return None, []

    prefer_zh = handler_text.contains_chinese(text)
    known_info = f"，其中敏感清單命中 {len(known)} 筆" if known else ""
    print(f"[偵測] 共 {len(matches)} 筆候選 (含正則規則與模型{known_info})：")
    matches = merge_overlaps(matches, prefer_zh)

    reverse = {v: k for k, v in mapping.items()}
    counters = {}
    for tag in mapping:
        m = TAG_RE.match(tag)
        if m:
            t, i = m.groups()
            counters[t] = max(counters.get(t, 0), int(i))

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


# ---------- 圖片 / PDF ----------
def _mask_media(path):
    """呼叫 handler_media 遮蔽圖片/PDF，並把偵測到的值記錄進敏感清單。
    回傳 (輸出檔路徑或 None, entries)。沒偵測到任何內容時，不留下輸出檔。"""
    ext = os.path.splitext(path)[1].lower()
    out = f"{os.path.splitext(path)[0]}_masked{ext}"
    existed = os.path.exists(out)
    func = handler_media.mask_pdf if ext == ".pdf" else handler_media.mask_image
    entries = list(func(path, out, handler_media.DEFAULT_MAP_FILE) or [])
    if not entries:
        if not existed and os.path.exists(out):
            try:
                os.remove(out)
            except OSError:
                pass
        return None, []
    record_sensitive([(e.get("value"), e.get("label")) for e in entries], os.path.basename(path))
    return out, entries


# ---------- 檔案分派 ----------
def dispatch_files(paths, mapping) -> list:
    """回傳實際產生的去識別化檔案路徑。不碰剪貼簿。"""
    produced = []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)

        if ext in MEDIA_EXTENSIONS:
            print(f"[分類] {path} -> 圖片/PDF，交給 handler_media 處理")
            try:
                out_path, entries = _mask_media(path)
            except Exception as e:
                print(f"[錯誤] 圖片/PDF 處理失敗：{e}")
                continue
            if out_path:
                produced.append(out_path)
                print(f"[完成] 已產生去識別化檔案：{out_path} (共 {len(entries)} 筆)")
            else:
                print("[結果] 未偵測到需要遮蔽的內容")

        elif ext in TABLE_EXTENSIONS:
            print(f"[分類] {path} -> 表格檔案，交給 handler_csv 處理 (欄位內有個資則整欄遮蔽)")
            tagger = make_tagger(mapping, name)
            try:
                out_path = handler_csv.mask_file(path, detect_cell, tagger)
            except Exception as e:
                print(f"[錯誤] 表格處理失敗：{e}")
                continue
            if out_path:
                tagger.flush()
                save_map(mapping)
                produced.append(out_path)
                print(f"[完成] 已產生去識別化檔案：{out_path}")
            else:
                print("[結果] 未偵測到需要遮蔽的內容")

        elif ext in CODE_EXTENSIONS or ext in TEXT_FILE_EXTENSIONS:
            is_code = ext in CODE_EXTENSIONS
            kind = "程式碼檔案" if is_code else "純文字檔案"
            print(f"[分類] {path} -> {kind}(依副檔名判斷)")
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError as e:
                print(f"[錯誤] 讀取失敗：{e}")
                continue
            masked = anonymize_text(content, mapping, is_code=is_code, source=name)
            if masked:
                out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(masked)
                produced.append(out_path)
                print(f"[完成] 已產生去識別化檔案：{out_path}")
            else:
                print("[結果] 未偵測到需要遮蔽的內容")

        else:
            print(f"[跳過] {path}：副檔名不在支援清單內")

    return produced


# ---------- 資料夾掃描 ----------
def _risk_of(labels):
    if not labels:
        return "無"
    if any(k in str(l).upper() for l in labels for k in HIGH_RISK_KEYWORDS):
        return "高"
    return "中"


def list_scan_files(folder, recursive=True):
    """列出資料夾內可處理的檔案 (略過已產生的 *_masked 檔與 SCAN_SKIP_DIRS)"""
    supported = CODE_EXTENSIONS | MEDIA_EXTENSIONS | TEXT_FILE_EXTENSIONS | TABLE_EXTENSIONS
    paths = []
    if recursive:
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SCAN_SKIP_DIRS]
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


def _scan_one(path, mapping):
    ext = os.path.splitext(path)[1].lower()
    name = os.path.basename(path)
    res = {"path": path, "kind": "", "hits": 0, "labels": {}, "output": None, "error": None}

    if ext in MEDIA_EXTENSIONS:
        res["kind"] = "圖片/PDF"
        out, entries = _mask_media(path)
        res.update(output=out, hits=len(entries),
                   labels=dict(Counter(e.get("label") or "UNKNOWN" for e in entries)))

    elif ext in TABLE_EXTENSIONS:
        res["kind"] = "表格"
        tagger = make_tagger(mapping, name)
        with contextlib.redirect_stdout(io.StringIO()):
            out = handler_csv.mask_file(path, detect_cell, tagger)
        if out:
            tagger.flush()
            report = list(getattr(handler_csv, "LAST_REPORT", []))   # [(欄名, 類型, 原因)]
            res.update(output=out, hits=len(report), labels=dict(Counter(r[1] for r in report)))

    else:
        is_code = ext in CODE_EXTENSIONS
        res["kind"] = "程式碼" if is_code else "文字"
        with open(path, encoding="utf-8", errors="ignore") as f:
            content = f.read()
        with contextlib.redirect_stdout(io.StringIO()):
            masked, matches = _anonymize_full(content, mapping, is_code, False, True, name)
        if masked:
            out = f"{os.path.splitext(path)[0]}_masked{ext}"
            with open(out, "w", encoding="utf-8") as f:
                f.write(masked)
            res.update(output=out, hits=len(matches), labels=dict(Counter(m[2] for m in matches)))

    res["risk"] = _risk_of(res["labels"]) if res["output"] else "無"
    return res


def scan_folder(folder, mapping, recursive=True, progress=None):
    """掃描資料夾內所有支援的檔案：偵測到敏感資訊的會產生 *_masked 去識別化檔案，
    並記錄進敏感清單。回傳每個檔案的結果 (高風險在前)：
        {"path","kind","hits","labels","output","error","risk"}"""
    files = list_scan_files(folder, recursive)
    print(f"[掃描] {folder}：共 {len(files)} 個可處理的檔案 (子資料夾: {'含' if recursive else '不含'})")
    results = []
    try:
        for i, path in enumerate(files, 1):
            if progress:
                progress(i, len(files), path)
            try:
                res = _scan_one(path, mapping)
            except Exception as e:
                res = {"path": path, "kind": "", "hits": 0, "labels": {}, "output": None,
                       "error": str(e), "risk": "錯誤"}
                print(f"[錯誤] {path}：{e}")
            else:
                if res["output"]:
                    print(f"[危險·{res['risk']}] {path}  ({res['kind']}，{res['hits']} 筆：{res['labels']})")
                else:
                    print(f"[安全] {path}")
            results.append(res)
    finally:
        save_map(mapping)   # 整批處理完才寫一次對照表
    order = {"高": 0, "中": 1, "錯誤": 2, "無": 3}
    results.sort(key=lambda r: (order.get(r["risk"], 9), r["path"]))
    return results


def format_scan_report(results):
    """把掃描結果整理成文字，給 log / 對話框 / 匯出用"""
    danger = [r for r in results if r["output"]]
    errors = [r for r in results if r["error"]]
    lines = [f"共掃描 {len(results)} 個檔案：危險 {len(danger)} 個、安全 {len(results) - len(danger) - len(errors)} 個"
             + (f"、處理失敗 {len(errors)} 個" if errors else "")]
    for r in danger:
        lines.append(f"  [{r['risk']}] {r['path']}  ({r['kind']}，{r['hits']} 筆：{r['labels']})")
        lines.append(f"       -> {r['output']}")
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