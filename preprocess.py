"""
preprocess.py - 送模型前的文字前處理 (UI 文字 / 剪貼簿 / 文字檔 / 表格 / 圖片與 PDF 的 OCR 文字共用)
====================================================
參考 text-preprocessing notebook 的清理流程，順序相同：

    1. 全形轉半形 (NFKC)   全形 Email / 網址 / 數字 / 空白先統一，後面的規則才抓得到
    2. 隱形字元清理        零寬字元、軟連字號、控制字元 (NFKC 不會處理)
    3. 網頁標籤刪除        script/style 連內容一起移除、標籤換成換行、HTML 實體還原 (只用在一般文字，程式碼不做)
    4. 簡繁統一            預設簡轉繁 (見下方 CHINESE_CONVERT)
    5. 換行與空白整理      \\r\\n 統一成 \\n、水平空白壓成單一空格、連續換行合併、去除首尾空白

notebook 裡的「編碼讀取」(utf-8-sig / utf-8 / cp950 / big5) 在 core._read_text_file 做；
「敏感資訊遮蔽」就是整個系統本身，所以不在這裡。

最重要的設計：【偵測用前處理後的文字，遮蔽卻要改回原文】
    前處理會改變字數 (刪字元、合併空白、NFKC 展開…)，偵測結果的位置如果直接拿去切原文會切歪。
    所以每個輸出字元都記住它對應原文的 [start, end) 區間 (Normalized.starts / ends)，
    偵測完用 Normalized.map_matches() 換算回原文座標。
    結果：使用者的原文只有被遮蔽的那一段會變，其餘 (全形、標籤、空白、簡體字) 原樣保留；
    對照表存的也是原文，還原後與原文完全一致。

簡繁只做「逐字」轉換 (OpenCC 的 s2t / t2s)，不用 notebook 的 s2twp：詞彙層級的轉換
(例如 内存→記憶體) 會改變字數，無法對回原文位置；逐字轉換長度不變。
沒有安裝 opencc 時會略過這一步 (只印一次警告)，其他步驟照常。
"""
import re
import html as _html
import unicodedata
import difflib
from array import array

# ========================= 設定區 =========================
ENABLED = True           # 總開關：False 就完全不做前處理 (偵測用原文)
OPT_NFKC = True          # 1. 全形轉半形
OPT_INVISIBLE = True     # 2. 隱形字元清理
OPT_HTML = True          # 3. 網頁標籤刪除 (程式碼一律不做，圖片 OCR 文字也不做)
CHINESE_CONVERT = "s2t"  # 4. 簡繁統一："s2t" 簡轉繁 / "t2s" 繁轉簡 / None 不轉
#   為什麼預設簡轉繁：RULES 裡的中文關鍵字 (統一編號、健保投保序號…) 都是繁體，簡體文字轉繁體後才抓得到。
#   注意：handler_text.py 目前的中文 NER 模型 (uer/roberta-base-finetuned-cluener2020-chinese) 是用簡體語料訓練的，
#   如果實測人名/地址的召回率偏低，可以試試 "t2s" 或 None，用測試程式比較 F1。
OPT_WHITESPACE = True    # 5. 換行與空白整理
APPLY_TO_MEDIA = True    # 圖片 / PDF 的 OCR 文字送進 handler_media.detect_matches 前也做前處理 (見 core.py)
MAX_CHARS = 2_000_000    # 超過這個長度就略過前處理 (逐字記錄位置很吃記憶體)，直接用原文偵測
# ==========================================================

_INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u00ad]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_BLOCK_RE = re.compile(r"<!--.*?-->|<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
# 只認真正的 HTML 標籤 (標籤名只含英數)：遮蔽標籤 <PERSON_1>、<EMAIL_ADDRESS_2> 有底線，不會被誤刪
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9:-]*(?:\s[^<>]*)?/?>|<![A-Za-z][^>]*>")
_ENTITY_RE = re.compile(r"&(?:#\d{1,7}|#[xX][0-9A-Fa-f]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});")
_CRLF_RE = re.compile(r"\r\n|\r")
_HSPACE_RE = re.compile(r"[^\S\n]+")
_NL_SPACE_RE = re.compile(r" ?\n ?")
_MANY_NL_RE = re.compile(r"\n+")

_warned = set()


def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        print(msg)


# ---------------- 位置對應的基本操作 ----------------
# state = (text, starts, ends)：text 的第 i 個字元對應原文的 [starts[i], ends[i])
def _apply(state, edits):
    """edits: 依位置排序、互不重疊的 [(a, b, 取代文字)]。被取代的區間，新字元都對應到整個舊區間。"""
    if not edits:
        return state
    text, st, en = state
    out, ns, ne, pos = [], array("i"), array("i"), 0
    for a, b, repl in edits:
        if a > pos:
            out.append(text[pos:a])
            ns.extend(st[pos:a])
            ne.extend(en[pos:a])
        if repl:
            s, e = min(st[a:b]), max(en[a:b])
            out.append(repl)
            ns.extend(array("i", [s]) * len(repl))
            ne.extend(array("i", [e]) * len(repl))
        pos = b
    if pos < len(text):
        out.append(text[pos:])
        ns.extend(st[pos:])
        ne.extend(en[pos:])
    return "".join(out), ns, ne


def _sub(state, rx, repl):
    edits = []
    for m in rx.finditer(state[0]):
        r = repl(m) if callable(repl) else repl
        if r != m.group():
            edits.append((m.start(), m.end(), r))
    return _apply(state, edits)


# ---------------- 各步驟 ----------------
def _is_mark(c):
    """會跟前一個字元合成的符號：組合符號 (Mn/Mc/Me)，以及半形片假名的濁音 / 半濁音 (ｶﾞ -> ガ)"""
    return c in "\uff9e\uff9f" or unicodedata.category(c).startswith("M")


def _st_nfkc(state):
    text, st, en = state
    if text.isascii():
        return state
    out, ns, ne, i, n = [], array("i"), array("i"), 0, len(text)
    while i < n:
        ch = text[i]
        # ASCII 基底字元後面緊接組合符號 (e + U+0301) 時不能走快速路徑，要一起正規化才會合成 é
        if ch.isascii() and not (i + 1 < n and not text[i + 1].isascii() and _is_mark(text[i + 1])):
            out.append(ch)
            ns.append(st[i])
            ne.append(en[i])
            i += 1
            continue
        j = i + 1                                    # 基底字元 + 後面接的組合符號當成一組一起正規化
        while j < n and _is_mark(text[j]):
            j += 1
        piece = unicodedata.normalize("NFKC", text[i:j])
        s, e = min(st[i:j]), max(en[i:j])
        out.append(piece)
        ns.extend(array("i", [s]) * len(piece))
        ne.extend(array("i", [e]) * len(piece))
        i = j
    return "".join(out), ns, ne


def _st_invisible(state):
    return _sub(state, _INVISIBLE_RE, "")


def _st_html(state):
    state = _sub(state, _HTML_BLOCK_RE, "\n")        # 註解、script、style (連內容一起移除)
    state = _sub(state, _HTML_TAG_RE, "\n")          # 標籤換成換行 (同 notebook 的 get_text("\n"))
    return _sub(state, _ENTITY_RE, lambda m: _html.unescape(m.group()))


_converters = {}
_conv_cache = {}


def _get_converter(kind):
    if kind not in _converters:
        conv = None
        try:
            from opencc import OpenCC
            for name in (kind, kind + ".json"):      # 不同版本的 opencc 套件設定檔名稱不同
                try:
                    conv = OpenCC(name)
                    break
                except Exception:
                    continue
        except Exception:
            pass
        if conv is None:
            _warn_once("opencc", "[前處理] 找不到 opencc (pip install opencc-python-reimplemented)，略過簡繁統一")
        _converters[kind] = conv
    return _converters[kind]


def _conv_char(kind, ch):
    key = (kind, ch)
    r = _conv_cache.get(key)
    if r is None:
        c = _converters[kind].convert(ch)
        r = c if len(c) == 1 else ch                 # 轉出多個字就保留原字，確保長度不變
        _conv_cache[key] = r
    return r


def _st_chinese(state):
    kind = CHINESE_CONVERT
    if not kind:
        return state
    text, st, en = state
    if not any("\u4e00" <= c <= "\u9fff" for c in text) or _get_converter(kind) is None:
        return state
    out = "".join(_conv_char(kind, c) if "\u4e00" <= c <= "\u9fff" else c for c in text)
    return (out, st, en) if len(out) == len(text) else state


def _st_whitespace(state):
    state = _sub(state, _CRLF_RE, "\n")              # 步驟同 notebook 的 normalize_newlines / normalize_space
    state = _sub(state, _HSPACE_RE, " ")
    state = _sub(state, _NL_SPACE_RE, "\n")
    state = _sub(state, _MANY_NL_RE, "\n")
    text = state[0]
    if not text.strip():
        return _apply(state, [(0, len(text), "")]) if text else state
    lead, tail = len(text) - len(text.lstrip()), len(text.rstrip())
    edits = ([(0, lead, "")] if lead else []) + ([(tail, len(text), "")] if tail < len(text) else [])
    return _apply(state, edits)


# (名稱, 是否啟用, 函式, 只用在一般文字)
def _stages():
    return [
        ("全形轉半形 (NFKC)", OPT_NFKC, _st_nfkc, False),
        ("隱形字元清理", OPT_INVISIBLE, _st_invisible, False),
        ("網頁標籤刪除", OPT_HTML, _st_html, True),
        (f"簡繁統一 ({CHINESE_CONVERT})", bool(CHINESE_CONVERT), _st_chinese, False),
        ("換行與空白整理", OPT_WHITESPACE, _st_whitespace, False),
    ]


# ---------------- 對外介面 ----------------
class Normalized:
    """前處理結果：text 是要送模型的文字；starts / ends 記錄每個字元對應原文的區間"""

    def __init__(self, original, text, starts, ends):
        self.original, self.text, self.starts, self.ends = original, text, starts, ends
        self.identity = (text == original)           # 完全沒變：座標不需要換算

    def to_orig(self, start, end):
        """前處理後文字的 [start, end) -> 原文的 [start, end)；對不到 (例如整段都是被刪掉的字元) 回傳 None"""
        if self.identity:
            return (start, end) if 0 <= start < end <= len(self.original) else None
        if not (0 <= start < end <= len(self.text)):
            return None
        s, e = min(self.starts[start:end]), max(self.ends[start:end])
        return (s, e) if e > s else None

    def map_matches(self, matches):
        """把偵測結果的座標換算回原文。支援 (start, end, ...) 的 tuple/list 與含 start/end 的 dict；
        格式認不得時回傳 None，讓呼叫端改用原文重新偵測 (不會默默給出錯誤座標)。"""
        try:
            matches = list(matches)
        except TypeError:
            return None
        if self.identity:
            return matches
        out, seen = [], set()
        for m in matches:
            if isinstance(m, dict) and isinstance(m.get("start"), int) and isinstance(m.get("end"), int):
                mapped = self.to_orig(m["start"], m["end"])
                if mapped:
                    out.append({**m, "start": mapped[0], "end": mapped[1]})
            elif isinstance(m, (tuple, list)) and len(m) >= 2 and isinstance(m[0], int) and isinstance(m[1], int):
                mapped = self.to_orig(m[0], m[1])
                if mapped:
                    item = type(m)(mapped + tuple(m[2:]))
                    if isinstance(item, tuple):
                        if item in seen:
                            continue
                        seen.add(item)
                    out.append(item)
            else:
                return None
        return out


def preprocess(text, html=True, trace=None):
    """text: 原文。html=False 時不做網頁標籤刪除 (程式碼 / OCR 文字用)。
    trace: 傳入 list 就會記錄 (步驟名稱, 前, 後)，給 describe() 用。回傳 Normalized。"""
    n = len(text or "")
    if not ENABLED or n == 0:
        return Normalized(text, text, array("i", range(n)), array("i", range(1, n + 1)))
    if n > MAX_CHARS:
        _warn_once("toolong", f"[前處理] 文字超過 {MAX_CHARS} 字元，略過前處理直接用原文偵測")
        return Normalized(text, text, array("i", range(n)), array("i", range(1, n + 1)))
    state = (text, array("i", range(n)), array("i", range(1, n + 1)))
    for name, enabled, fn, html_only in _stages():
        if not enabled or (html_only and not html):
            continue
        before = state[0]
        state = fn(state)
        if trace is not None:
            trace.append((name, before, state[0]))
    return Normalized(text, *state)


def detect(detect_fn, text, html=True):
    """前處理 -> 送模型 (detect_fn) -> 結果換算回原文座標。格式認不得時改用原文偵測。"""
    norm = preprocess(text, html)
    mapped = norm.map_matches(detect_fn(norm.text))
    return mapped if mapped is not None else detect_fn(text)


# ---------------- 預覽 (給 UI 顯示每一步改了什麼) ----------------
_ESC = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def vis(s, limit=40):
    """把看不見的字元顯示成跳脫碼，讓差異看得到"""
    out = "".join(_ESC.get(c) or (c if c.isprintable() and unicodedata.category(c) != "Mn" else f"\\u{ord(c):04x}")
                  for c in s)
    return out if len(out) <= limit else out[:limit // 2] + "…" + out[-limit // 2:]


def describe(text, html=True, maxseg=6):
    """回傳 (說明文字, 送模型的文字)；說明文字列出每個步驟改了哪些地方 (格式仿 notebook 的 report_step)"""
    if not ENABLED:
        return "前處理已關閉：偵測直接使用原文。", text
    trace = []
    norm = preprocess(text, html, trace)
    lines = [f"原文 {len(text)} 字元 → 送模型的文字 {len(norm.text)} 字元 "
             f"(偵測結果會換算回原文座標，遮蔽只動被偵測到的那一段)"]
    for name, a, b in trace:
        if a == b:
            lines.append(f"【{name}】沒有變動")
            continue
        if len(a) > 5000:
            lines.append(f"【{name}】字元 {len(a)} → {len(b)} ({len(b) - len(a):+d})")
            continue
        ops = [o for o in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes() if o[0] != "equal"]
        lines.append(f"【{name}】{len(ops)} 處差異，字元 {len(a)} → {len(b)} ({len(b) - len(a):+d})")
        for _, i1, i2, j1, j2 in ops[:maxseg]:
            lines.append(f"      「{vis(a[i1:i2]) or '∅'}」 → 「{vis(b[j1:j2]) or '∅'}」")
        if len(ops) > maxseg:
            lines.append(f"      …還有 {len(ops) - maxseg} 處差異")
    return "\n".join(lines), norm.text
