"""
preprocess.py - 送模型前的文字前處理 (UI 文字 / 剪貼簿 / 文字檔 / 表格 / 圖片與 PDF 的 OCR 文字共用)
====================================================
參考 text-preprocessing notebook 的清理流程，順序相同：

    1. 全形轉半形 (NFKC)   全形 Email / 網址 / 數字 / 空白先統一，後面的規則才抓得到
    2. 隱形字元清理        零寬字元、軟連字號、控制字元 (NFKC 不會處理)
    3. 網頁標籤處理        只把 HTML 標籤的骨架換成換行，標籤裡的屬性值 (href="mailto:…"、alt、title…)、
                           script / style / 註解的內容都【保留】；HTML 實體還原 (只用在一般文字，程式碼不做)
    4. 簡繁統一            預設不轉 (見下方 CHINESE_CONVERT；規則的簡繁關鍵字已在 handler_text 同時支援)
    5. 換行與空白整理      預設關閉 (見下方 OPT_WHITESPACE)

notebook 裡的「編碼讀取」(utf-8-sig / utf-8 / cp950 / big5) 在 core._read_text_file 做；
「敏感資訊遮蔽」就是整個系統本身，所以不在這裡。

【和 notebook 不同的地方，以及為什麼】notebook 是在「清理語料」，這裡是在「找出敏感資料再遮蔽」，
原則不一樣：前處理只能把文字改寫成比較好偵測的樣子，不能把可能含有個資的內容刪掉，也不能改變規則依賴的
字元類別。具體來說：
    - 網頁標籤：不刪 script / style / 註解的內容與標籤屬性值 (mailto:、網址參數、alt / title 都可能含個資，
      刪掉等於偵測不到、也遮蔽不到)；只認標準 HTML 標籤名，<John Smith> 這種不是標籤的角括號不動。
    - 空白與換行：預設不整理。檔名等規則的字元類別裡有「一般空白」，把全形空白 / Tab 壓成一般空白，
      會讓規則把相鄰的字一起吞進來 (例如「與　診斷證明.docx」被當成一個檔名)，偵測範圍就跟答案對不上。
      全形空白 / 不換行空白也不會被 NFKC 轉成一般空白。
    - NFKC：全形英數與符號照轉 (規則才抓得到)，但中文標點 (，；：！？（）～) 保持原樣，
      避免無謂地改變中文 NER 模型看到的文字。
    - 簡繁：逐字轉換會把台灣姓氏 游→遊、余→餘、范→範、涂→塗、于→於 改掉，所以預設不轉；
      需要轉的話可選 "auto" (整段判斷為簡體才轉)。

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
NFKC_KEEP = "，；：！？（）～"   # NFKC 不轉換的中文標點 (全形 -> 半形會改變中文 NER 看到的文字，規則也用不到)
OPT_HTML = True          # 3. 網頁標籤處理 (只換掉標籤骨架，屬性值與 script/style/註解內容都保留；程式碼與 OCR 文字不做)
CHINESE_CONVERT = None   # 4. 簡繁統一：None 不轉 (預設) / "auto" 整段判斷為簡體才簡轉繁 / "s2t" 一律簡轉繁 / "t2s" 一律繁轉簡
#   為什麼預設不轉：(1) 逐字轉換會把台灣姓氏 游→遊、余→餘、范→範、涂→塗、于→於 改掉；
#   (2) handler_text.py 的中文 NER 模型 (uer/roberta-base-finetuned-cluener2020-chinese) 是用簡體語料訓練的，
#   簡體文字轉成繁體再送它不一定比較好；(3) 規則裡繁體的關鍵字 (統一編號、健保投保序號) 已改成同時認簡繁，
#   不需要靠轉換。想實驗就用測試程式的 --ablation 比較 None / "auto" / "t2s"。
AUTO_MIN_SIMPLIFIED = 3  # "auto"：至少要有幾個簡體字形才考慮轉換
AUTO_RATIO = 4           # "auto"：簡體字形數要至少是繁體字形數的幾倍
AUTO_MIN_SHARE = 0.2     # "auto"：簡體字形數要占整段中文字的比例至少多少 (簡體文章通常有三、四成)
OPT_WHITESPACE = False   # 5. 換行與空白整理 (預設關閉：會改變規則依賴的字元類別，且 BERT 類模型本來就忽略空白)
APPLY_TO_MEDIA = True    # 圖片 / PDF 的 OCR 文字送進 handler_media.detect_matches 前也做前處理 (見 core.py)
MAX_CHARS = 2_000_000    # 超過這個長度就略過前處理 (逐字記錄位置很吃記憶體)，直接用原文偵測
# ==========================================================

_INVISIBLE_RE = re.compile(r"[\u200b-\u200d\u2060\ufeff\u00ad]|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
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
        if j == i + 1 and (ch in NFKC_KEEP or unicodedata.category(ch) == "Zs"):
            out.append(ch)                           # 中文標點、全形空白 / 不換行空白原樣保留
            ns.append(st[i])
            ne.append(en[i])
            i = j
            continue
        piece = unicodedata.normalize("NFKC", text[i:j])
        s, e = min(st[i:j]), max(en[i:j])
        out.append(piece)
        ns.extend(array("i", [s]) * len(piece))
        ne.extend(array("i", [e]) * len(piece))
        i = j
    return "".join(out), ns, ne


def _st_invisible(state):
    return _sub(state, _INVISIBLE_RE, "")


_HTML_TAG_NAMES = frozenset((
    "a abbr address area article aside audio b base bdi bdo big blockquote body br button canvas caption center cite "
    "code col colgroup data datalist dd del details dfn dialog div dl dt em embed fieldset figcaption figure font "
    "footer form frame frameset h1 h2 h3 h4 h5 h6 head header hr html i iframe img input ins kbd label legend li link "
    "main map mark menu meta meter nav noscript object ol optgroup option output p param picture pre progress q rp rt "
    "ruby s samp script section select small source span strike strong style sub summary sup svg table tbody td "
    "template textarea tfoot th thead time title tr tt u ul var video wbr").split())
_TAG_RE = re.compile(r"<(/?)([A-Za-z][A-Za-z0-9]*)((?:\s[^<>]*)?)(/?)>")
_ATTR_VALUE_RE = re.compile(r"""=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>=`]+))""")
_COMMENT_MARK_RE = re.compile(r"<!--|-->|<![Dd][Oo][Cc][Tt][Yy][Pp][Ee][^>]*>")


def _st_html(state):
    """只把標籤的「骨架」(<a href=" … " class=" … ">) 換成換行，屬性值與標籤之間的文字全部保留；
    script / style / 註解 的內容也保留 (只換掉開關標記)，因為裡面都可能有個資。非標準 HTML 標籤名 (<John Smith>) 不動。"""
    text = state[0]
    edits = []
    for m in _TAG_RE.finditer(text):
        if m.group(2).lower() not in _HTML_TAG_NAMES:
            continue
        attrs = m.group(3).strip().rstrip("/").strip()
        if attrs and "=" not in attrs:                # <b and c> 之類的一般文字：屬性必須是 名稱=值 才當成標籤
            continue
        a, b = m.span()
        pos = a
        for vm in _ATTR_VALUE_RE.finditer(m.group(0)):
            for g in (1, 2, 3):
                if vm.group(g):                       # 非空的屬性值：保留
                    vs, ve = a + vm.start(g), a + vm.end(g)
                    if vs > pos:
                        edits.append((pos, vs, "\n"))
                    pos = ve
                    break
        if b > pos:
            edits.append((pos, b, "\n"))
    edits += [(m.start(), m.end(), "\n") for m in _COMMENT_MARK_RE.finditer(text)]
    edits.sort()
    clean, last = [], 0
    for e in edits:                                   # 去掉重疊的 (保險)
        if e[0] >= last:
            clean.append(e)
            last = e[1]
    state = _apply(state, clean)
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


_AMBIGUOUS_TRAD = frozenset("余游范涂于里后干")    # 繁體文章裡也很常見的字 (台灣常見姓氏 / 里)


def _looks_simplified(text):
    """用字形統計判斷整段文字是簡體還是繁體：
    繁體字形 = 有對應簡體的字 (體 學 這…)；簡體字形 = 沒有簡轉繁以外的用途、轉繁會變的字 (体 学 这…)。
    余 于 范 游 涂 這類兩邊都是正字的姓氏會被算成簡體字形，但繁體文章裡繁體字形多很多，不會誤判。"""
    if _get_converter("s2t") is None or _get_converter("t2s") is None:
        return False
    trad = simp = total = 0
    for c in text:
        if "\u4e00" <= c <= "\u9fff":
            total += 1
            if c in _AMBIGUOUS_TRAD:                 # 兩邊都是正字的姓氏 / 地名用字，不能當簡體證據
                continue
            if _conv_char("t2s", c) != c:
                trad += 1
            elif _conv_char("s2t", c) != c:
                simp += 1
    return simp >= AUTO_MIN_SIMPLIFIED and simp >= AUTO_RATIO * trad and simp >= AUTO_MIN_SHARE * total


def _st_chinese(state):
    kind = CHINESE_CONVERT
    if not kind:
        return state
    text, st, en = state
    if not any("\u4e00" <= c <= "\u9fff" for c in text):
        return state
    if kind == "auto":
        kind = "s2t" if _looks_simplified(text) else None
        if not kind:
            return state
    if _get_converter(kind) is None:
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
        ("網頁標籤處理 (保留內容)", OPT_HTML, _st_html, True),
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
