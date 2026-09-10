"""
剪貼簿敏感資訊防護 - 主程式
====================================================
架構：
    main.py          <- 這支：監聽快速鍵、判斷複製到的是「檔案」還是「文字」、是「程式碼」還是
                          「一般文字」，決定要交給哪個模組處理，並負責標籤替換與剪貼簿讀寫
    handler_text.py  <- 一般文字的敏感資訊規則 (含 Presidio)
    handler_code.py  <- 程式碼的敏感資訊規則 (API Key/JWT/路徑 + 借用 handler_text 的個資規則)
    handler_media.py <- 圖片/PDF 的偵測(OCR)+塗黑+還原

想改快速鍵、副檔名分類、要用哪套方式判斷「這段文字是不是程式碼」，都在下面「設定區」改，
不用動下面的邏輯。

安裝：pip install pywin32 psutil pyperclip pynput
      (另外依 handler_text/handler_code/handler_media 檔頭的說明安裝 presidio / tesseract / pymupdf)
"""
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

# ========================= 設定區：想改快速鍵/副檔名/分類方式都在這裡改 =========================
HOTKEY_COPY = '<ctrl>+c'
HOTKEY_PASTE = '<ctrl>+v'

CODE_EXTENSIONS = {".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".go", ".rs",
                    ".php", ".rb", ".sh", ".sql", ".json", ".yml", ".yaml"}
MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp", ".pdf"}
TEXT_FILE_EXTENSIONS = {".txt", ".md", ".csv", ".log"}   # 純文字檔：讀出內容、按一般文字規則遮蔽

MAP_FILE = "./presidio/clipboard_map.txt"


def heuristic_is_code(text: str) -> bool:
    """快速判斷一段文字像不像程式碼：命中越多程式碼特徵，越可能是程式碼。門檻(>=3)可自行調整"""
    signals = re.findall(
        r"[{};]|=>|\bdef \b|\bclass \b|\bimport \b|#include|\bfunction\b|\bconst \b|\bvar \b|\breturn \b",
        text,
    )
    return len(signals) >= 3


# 分類模型註冊表：想加自己的判斷方式(例如真的呼叫一個 LLM 或雲端 API)，
# 寫一個 func(text) -> bool，往這裡加一行，再把 TEXT_CLASSIFY_MODE 改成該名稱即可，其他都不用動。
CLASSIFY_MODELS = {
    "heuristic": heuristic_is_code,
    # "qwen": your_qwen_is_code_function,
}
TEXT_CLASSIFY_MODE = "heuristic"
# ================================================================================================

TAG_RE = re.compile(r"<([A-Z_]+)_(\d+)>")


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
    with open(MAP_FILE, "w", encoding="utf-8") as f:
        for tag, val in mapping.items():
            f.write(f"{tag}\t{val}\n")


def merge_overlaps(matches):
    matches = sorted(matches, key=lambda m: (m[0], -(m[1] - m[0])))
    merged, last_end = [], -1
    for start, end, label in matches:
        if start >= last_end:
            merged.append((start, end, label))
            last_end = end
    return merged


def anonymize_text(text, mapping):
    is_code = is_code_text(text)
    matches = handler_code.detect(text) if is_code else handler_text.detect(text)
    if not matches:
        return None
    matches = merge_overlaps(matches)

    reverse = {v: k for k, v in mapping.items()}
    counters = {}
    for tag in mapping:
        m = TAG_RE.match(tag)
        if m:
            t, i = m.groups()
            counters[t] = max(counters.get(t, 0), int(i))

    out = text
    for start, end, etype in sorted(matches, key=lambda m: m[0], reverse=True):
        val = text[start:end]
        tag = reverse.get(val)
        if not tag:
            counters[etype] = counters.get(etype, 0) + 1
            tag = f"<{etype}_{counters[etype]}>"
            mapping[tag] = val
            reverse[val] = tag
        kind = "程式碼" if is_code else "一般文字"
        print(f"[遮蔽] {val!r} -> {tag}  (理由: {etype}, 判斷為{kind})")
        out = out[:start] + tag + out[end:]

    save_map(mapping)
    return out


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
    return "TEXT", pyperclip.paste()


def send_key_combination(vk_code):
    """模擬按下 Ctrl + 指定按鍵"""
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
def dispatch_files(paths, mapping):
    for path in paths:
        ext = os.path.splitext(path)[1].lower()

        if ext in MEDIA_EXTENSIONS:
            print(f"[分類] {path} -> 圖片/PDF，交給 handler_media 處理 (OCR + 正則 + Presidio)")
            out_path = handler_media.mask_file(path)
            copy_files_to_clipboard([out_path])
            print(f"[完成] 已產生去識別化檔案並放回剪貼簿：{out_path}")

        elif ext in CODE_EXTENSIONS or ext in TEXT_FILE_EXTENSIONS:
            kind = "程式碼檔案" if ext in CODE_EXTENSIONS else "純文字檔案"
            print(f"[分類] {path} -> {kind}，讀取內容後套用規則")
            try:
                with open(path, encoding="utf-8", errors="ignore") as f:
                    content = f.read()
            except OSError as e:
                print(f"[錯誤] 讀取失敗：{e}")
                continue

            masked = anonymize_text(content, mapping)
            if masked:
                out_path = f"{os.path.splitext(path)[0]}_masked{ext}"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(masked)
                copy_files_to_clipboard([out_path])
                print(f"[完成] 已產生去識別化檔案並放回剪貼簿：{out_path}")
            else:
                print("[結果] 未偵測到需要遮蔽的內容，剪貼簿維持原檔案")

        else:
            print(f"[跳過] {path}：副檔名不在 CODE_EXTENSIONS / MEDIA_EXTENSIONS / TEXT_FILE_EXTENSIONS 設定內")


def on_copy(mapping):
    print("\n" + "=" * 50)
    print("【偵測到複製快速鍵】")
    send_key_combination(ord('C'))
    time.sleep(0.15)  # 等待剪貼簿寫入

    title, proc = get_active_window_info()
    content_type, content = get_clipboard_content()
    print(f"來源程式: {proc} | 視窗標題: {title}")

    if content_type == "FILES":
        print(f"複製類型: 檔案 (共 {len(content)} 個)")
        dispatch_files(content, mapping)
    else:
        if TAG_RE.search(content):
            print("[分類] 內容含標籤 -> 還原模式")
            result = deanonymize_text(content, mapping)
        else:
            result = anonymize_text(content, mapping)
        if result and result != content:
            pyperclip.copy(result)
            print("[完成] 已更新剪貼簿內容")
        else:
            print("[結果] 未偵測到需要遮蔽的內容，剪貼簿維持原樣")
    print("=" * 50)


def on_paste(mapping):
    print("\n" + "=" * 50)
    print("【偵測到貼上快速鍵】")
    title, proc = get_active_window_info()
    content_type, content = get_clipboard_content()

    is_tagged = content_type == "TEXT" and bool(TAG_RE.search(content))
    if is_tagged:
        pyperclip.copy(deanonymize_text(content, mapping))
        print("[還原] 已暫時把真實內容放回剪貼簿供這次貼上使用")

    send_key_combination(ord('V'))

    if is_tagged:
        time.sleep(0.1)
        pyperclip.copy(content)  # 貼上後改回遮蔽版，避免真實內容留在剪貼簿裡
        print("[還原完畢] 剪貼簿已改回遮蔽版本")

    print(f"目標程式: {proc} | 視窗標題: {title}")
    print("=" * 50)


def start_listener():
    mapping = load_map()
    print("【監聽啟動】")
    print(f"  - {HOTKEY_COPY} : 複製並自動去識別化")
    print(f"  - {HOTKEY_PASTE} : 貼上 (若剪貼簿是標籤，會還原成真實內容貼上，貼完再改回標籤)")
    print("按 Ctrl+C 可停止腳本\n")

    hotkeys = {
        HOTKEY_COPY: lambda: on_copy(mapping),
        HOTKEY_PASTE: lambda: on_paste(mapping),
    }
    listener = keyboard.GlobalHotKeys(hotkeys)
    listener.start()
    try:
        # 用短暫輪詢取代 listener.join()：join() 會整個卡住主執行緒，
        # 導致 Ctrl+C 的 KeyboardInterrupt 傳不進來、程式關不掉。
        while listener.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[結束] 收到 Ctrl+C，正在關閉監聽...")
    finally:
        listener.stop()


if __name__ == "__main__":
    start_listener()
