"""
hotkey_service.py - 全域快速鍵服務 (main.py 與 ui.py 共用)
====================================================
把原本寫在 main.py 的快速鍵流程獨立出來，讓 main.py 與 ui.py 都能用，
而且兩者仍然互相獨立 (誰也不 import 誰，只 import 這支與 core)。

快速鍵設定存在同資料夾的 hotkeys.json：
    {"copy": "<ctrl>+<alt>+c", "paste": "<ctrl>+<alt>+v"}
  - paste 有值 -> 模式 A：專屬快速鍵。複製只算出覆蓋結果並暫存、不動剪貼簿；
                 按貼上鍵才暫時寫入剪貼簿、模擬 Ctrl+V、貼完立刻還原剪貼簿。
  - paste 為空 -> 模式 B：copy 通常設成 "<ctrl>+c"。原生複製寫完剪貼簿後，
                 我們讀出來處理並直接覆寫剪貼簿；原生 Ctrl+V 自然貼出處理後版本。
                 注意：處理完成前就按 Ctrl+V，貼出的會是原始內容 (原生貼上無法攔截)。
                 副作用：終端機按 Ctrl+C 也會觸發 on_copy。

ui.py 可以在執行中更改快速鍵：save_config() 後呼叫 service.start(新設定) 即重新綁定。
main.py 啟動時讀取 hotkeys.json (沒有檔案就用預設值)。
"""
import os
import json
import time
import threading

import win32api
import win32con
from pynput import keyboard

import core

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotkeys.json")
DEFAULT_CONFIG = {"copy": "<ctrl>+<alt>+c", "paste": "<ctrl>+<alt>+v"}

# 處理剪貼簿 / 對照表的工作一律序列化 (快速鍵與 ui 的背景工作共用)，避免同時改 mapping 或搶剪貼簿
WORK_LOCK = threading.RLock()


# ---------------- 設定檔 ----------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("copy"):
            cfg["copy"] = data["copy"]
        if "paste" in data:
            cfg["paste"] = data["paste"] or None   # 空字串 / null = 模式 B
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[警告] 讀取 {CONFIG_FILE} 失敗，改用預設快速鍵：{e}")
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"copy": cfg.get("copy"), "paste": cfg.get("paste")}, f, ensure_ascii=False, indent=2)


def validate_config(cfg):
    """回傳錯誤訊息字串；沒問題回傳 None"""
    copy, paste = cfg.get("copy"), cfg.get("paste")
    if not copy:
        return "複製快速鍵不可為空"
    for name, hk in (("複製", copy), ("貼上", paste)):
        if hk:
            try:
                keyboard.HotKey.parse(hk)
            except Exception as e:
                return f"{name}快速鍵格式錯誤：{hk} ({e})"
    if paste and copy == paste:
        return "複製與貼上快速鍵不可相同"
    return None


def send_key_combination(vk_code):
    """模擬 Ctrl + 指定按鍵 (只有模式 A 會用到)"""
    win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    win32api.keybd_event(vk_code, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(vk_code, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)


class HotkeyService:
    def __init__(self, mapping, config=None):
        self.mapping = mapping
        self.config = dict(config) if config else load_config()
        self._listener = None
        self._direct = not self.config.get("paste")

        self._override = {"kind": None, "content": None, "original": None}   # 模式 A 暫存
        self._last_copy = {"kind": None, "original": None, "masked": None}   # 模式 B 紀錄

        self._state_lock = threading.Lock()
        self._running = {"copy": False, "paste": False}

    # ---------------- 狀態 ----------------
    @property
    def running(self):
        return self._listener is not None and self._listener.running

    @property
    def direct_mode(self):
        return self._direct

    def _reset_state(self):
        self._override = {"kind": None, "content": None, "original": None}
        self._last_copy = {"kind": None, "original": None, "masked": None}

    # ---------------- 啟動 / 停止 ----------------
    def start(self, config=None):
        """啟動 (或用新設定重新啟動) 全域快速鍵。設定不合法會丟 ValueError。"""
        if config is not None:
            self.config = dict(config)
        err = validate_config(self.config)
        if err:
            raise ValueError(err)
        if self._listener is not None:
            self.stop()

        self._direct = not self.config.get("paste")
        self._reset_state()
        hotkeys = {self.config["copy"]: self._guarded("copy", self.on_copy)}
        if not self._direct:
            hotkeys[self.config["paste"]] = self._guarded("paste", self.on_paste)

        listener = keyboard.GlobalHotKeys(hotkeys)   # 格式錯誤會在這裡丟例外
        listener.start()
        self._listener = listener

        print("【監聽啟動】" + ("(模式 B：Ctrl+C 直接模式)" if self._direct else "(模式 A：專屬快速鍵)"))
        if self._direct:
            print(f"  - {self.config['copy']} : 直接複製，自動判斷遮蔽/還原並覆寫剪貼簿")
            print("  - Ctrl+V (原生) : 貼出處理後的版本")
        else:
            print(f"  - {self.config['copy']} : 有標籤就還原、沒標籤就遮蔽（只產生覆蓋結果）")
            print(f"  - {self.config['paste']} : 貼上覆蓋結果，貼完還原剪貼簿")
            print("  - Ctrl+V : 系統原生貼上，不受影響")

    def stop(self):
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
            print("[停止] 全域快速鍵已停用")

    # ---------------- callback 包裝 ----------------
    def _guarded(self, name, func):
        """callback 立刻返回 (不卡住鍵盤 hook)，實際工作丟到背景執行緒。
        同一種動作執行中再次觸發 (例如模擬按鍵造成的) 會被忽略；複製/貼上共用 WORK_LOCK 序列化。"""
        def wrapper():
            with self._state_lock:
                if self._running[name]:
                    return
                self._running[name] = True

            def run():
                try:
                    with WORK_LOCK:
                        func()
                except Exception as e:
                    print(f"[錯誤] {name} 處理失敗：{e}")
                finally:
                    with self._state_lock:
                        self._running[name] = False

            threading.Thread(target=run, daemon=True).start()
        return wrapper

    # ---------------- 複製 / 貼上 ----------------
    def on_copy(self):
        mapping = self.mapping
        direct = self._direct
        paste_hk = self.config.get("paste")
        print("\n" + "=" * 50)
        print("【偵測到 Ctrl+C】" if direct else "【偵測到複製快速鍵】")

        seq = core.clipboard_seq()
        if not direct:
            send_key_combination(ord('C'))  # 模式 A：自己模擬一次複製
        if not core.wait_clipboard_change(seq, timeout=1.0):
            print("[提示] 剪貼簿沒有變化 (可能複製了相同內容，或原生複製已先完成)，以目前剪貼簿內容處理")

        title, proc = core.get_active_window_info()
        content_type, content = core.get_clipboard_content()
        print(f"來源程式: {proc} | 視窗標題: {title}")

        if content_type == "FILES":
            print(f"複製類型: 檔案 (共 {len(content)} 個)")
            out_paths = core.dispatch_files(content, mapping)
            if out_paths:
                if direct:
                    core.copy_files_to_clipboard(out_paths)
                    self._last_copy = {"kind": "files", "original": content, "masked": out_paths}
                    print(f"[完成] 剪貼簿已直接覆寫成去識別化後的 {len(out_paths)} 個檔案，直接按原生 Ctrl+V 貼上即可")
                else:
                    self._override = {"kind": "files", "content": out_paths, "original": content}
                    print(f"[就緒] 覆蓋結果已產生 (共 {len(out_paths)} 個檔案)，按 {paste_hk} 才會貼上")
            else:
                self._reset_state()
                print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原樣" if direct else ""))
        else:
            result = core.process_text(content, mapping, action="auto")
            if result:
                if direct:
                    core.copy_text_to_clipboard(result)
                    self._last_copy = {"kind": "text", "original": content, "masked": result}
                    print("[完成] 剪貼簿已直接覆寫成處理後的內容，直接按原生 Ctrl+V 貼上即可")
                else:
                    self._override = {"kind": "text", "content": result, "original": content}
                    print(f"[就緒] 覆蓋結果已產生，按 {paste_hk} 才會貼上")
            else:
                self._reset_state()
                print("[結果] 未偵測到需要遮蔽的內容" + ("，剪貼簿維持原樣" if direct else ""))
        print("=" * 50)

    def on_paste(self):
        """只有模式 A 會註冊這個函式"""
        ov = self._override
        print("\n" + "=" * 50)
        print("【偵測到貼上快速鍵】")
        title, proc = core.get_active_window_info()

        if ov["kind"] == "text":
            core.copy_text_to_clipboard(ov["content"])
            time.sleep(0.05)
            send_key_combination(ord('V'))
            time.sleep(0.05)
            core.copy_text_to_clipboard(ov["original"])
            print(f"[完成] 已貼上覆蓋結果(文字)給 {proc} | {title}，剪貼簿已還原")
        elif ov["kind"] == "files":
            core.copy_files_to_clipboard(ov["content"])
            time.sleep(0.05)
            send_key_combination(ord('V'))
            time.sleep(0.05)
            core.copy_files_to_clipboard(ov["original"])
            print(f"[完成] 已貼上覆蓋結果(檔案)給 {proc} | {title}，剪貼簿已還原")
        else:
            print(f"[提示] 目前沒有覆蓋結果，直接貼上剪貼簿目前內容給 {proc} | {title}")
            send_key_combination(ord('V'))
        print("=" * 50)
