"""
entity_store.py - 敏感資料名單管理 (main.py / ui.py / core.py 共用)
====================================================
三份名單都存在 ./presidio/real/，程式結束時【不會】刪除：

  sensitive_items.json  敏感清單：遮蔽時自動記錄 + 使用者手動新增。清單裡的值之後會被當成
                        最高優先序的正則規則自動遮蔽 (見 detect_known)。
  blocked_items.json    黑名單：從敏感清單刪除的值會進來，之後【永遠不會】再被自動加入敏感清單。
                        (模型仍可能在文字中偵測到並遮蔽它，只是不會再被記進清單；
                         使用者明確手動新增同一個值時，會自動把它移出黑名單。)
  safe_items.json       安全清單：使用者確認「不是敏感資料」的值。偵測到的結果若剛好等於清單裡的值，
                        core 會直接略過、不遮蔽，也不會被記進敏感清單。

新增敏感資料時可以不指定類型：classify_entity() 會用 handler_text / handler_code 的規則與模型
(Presidio、中文 NER) 判斷這個值是什麼，判斷不出來才用 SENSITIVE。

這支只依賴標準函式庫；handler_* 只在需要判斷類型時才延遲 import，
所以 `python main.py list` 這類指令不用等模型載入。
"""
import os
import re
import json
import time
import threading

REAL_DIR = "./presidio/real"
KNOWN_MIN_LEN = 3     # 自動記錄 / 自動遮蔽：太短的值不處理，避免誤傷一般文字
MANUAL_MIN_LEN = 2    # 手動新增：至少 2 個字 (1 個字會讓每個出現處都被遮蔽)

_lock = threading.RLock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _clean_label(label):
    label = re.sub(r"[^A-Z_]", "_", str(label or "SENSITIVE").upper()).strip("_")
    return label or "SENSITIVE"


class _Store:
    """一份 JSON 名單 (值 -> 記錄)；檔案被別的程式 (例如另一個 main / ui) 改過會自動重新載入"""

    def __init__(self, filename):
        self.filename = filename
        self.mtime = "unset"
        self.data = {}
        self.version = 0

    @property
    def path(self):
        return os.path.join(REAL_DIR, self.filename)

    def file_mtime(self):
        try:
            return os.path.getmtime(self.path)
        except OSError:
            return None

    def sync(self):                       # 呼叫端需持有 _lock
        mt = self.file_mtime()
        if mt == self.mtime:
            return
        data = {}
        if mt is not None:
            try:
                with open(self.path, encoding="utf-8") as f:
                    for rec in json.load(f):
                        if isinstance(rec, dict) and rec.get("value"):
                            data[rec["value"]] = rec
            except Exception as e:
                print(f"[警告] 讀取 {self.path} 失敗：{e}")
                try:                      # 壞掉的檔案先改名保留，避免下次存檔把它蓋掉
                    os.replace(self.path, self.path + ".bad")
                except OSError:
                    pass
                mt = None
        self.data, self.mtime = data, mt
        self.version += 1

    def save(self):                       # 呼叫端需持有 _lock；先寫暫存檔再取代
        os.makedirs(REAL_DIR, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(self.data.values()), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
        self.mtime = self.file_mtime()
        self.version += 1


_items = _Store("sensitive_items.json")
_blocked = _Store("blocked_items.json")
_safe = _Store("safe_items.json")
_stores = (_items, _blocked, _safe)

SENSITIVE_FILE = _items.path
BLOCKED_FILE = _blocked.path
SAFE_FILE = _safe.path


def set_real_dir(path):
    """改存放資料夾 (測試用)，並強制重新載入"""
    global REAL_DIR, SENSITIVE_FILE, BLOCKED_FILE, SAFE_FILE
    with _lock:
        REAL_DIR = path
        for s in _stores:
            s.mtime, s.data = "unset", {}
        SENSITIVE_FILE, BLOCKED_FILE, SAFE_FILE = _items.path, _blocked.path, _safe.path


def _sync_all():
    for s in _stores:
        s.sync()


def _save(*stores):
    for s in stores:
        try:
            s.save()
        except OSError as e:
            print(f"[警告] 儲存 {s.path} 失敗：{e}")


def signature():
    """三個檔案的修改時間；ui 用它判斷名單是否被別的程式 (main / 快速鍵) 改過"""
    return tuple(s.file_mtime() for s in _stores)


# ---------------- 讀取 ----------------
def _sorted(store, key):
    with _lock:
        store.sync()
        return sorted((dict(r) for r in store.data.values()), key=lambda r: r.get(key, ""), reverse=True)


def load_items():
    return _sorted(_items, "last_seen")


def load_blocked():
    return _sorted(_blocked, "blocked_at")


def load_safe():
    return _sorted(_safe, "added_at")


def items_count():
    with _lock:
        _items.sync()
        return len(_items.data)


_safe_cache = {"version": -1, "set": frozenset()}


def safe_values():
    """安全清單的值 (frozenset)；core 每次偵測前取一次，用來略過已標為安全的結果"""
    with _lock:
        _safe.sync()
        if _safe_cache["version"] != _safe.version:
            _safe_cache.update(version=_safe.version, set=frozenset(_safe.data))
        return _safe_cache["set"]


def is_blocked(value):
    with _lock:
        _blocked.sync()
        return (value or "").strip() in _blocked.data


# ---------------- 自動記錄 (遮蔽時呼叫) ----------------
def record_sensitive(entries, source=""):
    """entries: [(值, 類型), ...]。新值加入敏感清單，已存在的累計次數。
    黑名單、安全清單裡的值，以及太短的值一律不記錄。回傳新增筆數。"""
    entries = list(entries)
    if not entries:
        return 0
    now = _now()
    added = skipped = 0
    with _lock:
        _sync_all()
        data, touched = _items.data, False
        for value, label in entries:
            v = (value or "").strip()
            if len(v) < KNOWN_MIN_LEN:
                continue
            if v in _blocked.data or v in _safe.data:
                skipped += 1
                continue
            rec = data.get(v)
            if rec is None:
                data[v] = {"value": v, "label": _clean_label(label), "count": 1,
                           "first_seen": now, "last_seen": now,
                           "sources": [source] if source else [],
                           "origin": "auto", "label_source": "偵測"}
                added += 1
            else:
                rec["count"] = rec.get("count", 0) + 1
                rec["last_seen"] = now
                if source and source not in rec.setdefault("sources", []):
                    rec["sources"] = (rec["sources"] + [source])[-5:]
            touched = True
        if touched:
            _save(_items)
    if added:
        print(f"[敏感清單] 新增 {added} 筆 -> {_items.path}")
    if skipped:
        print(f"[敏感清單] 略過 {skipped} 筆 (在黑名單或安全清單內)")
    return added


# ---------------- 類型判斷 (手動新增時不指定類型) ----------------
_CARRIERS_ZH = ["聯絡人是{v}，請盡快回覆。", "資料如下：{v}。"]
_CARRIERS_EN = ["Please contact {v} for details.", "Details: {v}."]


def _pick(matches, start, end, prefer_zh):
    """從偵測結果裡挑出最貼近 [start, end) 這段值的一筆：規則 > 吻合語言的模型 > 其他；再比涵蓋率與分數"""
    span = max(1, end - start)
    best = None
    for s, e, label, score, src in matches:
        cov = max(0, min(e, end) - max(s, start)) / span
        if cov < 0.6 or (e - s) > span * 1.5 + 2:
            continue
        tier = 0 if src == "regex" else (1 if (src == "zh") == prefer_zh else 2)
        key = (tier, -cov, -(score or 0.0))
        if best is None or key < best[0]:
            best = (key, label, score, src)
    return best


def classify_entity(value):
    """用既有的規則與模型判斷一個值是什麼類型。回傳 (類型, 信心分數或 None, 判斷方式)。
    順序：值本身 (文字規則/模型 -> 程式碼規則) -> 放進幾句情境句再判斷 (NER 需要上下文才認得出人名)。
    都判斷不出來就回傳 ("SENSITIVE", None, "預設 (模型無法判斷)")。"""
    v = (value or "").strip()
    try:
        import handler_text
    except Exception as e:
        return "SENSITIVE", None, f"預設 (無法載入模型：{e})"
    try:
        import handler_code
    except Exception:
        handler_code = None

    prefer_zh = handler_text.contains_chinese(v)
    stages = [("", handler_text.detect, v, 0, len(v))]
    if handler_code is not None:
        stages.append(("", handler_code.detect, v, 0, len(v)))
    for tpl in (_CARRIERS_ZH if prefer_zh else _CARRIERS_EN):
        text = tpl.format(v=v)
        start = len(tpl.split("{v}")[0])
        stages.append(("情境句", handler_text.detect, text, start, start + len(v)))

    for tag, detect, text, start, end in stages:
        try:
            best = _pick(detect(text), start, end, prefer_zh)
        except Exception as e:
            print(f"[警告] 類型判斷時偵測失敗：{e}")
            continue
        if best:
            _, label, score, src = best
            how = ("規則判斷" if src == "regex" else "模型判斷") + (f" ({tag})" if tag else "")
            return _clean_label(label), score, how
    return "SENSITIVE", None, "預設 (模型無法判斷)"


# ---------------- 手動新增 ----------------
def parse_entity_lines(text):
    """解析使用者輸入 / 匯入的文字：每行一筆，可寫「值」或「類型<Tab>值」；# 開頭與空行忽略。
    回傳 [(值, 類型或 None), ...]"""
    out = []
    for line in (text or "").splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            label, _, value = line.partition("\t")
            label, value = label.strip().upper(), value.strip()
            if value and re.fullmatch(r"[A-Z_]+", label):
                out.append((value, label))
                continue
        out.append((line.strip(), None))
    return out


def add_manual(items, source="手動新增"):
    """items: [(值, 類型或 None), ...]。類型是 None 時由模型判斷。
    使用者明確新增的值會自動移出黑名單 / 安全清單。回傳每筆的結果：
        {"value","label","how","status","note"}  status: 新增 / 已存在 / 略過"""
    prepared = []
    for value, label in items:
        v = (value or "").strip()
        if len(v) < MANUAL_MIN_LEN:
            prepared.append((v, None, None, ""))
        elif label:
            prepared.append((v, _clean_label(label), None, "使用者指定"))
        else:
            lab, score, how = classify_entity(v)         # 較慢 (跑模型)，在拿鎖之前做完
            prepared.append((v, lab, score, how))

    now = _now()
    results, seen = [], set()
    with _lock:
        _sync_all()
        ch_items = ch_block = ch_safe = False
        for v, label, score, how in prepared:
            if len(v) < MANUAL_MIN_LEN:
                results.append({"value": v, "label": "", "how": "", "status": "略過",
                                "note": f"太短 (至少 {MANUAL_MIN_LEN} 個字)"})
                continue
            if v in seen:
                continue
            seen.add(v)
            notes = []
            if _blocked.data.pop(v, None) is not None:
                ch_block = True
                notes.append("已從黑名單移出")
            if _safe.data.pop(v, None) is not None:
                ch_safe = True
                notes.append("已從安全清單移出")
            how_txt = how + (f" {score:.2f}" if score else "")
            rec = _items.data.get(v)
            if rec is None:
                _items.data[v] = {"value": v, "label": label, "count": 1, "first_seen": now,
                                  "last_seen": now, "sources": [source], "origin": "manual",
                                  "label_source": how_txt}
                status = "新增"
            else:
                rec["origin"], rec["last_seen"] = "manual", now
                if how == "使用者指定" or rec.get("label") in (None, "", "SENSITIVE"):
                    rec["label"], rec["label_source"] = label, how_txt
                label, status = rec["label"], "已存在"
            ch_items = True
            results.append({"value": v, "label": label, "how": how_txt, "status": status,
                            "note": "；".join(notes)})
        if ch_items:
            _save(_items)
        if ch_block:
            _save(_blocked)
        if ch_safe:
            _save(_safe)
    return results


# ---------------- 刪除 (黑名單) / 安全清單 ----------------
def block_items(values):
    """從敏感清單刪除，並加入黑名單 (之後永遠不會再被自動加進敏感清單)。回傳處理筆數。"""
    now, n = _now(), 0
    with _lock:
        _sync_all()
        for v in values:
            v = (v or "").strip()
            if not v:
                continue
            rec = _items.data.pop(v, None) or {}
            _blocked.data[v] = {"value": v, "label": rec.get("label", ""),
                                "count": rec.get("count", 0), "blocked_at": now}
            n += 1
        if n:
            _save(_items, _blocked)
    return n


def unblock(values):
    """移出黑名單 (之後又可以被自動記錄)。回傳移除筆數。"""
    with _lock:
        _blocked.sync()
        n = sum(1 for v in values if _blocked.data.pop((v or "").strip(), None) is not None)
        if n:
            _save(_blocked)
    return n


def mark_safe(values, note=""):
    """加入安全清單 (永久保留)，同時從敏感清單移除。回傳處理筆數。"""
    now, n = _now(), 0
    with _lock:
        _sync_all()
        for v in values:
            v = (v or "").strip()
            if not v:
                continue
            _items.data.pop(v, None)
            _safe.data[v] = {"value": v, "note": note, "added_at": now}
            n += 1
        if n:
            _save(_items, _safe)
    return n


def unsafe(values):
    """移出安全清單。回傳移除筆數。"""
    with _lock:
        _safe.sync()
        n = sum(1 for v in values if _safe.data.pop((v or "").strip(), None) is not None)
        if n:
            _save(_safe)
    return n


# ---------------- 自動遮蔽：把敏感清單當成一條正則規則 ----------------
_known_cache = {"version": -1, "regex": None}


def _known_regex():                        # 呼叫端需持有 _lock
    if _known_cache["version"] != _items.version:
        parts = []
        for v in sorted(_items.data, key=len, reverse=True):     # 長的優先，避免短的先蓋掉長的
            if len(v) < KNOWN_MIN_LEN:
                continue
            p = re.escape(v)
            if re.match(r"[A-Za-z0-9]", v[0]):
                p = r"(?<![A-Za-z0-9])" + p       # 英數開頭：前面不能緊接英數
            if re.match(r"[A-Za-z0-9]", v[-1]):
                p += r"(?![A-Za-z0-9])"
            parts.append(p)
        _known_cache.update(version=_items.version, regex=re.compile("|".join(parts)) if parts else None)
    return _known_cache["regex"]


def detect_known(text):
    """敏感清單 -> [(start, end, label, None, "regex")] (來源 "regex"，優先序最高)"""
    with _lock:
        _items.sync()
        rx = _known_regex()
        if not rx:
            return []
        data = _items.data
        return [(m.start(), m.end(), data[m.group(0)]["label"] if m.group(0) in data else "SENSITIVE",
                 None, "regex") for m in rx.finditer(text)]
