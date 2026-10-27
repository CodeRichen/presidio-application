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

【取代原生 Ctrl+C / Ctrl+V】(模式 A 且快速鍵剛好設成 <ctrl>+c / <ctrl>+v)
  pynput 的 GlobalHotKeys 只是「偵聽」，不會擋掉按鍵，所以原生複製/貼上還是會照常執行
  (結果就是貼上兩次，或貼出原始未遮蔽的內容)。這種設定下改用低階鍵盤 hook 自己比對按鍵：
    - 使用者實體按下的 Ctrl+C / Ctrl+V 會被吞掉，應用程式收不到，由本服務取代。
    - 本服務自己模擬出來的按鍵 (帶有 injected 旗標) 一律放行，不會觸發自己，也不會被吞掉。
    - 複製的結果直接寫回剪貼簿：
        1. 複製的是一般內容 -> 剪貼簿變成遮蔽後 (帶標籤) 的內容
        2. 複製的是帶標籤的內容 -> 剪貼簿變成還原後的內容
        3. 沒有需要處理的內容 -> 剪貼簿維持原樣
    - 右鍵選單的「複製 / 貼上」是應用程式自己處理的，沒辦法攔截；所以改成「監聽剪貼簿」：
      不是由快速鍵觸發的複製 (右鍵複製、網頁的複製按鈕、Ctrl+X...) 只要剪貼簿內容變了，
      就用同樣的規則處理並寫回剪貼簿。剪貼簿裡永遠是處理後的內容，
      所以不論用 Ctrl+V、右鍵貼上或其他貼上方式，貼出的都是處理後的內容。
    - 快速鍵貼上時，若剪貼簿還沒處理過 (複製剛發生、監聽尚未追上，或程式剛啟動)，會先處理再貼上。
    - 取捨：這個設定下剪貼簿不會保留原文 (原文可從對照表還原)。
    - 程式啟動當下剪貼簿裡已有的內容，監聽不會主動改寫；但第一次按貼上快速鍵時會先處理。
  其他快速鍵設定 (例如 <ctrl>+<alt>+c / <ctrl>+<alt>+v) 維持原本 GlobalHotKeys 的行為，不受影響。
  需求：pynput >= 1.7 (win32_event_filter)。

ui.py 可以在執行中更改快速鍵：save_config() 後呼叫 service.start(新設定) 即重新綁定。
main.py 啟動時讀取 hotkeys.json (沒有檔案就用預設值)。
"""
import os
import re
import sys
import json
import time
import threading
from importlib import metadata

import win32api
import win32con
from pynput import keyboard

import core

# 打包成單一 EXE 後 __file__ 會指到執行時解壓縮的暫存資料夾 (關掉就刪除)，設定檔要放在 EXE 旁邊才存得住
APP_DIR = (os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False)
           else os.path.dirname(os.path.abspath(__file__)))
CONFIG_FILE = os.path.join(APP_DIR, "hotkeys.json")
DEFAULT_CONFIG = {"copy": "<ctrl>+c", "paste": "<ctrl>+v"}

# 處理剪貼簿 / 對照表的工作一律序列化 (快速鍵與 ui 的背景工作共用)，避免同時改 mapping 或搶剪貼簿
WORK_LOCK = threading.RLock()

PASTE_SETTLE_DELAY = 0.05     # 把覆蓋結果寫進剪貼簿後，等多久再送出貼上
PASTE_RESTORE_DELAY = 0.25    # 送出貼上後，等多久才把剪貼簿還原 (太早還原，反應慢的程式會貼到原始內容)
CLIPBOARD_POLL_INTERVAL = 0.3 # 取代原生 Ctrl+C/V 模式：多久檢查一次剪貼簿有沒有被「快速鍵以外」的方式改動
CLIPBOARD_SETTLE_DELAY = 0.2  # 偵測到變動後，等剪貼簿序號穩定這麼久才處理 (有些程式複製時會連續寫好幾次)

# Windows 低階鍵盤 hook 的訊息常數
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
LLKHF_INJECTED = 0x10         # 這個按鍵事件是程式模擬出來的 (keybd_event / SendInput)


# ---------------- 快速鍵字串解析 (只有「取代原生 Ctrl+C / Ctrl+V」模式會用到) ----------------
_MOD_ALIASES = {
    "ctrl": "ctrl", "ctrl_l": "ctrl", "ctrl_r": "ctrl",
    "alt": "alt", "alt_l": "alt", "alt_r": "alt", "alt_gr": "alt",
    "shift": "shift", "shift_l": "shift", "shift_r": "shift",
    "cmd": "cmd", "cmd_l": "cmd", "cmd_r": "cmd",
}
_SPECIAL_VK = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backspace": 0x08,
    "delete": 0x2E, "insert": 0x2D, "home": 0x24, "end": 0x23,
    "page_up": 0x21, "page_down": 0x22, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
# 會被本服務取代掉的原生組合：(修飾鍵集合, 主按鍵 VK)
NATIVE_COMBOS = {
    (frozenset({"ctrl"}), ord("C")),
    (frozenset({"ctrl"}), ord("V")),
}


def _pynput_version():
    """從已安裝的套件資訊讀 pynput 版本 (pynput.__version__ 不一定存在，不能靠它)；讀不到回傳 None"""
    try:
        return tuple(int(n) for n in re.findall(r"\d+", metadata.version("pynput"))[:3])
    except Exception:
        return None


def parse_hotkey(hk):
    """把 "<ctrl>+<alt>+c" 這種字串解析成 (修飾鍵集合, 主按鍵 VK 碼)。不支援的格式丟 ValueError。"""
    mods, main_vk = set(), None
    for raw in hk.lower().split("+"):
        part = raw.strip()
        vk = None
        if part.startswith("<") and part.endswith(">") and len(part) > 2:
            name = part[1:-1]
            if name in _MOD_ALIASES:
                mods.add(_MOD_ALIASES[name])
                continue
            if name in _SPECIAL_VK:
                vk = _SPECIAL_VK[name]
            elif re.fullmatch(r"f\d{1,2}", name) and 1 <= int(name[1:]) <= 24:
                vk = 0x70 + int(name[1:]) - 1
            elif name.isdigit():
                vk = int(name)                      # pynput 也允許直接寫 VK 數字，例如 <65>
            else:
                raise ValueError(f"不支援的按鍵 {part}")
        elif len(part) == 1:
            if part.isascii() and part.isalnum():
                vk = ord(part.upper())              # 英文字母 / 數字的 VK 碼就是大寫字元的碼
            else:
                scan = win32api.VkKeyScan(part) & 0xFF
                if scan == 0xFF:
                    raise ValueError(f"找不到按鍵 {part}")
                vk = scan
        else:
            raise ValueError(f"無法解析 {part!r}")
        if main_vk is not None:
            raise ValueError("只能有一個主按鍵")
        main_vk = vk
    if main_vk is None:
        raise ValueError("缺少主按鍵")
    return frozenset(mods), main_vk


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


# ---------------- 模擬按鍵 ----------------
def _is_down(vk):
    return bool(win32api.GetAsyncKeyState(vk) & 0x8000)


def _key_down(vk):
    win32api.keybd_event(vk, 0, 0, 0)


def _key_up(vk):
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)


def _current_modifiers():
    """目前按著的修飾鍵集合，用來判斷是不是剛好等於快速鍵要求的組合"""
    mods = set()
    if _is_down(win32con.VK_CONTROL):
        mods.add("ctrl")
    if _is_down(win32con.VK_MENU):
        mods.add("alt")
    if _is_down(win32con.VK_SHIFT):
        mods.add("shift")
    if _is_down(win32con.VK_LWIN) or _is_down(win32con.VK_RWIN):
        mods.add("cmd")
    return frozenset(mods)


def send_key_combination(vk_code):
    """模擬 Ctrl + 指定按鍵 (只有模式 A 會用到)。
    - 先放開使用者可能還壓著的 Alt / Shift (例如快速鍵是 Ctrl+Alt+C)，避免變成 Ctrl+Alt+C 之類的組合。
    - 若使用者實體上還壓著 Ctrl (剛按 Ctrl+V 還沒放開)，結束時不放開 Ctrl，維持使用者原本的按鍵狀態，
      這樣按住 Ctrl 連續按 V 時，後面的按鍵才還能被正確偵測到。"""
    for vk in (win32con.VK_MENU, win32con.VK_SHIFT):
        if _is_down(vk):
            _key_up(vk)
    ctrl_was_down = _is_down(win32con.VK_CONTROL)
    time.sleep(0.05)
    if not ctrl_was_down:
        _key_down(win32con.VK_CONTROL)
    _key_down(vk_code)
    time.sleep(0.05)
    _key_up(vk_code)
    if not ctrl_was_down:
        _key_up(win32con.VK_CONTROL)


class HotkeyService:
    def __init__(self, mapping, config=None):
        self.mapping = mapping
        self.config = dict(config) if config else load_config()
        self._listener = None
        self._direct = not self.config.get("paste")

        self._override = {"kind": None, "content": None, "original": None, "seq": None}   # 模式 A 暫存
        self._last_copy = {"kind": None, "original": None, "masked": None}                # 模式 B 紀錄

        self._state_lock = threading.Lock()
        self._running = {"copy": False, "paste": False}

        # 取代原生 Ctrl+C/V 模式用：剪貼簿監聽
        self._replace_native = False
        self._handled_seq = None     # 已經處理過 (或程式自己寫入) 的剪貼簿序號；不等於目前序號 = 還沒處理
        self._baseline_seq = None    # 監聽啟動當下的序號，監聽不主動改寫它
        self._monitor_stop = None

    # ---------------- 狀態 ----------------
    @property
    def running(self):
        return self._listener is not None and self._listener.running

    @property
    def direct_mode(self):
        return self._direct

    def _reset_state(self):
        self._override = {"kind": None, "content": None, "original": None, "seq": None}
        self._last_copy = {"kind": None, "original": None, "masked": None}
        self._handled_seq = None

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

        bindings = [] if self._direct else self._parse_bindings()
        replace_native = any(b["suppress"] for b in bindings)   # 有綁到原生 Ctrl+C / Ctrl+V -> 要攔截

        callbacks = {"copy": self._guarded("copy", self.on_copy, allow_queue=replace_native)}
        if not self._direct:
            callbacks["paste"] = self._guarded("paste", self.on_paste, allow_queue=replace_native)

        if replace_native:
            listener = self._build_hook_listener(bindings, callbacks)
        else:
            hotkeys = {self.config["copy"]: callbacks["copy"]}
            if not self._direct:
                hotkeys[self.config["paste"]] = callbacks["paste"]
            listener = keyboard.GlobalHotKeys(hotkeys)   # 格式錯誤會在這裡丟例外

        listener.start()
        self._listener = listener
        self._replace_native = replace_native
        if replace_native:
            self._start_monitor()

        print("【監聽啟動】" + ("(模式 B：Ctrl+C 直接模式)" if self._direct else "(模式 A：專屬快速鍵)"))
        if self._direct:
            print(f"  - {self.config['copy']} : 直接複製，自動判斷遮蔽/還原並覆寫剪貼簿")
            print("  - Ctrl+V (原生) : 貼出處理後的版本")
        else:
            suppressed = {b["name"] for b in bindings if b["suppress"]}
            copy_note = "  [原生複製已被取代]" if "copy" in suppressed else ""
            paste_note = "  [原生貼上已被取代]" if "paste" in suppressed else ""
            if replace_native:
                print(f"  - {self.config['copy']} : 複製後有標籤就還原、沒標籤就遮蔽，並把結果寫回剪貼簿{copy_note}")
                print(f"  - {self.config['paste']} : 貼上 (貼上前若剪貼簿還沒處理過會先處理){paste_note}")
                print(f"  - 右鍵複製 / 右鍵貼上 / 網頁複製鈕 / Ctrl+X : 由剪貼簿監聽處理 "
                      f"(每 {CLIPBOARD_POLL_INTERVAL} 秒檢查一次)，複製完成後剪貼簿就是處理後的內容")
            else:
                print(f"  - {self.config['copy']} : 有標籤就還原、沒標籤就遮蔽（只產生覆蓋結果）{copy_note}")
                print(f"  - {self.config['paste']} : 貼上覆蓋結果，貼完還原剪貼簿{paste_note}")
                print("  - Ctrl+V : 系統原生貼上，不受影響")

    def stop(self):
        self._stop_monitor()
        if self._listener is not None:
            try:
                self._listener.stop()
            except Exception:
                pass
            self._listener = None
            print("[停止] 全域快速鍵已停用")

    # ---------------- 快速鍵比對 / 攔截 ----------------
    def _parse_bindings(self):
        """解析模式 A 的複製/貼上快速鍵，並標記哪些剛好是原生 Ctrl+C / Ctrl+V (需要攔截)"""
        bindings = []
        for name in ("copy", "paste"):
            hk = self.config.get(name)
            if not hk:
                continue
            try:
                parsed = parse_hotkey(hk)
            except ValueError:
                bindings.append({"name": name, "hk": hk, "parsed": None, "suppress": False})
                continue
            bindings.append({"name": name, "hk": hk, "parsed": parsed, "suppress": parsed in NATIVE_COMBOS})
        return bindings

    def _build_hook_listener(self, bindings, callbacks):
        """低階鍵盤 hook：自己比對按鍵，命中原生組合時吞掉實體按鍵，本服務模擬出來的按鍵一律放行"""
        labels = {"copy": "複製", "paste": "貼上"}
        for b in bindings:
            if b["parsed"] is None:
                raise ValueError(f"{labels[b['name']]}快速鍵 {b['hk']} 無法在「取代原生複製/貼上」模式使用，請改用一般的按鍵組合")
        ver = _pynput_version()
        if ver is not None and ver < (1, 7):          # 讀不到版本時不擋，交給實際執行判斷
            raise RuntimeError(f"取代原生 Ctrl+C / Ctrl+V 需要 pynput 1.7 以上 (目前 {'.'.join(map(str, ver))})，"
                               "請執行 pip install -U pynput")

        table = [(b["name"], b["parsed"][0], b["parsed"][1], b["suppress"]) for b in bindings]
        held = {}   # vk -> 這次按下是否已被吞掉；用來忽略按住不放的自動重複，以及把對應的 keyup 一起吞掉

        def event_filter(msg, data):
            suppress = False
            try:
                if data.flags & LLKHF_INJECTED:
                    return False                      # 模擬出來的按鍵 (含本服務自己送的)：放行
                vk = data.vkCode
                if msg in (WM_KEYDOWN, WM_SYSKEYDOWN):
                    matched = None
                    for name, b_mods, b_vk, b_sup in table:
                        if vk == b_vk and _current_modifiers() == b_mods:
                            matched = (name, b_sup)
                            break
                    if matched:
                        name, b_sup = matched
                        if vk not in held:            # 第一次按下才觸發；自動重複只吞掉、不重複觸發
                            callbacks[name]()
                        held[vk] = b_sup
                        suppress = b_sup
                    else:
                        held.pop(vk, None)            # 不是我們的組合 (例如單獨按 V)：清掉殘留狀態並放行
                elif msg in (WM_KEYUP, WM_SYSKEYUP):
                    if vk in held:
                        suppress = held.pop(vk)
            except Exception as e:                    # hook 裡出錯不能往外丟，否則 pynput 會停掉整個監聽
                print(f"[錯誤] 鍵盤事件處理失敗：{e}")
                return False
            if suppress:
                listener.suppress_event()             # 丟出特殊例外，讓 hook 回傳「不要再往下傳」
            return False                              # False = 不需要呼叫 on_press / on_release

        listener = keyboard.Listener(win32_event_filter=event_filter)
        return listener

    # ---------------- callback 包裝 ----------------
    def _guarded(self, name, func, allow_queue=False):
        """callback 立刻返回 (不卡住鍵盤 hook)，實際工作丟到背景執行緒。
        預設：同一種動作執行中再次觸發 (例如模擬按鍵造成的) 會被忽略。
        allow_queue=True (取代原生 Ctrl+C/V 模式)：模擬按鍵不會觸發本服務，所以不需要忽略；
        使用者連按兩次貼上時，第二次會排隊等第一次做完 (由 WORK_LOCK 序列化)，不會被吃掉。"""
        def wrapper():
            if not allow_queue:
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
                    if not allow_queue:
                        with self._state_lock:
                            self._running[name] = False

            threading.Thread(target=run, daemon=True).start()
        return wrapper

    # ---------------- 剪貼簿監聽 (取代原生 Ctrl+C/V 模式) ----------------
    def ignore_clipboard_change(self):
        """程式自己 (例如 ui.py 的「結果複製到剪貼簿」) 剛寫了剪貼簿時呼叫，
        告訴監聽這不是使用者的複製，不要再處理一次 (否則帶標籤的結果會被當成要還原)。"""
        self._handled_seq = core.clipboard_seq()

    def _process_clipboard_in_place(self):
        """讀目前剪貼簿 -> 遮蔽/還原 -> 把結果寫回剪貼簿。回傳 "text" / "files" / None (沒有需要處理的內容)。
        呼叫者必須已持有 WORK_LOCK。處理完會記下剪貼簿序號，之後的監聽與貼上就不會再處理同一份內容。"""
        content_type, content = core.get_clipboard_content()
        ov = self._compute_override(content_type, content)
        if ov["kind"] == "text":
            core.copy_text_to_clipboard(ov["content"])
        elif ov["kind"] == "files":
            core.copy_files_to_clipboard(ov["content"])
        self._handled_seq = core.clipboard_seq()
        return ov["kind"]

    @staticmethod
    def _print_in_place_result(kind):
        if kind:
            print("[完成] 剪貼簿已改寫成處理後的內容；之後不論 Ctrl+V、右鍵貼上或其他貼上方式，貼出的都是處理後的內容")
        else:
            print("[結果] 未偵測到需要遮蔽 / 還原的內容，剪貼簿維持原樣")

    def _start_monitor(self):
        self._baseline_seq = core.clipboard_seq()
        stop = threading.Event()
        self._monitor_stop = stop
        threading.Thread(target=self._monitor_loop, args=(stop,), daemon=True).start()

    def _stop_monitor(self):
        if self._monitor_stop is not None:
            self._monitor_stop.set()
            self._monitor_stop = None

    def _monitor_loop(self, stop):
        """定期檢查剪貼簿序號。序號變了、而且不是快速鍵流程 / 程式自己寫的 (handled)，
        就視為使用者用右鍵複製、網頁複製鈕、Ctrl+X 等方式複製，照同樣規則處理並寫回剪貼簿。"""
        while not stop.wait(CLIPBOARD_POLL_INTERVAL):
            try:
                if core.clipboard_seq() in (self._handled_seq, self._baseline_seq):
                    continue
                seq = core.clipboard_seq()
                while not stop.wait(CLIPBOARD_SETTLE_DELAY):      # 等序號穩定 (有些程式複製時會連寫好幾次)
                    now = core.clipboard_seq()
                    if now == seq:
                        break
                    seq = now
                if stop.is_set():
                    return
                with WORK_LOCK:                                   # 等快速鍵 / ui 正在做的工作做完
                    if stop.is_set():
                        return
                    if core.clipboard_seq() in (self._handled_seq, self._baseline_seq):
                        continue                                  # 等鎖的期間已被處理掉
                    self._process_external_copy()
            except Exception as e:
                print(f"[錯誤] 剪貼簿監聽失敗：{e}")

    def _process_external_copy(self):
        print("\n" + "=" * 50)
        print("【偵測到剪貼簿變動 (右鍵複製 / 網頁複製鈕 / 剪下 ...)】")
        title, proc = core.get_active_window_info()
        print(f"來源程式: {proc} | 視窗標題: {title}")
        for attempt in range(3):                                  # 剪貼簿偶爾被別的程式鎖住，重試幾次
            try:
                self._print_in_place_result(self._process_clipboard_in_place())
                break
            except Exception as e:
                if attempt == 2:
                    print(f"[錯誤] 處理剪貼簿失敗，這次不處理：{e}")
                    self._handled_seq = core.clipboard_seq()     # 避免每 0.3 秒重複失敗洗版
                else:
                    time.sleep(0.2)
        print("=" * 50)

    # ---------------- 覆蓋結果 ----------------
    def _compute_override(self, content_type, content):
        """依目前剪貼簿內容算出覆蓋結果。回傳的 dict 一律帶 seq (算完當下的剪貼簿序號)，
        貼上時用它判斷剪貼簿是不是在這之後被改過。kind 為 None 代表沒有需要處理的內容。"""
        mapping = self.mapping
        if content_type == "FILES":
            print(f"複製類型: 檔案 (共 {len(content)} 個)")
            out_paths = core.dispatch_files(content, mapping)
            if out_paths:
                return {"kind": "files", "content": out_paths, "original": content, "seq": core.clipboard_seq()}
        else:
            result = core.process_text(content, mapping, action="auto")
            if result:
                return {"kind": "text", "content": result, "original": content, "seq": core.clipboard_seq()}
        return {"kind": None, "content": None, "original": None, "seq": core.clipboard_seq()}

    # ---------------- 複製 / 貼上 ----------------
    def on_copy(self):
        mapping = self.mapping
        direct = self._direct
        paste_hk = self.config.get("paste")
        print("\n" + "=" * 50)
        print("【偵測到 Ctrl+C】" if direct else "【偵測到複製快速鍵】")

        seq = core.clipboard_seq()
        if not direct:
            send_key_combination(ord('C'))  # 模式 A：自己模擬一次複製 (實體按鍵若是 Ctrl+C 已被吞掉，所以只會複製一次)
        changed = core.wait_clipboard_change(seq, timeout=1.0)

        if self._replace_native:
            # 取代原生複製：只處理「真的複製成功」的內容。沒變化 (沒選取東西) 時不能處理，
            # 否則會把剪貼簿裡已處理好的內容再處理一次 (帶標籤的內容會被還原)
            if not changed:
                print("[提示] 剪貼簿沒有變化 (沒有選取內容或複製失敗)，不處理，剪貼簿維持原樣")
            else:
                title, proc = core.get_active_window_info()
                print(f"來源程式: {proc} | 視窗標題: {title}")
                self._print_in_place_result(self._process_clipboard_in_place())
            print("=" * 50)
            return

        if not changed:
            print("[提示] 剪貼簿沒有變化 (可能複製了相同內容，或原生複製已先完成)，以目前剪貼簿內容處理")

        title, proc = core.get_active_window_info()
        content_type, content = core.get_clipboard_content()
        print(f"來源程式: {proc} | 視窗標題: {title}")

        if not direct:
            # 模式 A：只算出覆蓋結果並暫存，不動剪貼簿
            ov = self._compute_override(content_type, content)
            self._override = ov
            if ov["kind"] == "files":
                print(f"[就緒] 覆蓋結果已產生 (共 {len(ov['content'])} 個檔案)，按 {paste_hk} 才會貼上")
            elif ov["kind"] == "text":
                print(f"[就緒] 覆蓋結果已產生，按 {paste_hk} 才會貼上")
            else:
                print("[結果] 未偵測到需要遮蔽的內容")
            print("=" * 50)
            return

        # 模式 B：直接覆寫剪貼簿
        if content_type == "FILES":
            print(f"複製類型: 檔案 (共 {len(content)} 個)")
            out_paths = core.dispatch_files(content, mapping)
            if out_paths:
                core.copy_files_to_clipboard(out_paths)
                self._last_copy = {"kind": "files", "original": content, "masked": out_paths}
                print(f"[完成] 剪貼簿已直接覆寫成去識別化後的 {len(out_paths)} 個檔案，直接按原生 Ctrl+V 貼上即可")
            else:
                self._reset_state()
                print("[結果] 未偵測到需要遮蔽的內容，剪貼簿維持原樣")
        else:
            result = core.process_text(content, mapping, action="auto")
            if result:
                core.copy_text_to_clipboard(result)
                self._last_copy = {"kind": "text", "original": content, "masked": result}
                print("[完成] 剪貼簿已直接覆寫成處理後的內容，直接按原生 Ctrl+V 貼上即可")
            else:
                self._reset_state()
                print("[結果] 未偵測到需要遮蔽的內容，剪貼簿維持原樣")
        print("=" * 50)

    def on_paste(self):
        """只有模式 A 會註冊這個函式"""
        print("\n" + "=" * 50)
        print("【偵測到貼上快速鍵】")
        title, proc = core.get_active_window_info()

        if self._replace_native:
            # 剪貼簿裡本來就應該是處理後的內容；若還沒處理過 (複製剛發生、監聽還沒追上、程式剛啟動) 先處理再貼
            if core.clipboard_seq() != self._handled_seq:
                print("[提示] 剪貼簿內容還沒處理過，貼上前先處理")
                self._print_in_place_result(self._process_clipboard_in_place())
            send_key_combination(ord('V'))
            print(f"[完成] 已貼上剪貼簿內容給 {proc} | {title}")
            print("=" * 50)
            return

        ov = self._override
        if ov.get("seq") != core.clipboard_seq():
            # 還沒用複製快速鍵複製過，或剪貼簿在那之後被別的方式改過 (網頁的複製按鈕、右鍵、Ctrl+X...)
            # -> 不能貼過期的結果，也不能直接貼原始內容，改以目前剪貼簿重新處理
            print("[提示] 剪貼簿在複製快速鍵之後有變動，以目前剪貼簿內容重新處理")
            content_type, content = core.get_clipboard_content()
            ov = self._override = self._compute_override(content_type, content)

        if ov["kind"] == "text":
            core.copy_text_to_clipboard(ov["content"])
            time.sleep(PASTE_SETTLE_DELAY)
            send_key_combination(ord('V'))
            time.sleep(PASTE_RESTORE_DELAY)
            core.copy_text_to_clipboard(ov["original"])
            ov["seq"] = core.clipboard_seq()    # 我們自己寫剪貼簿不算「被改動」，更新序號讓同一份結果可以重複貼
            print(f"[完成] 已貼上覆蓋結果(文字)給 {proc} | {title}，剪貼簿已還原")
        elif ov["kind"] == "files":
            core.copy_files_to_clipboard(ov["content"])
            time.sleep(PASTE_SETTLE_DELAY)
            send_key_combination(ord('V'))
            time.sleep(PASTE_RESTORE_DELAY)
            core.copy_files_to_clipboard(ov["original"])
            ov["seq"] = core.clipboard_seq()
            print(f"[完成] 已貼上覆蓋結果(檔案)給 {proc} | {title}，剪貼簿已還原")
        else:
            print(f"[提示] 沒有需要遮蔽/還原的內容，原樣貼上剪貼簿內容給 {proc} | {title}")
            send_key_combination(ord('V'))
        print("=" * 50)
