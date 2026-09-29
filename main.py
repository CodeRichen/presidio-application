"""
剪貼簿敏感資訊防護 - 主程式
====================================================
本檔案支援兩種「複製/貼上」運作模式，靠下面設定區的 HOTKEY_PASTE 是否有值自動切換，
不用改任何其他程式碼、也不用維護兩份檔案：

    模式 A・專屬快速鍵 (HOTKEY_PASTE 有設定值，例如 '<ctrl>+<alt>+v')：
        HOTKEY_COPY / HOTKEY_PASTE 是另外一組按鍵組合，跟系統原生 Ctrl+C/Ctrl+V 分開、互不干擾。
        複製時只是「算出覆蓋結果、暫存起來」，不動系統剪貼簿；要等按下專屬的貼上鍵，
        才把覆蓋結果「暫時」寫進剪貼簿、模擬按一次 Ctrl+V、貼完馬上把剪貼簿還原成原始內容，
        避免覆蓋結果留在系統剪貼簿上被其他程式讀到。原生 Ctrl+C / Ctrl+V 完全不受影響。

    模式 B・Ctrl+C 直接當快速鍵 (HOTKEY_PASTE 留空 None 或 "")：
        HOTKEY_COPY 直接就是 Ctrl+C 本身。按下 Ctrl+C 的當下，目標視窗本來就會自己把原始內容
        寫進系統剪貼簿(作業系統原生行為，跟這支程式無關；pynput 的全域快速鍵只是「額外」監聽
        同一組按鍵，不會攔截或取代原生行為，所以完全不需要、也不能再模擬一次 Ctrl+C)。
        我們要做的只是：等原生複製寫完剪貼簿、讀出來、算出遮蔽/還原後的版本，然後
        【直接覆寫剪貼簿】成這個新版本——原本(舊/未覆蓋)的內容就直接被新(已覆蓋)的取代掉。
        之後使用者按下「原生」Ctrl+V 貼上時，剪貼簿裡本來就已經是新版本了，系統會自動貼上
        新版本內容——完全不需要、也不能再另外監聽 Ctrl+V、模擬貼上，不然會變成
        「原生貼一次 + 我們模擬貼一次」，同一份內容被貼兩次。這個模式下不會註冊任何貼上快速鍵。

        注意（模式 B 專屬的副作用）：因為 HOTKEY_COPY 直接綁定 Ctrl+C，在「終端機視窗」按
        Ctrl+C 也會同時觸發這支程式的 on_copy（嘗試讀取終端機當下剪貼簿內容並覆寫），跟終端機
        自己原本用 Ctrl+C 觸發 KeyboardInterrupt 是兩件不相干的事，不會互相取代，但保險起見，
        如果 Ctrl+C 停不掉腳本，直接關閉終端機視窗即可。

架構：
    main.py          <- 這支：監聽快速鍵、判斷複製到的是「檔案」還是「文字」、是「程式碼」還是
                          「一般文字」，決定要交給哪個模組處理，並負責標籤替換與剪貼簿讀寫
    handler_text.py  <- 一般文字的敏感資訊規則 (含 Presidio)
    handler_code.py  <- 程式碼的敏感資訊規則 (API Key/JWT/路徑 + 借用 handler_text 的個資規則)
    handler_media.py <- 圖片/PDF 的偵測(OCR)+塗黑+還原

想改快速鍵/模式、副檔名分類、要用哪套方式判斷「這段文字是不是程式碼」，都在下面「設定區」改，
不用動下面的邏輯。

安裝：pip install pywin32 psutil pyperclip pynput
      (另外依 handler_text/handler_code/handler_media 檔頭的說明安裝 presidio / tesseract / pymupdf)
"""

import io
import csv
import os
import re
import time
import struct
import win32clipboard
import win32gui
import win32process
import win32api
import win32con
import psutil
import pyperclip
from pynput import keyboard

import handler_text
import handler_code
import handler_media

# ========================= 設定區：想改快速鍵/模式/副檔名/分類方式都在這裡改 =========================
HOTKEY_COPY = '<ctrl>+<alt>+c'    # 有標籤就還原、沒標籤就遮蔽 (跟系統原生 Ctrl+C 分開，不互相干擾)
HOTKEY_PASTE = '<ctrl>+<alt>+v'   # 單純貼上剪貼簿目前的內容，不做任何還原/改回標籤的動作
# ↑↑↑ 把上面兩行改成這樣就會切換成「模式 B：Ctrl+C 直接當快速鍵」，複製時直接覆寫剪貼簿，
#     不需要、也不會註冊任何貼上快速鍵 (原生 Ctrl+V 自然就會貼出覆寫後的內容)：
#         HOTKEY_COPY = '<ctrl>+c'
#         HOTKEY_PASTE = None

MASK_MODE = "field"
# "exact" = 原本精確遮蔽
# "line"  = 有 PII 就整行遮蔽
# "field" = 有 PII 就只遮該欄位

FIELD_FORMAT_CSV = "csv"
FIELD_FORMAT_TSV = "tsv"
FIELD_FORMAT_PIPE = "pipe"
FIELD_FORMAT_SEMICOLON = "semicolon"
FIELD_FORMAT_LINE = "line"
FIELD_FORMAT_UNKNOWN = "unknown"

DIRECT_MODE = not HOTKEY_PASTE  # 由上面兩行自動算出，下面邏輯都靠這個布林值分流，不用再改別的地方

CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs",
                    ".php", ".rb", ".sh", ".sql", ".json", ".yml", ".yaml"}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp", ".pdf"}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".log"}   # 純文字檔：讀出內容、按一般文字規則遮蔽

MAP_FILE = "./presidio/real/clipboard_map.txt"

_log_callback = None
_result_callback = None


def set_log_callback(callback):
    global _log_callback
    _log_callback = callback


def set_result_callback(callback):
    global _result_callback
    _result_callback = callback

def debug_log(message):

    # Terminal 照樣顯示
    print(message)

    # 如果 UI 有啟動，也送一份給 UI
    if _log_callback is not None:
        _log_callback(str(message))

def heuristic_is_code(text: str) -> bool:
    """快速判斷一段文字像不像程式碼：命中越多程式碼特徵，越可能是程式碼。門檻(>=3)可自行調整。
    只用在「直接複製一段文字」(沒有檔名可判斷) 的情況；檔案有副檔名時一律用副檔名判斷，不會呼叫這裡。"""
    signals = re.findall(
        r"[{};]|=>|\bdef \b|\bclass \b|\bimport \b|#include|\bfunction\b|\bconst \b|\bvar \b|\breturn \b",
        text,
    )
    return len(signals) >= 3


# 分類模型註冊表：想加自己的判斷方式(例如真的呼叫一個 LLM 或雲端 API)，
# 寫一個 func(text) -> bool，往這裡加一行，再把 TEXT_CLASSIFY_MODE 改成該名稱即可，其他都不用動。
# 注意：這個註冊表只影響「沒有副檔名可判斷」的原始剪貼簿文字，檔案一律用 CODE_EXTENSIONS/TEXT_FILE_EXTENSIONS 判斷。
CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
    # "qwen": your_qwen_is_code_function,
}
TEXT_CLASSIFY_MODE = "heuristic"
# ================================================================================================

TAG_RE = re.compile(r"<([A-Z_]+)_(\d+)>")

# 模式 A 用：Ctrl+Alt+C 產生的「覆蓋結果」暫存在這裡，不會寫進系統剪貼簿；
# 只有按 HOTKEY_PASTE 才會短暫寫入剪貼簿貼上，貼完立刻還原成 original，避免覆蓋結果留在剪貼簿上被其他程式讀到。
# kind: "text" | "files" | None，content 是覆蓋結果本身，original 是複製當下剪貼簿原本的內容。
_override = {"kind": None, "content": None, "original": None}

# 模式 B 用：純紀錄，方便你在終端機/debug 時查看「上一次複製」的新舊兩個版本；
# 不影響剪貼簿實際內容——剪貼簿本身永遠只有一份，就是 on_copy 覆寫後的那份 (masked)。
#   original -> 舊的、原生複製當下、尚未被覆蓋的內容
#   masked   -> 新的、經過遮蔽/還原處理、已經直接覆寫進剪貼簿的內容
_last_copy = {"kind": None, "original": None, "masked": None}


def is_code_text(text: str) -> bool:
    return CLASSIFY_MODELS[TEXT_CLASSIFY_MODE](text)


# ---------- 文字/程式碼共用對照表 (圖片/PDF 另有自己的一份，在 handler_media 裡管理) ----------
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


def cleanup_map_files():
    """程式結束時把對照表刪掉，不留明碼敏感資訊在硬碟上"""
    for f in (MAP_FILE, handler_media.DEFAULT_MAP_FILE):
        try:
            os.remove(f)
            print(f"[清理] 已刪除 {f}")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[警告] 刪除 {f} 失敗：{e}")


def _priority_tier(source: str, prefer_zh: bool) -> int:
    """重疊時決定「誰贏」的優先序，數字越小越優先：
        0 -> 正則規則 (source == "regex")，不管中英文一律最優先
        1 -> 跟整段文字語言「吻合」的模型：文字含中文時是中文 NER (zh)，否則是 Presidio 英文模型 (en)
        2 -> 跟整段文字語言「不吻合」的模型 (次要，通常是誤判機率較高的那個)
    prefer_zh 是「這段文字含不含中文」的判斷結果 (handler_text.contains_chinese)，
    不是逐筆判斷每個候選字串本身的語言——是用整段文字的語言去決定 en/zh 兩個模型誰優先。"""
    if source == "regex":
        return 0
    if source == "zh":
        return 1 if prefer_zh else 2
    if source == "en":
        return 2 if prefer_zh else 1
    return 3  # 保底：理論上不會出現 "regex"/"en"/"zh" 以外的 source


def merge_overlaps(matches, prefer_zh: bool):
    """matches 是 (start, end, label, score, source) 的 5 元素 tuple。

    重疊處理優先序 (見 _priority_tier)：
        1) 正則規則優先，不管中英文
        2) 其餘模型類的結果，依整段文字是否含中文決定：
           文字含中文 -> 中文 NER 模型優先於 Presidio 英文模型；不含中文 -> 反過來
        3) 同一優先序內，範圍(end-start)較長的優先；再相同則信心分數較高的優先
           (正則規則沒有分數，比較時當成 0，但因為正則規則的優先序本來就最高，
           這條 tie-break 只會在「兩個正則規則互相重疊」時才有意義)

    只要跟「已經保留」的結果重疊，優先序較低的候選就會被淘汰；不管保留或淘汰，
    每一筆的信心分數都會印出來（regex 沒有分數會印 "None(規則比對)"），方便確認
    究竟是哪個規則/模型的結果最後勝出、哪些候選是因為跟別人重疊才被拿掉的。"""
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


def anonymize_text(text, mapping, is_code=None):
    """is_code=None 時 (原始剪貼簿文字、沒有副檔名可判斷) 才會用 is_code_text() 猜；
    檔案處理 (dispatch_files) 一律明確傳入 is_code，不會走猜測那條路。"""
    if is_code is None:
        is_code = is_code_text(text)
    matches = handler_code.detect(text) if is_code else handler_text.detect(text)
    if not matches:
        return None

    # 用整段文字判斷中英文，決定重疊時中/英模型誰優先；跟每筆候選各自的 source 一起交給 merge_overlaps
    prefer_zh = handler_text.contains_chinese(text)
    print(f"[偵測] 共 {len(matches)} 筆候選 (含正則規則與模型)：")
    matches = merge_overlaps(matches, prefer_zh)

    reverse = {v: k for k, v in mapping.items()}
    counters = {}
    for tag in mapping:
        m = TAG_RE.match(tag)
        if m:
            t, i = m.groups()
            counters[t] = max(counters.get(t, 0), int(i))

    out = text
    for start, end, etype, score, source in sorted(matches, key=lambda m: m[0], reverse=True):
        val = text[start:end]
        tag = reverse.get(val)
        if not tag:
            counters[etype] = counters.get(etype, 0) + 1
            tag = f"<{etype}_{counters[etype]}>"
            mapping[tag] = val
            reverse[val] = tag
        kind = "程式碼" if is_code else "一般文字"
        score_info = f", 信心分數 {score}" if score is not None else ""
        print(f"[遮蔽] {val!r} -> {tag}  (理由: {etype}, 判斷為{kind}{score_info}, 來源={source})")
        out = out[:start] + tag + out[end:]

    save_map(mapping)
    return out

# ============================================================
# Field-level anonymization
# ============================================================

FIELD_MIN_SCORE = 0.70


# ==================================================
# 可以觸發整個 Field 遮蔽的 Entity 類型
# ==================================================

FIELD_SENSITIVE_ENTITIES = {
    "PERSON",

    "TW_PHONE",
    "PHONE_NUMBER",

    "EMAIL",
    "EMAIL_ADDRESS",

    "ADDRESS",
    "LOCATION",

    "TW_ID",

    "CREDIT_CARD",
    "US_BANK_NUMBER",
    "BANK_ACCOUNT",

    "US_SSN",
    "DRIVER_LICENSE",
    "PASSPORT",

    "IP_ADDRESS",

    "MEDICAL_RECORD",
    "TAX_ID",
    "INSURANCE_ID",
}

# 真正用來分隔「欄位」的符號。
# 注意：
#   ":" / "：" 不放進來，因為它們通常是 key:value 的內部分隔。
#
# 支援：
#   |
#   ,
#   tab
#   ;
#   ；
#
# 換行會另外處理。


FIELD_SEPARATOR_RE = re.compile(r"(\s*\|\s*|\t+|\s*,\s*|\s*[;；]\s*)")

# block_type

from dataclasses import dataclass

@dataclass
class TextBlock:
    text: str
    block_type: str

BLOCK_CODE = "code"
BLOCK_CSV = "csv"
BLOCK_TSV = "tsv"
BLOCK_PIPE = "pipe"
BLOCK_SEMICOLON = "semicolon"
BLOCK_TEXT = "text"
BLOCK_BLANK = "blank"

def has_strong_code_signal(text):
    """
    判斷一段文字是否有明顯程式碼特徵。
    這裡只使用較強的 signal，
    避免普通文字被誤判為 code。
    """

    code_patterns = [
        # Python
        r"^\s*def\s+\w+\s*\(",
        r"^\s*class\s+\w+.*:",
        r"^\s*(from|import)\s+\w+",
        r"^\s*if\s+.+:",
        r"^\s*for\s+.+:",
        r"^\s*while\s+.+:",

        # Java / C / C++
        r"^\s*public\s+class\s+\w+",
        r"^\s*(public|private|protected)\s+static\s+",
        r"^\s*#include\s*[<\"]",
        r"^\s*(int|void|double|float|char)\s+main\s*\(",
        r"\bSystem\.out\.println\s*\(",
        r"\bstd::",

        # JavaScript
        r"^\s*(const|let|var)\s+\w+\s*=",
        r"\bconsole\.log\s*\(",

        # common function call / assignment
        r"^\s*\w+\s*=\s*[\"'\[\{\d]",
    ]

    for pattern in code_patterns:
        if re.search(pattern, text, re.MULTILINE):
            return True

    # braces + programming syntax
    if "{" in text and "}" in text:
        if "=" in text or ";" in text or "(" in text:
            return True

    return False

def classify_block(text):
    if not text.strip():
        return BLOCK_BLANK

    # -----------------------------------
    # 1. 強程式碼訊號優先
    # -----------------------------------
    if has_strong_code_signal(text):
        return BLOCK_CODE

    # -----------------------------------
    # 2. 結構化資料
    # -----------------------------------
    fmt = detect_field_format(text)

    if fmt == FIELD_FORMAT_CSV:
        return BLOCK_CSV

    if fmt == FIELD_FORMAT_TSV:
        return BLOCK_TSV

    if fmt == FIELD_FORMAT_PIPE:
        return BLOCK_PIPE

    if fmt == FIELD_FORMAT_SEMICOLON:
        return BLOCK_SEMICOLON

    # -----------------------------------
    # 3. 普通文字
    # -----------------------------------
    return BLOCK_TEXT

def segment_text(text):
    """
    先利用空白行切成 logical blocks，
    再分別判斷每個 block 的類型。
    """

    blocks = []

    # 保留空白行，之後才能重組原文
    parts = re.split(r"(\r?\n\s*\r?\n)", text)

    for part in parts:
        if not part:
            continue

        if re.fullmatch(r"\r?\n\s*\r?\n", part):
            blocks.append(
                TextBlock(
                    text=part,
                    block_type=BLOCK_BLANK
                )
            )
            continue

        block_type = classify_block(part)

        blocks.append(
            TextBlock(
                text=part,
                block_type=block_type
            )
        )

    return blocks

def anonymize_segmented_text(text, mapping):
    blocks = segment_text_v2(text)

    output = []
    changed = False

    print("\n========== SEGMENTATION ==========")

    for i, block in enumerate(blocks):

        preview = block.text.strip().replace("\n", " ")[:60]

        debug_log(
            f"[BLOCK {i}] "
            f"type={block.block_type} "
            f"| {preview!r}"
        )

        # -----------------------------------
        # 空白
        # -----------------------------------
        if block.block_type == BLOCK_BLANK:
            output.append(block.text)
            continue

        result = None

        # -----------------------------------
        # CODE
        # -----------------------------------
        if block.block_type == BLOCK_CODE:

            result = anonymize_text(
                block.text,
                mapping,
                is_code=True
            )

        # -----------------------------------
        # structured field data
        # -----------------------------------
        elif block.block_type in {
            BLOCK_CSV,
            BLOCK_TSV,
            BLOCK_PIPE,
            BLOCK_SEMICOLON,
        }:

            result = anonymize_fields(
                block.text,
                mapping,
                is_code=False
            )

        # -----------------------------------
        # normal text
        # -----------------------------------
        else:

            result = anonymize_text(
                block.text,
                mapping,
                is_code=False
            )

        if result is None:
            output.append(block.text)

        else:
            output.append(result)
            changed = True

    if not changed:
        return None

    save_map(mapping)

    result = "".join(output)

    if _result_callback is not None:
        _result_callback(result)

    return result

def classify_line(line):
    """
    對單一行進行初步分類。
    """

    stripped = line.strip()

    if not stripped:
        return BLOCK_BLANK

    # ------------------------------
    # 1. 強程式碼特徵
    # ------------------------------
    if has_strong_code_signal(line):
        return BLOCK_CODE

    # ------------------------------
    # 2. TSV
    # ------------------------------
    if "\t" in line:
        return BLOCK_TSV

    # ------------------------------
    # 3. PIPE
    # ------------------------------
    if "|" in line:
        return BLOCK_PIPE

    # ------------------------------
    # 4. CSV
    # ------------------------------
    try:
        row = next(csv.reader([line]))

        if len(row) >= 3:
            return BLOCK_CSV

    except csv.Error:
        pass

    # ------------------------------
    # 5. Semicolon
    # ------------------------------
    try:
        row = next(
            csv.reader(
                [line],
                delimiter=";"
            )
        )

        if len(row) >= 3:
            return BLOCK_SEMICOLON

    except csv.Error:
        pass

    return BLOCK_TEXT

def is_code_continuation(line):
    """
    判斷一行是否很像前一個 code block 的延續。

    注意：
    這個函式不能單獨決定它是不是 code，
    必須搭配上一行已經是 CODE 的 context 使用。
    """

    stripped = line.strip()

    if not stripped:
        return False

    patterns = [
    # function call
    r"^[A-Za-z_]\w*\s*\(.*\)\s*;?$",

    # object.method(...)
    r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\s*\(.*\)\s*;?$",

    # Java / C / C++ typed variable declaration
    r"^(?:String|int|long|double|float|boolean|char|byte|short)\s+\w+\s*(?:=.*)?;?$",

    # return
    r"^return\b.*;?$",

    # break / continue
    r"^(break|continue)\s*;?$",

    # closing syntax
    r"^[}\]\)]+\s*;?,?$",

    # else / catch / finally
    r"^(else|finally)\b",
    r"^catch\s*\(",
]

    return any(
        re.match(pattern, stripped)
        for pattern in patterns
    )

def segment_text_v2(text):
    """
    Line-level segmentation。

    1. 每一行先分類
    2. 相鄰相同類型合併成同一個 block
    3. 保留換行
    """

    lines = text.splitlines(keepends=True)

    blocks = []

    current_type = None
    current_lines = []

    def flush():
        nonlocal current_type
        nonlocal current_lines

        if not current_lines:
            return

        blocks.append(
            TextBlock(
                text="".join(current_lines),
                block_type=current_type
            )
        )

        current_type = None
        current_lines = []

    for line in lines:

        raw_type = classify_line(line)
        line_type = raw_type

        # ==========================================
        # Context-aware smoothing
        # ==========================================

        if (
            raw_type == BLOCK_TEXT
            and current_type == BLOCK_CODE
            and is_code_continuation(line)
        ):
            line_type = BLOCK_CODE

            print(
                f"[SMOOTH] TEXT -> CODE | "
                f"{line.rstrip()!r}"
            )

        debug_log(
            f"[LINE TYPE] "
            f"{line_type:<10} | "
            f"{line.rstrip()!r}"
        )

        # --------------------------
        # 空白行自己成為 boundary
        # --------------------------
        if line_type == BLOCK_BLANK:

            flush()

            blocks.append(
                TextBlock(
                    text=line,
                    block_type=BLOCK_BLANK
                )
            )

            continue

        # --------------------------
        # 第一行
        # --------------------------
        if current_type is None:

            current_type = line_type
            current_lines.append(line)

            continue

        # --------------------------
        # 與上一行同類型
        # --------------------------
        if line_type == current_type:

            current_lines.append(line)

            continue

        # --------------------------
        # 類型改變 → block boundary
        # --------------------------
        flush()

        current_type = line_type
        current_lines.append(line)

    flush()

    return blocks

def _is_reliable_field_match(match):
    """
    判斷偵測結果是否足以觸發整個 field 遮蔽。
    """

    start, end, label, score, source = match

    # 1. 不是敏感類型 -> 不遮
    if label not in FIELD_SENSITIVE_ENTITIES:
        return False

    # 2. Regex 命中 -> 直接接受
    if source == "regex":
        return True

    # 3. NLP 模型需要達到 threshold
    if score is not None and score >= FIELD_MIN_SCORE:
        return True

    return False


def _next_field_tag(mapping):
    """
    找下一個可用的 <SENSITIVE_FIELD_n>。

    例如 mapping 已經有：
        <SENSITIVE_FIELD_1>
        <SENSITIVE_FIELD_2>

    就回傳：
        <SENSITIVE_FIELD_3>
    """
    index = 1

    while f"<SENSITIVE_FIELD_{index}>" in mapping:
        index += 1

    return f"<SENSITIVE_FIELD_{index}>"

def _anonymize_single_field(field, mapping):
    """
    處理單一欄位。

    有可靠 PII：
        姓名：王小明
            ↓
        <SENSITIVE_FIELD_1>

    沒有 PII：
        興趣：打電動
            ↓
        興趣：打電動
    """

    # 空欄位不處理
    if not field or not field.strip():
        return field, False

    matches = handler_text.detect(field)

    # Debug
    debug_log(f"\n[FIELD] {field}")

    for start, end, label, score, source in matches:
        debug_log(
            f"  偵測到: '{field[start:end]}'"
            f" | label={label}"
            f" | score={score}"
            f" | source={source}"
        )

    # 過濾掉低信心模型結果
    reliable_matches = [
        m for m in matches
        if _is_reliable_field_match(m)
    ]

    if not reliable_matches:
        return field, False

    # 建立新的 field tag
    tag = _next_field_tag(mapping)

    # 注意：
    # mapping 要保存完整 field，
    # 這樣 deanonymize_text() 才能整欄還原。
    mapping[tag] = field

    return tag, True



def detect_field_format(text):
    """
    判斷 clipboard 文字的欄位格式。

    支援：
        TSV
        PIPE
        CSV
        SEMICOLON
        LINE
        UNKNOWN

    CSV 判斷：
    - 多行：每一行欄數一致且 > 1
    - 單行：至少 3 個欄位（2 個逗號）
    """
    print("\n===== FORMAT DEBUG =====")
    print("repr(text):", repr(text))
    print("contains TAB:", "\t" in text)
    print("TAB count:", text.count("\t"))
    print("========================")

    lines = [line for line in text.splitlines() if line.strip()]

    if not lines:
        return FIELD_FORMAT_UNKNOWN

    # ==================================================
    # 1. TSV / Excel
    # ==================================================

    if any("\t" in line for line in lines):
        return FIELD_FORMAT_TSV

    # ==================================================
    # 2. Pipe
    # ==================================================

    if any("|" in line for line in lines):
        return FIELD_FORMAT_PIPE

    # ==================================================
    # 3. CSV
    # ==================================================

    try:
        rows = list(
            csv.reader(
                io.StringIO(text)
            )
        )

        rows = [
            row for row in rows
            if row
        ]

        if rows:

            column_counts = [
                len(row)
                for row in rows
            ]

            # ------------------------------
            # 多行 CSV
            # ------------------------------

            if len(rows) >= 2:

                if (
                    min(column_counts) > 1
                    and len(set(column_counts)) == 1
                ):
                    return FIELD_FORMAT_CSV

            # ------------------------------
            # 單行 CSV
            # ------------------------------

            elif len(rows) == 1:

                # 至少三個欄位才視為 CSV
                #
                # 王小明,22,0912345678
                # -> 3 fields -> CSV
                #
                # Hello, my name is Allen
                # -> 2 fields -> 不算 CSV

                if len(rows[0]) >= 3:
                    return FIELD_FORMAT_CSV

    except csv.Error:
        pass

    # ==================================================
    # 4. Semicolon
    # ==================================================

    try:
        rows = list(
            csv.reader(
                io.StringIO(text),
                delimiter=";"
            )
        )

        rows = [
            row for row in rows
            if row
        ]

        if rows:

            column_counts = [
                len(row)
                for row in rows
            ]

            # 多行
            if len(rows) >= 2:

                if (
                    min(column_counts) > 1
                    and len(set(column_counts)) == 1
                ):
                    return FIELD_FORMAT_SEMICOLON

            # 單行
            elif len(rows) == 1:

                if len(rows[0]) >= 3:
                    return FIELD_FORMAT_SEMICOLON

    except csv.Error:
        pass

    # ==================================================
    # 5. 普通文字
    # ==================================================

    return FIELD_FORMAT_LINE

def _split_fields_by_format(line, field_format):
    """
    根據 detect_field_format() 的結果切割單一行。

    回傳：
        [
            field,
            separator,
            field,
            separator,
            ...
        ]

    separator 會保留，方便後面重新組合。
    """

    # ------------------------------------------
    # TSV / Excel
    # ------------------------------------------
    if field_format == FIELD_FORMAT_TSV:
        return re.split(r"(\t+)", line)

    # ------------------------------------------
    # Pipe
    # ------------------------------------------
    if field_format == FIELD_FORMAT_PIPE:
        return re.split(r"(\s*\|\s*)", line)

    # ------------------------------------------
    # CSV
    # ------------------------------------------
    if field_format == FIELD_FORMAT_CSV:
        try:
            fields = next(csv.reader([line]))

            parts = []

            for i, field in enumerate(fields):
                if i > 0:
                    parts.append(",")

                parts.append(field)

            return parts

        except (csv.Error, StopIteration):
            return [line]

    # ------------------------------------------
    # Semicolon
    # ------------------------------------------
    if field_format == FIELD_FORMAT_SEMICOLON:
        try:
            fields = next(
                csv.reader(
                    [line],
                    delimiter=";"
                )
            )

            parts = []

            for i, field in enumerate(fields):
                if i > 0:
                    parts.append(";")

                parts.append(field)

            return parts

        except (csv.Error, StopIteration):
            return [line]

    # ------------------------------------------
    # LINE / UNKNOWN
    # ------------------------------------------
    # 普通文字完全不按照 , ; | 等符號切割
    return [line]

def _anonymize_csv(text, mapping, delimiter=","):
    """
    使用 Python csv 模組處理 CSV，
    避免 quoted field 裡面的逗號被錯誤切割。
    """

    input_stream = io.StringIO(text)

    reader = csv.reader(
        input_stream,
        delimiter=delimiter
    )

    output_stream = io.StringIO(
        newline=""
    )

    writer = csv.writer(
        output_stream,
        delimiter=delimiter,
        lineterminator="\n"
    )

    changed = False

    for row in reader:

        new_row = []

        for field in row:

            masked_field, field_changed = (
                _anonymize_single_field(
                    field,
                    mapping
                )
            )

            new_row.append(masked_field)

            if field_changed:
                changed = True

        writer.writerow(new_row)

    if not changed:
        return None

    return output_stream.getvalue()

def anonymize_fields(text, mapping, is_code=None):

    print("\n========== ANONYMIZE_FIELDS ==========")

    field_format = detect_field_format(text)

    print(f"[FIELD FORMAT] {field_format}")
    print(f"[INPUT is_code] {is_code}")

    # 只有 LINE / UNKNOWN 才做 code detection
    if field_format in (
        FIELD_FORMAT_LINE,
        FIELD_FORMAT_UNKNOWN
    ):

        if is_code is None:
            is_code = is_code_text(text)

        print(f"[CODE DETECTION] {is_code}")

        if is_code:
            print("[ROUTE] CODE -> anonymize_text()")

            return anonymize_text(
                text,
                mapping,
                is_code=True
            )

    # CSV
    if field_format == FIELD_FORMAT_CSV:

        print("[ROUTE] CSV -> _anonymize_csv()")

        result = _anonymize_csv(
            text,
            mapping,
            delimiter=","
        )

        if result is not None:
            save_map(mapping)

        return result

    # Semicolon
    if field_format == FIELD_FORMAT_SEMICOLON:

        print("[ROUTE] SEMICOLON -> _anonymize_csv()")

        result = _anonymize_csv(
            text,
            mapping,
            delimiter=";"
        )

        if result is not None:
            save_map(mapping)

        return result

    print(
        f"[ROUTE] {field_format} "
        "-> normal field processing"
    )

    # ==========================================
    # 以下保持你原本 TSV / PIPE / LINE 的程式
    # ==========================================

    lines = text.splitlines(keepends=True)

    output = []
    changed = False

    for line in lines:

        if line.endswith("\r\n"):
            content = line[:-2]
            newline = "\r\n"

        elif line.endswith("\n"):
            content = line[:-1]
            newline = "\n"

        elif line.endswith("\r"):
            content = line[:-1]
            newline = "\r"

        else:
            content = line
            newline = ""

        if not content.strip():
            output.append(line)
            continue

        parts = _split_fields_by_format(
            content,
            field_format
        )

        new_parts = []

        for i, part in enumerate(parts):

            if i % 2 == 1:
                new_parts.append(part)
                continue

            masked_field, field_changed = (
                _anonymize_single_field(
                    part,
                    mapping
                )
            )

            new_parts.append(masked_field)

            if field_changed:
                changed = True

        output.append(
            "".join(new_parts) + newline
        )

    if not changed:
        return None

    save_map(mapping)

    return "".join(output)

def deanonymize_text(text, mapping):
    return TAG_RE.sub(lambda m: mapping.get(m.group(0), m.group(0)), text)


# ---------- 視窗/剪貼簿讀取工具 ----------
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
    """剪貼簿常常會被別的程式(甚至是我們自己剛模擬完的 Ctrl+C)短暫佔用，
    這時 OpenClipboard 會直接丟例外；與其讓整個 callback 炸掉，重試個幾次通常就過了。"""
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
    copied_files = []
    try:
        win32clipboard.OpenClipboard()
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
    if copied_files:
        return "FILES", copied_files
    return "TEXT", _paste_with_retry()


def send_key_combination(vk_code):
    """模擬按下 Ctrl + 指定按鍵。只有模式 A 會用到 (模式 B 的快速鍵本身就是原生按鍵，
    不需要、也不能再模擬一次，見檔頭「模式 B」說明)。"""
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def copy_files_to_clipboard(paths):
    """把檔案路徑清單設進剪貼簿，效果等同在檔案總管『複製』這些檔案，之後貼上會貼出這些檔案"""
    file_list = "\0".join(os.path.abspath(p) for p in paths) + "\0\0"
    dropfiles_header = struct.pack("Iiiii", 20, 0, 0, 0, 1)  # 20=結構大小, fWide=1 表示用寬字元
    data = dropfiles_header + file_list.encode("utf-16le")
    win32clipboard.OpenClipboard()
    win32clipboard.EmptyClipboard()
    win32clipboard.SetClipboardData(win32clipboard.CF_HDROP, data)
    win32clipboard.CloseClipboard()


# ---------- 分類 + 分派 ----------
def dispatch_files(paths, mapping) -> list:
    """回傳實際產生的去識別化檔案路徑清單(順序對應輸入的 paths，跳過的/沒偵測到內容的不會出現)。
    不會動剪貼簿——剪貼簿只在「模式 A 按下 HOTKEY_PASTE」或「模式 B 複製當下」才會被寫入，
    這兩種情況都是呼叫端(on_copy)決定，這支函式本身不碰剪貼簿。"""
    produced = []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()

        if ext in MEDIA_EXTENSIONS:
            print(f"[分類] {path} -> 圖片/PDF，交給 handler_media 處理 (OCR + 正則 + Presidio)")
            out_path = handler_media.mask_file(path)
            produced.append(out_path)
            print(f"[完成] 已產生去識別化檔案：{out_path}")

        elif ext in CODE_EXTENSIONS or ext in TEXT_FILE_EXTENSIONS:
            is_code = ext in CODE_EXTENSIONS
            kind = "程式碼檔案" if is_code else "純文字檔案"
            print(f"[分類] {path} -> {kind}(依副檔名判斷)，讀取內容後套用規則")
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError as e:
                print(f"[錯誤] 讀取失敗：{e}")
                continue

            masked = anonymize_text(content, mapping, is_code=is_code)
            if masked:
                out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(masked)
                produced.append(out_path)
                print(f"[完成] 已產生去識別化檔案：{out_path}")
            else:
                print("[結果] 未偵測到需要遮蔽的內容")

        else:
            print(f"[跳過] {path}：副檔名不在 CODE_EXTENSIONS / MEDIA_EXTENSIONS / TEXT_FILE_EXTENSIONS 設定內")

    return produced


def on_copy(mapping):
    """
    模式 A：只算出覆蓋結果存進 _override，不動剪貼簿，要等 HOTKEY_PASTE 才會真正寫入。
    模式 B：直接把覆蓋結果寫進剪貼簿，原生 Ctrl+V 自然就會貼出新版本，不用另外處理貼上。
    這兩種模式共用同一套偵測/分類/遮蔽邏輯，差別只在「複製完之後，結果要暫存還是直接覆寫」。
    """
    global _override, _last_copy
    print("\n" + "=" * 50)
    print("【偵測到 Ctrl+C】" if DIRECT_MODE else "【偵測到複製快速鍵】")

    if DIRECT_MODE:
        time.sleep(0.15)  # 模式 B：快速鍵就是原生 Ctrl+C，等它自己把資料寫進剪貼簿即可，不用模擬按鍵
    else:
        send_key_combination(ord('C'))  # 模式 A：快速鍵不是原生複製鍵，要自己模擬一次才能拿到剪貼簿內容
        time.sleep(0.15)

    title, proc = get_active_window_info()
    content_type, content = get_clipboard_content()
    print(f"來源程式: {proc} | 視窗標題: {title}")

    if content_type == "FILES":
        print(f"複製類型: 檔案 (共 {len(content)} 個)")
        out_paths = dispatch_files(content, mapping)
        if out_paths:
            if DIRECT_MODE:
                copy_files_to_clipboard(out_paths)  # 直接覆寫：舊的檔案清單被新的去識別化檔案取代
                _last_copy = {"kind": "files", "original": content, "masked": out_paths}
                print(f"[完成] 剪貼簿已直接覆寫成去識別化後的 {len(out_paths)} 個檔案，直接按原生 Ctrl+V 貼上即可")
            else:
                _override = {"kind": "files", "content": out_paths, "original": content}
                print(f"[就緒] 覆蓋結果已產生 (共 {len(out_paths)} 個檔案)，按 {HOTKEY_PASTE} 才會貼上；剪貼簿本身不受影響")
        else:
            _override = {"kind": None, "content": None, "original": None}
            _last_copy = {"kind": None, "original": None, "masked": None}
            print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原生複製的內容不變" if DIRECT_MODE else ""))
    else:
        if TAG_RE.search(content):
            #已經有 tag -> 還原
            print("[分類] 內容含標籤 -> 還原模式")
            result = deanonymize_text(content, mapping)
        else:
            # 這裡沒有檔名可判斷，anonymize_text 內部才會用 is_code_text() 猜測
            if MASK_MODE == "field":
                result = anonymize_segmented_text(content, mapping)
            else:
                result = anonymize_text(content, mapping)
        if result and result != content:
            if DIRECT_MODE:
                pyperclip.copy(result)  # 直接覆寫：舊的原始文字被新的遮蔽/還原後文字取代
                _last_copy = {"kind": "text", "original": content, "masked": result}
                print("[完成] 剪貼簿已直接覆寫成遮蔽/還原後的內容，直接按原生 Ctrl+V 貼上即可")
            else:
                _override = {"kind": "text", "content": result, "original": content}
                print(f"[就緒] 覆蓋結果已產生，按 {HOTKEY_PASTE} 才會貼上；剪貼簿本身不受影響")
        else:
            _override = {"kind": None, "content": None, "original": None}
            _last_copy = {"kind": None, "original": None, "masked": None}
            print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原生複製的內容不變" if DIRECT_MODE else ""))
    print("=" * 50)


def on_paste(mapping):
    """只有模式 A 會註冊/用到這個函式 (模式 B 不會把它加進 start_listener 的 hotkeys，
    也不需要——原生 Ctrl+V 自己就會貼出 on_copy 已經直接覆寫進剪貼簿的內容)。"""
    print("\n" + "=" * 50)
    print("【偵測到貼上快速鍵】")
    title, proc = get_active_window_info()

    if _override["kind"] == "text":
        pyperclip.copy(_override["content"])
        time.sleep(0.05)
        send_key_combination(ord('V'))
        time.sleep(0.05)
        pyperclip.copy(_override["original"])  # 貼完立刻還原剪貼簿，覆蓋結果不會留在系統剪貼簿上
        print(f"[完成] 已貼上覆蓋結果(文字)給 {proc} | {title}，剪貼簿已還原為原始內容")
    elif _override["kind"] == "files":
        copy_files_to_clipboard(_override["content"])
        time.sleep(0.05)
        send_key_combination(ord('V'))
        time.sleep(0.05)
        copy_files_to_clipboard(_override["original"])  # 同上，貼完馬上還原
        print(f"[完成] 已貼上覆蓋結果(檔案)給 {proc} | {title}，剪貼簿已還原為原始內容")
    else:
        print(f"[提示] 目前沒有覆蓋結果，直接貼上剪貼簿目前內容給 {proc} | {title}")
        send_key_combination(ord('V'))
    print("=" * 50)


_busy = False  # 防止「模擬按鍵」又觸發同一組快速鍵造成無限遞迴 (模式 A 快速鍵跟系統原生複製/貼上鍵相同時必須有這層防護；
                # 模式 B 保守起見也保留這層防護，即使理論上不會自己觸發自己)


def _guarded(func):
    def wrapper():
        global _busy
        if _busy:
            return  # 正在處理上一次觸發，忽略這次(通常就是我們自己模擬按鍵造成的重複觸發)
        _busy = True
        try:
            func()
        finally:
            _busy = False
    return wrapper


def create_listener():
    mapping = load_map()

    hotkeys = {
        HOTKEY_COPY:
            _guarded(lambda: on_copy(mapping))
    }

    if not DIRECT_MODE:
        hotkeys[HOTKEY_PASTE] = _guarded(
            lambda: on_paste(mapping)
        )

    listener = keyboard.GlobalHotKeys(hotkeys)
    listener.start()

    return listener

def start_listener():
    listener = create_listener()

    print(
        "【監聽啟動】"
        + (
            "(模式 B：Ctrl+C 直接模式)"
            if DIRECT_MODE
            else "(模式 A：專屬快速鍵)"
        )
    )

    try:
        while listener.running:
            time.sleep(0.2)

    except KeyboardInterrupt:
        print("\n[結束] 收到中斷信號，正在關閉監聽...")

    finally:
        listener.stop()
        cleanup_map_files()

if __name__ == "__main__":
    start_listener()