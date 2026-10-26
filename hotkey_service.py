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
    - 貼上時一律以「覆蓋結果」為準：
        1. 複製的是一般內容 -> 貼出遮蔽後 (帶標籤) 的內容
        2. 複製的是帶標籤的內容 -> 貼出還原後的內容
        3. 沒有需要處理的內容 -> 才原樣貼上
    - 如果剪貼簿在複製之後被別的方式改動過 (網頁的「複製」按鈕、右鍵複製、Ctrl+X...)，
      貼上時會以目前剪貼簿內容重新處理一次，避免貼出過期的結果或原始敏感內容。
  其他快速鍵設定 (例如 <ctrl>+<alt>+c / <ctrl>+<alt>+v) 維持原本 GlobalHotKeys 的行為，不受影響。
  需求：pynput >= 1.7 (win32_event_filter)。

【右鍵選單的複製 / 貼上】(只在「取代原生 Ctrl+C / Ctrl+V」模式啟用)
  右鍵選單裡的「複製」「貼上」是應用程式自己處理的，無法像快速鍵一樣直接攔截，所以用「暫時換內容」的方式處理：
    - 右鍵按下 (或按選單鍵 / Shift+F10) 時：若有覆蓋結果，先把它暫時寫進剪貼簿 (稱為「暫放」)。
      使用者在選單按「貼上」，應用程式讀到的就是覆蓋結果。
    - 選單操作結束 (下一次滑鼠點擊 / Enter / Esc / 按任意字母鍵) 後，稍等一下：
        * 剪貼簿沒被動過 -> 還原成原始內容 (跟快速鍵貼上一樣，貼完就還原)。
        * 剪貼簿被動過 (使用者按了選單的「複製」或「剪下」) -> 以新的剪貼簿內容重新算出覆蓋結果，
          之後不管用 Ctrl+V 還是右鍵貼上，都會貼出處理後的版本。
    - 暫放期間若又按了 Ctrl+C / Ctrl+V，會先還原剪貼簿再處理，不會把覆蓋結果當成原始內容再處理一次。
  限制：滑鼠 hook 只能「看到點擊」，看不到點的是哪個選單項目，所以是靠剪貼簿有沒有變動來判斷。
        模式 B (paste 為空) 剪貼簿本來就已被覆寫，右鍵貼上天然就是處理後版本，不需要這個機制。

ui.py 可以在執行中更改快速鍵：save_config() 後呼叫 service.start(新設定) 即重新綁定。
main.py 啟動時讀取 hotkeys.json (沒有檔案就用預設值)。
"""
import os
import re
import json
import time
import threading
from importlib import metadata

import win32api
import win32con
from pynput import keyboard, mouse

import core

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hotkeys.json")
DEFAULT_CONFIG = {"copy": "<ctrl>+c", "paste": "<ctrl>+v"}

# 處理剪貼簿 / 對照表的工作一律序列化 (快速鍵與 ui 的背景工作共用)，避免同時改 mapping 或搶剪貼簿
WORK_LOCK = threading.RLock()

PASTE_SETTLE_DELAY = 0.05     # 把覆蓋結果寫進剪貼簿後，等多久再送出貼上
PASTE_RESTORE_DELAY = 0.25    # 送出貼上後，等多久才把剪貼簿還原 (太早還原，反應慢的程式會貼到原始內容)

CONTEXT_RESTORE_DELAY = 0.6   # 右鍵選單操作結束後，等多久才還原 / 重算剪貼簿 (選單項目是在放開滑鼠時才執行)
CONTEXT_MAX_SECONDS = 60      # 右鍵暫放最長維持多久；萬一沒偵測到選單結束，時間到也會強制結束

# Windows 低階鍵盤 / 滑鼠 hook 的訊息常數
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_LBUTTONDOWN, WM_RBUTTONDOWN = 0x0201, 0x0204
WM_MBUTTONDOWN, WM_XBUTTONDOWN = 0x0207, 0x020B
LLKHF_INJECTED = 0x10         # 這個按鍵事件是程式模擬出來的 (keybd_event / SendInput)
LLMHF_INJECTED = 0x01         # 這個滑鼠事件是程式模擬出來的

VK_APPS = 0x5D                # 鍵盤上的「選單鍵」
VK_F10 = 0x79
# 開著選單時用來導覽的按鍵 (修飾鍵、方向鍵、Tab)：按了不代表選單操作結束
_MENU_NAV_VKS = {0x09, 0x10, 0x11, 0x12, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5, 0x5B, 0x5C,
                 0x25, 0x26, 0x27, 0x28}


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
        self._mouse_listener = None
        self._direct = not self.config.get("paste")

        self._override = {"kind": None, "content": None, "original": None, "seq": None}   # 模式 A 暫存
        self._last_copy = {"kind": None, "original": None, "masked": None}                # 模式 B 紀錄

        # 右鍵選單 (取代原生 Ctrl+C/V 模式)
        self._ctx_active = False     # 目前是否在「右鍵選單期間」
        self._ctx_token = 0          # 每開一次選單加 1，用來讓過期的背景工作自己放棄
        self._staged = None          # 暫放在剪貼簿裡的覆蓋結果 (結束時要還原)

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
        self._override = {"kind": None, "content": None, "original": None, "seq": None}
        self._last_copy = {"kind": None, "original": None, "masked": None}
        self._staged = None
        self._ctx_active = False
        self._ctx_token += 1

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

        if replace_native:
            # 右鍵選單的複製 / 貼上：另外監聽滑鼠點擊 (只觀察，不攔截)
            self._mouse_listener = self._build_mouse_listener()
            self._mouse_listener.start()

        print("【監聽啟動】" + ("(模式 B：Ctrl+C 直接模式)" if self._direct else "(模式 A：專屬快速鍵)"))
        if self._direct:
            print(f"  - {self.config['copy']} : 直接複製，自動判斷遮蔽/還原並覆寫剪貼簿")
            print("  - Ctrl+V (原生) : 貼出處理後的版本")
        else:
            suppressed = {b["name"] for b in bindings if b["suppress"]}
            copy_note = "  [原生複製已被取代]" if "copy" in suppressed else ""
            paste_note = "  [原生貼上已被取代；沒有覆蓋結果時才原樣貼上]" if "paste" in suppressed else ""
            print(f"  - {self.config['copy']} : 有標籤就還原、沒標籤就遮蔽（只產生覆蓋結果）{copy_note}")
            print(f"  - {self.config['paste']} : 貼上覆蓋結果，貼完還原剪貼簿{paste_note}")
            if "paste" not in suppressed:
                print("  - Ctrl+V : 系統原生貼上，不受影響")
            if replace_native:
                print("  - 右鍵選單的複製 / 貼上 : 已監聽 (右鍵時暫放覆蓋結果，選單結束後還原)")

    def stop(self):
        if self._mouse_listener is not None:
            try:
                self._mouse_listener.stop()
            except Exception:
                pass
            self._mouse_listener = None
        self._ctx_active = False
        self._ctx_token += 1
        with WORK_LOCK:
            try:
                self._unstage()          # 還有暫放在剪貼簿的覆蓋結果就先還原，避免停止後剪貼簿留著處理後的內容
            except Exception as e:
                print(f"[警告] 停止時還原剪貼簿失敗：{e}")
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
                        # 右鍵選單期間按了「選單導覽以外」的鍵 (Enter / Esc / 選單底線字母...)：選單操作結束
                        if self._ctx_active and vk not in _MENU_NAV_VKS:
                            self._ctx_end()
                        # 鍵盤叫出右鍵選單：選單鍵 或 Shift+F10
                        if vk == VK_APPS or (vk == VK_F10 and _current_modifiers() == frozenset({"shift"})):
                            if not self._ctx_active:
                                self._ctx_start()
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

    def _build_mouse_listener(self):
        """低階滑鼠 hook：只觀察點擊，從不攔截。
        右鍵按下 = 右鍵選單開始；之後下一次任何滑鼠按下 = 使用者點了選單項目 (或點空白處關掉選單)。"""
        def event_filter(msg, data):
            try:
                if data.flags & LLMHF_INJECTED:
                    return False
                if msg in (WM_LBUTTONDOWN, WM_RBUTTONDOWN, WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                    if self._ctx_active:
                        self._ctx_end()
                    if msg == WM_RBUTTONDOWN:
                        self._ctx_start()
            except Exception as e:
                print(f"[錯誤] 滑鼠事件處理失敗：{e}")
            return False                              # 不吞掉任何滑鼠事件，也不需要 on_click

        return mouse.Listener(win32_event_filter=event_filter)

    # ---------------- 右鍵選單 (暫放 / 還原) ----------------
    # 這三個 _ctx_* 會在 hook 執行緒被呼叫，所以只改旗標、馬上把實際工作丟到背景執行緒，hook 不能被卡住。
    def _ctx_start(self):
        self._ctx_token += 1
        token = self._ctx_token
        self._ctx_active = True
        threading.Thread(target=self._ctx_begin_job, args=(token,), daemon=True).start()
        threading.Thread(target=self._ctx_timeout_job, args=(token,), daemon=True).start()

    def _ctx_end(self):
        if not self._ctx_active:
            return
        self._ctx_active = False
        threading.Thread(target=self._ctx_finish_job, args=(self._ctx_token,), daemon=True).start()

    def _ctx_timeout_job(self, token):
        time.sleep(CONTEXT_MAX_SECONDS)
        if token == self._ctx_token and self._ctx_active:
            self._ctx_end()

    def _ctx_begin_job(self, token):
        """右鍵按下：把覆蓋結果暫放進剪貼簿，讓選單的「貼上」貼到處理後的內容"""
        try:
            with WORK_LOCK:
                if token != self._ctx_token:
                    return
                self._unstage()                         # 上一次還沒還原的先還原
                ov = self._override
                if ov.get("seq") != core.clipboard_seq():
                    # 還沒複製過，或剪貼簿在那之後被別的方式改過 -> 以目前剪貼簿重新算
                    self._recompute_from_clipboard()
                    ov = self._override
                if ov["kind"] not in ("text", "files"):
                    return                              # 沒有需要處理的內容：什麼都不動，選單照原樣貼上
                if ov["kind"] == "text":
                    core.copy_text_to_clipboard(ov["content"])
                else:
                    core.copy_files_to_clipboard(ov["content"])
                self._staged = {"ov": ov, "kind": ov["kind"], "seq": core.clipboard_seq(), "token": token}
                print("[右鍵選單] 已暫放覆蓋結果到剪貼簿，選單結束後會還原")
        except Exception as e:
            print(f"[錯誤] 右鍵選單暫放失敗：{e}")

    def _ctx_finish_job(self, token):
        """選單操作結束：等選單項目執行完，再還原剪貼簿 (沒被動過) 或重算覆蓋結果 (被複製/剪下動過)"""
        time.sleep(CONTEXT_RESTORE_DELAY)
        try:
            with WORK_LOCK:
                if token != self._ctx_token:
                    return                              # 已經又開了新的選單，交給新的那一輪處理
                st = self._staged
                if st and st["token"] == token:
                    self._unstage(recompute=True)
                elif st is None and self._override.get("seq") != core.clipboard_seq():
                    # 沒有暫放 (原本沒有覆蓋結果)，但剪貼簿變了 -> 使用者按了選單的複製 / 剪下
                    print("[右鍵選單] 偵測到剪貼簿變動 (複製/剪下)，重新處理")
                    self._recompute_from_clipboard()
        except Exception as e:
            print(f"[錯誤] 右鍵選單結束處理失敗：{e}")

    def _recompute_from_clipboard(self):
        content_type, content = core.get_clipboard_content()
        self._override = self._compute_override(content_type, content)
        if self._override["kind"]:
            print("[就緒] 覆蓋結果已更新")
        else:
            print("[結果] 未偵測到需要遮蔽的內容")

    def _unstage(self, recompute=False):
        """把暫放在剪貼簿的覆蓋結果收回來。呼叫時必須已持有 WORK_LOCK。
        - 剪貼簿沒被動過 -> 還原成原始內容
        - 剪貼簿被動過 (選單的複製/剪下、其他程式) -> 不能蓋掉使用者新複製的東西；recompute=True 時以新內容重算"""
        st = self._staged
        if not st:
            return
        self._staged = None
        ov = st["ov"]
        if core.clipboard_seq() == st["seq"]:
            if st["kind"] == "text":
                core.copy_text_to_clipboard(ov["original"])
            else:
                core.copy_files_to_clipboard(ov["original"])
            ov["seq"] = core.clipboard_seq()            # 我們自己寫剪貼簿不算「被改動」，同一份結果可繼續貼
            print("[右鍵選單] 剪貼簿已還原")
        elif recompute:
            print("[右鍵選單] 偵測到剪貼簿變動 (複製/剪下)，重新處理")
            self._recompute_from_clipboard()

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

        self._unstage()   # 右鍵選單暫放中的覆蓋結果先還原，否則會把它當成「原始內容」再處理一次

        seq = core.clipboard_seq()
        if not direct:
            send_key_combination(ord('C'))  # 模式 A：自己模擬一次複製 (實體按鍵若是 Ctrl+C 已被吞掉，所以只會複製一次)
        if not core.wait_clipboard_change(seq, timeout=1.0):
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

        self._unstage()   # 右鍵選單暫放中的覆蓋結果先還原 (同時把 ov["seq"] 更新回來，下面就不會誤判成過期)

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