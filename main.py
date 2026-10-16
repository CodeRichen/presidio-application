"""
剪貼簿敏感資訊防護 - 主程式 (全域快速鍵)
====================================================
靠 HOTKEY_PASTE 是否有值自動切換兩種模式：

  模式 A・專屬快速鍵 (HOTKEY_PASTE 有值)：
      複製時只算出覆蓋結果並暫存，不動剪貼簿；按專屬貼上鍵才暫時寫入剪貼簿、
      模擬 Ctrl+V、貼完立刻還原剪貼簿。原生 Ctrl+C / Ctrl+V 不受影響。

  模式 B・Ctrl+C 直接當快速鍵 (HOTKEY_PASTE = None)：
      原生 Ctrl+C 先把資料寫進剪貼簿，我們讀出來處理後直接覆寫剪貼簿；
      之後原生 Ctrl+V 就會貼出處理後版本，不註冊任何貼上快速鍵。
      副作用：終端機按 Ctrl+C 也會觸發 on_copy；停不掉腳本時直接關閉終端機視窗。

架構：
    core.py  <- 共用核心 (遮蔽/還原、分派到 code/text/media/field/csv、剪貼簿工具)
    main.py  <- 這支：只負責快速鍵與模式 A/B 流程
    ui.py    <- 圖形介面，跟這支互相獨立，同樣只呼叫 core

安裝：pip install pywin32 psutil pyperclip pynput
"""
import time

import win32api
import win32con
from pynput import keyboard

import core

# ========================= 設定區 =========================
HOTKEY_COPY = '<ctrl>+<alt>+c'
HOTKEY_PASTE = '<ctrl>+<alt>+v'
# 切換成模式 B：
#     HOTKEY_COPY = '<ctrl>+c'
#     HOTKEY_PASTE = None
# ==========================================================

DIRECT_MODE = not HOTKEY_PASTE

# 模式 A：覆蓋結果暫存 (不寫進系統剪貼簿)
_override = {"kind": None, "content": None, "original": None}
# 模式 B：純紀錄，方便 debug
_last_copy = {"kind": None, "original": None, "masked": None}


def send_key_combination(vk_code):
    """模擬 Ctrl + 指定按鍵 (只有模式 A 會用到)"""
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


def _reset_state():
    global _override, _last_copy
    _override = {"kind": None, "content": None, "original": None}
    _last_copy = {"kind": None, "original": None, "masked": None}


def on_copy(mapping):
    global _override, _last_copy
    print("\n" + "=" * 50)
    print("【偵測到 Ctrl+C】" if DIRECT_MODE else "【偵測到複製快速鍵】")

    if DIRECT_MODE:
        time.sleep(0.15)  # 等原生複製寫完剪貼簿
    else:
        send_key_combination(ord('C'))
        time.sleep(0.15)

    title, proc = core.get_active_window_info()
    content_type, content = core.get_clipboard_content()
    print(f"來源程式: {proc} | 視窗標題: {title}")

    if content_type == "FILES":
        print(f"複製類型: 檔案 (共 {len(content)} 個)")
        out_paths = core.dispatch_files(content, mapping)
        if out_paths:
            if DIRECT_MODE:
                core.copy_files_to_clipboard(out_paths)
                _last_copy = {"kind": "files", "original": content, "masked": out_paths}
                print(f"[完成] 剪貼簿已直接覆寫成去識別化後的 {len(out_paths)} 個檔案，直接按原生 Ctrl+V 貼上即可")
            else:
                _override = {"kind": "files", "content": out_paths, "original": content}
                print(f"[就緒] 覆蓋結果已產生 (共 {len(out_paths)} 個檔案)，按 {HOTKEY_PASTE} 才會貼上")
        else:
            _reset_state()
            print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原樣" if DIRECT_MODE else ""))
    else:
        result = core.process_text(content, mapping, action="auto")
        if result:
            if DIRECT_MODE:
                core.copy_text_to_clipboard(result)
                _last_copy = {"kind": "text", "original": content, "masked": result}
                print("[完成] 剪貼簿已直接覆寫成處理後的內容，直接按原生 Ctrl+V 貼上即可")
            else:
                _override = {"kind": "text", "content": result, "original": content}
                print(f"[就緒] 覆蓋結果已產生，按 {HOTKEY_PASTE} 才會貼上")
        else:
            _reset_state()
            print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原樣" if DIRECT_MODE else ""))
    print("=" * 50)


def on_paste(mapping):
    """只有模式 A 會註冊這個函式"""
    print("\n" + "=" * 50)
    print("【偵測到貼上快速鍵】")
    title, proc = core.get_active_window_info()

    if _override["kind"] == "text":
        core.copy_text_to_clipboard(_override["content"])
        time.sleep(0.05)
        send_key_combination(ord('V'))
        time.sleep(0.05)
        core.copy_text_to_clipboard(_override["original"])
        print(f"[完成] 已貼上覆蓋結果(文字)給 {proc} | {title}，剪貼簿已還原")
    elif _override["kind"] == "files":
        core.copy_files_to_clipboard(_override["content"])
        time.sleep(0.05)
        send_key_combination(ord('V'))
        time.sleep(0.05)
        core.copy_files_to_clipboard(_override["original"])
        print(f"[完成] 已貼上覆蓋結果(檔案)給 {proc} | {title}，剪貼簿已還原")
    else:
        print(f"[提示] 目前沒有覆蓋結果，直接貼上剪貼簿目前內容給 {proc} | {title}")
        send_key_combination(ord('V'))
    print("=" * 50)


_busy = False  # 防止模擬按鍵又觸發同一組快速鍵造成遞迴


def _guarded(func):
    def wrapper():
        global _busy
        if _busy:
            return
        _busy = True
        try:
            func()
        finally:
            _busy = False
    return wrapper


def start_listener():
    mapping = core.load_map()
    print("【監聽啟動】" + ("(模式 B：Ctrl+C 直接模式)" if DIRECT_MODE else "(模式 A：專屬快速鍵)"))
    if DIRECT_MODE:
        print(f"  - {HOTKEY_COPY} : 直接複製，自動判斷遮蔽/還原並覆寫剪貼簿")
        print("  - Ctrl+V (原生) : 貼出處理後的版本")
        print("提醒：終端機按 Ctrl+C 若無法中止腳本，直接關閉終端機視窗即可\n")
    else:
        print(f"  - {HOTKEY_COPY} : 有標籤就還原、沒標籤就遮蔽（只產生覆蓋結果）")
        print(f"  - {HOTKEY_PASTE} : 貼上覆蓋結果，貼完還原剪貼簿")
        print("  - Ctrl+V : 系統原生貼上，不受影響")
        print("在終端機視窗按 Ctrl+C 可停止腳本\n")

    hotkeys = {HOTKEY_COPY: _guarded(lambda: on_copy(mapping))}
    if not DIRECT_MODE:
        hotkeys[HOTKEY_PASTE] = _guarded(lambda: on_paste(mapping))

    listener = keyboard.GlobalHotKeys(hotkeys)
    listener.start()
    try:
        while listener.running:
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n[結束] 收到中斷信號，正在關閉監聽...")
    finally:
        listener.stop()
        core.cleanup_map_files()


if __name__ == "__main__":
    start_listener()