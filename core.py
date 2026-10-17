"""
core.py - main.py / ui.py / hotkey_service.py 共用的核心
====================================================
放：設定、對照表、標籤遮蔽/還原、重疊處理、檔案分派 (code/text/media/field/csv)、
    剪貼簿讀寫工具、模型預熱。
log 一律用 print；ui.py 會把 sys.stdout 接到畫面上的記錄區。
"""
import os
import io
import re
import sys
import time
import struct
import contextlib

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

MAP_FILE = "./presidio/real/clipboard_map.txt"


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
    """結束時刪除對照表，不留明碼敏感資訊在硬碟上"""
    for f in (MAP_FILE, handler_media.DEFAULT_MAP_FILE):
        try:
            os.remove(f)
            print(f"[清理] 已刪除 {f}")
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[警告] 刪除 {f} 失敗：{e}")


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
    """優先序：正則 > (依整段語言) 吻合的模型 > 不吻合的模型；同層範圍長者優先，再比信心分數。"""
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
def anonymize_text(text, mapping, is_code=None, save=True):
    """is_code=None 才會用分類器猜；檔案處理一律明確傳入。"""
    if is_code is None:
        is_code = is_code_text(text)
    matches = list(handler_code.detect(text) if is_code else handler_text.detect(text))
    if handler_field is not None:
        matches += list(handler_field.detect(text))
    if not matches:
        return None

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

    if save:
        save_map(mapping)
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


def make_tagger(mapping):
    """回傳 tag_value(etype, val)：同值同標籤，寫入共用 mapping (給 handler_csv 用)。"""
    state = {"n": -1, "reverse": {}, "counters": {}}

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

    return tag_value


# ---------- 檔案分派 ----------
def dispatch_files(paths, mapping) -> list:
    """回傳實際產生的去識別化檔案路徑。不碰剪貼簿。"""
    produced = []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()

        if ext in MEDIA_EXTENSIONS:
            print(f"[分類] {path} -> 圖片/PDF，交給 handler_media 處理")
            try:
                out_path = handler_media.mask_file(path)
            except Exception as e:
                print(f"[錯誤] 圖片/PDF 處理失敗：{e}")
                continue
            produced.append(out_path)
            print(f"[完成] 已產生去識別化檔案：{out_path}")

        elif ext in TABLE_EXTENSIONS:
            print(f"[分類] {path} -> 表格檔案，交給 handler_csv 處理 (欄位內有個資則整欄遮蔽)")

            def detect_cell(text):
                # 只用來判斷這格有沒有個資/什麼類型：用暫時的空對照表，不污染共用對照表；
                # 逐格偵測訊息太多，順便靜音
                with contextlib.redirect_stdout(io.StringIO()):
                    return anonymize_text(text, {}, is_code=False, save=False)

            try:
                out_path = handler_csv.mask_file(path, detect_cell, make_tagger(mapping))
            except Exception as e:
                print(f"[錯誤] 表格處理失敗：{e}")
                continue
            if out_path:
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
            print(f"[跳過] {path}：副檔名不在支援清單內")

    return produced


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
    """啟動時先跑一次，讓 Presidio / NER 模型提前載入，第一次複製才不會卡幾秒"""
    sample = "王小明的 Email 是 test@example.com，電話 0912-345-678，API_KEY=sk-1234567890abcdef"
    print("[預熱] 載入模型中…")
    t0 = time.time()
    errors = []
    with contextlib.redirect_stdout(io.StringIO()):  # 預熱過程不印偵測細節
        for is_code in (False, True):
            try:
                anonymize_text(sample, {}, is_code=is_code, save=False)
            except Exception as e:
                errors.append(f"[警告] 預熱失敗 (is_code={is_code})：{e}")
    for msg in errors:
        print(msg)
    print(f"[預熱] 完成，耗時 {time.time() - t0:.1f} 秒")