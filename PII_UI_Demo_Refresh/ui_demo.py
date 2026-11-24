"""
ui.py - 剪貼簿敏感資訊防護 圖形介面 (tkinter，不需額外安裝)
====================================================
與 main.py 互相獨立，只 import core.py / entity_store.py / hotkey_service.py。
分頁：
  - 文字      貼上/輸入文字，選擇「遮蔽」或「還原」後按「判斷」；無變化時依情況提示使用者。
              判斷完輸入框會清空，結果接在結果框原有內容後面 (結果不會被清掉，要清再按「清空」)。
              送模型前統一先做前處理 (全形轉半形、隱形字元、網頁標籤、簡繁、空白)，
              可開關，也能「預覽前處理」看每一步改了什麼；遮蔽只動被偵測到的那一段，原文其餘部分不變
  - 檔案      選擇多個檔案 -> 去識別化
  - 資料夾掃描 選一個資料夾 -> 在同層建立 <原名>_masked，先複製所有內容，再只遮蔽複本裡含敏感資料的檔案
              (檔名不變、原資料夾不動)，並列出哪些檔案危險
  - 名單管理  敏感清單 (點 ✕ 一下就刪除，不用確認，刪掉的值進黑名單永遠不會再被自動加入)
              / 新增敏感資料 (類型可不填，由模型判斷) / 黑名單 / 安全清單 (永遠不遮蔽)
  - 快速鍵    錄製並更改快速鍵、啟動/停止全域快速鍵
  - 對照表    遮蔽標籤與原文的對照 (結束時刪除；名單不會)
耗時工作在背景執行緒跑，介面不會卡住；所有 print 顯示在下方記錄區。
"""
import os
import re
import sys
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
from tkinter import font as tkfont

import app_env  # 必須最先 import：打包成 EXE 時的工作目錄 / 模型資料夾 / 離線設定，要在載入模型的 core 之前完成
import core
import entity_store
import hotkey_service
import preprocess
import time

# BEGIN GUI_LOG_QUEUE_OPTIMIZATION_V1
# GUI updates are time-bounded and batched; media processing remains in its worker.
GUI_LOG_POLL_MAX_ITEMS = 250
GUI_LOG_POLL_BUDGET_MS = 12.0
GUI_LOG_QUEUE_LIMIT = 12000       # Backpressure only for display logs
GUI_LOG_MAX_RECORD_CHARS = 16000  # Do not put arbitrarily large writes in Tk
GUI_LOG_MAX_LINES = 3000          # Bounded on-screen history
# END GUI_LOG_QUEUE_OPTIMIZATION_V1


# UI 啟動時預設使用 AC + Chunk
os.environ.setdefault("PII_NER_BACKEND", "ac")
os.environ.setdefault("PII_C_LONG_MODE", "chunk")


# ---------- 快速鍵錄製：Tk 按鍵事件 -> pynput 格式 ----------
_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                     "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                     "Caps_Lock", "Num_Lock", "Scroll_Lock"}
_SPECIAL_KEYS = {"space": "<space>", "Return": "<enter>", "Tab": "<tab>", "BackSpace": "<backspace>",
                 "Delete": "<delete>", "Insert": "<insert>", "Home": "<home>", "End": "<end>",
                 "Prior": "<page_up>", "Next": "<page_down>",
                 "Up": "<up>", "Down": "<down>", "Left": "<left>", "Right": "<right>"}

KIND_CODE = "程式"          # 文字分頁的內容類型 (二選一)
KIND_TEXT = "一般文字"

AUTO_LABEL = "自動判斷 (由模型決定)"
LABEL_CHOICES = [AUTO_LABEL, "PERSON", "ORGANIZATION", "ADDRESS", "LOCATION", "POSITION", "TW_PHONE",
                 "EMAIL", "TW_ID", "CREDIT_CARD", "URL", "FILE_NAME", "CREDENTIAL_LIKE", "GENERIC_SECRET"]


def describe_text_action_result(action, original, result, mapping):
    """根據處理前後差異與目前對照表，回傳 (層級, 標題, 給使用者的訊息)。

    層級為 ok/info/warning。不能只因文字含標籤就宣稱整份文件已安全遮蔽；
    也不能把「缺少還原對照表」誤說成「已經還原」。
    """
    tags = [m.group(0) for m in core.TAG_RE.finditer(original)]

    if action == "mask":
        if result:
            return ("ok", "遮蔽完成", "已遮蔽新偵測到的敏感資訊，既有遮蔽標籤會保留。")
        if tags:
            return (
                "info", "沒有新增需要遮蔽的內容",
                f"文字中已有 {len(tags)} 處遮蔽標籤，本次沒有偵測到其他需要遮蔽的資訊。"
                "可能已處理過；但模型仍可能漏判，不能保證所有個資都已遮蔽。",
            )
        return (
            "info", "沒有偵測到可遮蔽資訊",
            "本次沒有找到需要遮蔽的內容。可能原文沒有個資，也可能是模型未能辨識。",
        )

    if action == "restore":
        if not tags:
            return (
                "info", "沒有可還原的遮蔽標籤",
                "這段文字沒有 <PERSON_1> 這類遮蔽標籤，可能已經還原，或原本就沒有被遮蔽過。",
            )
        restorable = sum(mapping.get(tag, tag) != tag for tag in tags)
        missing = len(tags) - restorable
        if not result:
            return (
                "warning", "無法還原",
                f"找到 {len(tags)} 處遮蔽標籤，但目前對照表沒有可用的原始資料。"
                "可能已清除對照表，或標籤來自其他執行階段。",
            )
        if missing:
            return (
                "warning", "部分還原完成",
                f"已還原 {restorable} 處標籤，另有 {missing} 處因缺少對照資料而保留原樣。",
            )
        return ("ok", "還原完成", f"已成功還原 {restorable} 處遮蔽標籤。")

    raise ValueError(f"不支援的文字處理模式：{action}")


def hotkey_from_event(event):
    """回傳 (快速鍵字串, 錯誤訊息)。只按了修飾鍵時回傳 (None, None)，表示繼續等待。"""
    ks = event.keysym
    if ks in _MODIFIER_KEYSYMS:
        return None, None

    mods = []
    if event.state & 0x4:
        mods.append("<ctrl>")
    if event.state & 0x20000:   # Windows 的 Alt
        mods.append("<alt>")
    if event.state & 0x1:
        mods.append("<shift>")

    if re.fullmatch(r"F\d{1,2}", ks):
        key = f"<{ks.lower()}>"
    elif ks in _SPECIAL_KEYS:
        key = _SPECIAL_KEYS[ks]
    elif len(ks) == 1:
        key = ks.lower()
    else:
        return None, f"不支援的按鍵：{ks}"

    if not mods and not key.startswith("<f"):
        return None, "請至少搭配一個修飾鍵 (Ctrl / Alt / Shift)，避免影響一般打字"
    return "+".join(mods + [key]), None


class QueueWriter:
    """Route stdout to UI, without blocking a busy worker on an overloaded log queue.

    Only the GUI history is limited. PII detection and saved outputs are unchanged.
    """
    def __init__(self, q):
        self.q = q
        self._drop_lock = threading.Lock()
        self._dropped = 0

    def write(self, s):
        if not s:
            return
        if len(s) > GUI_LOG_MAX_RECORD_CHARS:
            s = s[:GUI_LOG_MAX_RECORD_CHARS] + "\n[GUI] 單筆日誌過長，顯示部分已省略。\n"
        with self._drop_lock:
            # Keep important errors visible under extreme logging load.
            important = s.lstrip().startswith(("[錯誤]", "[PDF安全檢查]", "[PDF安全輸出]"))
            if not important and self.q.qsize() >= GUI_LOG_QUEUE_LIMIT:
                self._dropped += 1
                if self._dropped == 1:
                    self.q.put(("log", "\n[GUI] 畫面日誌過量，部分內容已略過；不影響實際遮蔽結果。\n"))
                return
            if self._dropped:
                self.q.put(("log", f"\n[GUI] 佇列過載，省略 {self._dropped} 段畫面日誌（不影響實際處理）。\n"))
                self._dropped = 0
            self.q.put(("log", s))

    def flush(self):
        pass


def make_tree(parent, columns, height=None):
    """建立帶捲軸的 Treeview。columns: [(id, 標題, 寬度, 對齊)]"""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        selectmode="extended", **({"height": height} if height else {}))
    for cid, text, w, anchor in columns:
        tree.heading(cid, text=text)
        tree.column(cid, width=w, anchor=anchor, stretch=(cid != columns[0][0]))
    sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=sb.set)
    tree.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")
    return tree


# ================= 評審展示 UI：純視覺主題（無新依賴） =================
DEMO_PALETTE = {
    "navy": "#12233E", "navy_dark": "#0B1830", "blue": "#2563EB",
    "blue_hover": "#1D4ED8", "teal": "#0F766E", "bg": "#F3F6FB",
    "card": "#FFFFFF", "muted": "#607086", "text": "#14253D",
    "line": "#DDE6F1", "soft_blue": "#E8F0FF", "green": "#047857",
}


def apply_demo_theme(root):
    """只調整 Tk/ttk 視覺外觀，不接觸 OCR、PII、檔案或資料庫。"""
    c = DEMO_PALETTE
    family = "Microsoft JhengHei UI" if os.name == "nt" else "Noto Sans CJK TC"
    root._demo_fonts = {
        "body": tkfont.Font(root=root, family=family, size=11),
        "small": tkfont.Font(root=root, family=family, size=10),
        "subtitle": tkfont.Font(root=root, family=family, size=12),
        "title": tkfont.Font(root=root, family=family, size=21, weight="bold"),
        "heading": tkfont.Font(root=root, family=family, size=16, weight="bold"),
        "button": tkfont.Font(root=root, family=family, size=11, weight="bold"),
        "mono": tkfont.Font(root=root, family="Consolas", size=10),
    }
    f = root._demo_fonts
    root.option_add("*Font", f["body"])
    root.configure(bg=c["bg"])
    st = ttk.Style(root)
    if "clam" in st.theme_names():
        st.theme_use("clam")
    st.configure(".", font=f["body"], background=c["bg"], foreground=c["text"])
    st.configure("TFrame", background=c["bg"])
    st.configure("TLabel", background=c["bg"], foreground=c["text"], font=f["body"])
    st.configure("Muted.TLabel", foreground=c["muted"], font=f["small"])
    st.configure("Heading.TLabel", foreground=c["navy"], font=f["heading"])
    st.configure("Footer.TFrame", background=c["navy"])
    st.configure("Footer.TLabel", background=c["navy"], foreground="#E5EDF9", font=f["small"])
    st.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=(2, 8, 0, 0))
    st.configure("TNotebook.Tab", padding=(17, 11), background="#E5ECF6", foreground=c["muted"],
                 font=f["button"], borderwidth=0)
    st.map("TNotebook.Tab", background=[("selected", c["card"]), ("active", "#D5E3FA")],
           foreground=[("selected", c["blue"]), ("active", c["navy"])])
    st.configure("TLabelframe", background=c["card"], bordercolor=c["line"], borderwidth=1,
                 relief="solid", padding=(12, 9))
    st.configure("TLabelframe.Label", background=c["bg"], foreground=c["navy"],
                 font=f["button"])
    st.configure("TButton", background="#E6ECF4", foreground=c["navy"], padding=(12, 8),
                 bordercolor=c["line"], borderwidth=1, relief="flat", font=f["body"])
    st.map("TButton", background=[("disabled", "#E5E7EB"), ("pressed", "#CEDBF0"),
                                   ("active", "#DAE7F8")],
           foreground=[("disabled", "#8391A3")])
    st.configure("Primary.TButton", background=c["blue"], foreground="#FFFFFF",
                 padding=(15, 9), borderwidth=0, relief="flat", font=f["button"])
    st.map("Primary.TButton", background=[("disabled", "#AFC3EC"), ("pressed", c["navy"]),
                                           ("active", c["blue_hover"])],
           foreground=[("disabled", "#F8FAFF"), ("active", "#FFFFFF")])
    st.configure("Success.TButton", background=c["teal"], foreground="#FFFFFF",
                 padding=(15, 9), borderwidth=0, relief="flat", font=f["button"])
    st.map("Success.TButton", background=[("disabled", "#A6CFC9"), ("active", "#0B645F")],
           foreground=[("active", "#FFFFFF")])
    st.configure("Danger.TButton", background="#FFF1F2", foreground="#B91C1C", padding=(12, 8),
                 bordercolor="#FECACA", font=f["body"])
    st.map("Danger.TButton", background=[("active", "#FFE4E6")])
    st.configure("TEntry", padding=(8, 7), bordercolor=c["line"], fieldbackground="#FFFFFF",
                 foreground=c["text"])
    st.configure("TCombobox", padding=(8, 6), fieldbackground="#FFFFFF", bordercolor=c["line"])
    st.configure("TCheckbutton", background=c["bg"], font=f["body"], foreground=c["text"])
    st.configure("TRadiobutton", background=c["bg"], font=f["body"], foreground=c["text"])
    st.configure("Treeview", background="#FFFFFF", fieldbackground="#FFFFFF", foreground=c["text"],
                 rowheight=30, font=f["small"], bordercolor=c["line"], borderwidth=0)
    st.map("Treeview", background=[("selected", "#DDEBFF")],
           foreground=[("selected", c["navy"])])
    st.configure("Treeview.Heading", background="#EAF0F9", foreground=c["navy"],
                 font=f["button"], padding=(8, 8), borderwidth=0)
    st.map("Treeview.Heading", background=[("active", "#DAE6F7")])
    return c


def style_demo_listbox(widget, fonts):
    widget.configure(font=fonts["body"], bg="#FFFFFF", fg=DEMO_PALETTE["text"],
                     selectbackground="#DCEBFF", selectforeground=DEMO_PALETTE["navy"],
                     highlightthickness=1, highlightbackground=DEMO_PALETTE["line"],
                     bd=0, relief="flat", activestyle="none")


def style_demo_text(widget, fonts, *, console=False):
    widget.configure(font=fonts["mono"] if console else fonts["body"],
                     bg="#F8FAFD" if console else "#FFFFFF", fg=DEMO_PALETTE["text"],
                     insertbackground=DEMO_PALETTE["blue"],
                     selectbackground="#C8DBFD", selectforeground=DEMO_PALETTE["navy"],
                     bd=0, relief="flat", highlightthickness=1,
                     highlightbackground=DEMO_PALETTE["line"], padx=12, pady=9)
# ================= 展示 UI 視覺主題結束 =================


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("離線個資偵測與去識別化系統｜畢業專題展示版")
        self.geometry("1370x900")
        self.minsize(1050, 720)
        self._demo_colors = apply_demo_theme(self)

        self.mapping = core.load_map()
        self.q = queue.Queue()
        self.busy = False
        self._on_done = None
        self.files = []
        self.outputs = []
        self.scan_results = []
        self.scan_dest = None
        self._recording = None      # (StringVar, Entry) 正在錄製快速鍵的欄位
        self._map_n = -1            # 偵測快速鍵背景處理造成的對照表變動
        self._tick = 0
        self._store_sig = None      # 名單檔案的修改時間，變了就自動重新整理
        self._items_rows, self._blocked_rows, self._safe_rows = [], [], []
        self.service = hotkey_service.HotkeyService(self.mapping)

        sys.stdout = QueueWriter(self.q)  # 之後所有 print 都進記錄區

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        print(f"[就緒] 對照表 {len(self.mapping)} 筆、敏感清單 {entity_store.items_count()} 筆、"
              f"安全清單 {len(entity_store.load_safe())} 筆、黑名單 {len(entity_store.load_blocked())} 筆")
        self._start(core.warmup, "載入模型中…", None)  # 背景預熱


    def _build_brand_header(self):
        """固定標題列；僅顯示功能資訊，不宣稱 PDF 安全驗收或 GPU 已上線。"""
        c = self._demo_colors
        header = tk.Frame(self, bg=c["navy"], height=94)
        header.pack(fill="x")
        header.pack_propagate(False)
        row = tk.Frame(header, bg=c["navy"])
        row.pack(fill="both", expand=True, padx=24, pady=15)
        left = tk.Frame(row, bg=c["navy"])
        left.pack(side="left", fill="y")
        tk.Label(left, text="◆  離線個資偵測與去識別化系統", font=self._demo_fonts["heading"],
                 fg="#FFFFFF", bg=c["navy"], anchor="w").pack(anchor="w")
        tk.Label(left, text="AI 個資辨識  /  多格式遮蔽  /  剪貼簿保護", font=self._demo_fonts["small"],
                 fg="#C8D7EF", bg=c["navy"], anchor="w").pack(anchor="w", pady=(5, 0))
        tk.Label(row, text="本機處理  ·  畢業專題展示版", bg="#174A62", fg="#E8FBF7",
                 font=self._demo_fonts["small"], padx=14, pady=8).pack(side="right", anchor="center")
        tk.Frame(self, bg="#2D78DB", height=3).pack(fill="x")

    def _section_intro(self, parent, title, subtitle):
        head = ttk.Frame(parent)
        head.pack(fill="x", pady=(0, 12))
        ttk.Label(head, text=title, style="Heading.TLabel").pack(anchor="w")
        ttk.Label(head, text=subtitle, style="Muted.TLabel").pack(anchor="w", pady=(5, 0))

    def _overview_card(self, parent, col, row, number, title, detail, action, target):
        c = self._demo_colors
        outer = tk.Frame(parent, bg=c["line"])
        outer.grid(row=row, column=col, sticky="nsew", padx=8, pady=8)
        card = tk.Frame(outer, bg=c["card"])
        card.pack(fill="both", expand=True, padx=1, pady=1)
        top = tk.Frame(card, bg=c["card"])
        top.pack(fill="x", padx=15, pady=(9, 4))
        tk.Label(top, text=number, bg=c["soft_blue"], fg=c["blue"],
                 font=self._demo_fonts["button"], padx=9, pady=4).pack(side="left")
        tk.Label(top, text=title, bg=c["card"], fg=c["navy"],
                 font=self._demo_fonts["heading"]).pack(side="left", padx=12)
        tk.Label(card, text=detail, bg=c["card"], fg=c["muted"], font=self._demo_fonts["body"],
                 anchor="w", justify="left", wraplength=480).pack(fill="x", padx=15, pady=(2, 7))
        ttk.Button(card, text=action, style="Primary.TButton" if number == "01" else "TButton",
                   command=lambda: self.nb.select(self._demo_pages[target])).pack(anchor="w", padx=15, pady=(0, 9))

    def _build_overview(self, notebook):
        home = ttk.Frame(notebook, padding=(16, 16, 16, 14))
        notebook.add(home, text="總覽")
        self._demo_pages = {"home": home}
        self._section_intro(home, "專題功能總覽", "以合成測試資料展示系統功能；請從下方選擇操作項目。")
        hero = tk.Frame(home, bg="#EAF2FF")
        hero.pack(fill="x", padx=8, pady=(0, 12))
        tk.Label(hero, text="讓個人資料去識別化更容易", font=self._demo_fonts["heading"],
                 bg="#EAF2FF", fg=self._demo_colors["navy"]).pack(anchor="w", padx=22, pady=(9, 0))
        tk.Label(hero, text="文字與程式碼辨識  ·  圖片 / PDF 文字遮蔽  ·  剪貼簿操作  ·  本機資料管理",
                 font=self._demo_fonts["body"], bg="#EAF2FF", fg=self._demo_colors["muted"]).pack(
                     anchor="w", padx=22, pady=(0, 11))
        cards = ttk.Frame(home)
        cards.pack(fill="both", expand=True)
        cards.grid_columnconfigure(0, weight=1, uniform="overview")
        cards.grid_columnconfigure(1, weight=1, uniform="overview")
        cards.grid_rowconfigure(0, weight=1)
        cards.grid_rowconfigure(1, weight=1)
        self._overview_card(cards, 0, 0, "01", "文字去識別化",
                            "輸入合成姓名、電話或 Email；使用 AI 模型與規則辨識並以標籤遮蔽。",
                            "開始文字展示", "text")
        self._overview_card(cards, 1, 0, "02", "檔案與 PDF 處理",
                            "選擇合成圖片、PDF 或文件，查看處理結果與輸出檔案清單。",
                            "開啟檔案處理", "file")
        self._overview_card(cards, 0, 1, "03", "資料夾掃描",
                            "在原資料夾旁建立輸出副本並逐檔掃描；正式資料請先確認處理限制。",
                            "查看資料夾功能", "scan")
        self._overview_card(cards, 1, 1, "04", "名單、快速鍵與對照表",
                            "管理敏感清單與安全清單、設定全域快捷鍵，以及查看遮蔽標籤。",
                            "檢視管理功能", "lists")
        ttk.Label(home, text="展示提醒：請使用虛構個資；此首頁不代表模型準確率或正式 PDF 安全驗收結果。",
                  style="Muted.TLabel").pack(anchor="w", padx=9, pady=(12, 0))

    # ================= 介面 =================
    def _build(self):
        self._build_brand_header()
        self.nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=16, pady=(6, 5))
        self._build_overview(nb)

        # ===== 文字分頁 =====
        t = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(t, text="文字處理")
        self._demo_pages["text"] = t
        self._section_intro(t, "文字去識別化", "支援中文、英文及規則辨識。可選擇遮蔽或依現有對照表還原。")

        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="內容類型：").pack(side="left")
        self.kind_var = tk.StringVar(value=KIND_TEXT)
        # 兩個互斥的切換按鈕：按下其中一個，另一個自動彈起 (共用同一個 variable)
        for kind in (KIND_CODE, KIND_TEXT):
            tk.Radiobutton(bar, text=kind, variable=self.kind_var, value=kind, indicatoron=False,
                           width=9, pady=5, selectcolor="#DCEBFF",
                           bg="#E6ECF4", fg="#14253D", activebackground="#DCEBFF",
                           relief="flat", bd=0, font=self._demo_fonts["body"]).pack(side="left", padx=2)
        self.btn_read = ttk.Button(bar, text="讀取剪貼簿", command=self.read_clipboard_text)
        self.btn_read.pack(side="left", padx=(12, 2))
        self.btn_run = ttk.Button(bar, text="開始辨識 / 遮蔽", command=self.run_text, style="Primary.TButton")
        self.btn_run.pack(side="left", padx=2)
        self.pp_var = tk.BooleanVar(value=preprocess.ENABLED)
        ttk.Checkbutton(bar, text="送模型前先前處理", variable=self.pp_var,
                        command=self._toggle_preprocess).pack(side="left", padx=(14, 2))
        ttk.Button(bar, text="預覽前處理", command=self.preview_preprocess).pack(side="left", padx=2)

        # 使用者明確選擇文字處理方式，預設只遮蔽，避免自動還原造成個資外露。
        mode_bar = ttk.Frame(t)
        mode_bar.pack(fill="x", pady=(3, 5))
        ttk.Label(mode_bar, text="處理方式：").pack(side="left")
        self.text_action_var = tk.StringVar(value="mask")
        ttk.Radiobutton(mode_bar, text="遮蔽（保留已存在標籤）", variable=self.text_action_var,
                        value="mask").pack(side="left", padx=(5, 15))
        ttk.Radiobutton(mode_bar, text="還原（依對照表）", variable=self.text_action_var,
                        value="restore").pack(side="left")
        ttk.Label(t, text=(
            "A（uer/roberta-base-finetuned-cluener2020-chinese）  "
            "C（Davlan/bert-base-multilingual-cased-ner-hrl）"
        ), foreground="#475569", justify="left", wraplength=980).pack(anchor="w", pady=(0, 5))

        # 在文字分頁保留最近一次的處理狀態，提示視窗關閉後仍可查看。
        self.text_notice_var = tk.StringVar(value="選擇遮蔽或還原，按「判斷」執行。")
        ttk.Label(t, textvariable=self.text_notice_var, foreground="#475569",
                  justify="left", wraplength=980).pack(anchor="w", pady=(0, 5))

        panes = ttk.PanedWindow(t, orient="vertical")
        panes.pack(fill="both", expand=True)
        f1 = ttk.LabelFrame(panes, text="輸入")
        f2 = ttk.LabelFrame(panes, text="結果")
        self.txt_in = scrolledtext.ScrolledText(f1, height=10, wrap="word", undo=True)
        style_demo_text(self.txt_in, self._demo_fonts)
        self.txt_in.pack(fill="both", expand=True)
        self.txt_out = scrolledtext.ScrolledText(f2, height=10, wrap="word")
        style_demo_text(self.txt_out, self._demo_fonts)
        self.txt_out.pack(fill="both", expand=True)
        panes.add(f1, weight=1)
        panes.add(f2, weight=1)

        bar2 = ttk.Frame(t)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="結果複製到剪貼簿", command=self.copy_result_text).pack(side="left")
        ttk.Button(bar2, text="結果移到輸入框", command=self.result_to_input).pack(side="left", padx=4)
        ttk.Button(bar2, text="清空", command=self.clear_text).pack(side="left")
        # 讓底部結果操作鍵保留可見空間，避免大型輸入/輸出框將它擠出螢幕。
        panes.pack_forget()
        bar2.pack_forget()
        bar2.pack(side="bottom", fill="x", pady=4)
        panes.pack(side="top", fill="both", expand=True)

        # ===== 檔案分頁 =====
        f = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(f, text="檔案 / PDF")
        self._demo_pages["file"] = f
        self._section_intro(f, "圖片與文件去識別化", "新增合成圖片、PDF 或文字檔，執行個資偵測與遮蔽；下方保留原始檔案處理流程。")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="新增檔案…", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="從剪貼簿讀取檔案", command=self.read_clipboard_files).pack(side="left", padx=4)
        ttk.Button(bar, text="移除選取", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="清空清單", command=self.clear_files).pack(side="left", padx=4)
        self.btn_run_files = ttk.Button(bar, text="開始處理檔案", command=self.run_files, style="Primary.TButton")
        self.btn_run_files.pack(side="right")
        # SEAMLESS_RESTORE_VISIBLE_UI_V2: 固定顯示在檔案處理操作列。
        self.btn_restore_media = ttk.Button(bar, text="還原 PDF／圖片…",
                                            command=self.restore_media, style="Success.TButton")
        self.btn_restore_media.pack(side="right", padx=(0, 8))

        lf = ttk.LabelFrame(f, text="待處理檔案")
        lf.pack(fill="both", expand=True, pady=(0, 4))
        self.lst_files = tk.Listbox(lf, selectmode="extended", height=5)
        style_demo_listbox(self.lst_files, self._demo_fonts)
        self.lst_files.pack(fill="both", expand=True)

        lo = ttk.LabelFrame(f, text="已產生的去識別化檔案")
        lo.pack(fill="both", expand=True)
        self.lst_out = tk.Listbox(lo, selectmode="extended", height=5)
        style_demo_listbox(self.lst_out, self._demo_fonts)
        self.lst_out.pack(fill="both", expand=True)

        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="輸出檔案複製到剪貼簿", command=self.copy_outputs).pack(side="left")
        ttk.Button(bar2, text="開啟所在資料夾", command=self.open_output_folder).pack(side="left", padx=4)

        # ===== 資料夾掃描分頁 =====
        s = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(s, text="資料夾掃描")
        self._demo_pages["scan"] = s
        self._section_intro(s, "資料夾掃描", "系統會建立整份資料夾的副本；請不要將未驗收的副本視為已安全去識別化。")
        self._build_scan_tab(s)

        # ===== 名單管理分頁 =====
        lists = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(lists, text="名單管理")
        self._demo_pages["lists"] = lists
        self._section_intro(lists, "個資名單管理", "管理敏感清單、黑名單與安全清單；請勿在公開展示時開啟真實明碼清單。")
        self._build_lists_tab(lists)

        # ===== 快速鍵分頁 =====
        h = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(h, text="快速鍵")
        self._demo_pages["hotkey"] = h
        self._section_intro(h, "快速鍵與剪貼簿操作", "可錄製組合鍵，讓剪貼簿內容快速進行去識別化處理。")
        self._build_hotkey_tab(h)

        # ===== 對照表分頁 =====
        m = ttk.Frame(nb, padding=(16, 14, 16, 12))
        nb.add(m, text="對照表")
        self._demo_pages["map"] = m
        self._section_intro(m, "遮蔽標籤對照表", "這裡會顯示明碼原始值；公開展示時請只使用虛構測試資料。")
        bar = ttk.Frame(m)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="重新整理", command=self.refresh_map_view).pack(side="left")
        ttk.Button(bar, text="清除對照表", command=self.clear_map, style="Danger.TButton").pack(side="left", padx=4)
        self.tree = make_tree(m, [("tag", "標籤", 200, "w"), ("val", "原始值 (明碼)", 600, "w")])
        self.refresh_map_view()

        nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_lists())

        # ===== 記錄區 =====
        lg = ttk.LabelFrame(self, text="執行紀錄", padding=(10, 6))
        lg.pack(fill="x", expand=False, padx=16, pady=(0, 9))
        self.log = scrolledtext.ScrolledText(lg, height=4, wrap="none", state="disabled")
        style_demo_text(self.log, self._demo_fonts, console=True)
        self.log.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就緒")
        footer = ttk.Frame(self, style="Footer.TFrame", padding=(18, 9, 18, 9))
        footer.pack(fill="x")
        ttk.Label(footer, text="●  系統狀態", style="Footer.TLabel").pack(side="left")
        ttk.Label(footer, textvariable=self.status, style="Footer.TLabel").pack(side="left", padx=(12, 0))
        ttk.Label(footer, text="畢業專題展示  ·  請使用合成資料", style="Footer.TLabel").pack(side="right")

        # 固定狀態列與執行紀錄可見；Notebook 只使用剩餘的顯示空間。
        # 須以 bottom -> log -> notebook 的順序重新 pack，避免首頁把記錄區擠出畫面。
        self.nb.pack_forget()
        lg.pack_forget()
        footer.pack_forget()
        footer.pack(side="bottom", fill="x")
        lg.pack(side="bottom", fill="x", expand=False, padx=16, pady=(0, 9))
        self.nb.pack(side="top", fill="both", expand=True, padx=16, pady=(6, 5))

        self._busy_widgets = [self.btn_read, self.btn_run,
                              self.btn_run_files, self.btn_restore_media, self.btn_scan, self.btn_add]
        self.refresh_lists()

    # ================= 資料夾掃描 =================
    def _build_scan_tab(self, parent):
        self.scan_dir = tk.StringVar(value="")
        self.scan_recursive = tk.BooleanVar(value=True)
        self.scan_summary = tk.StringVar(value="尚未掃描")

        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=6)
        ttk.Label(bar, text="資料夾：").pack(side="left")
        ttk.Entry(bar, textvariable=self.scan_dir).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(bar, text="瀏覽…", command=self.pick_scan_dir).pack(side="left")
        ttk.Checkbutton(bar, text="包含子資料夾", variable=self.scan_recursive).pack(side="left", padx=8)
        self.btn_scan = ttk.Button(bar, text="開始掃描並遮蔽", command=self.run_scan, style="Primary.TButton")
        self.btn_scan.pack(side="left")

        ttk.Label(parent, justify="left", wraplength=1000, foreground="#555555", text=(
            "流程：在選擇的資料夾「同層」建立 <資料夾名>_masked，先把裡面所有東西複製過去，再只處理複製出來的檔案——"
            "檔名不變，有敏感資料的檔案內容會被遮蔽，其他檔案原樣保留。原資料夾完全不動。")
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(parent, textvariable=self.scan_summary, font=("", 10, "bold")).pack(anchor="w", pady=(2, 4))

        self.scan_tree = make_tree(parent, [("risk", "風險", 60, "center"), ("file", "檔案 (相對路徑)", 420, "w"),
                                            ("kind", "類型", 80, "center"), ("hits", "筆數", 60, "center"),
                                            ("labels", "偵測到的類型", 360, "w")])
        self.scan_tree.tag_configure("高", foreground="#b91c1c")
        self.scan_tree.tag_configure("中", foreground="#b45309")
        self.scan_tree.tag_configure("錯誤", foreground="#6b7280")

        bar2 = ttk.Frame(parent)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="開啟遮蔽後的資料夾", command=self.open_scan_dest).pack(side="left")
        ttk.Button(bar2, text="開啟選取檔案的位置", command=self.open_scan_file).pack(side="left", padx=4)

    def pick_scan_dir(self):
        d = filedialog.askdirectory(title="選擇要掃描的資料夾")
        if d:
            self.scan_dir.set(os.path.normpath(d))

    def run_scan(self):
        folder = self.scan_dir.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("提示", "請先選擇有效的資料夾")
            return
        recursive = self.scan_recursive.get()

        def progress(i, total, path):
            self.q.put(("status", f"掃描中 {i}/{total}：{os.path.basename(path)}"))

        def job():
            print("\n" + "=" * 50)
            return core.scan_folder(folder, self.mapping, recursive=recursive, progress=progress)

        def done(res):
            dest, results = res
            self.scan_dest, self.scan_results = dest, results
            self.scan_tree.delete(*self.scan_tree.get_children())
            for i, r in enumerate(results):
                rel = os.path.relpath(r["path"], folder)
                labels = ", ".join(f"{k}×{v}" for k, v in r["labels"].items()) or (r["error"] or "")
                self.scan_tree.insert("", "end", iid=str(i), tags=(r["risk"],), values=(
                    r["risk"] if r["risk"] != "無" else "安全", rel, r["kind"], r["hits"] or "", labels))
            danger = [r for r in results if r["output"]]
            high = sum(1 for r in danger if r["risk"] == "高")
            self.scan_summary.set(f"共 {len(results)} 個檔案：危險 {len(danger)} 個 (高風險 {high})，"
                                  f"安全 {len(results) - len(danger)} 個　→　已遮蔽的副本：{dest}")
            print("\n" + core.format_scan_report(results, dest))
            self.refresh_lists()
            if danger:
                lines = [f"[{r['risk']}] {os.path.relpath(r['path'], folder)}" for r in danger[:15]]
                more = f"\n… 另外 {len(danger) - 15} 個，請看清單" if len(danger) > 15 else ""
                messagebox.showwarning("發現含敏感資訊的檔案",
                                       f"共 {len(danger)} 個檔案含敏感資訊。\n"
                                       f"遮蔽後的完整資料夾：\n{dest}\n\n" + "\n".join(lines) + more)
            else:
                messagebox.showinfo("掃描完成", f"沒有發現含敏感資訊的檔案。\n(複製的資料夾：{dest})")

        self._start(job, "複製並掃描資料夾中…", done)

    def open_scan_dest(self):
        if self.scan_dest and os.path.isdir(self.scan_dest):
            subprocess.Popen(["explorer", os.path.normpath(self.scan_dest)])
        else:
            messagebox.showinfo("提示", "還沒有掃描結果")

    def open_scan_file(self):
        sel = self.scan_tree.selection()
        if not sel or not self.scan_dest:
            return
        r = self.scan_results[int(sel[0])]
        path = os.path.join(self.scan_dest, os.path.relpath(r["path"], self.scan_dir.get().strip()))
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    # ================= 名單管理 =================
    def _build_lists_tab(self, parent):
        nb2 = ttk.Notebook(parent)
        nb2.pack(fill="both", expand=True)

        # ---------- 敏感清單 ----------
        f = ttk.Frame(nb2)
        nb2.add(f, text="敏感清單")
        self.item_filter = tk.StringVar()
        self.items_info = tk.StringVar()
        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="篩選：").pack(side="left")
        ttk.Entry(bar, textvariable=self.item_filter, width=22).pack(side="left", padx=4)
        ttk.Button(bar, text="刪除選取", command=self.block_selected).pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="標為安全", command=self.safe_selected).pack(side="left", padx=2)
        ttk.Button(bar, text="重新整理", command=self.refresh_lists).pack(side="left", padx=2)
        ttk.Label(bar, textvariable=self.items_info).pack(side="left", padx=10)
        self.item_filter.trace_add("write", lambda *_: self.refresh_lists())
        self.items_tree = make_tree(f, [("x", "刪除", 54, "center"), ("value", "敏感值 (明碼)", 280, "w"),
                                        ("label", "類型", 130, "w"), ("count", "次數", 50, "center"),
                                        ("how", "類型判斷", 150, "w"), ("last", "最近出現", 140, "w"),
                                        ("sources", "來源", 220, "w")])
        self.items_tree.bind("<ButtonRelease-1>", lambda e: self._click_x(e, self.items_tree, self._items_rows,
                                                                          lambda vals: self._do_block(vals)))
        self.items_tree.bind("<Delete>", lambda _e: self.block_selected())
        ttk.Label(f, justify="left", wraplength=1000, foreground="#555555", text=(
            "• 點一下最左邊的「✕」(或選取後按 Delete / 「刪除選取」) 就會立刻刪除，不會再問。\n"
            "• 刪掉的值會進「黑名單」，之後永遠不會再被自動加進這份清單；想反悔到「黑名單」分頁移出即可。\n"
            "• 「標為安全」會把值放進「安全清單」：之後偵測到也不遮蔽。\n"
            f"• 清單存在 {entity_store.SENSITIVE_FILE}，結束程式時不會被刪除；內容是明碼，請注意檔案權限。")
        ).pack(fill="x", padx=4, pady=4)

        # ---------- 新增敏感資料 ----------
        a = ttk.Frame(nb2)
        nb2.add(a, text="新增敏感資料")
        ttk.Label(a, text="輸入要加入敏感清單的資料 (一行一筆；可只填值，也可寫成「類型<Tab>值」)：").pack(anchor="w", pady=(6, 2))
        self.txt_add = scrolledtext.ScrolledText(a, height=7, wrap="word", undo=True)
        style_demo_text(self.txt_add, self._demo_fonts)
        self.txt_add.pack(fill="x")
        bar = ttk.Frame(a)
        bar.pack(fill="x", pady=6)
        ttk.Label(bar, text="類型：").pack(side="left")
        self.add_label = tk.StringVar(value=AUTO_LABEL)
        ttk.Combobox(bar, textvariable=self.add_label, values=LABEL_CHOICES, width=26).pack(side="left", padx=4)
        self.btn_add = ttk.Button(bar, text="新增到敏感清單", command=self.run_add_entities, style="Success.TButton")
        self.btn_add.pack(side="left", padx=(12, 2))
        ttk.Button(bar, text="新增到安全清單", command=self.add_to_safe_from_box).pack(side="left", padx=2)
        ttk.Button(bar, text="匯入文字檔…", command=self.import_entity_file).pack(side="left", padx=2)
        ttk.Button(bar, text="清空", command=lambda: self.txt_add.delete("1.0", "end")).pack(side="left", padx=2)
        ttk.Label(a, justify="left", wraplength=1000, foreground="#555555", text=(
            "• 類型不用填：選「自動判斷」(預設) 時，系統會用規則與模型 (Presidio、中文 NER) 判斷這個值是人名、機構、地址、電話…；"
            "實在判斷不出來才標成 SENSITIVE。想自己指定就從下拉選單挑，或直接輸入。\n"
            "• 如果值原本在黑名單或安全清單裡，明確手動新增會自動把它移出。")
        ).pack(fill="x", pady=(0, 4))
        self.add_tree = make_tree(a, [("value", "值", 300, "w"), ("label", "判斷出的類型", 150, "w"),
                                      ("how", "判斷方式", 200, "w"), ("status", "結果", 70, "center"),
                                      ("note", "備註", 300, "w")], height=8)

        # ---------- 黑名單 ----------
        b = ttk.Frame(nb2)
        nb2.add(b, text="黑名單")
        bar = ttk.Frame(b)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="移出黑名單 (選取)", command=self.unblock_selected).pack(side="left")
        self.blocked_info = tk.StringVar()
        ttk.Label(bar, textvariable=self.blocked_info).pack(side="left", padx=10)
        self.blocked_tree = make_tree(b, [("x", "移出", 54, "center"), ("value", "值 (明碼)", 320, "w"),
                                          ("label", "原類型", 140, "w"), ("count", "原次數", 60, "center"),
                                          ("at", "加入黑名單時間", 160, "w")])
        self.blocked_tree.bind("<ButtonRelease-1>", lambda e: self._click_x(
            e, self.blocked_tree, self._blocked_rows, lambda vals: self._do_unblock(vals)))
        ttk.Label(b, justify="left", wraplength=1000, foreground="#555555", text=(
            "黑名單：從敏感清單刪除的值。這些值永遠不會再被「自動」加進敏感清單 "
            "(模型仍可能在文字裡偵測到並遮蔽它，只是不會再被記錄)。檔案永久保留："
            f"{entity_store.BLOCKED_FILE}")
        ).pack(fill="x", padx=4, pady=4)

        # ---------- 安全清單 ----------
        sf = ttk.Frame(nb2)
        nb2.add(sf, text="安全清單")
        bar = ttk.Frame(sf)
        bar.pack(fill="x", pady=4)
        self.safe_input = tk.StringVar()
        ttk.Label(bar, text="新增安全資料：").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.safe_input, width=34)
        ent.pack(side="left", padx=4)
        ent.bind("<Return>", lambda _e: self.add_safe_entry())
        ttk.Button(bar, text="加入", command=self.add_safe_entry).pack(side="left")
        ttk.Button(bar, text="移出安全清單 (選取)", command=self.unsafe_selected).pack(side="left", padx=(12, 2))
        self.safe_info = tk.StringVar()
        ttk.Label(bar, textvariable=self.safe_info).pack(side="left", padx=10)
        self.safe_tree = make_tree(sf, [("x", "移出", 54, "center"), ("value", "值 (明碼)", 340, "w"),
                                        ("note", "備註", 240, "w"), ("at", "加入時間", 160, "w")])
        self.safe_tree.bind("<ButtonRelease-1>", lambda e: self._click_x(
            e, self.safe_tree, self._safe_rows, lambda vals: self._do_unsafe(vals)))
        ttk.Label(sf, justify="left", wraplength=1000, foreground="#555555", text=(
            "安全清單：確認「不是敏感資料」的值 (例如公司公開的名稱、範例帳號)。偵測結果剛好等於清單裡的值時會直接略過、不遮蔽，"
            f"也不會被記進敏感清單。檔案永久保留：{entity_store.SAFE_FILE}")
        ).pack(fill="x", padx=4, pady=4)

    # ---------- 通用：點 ✕ 欄位立刻執行 ----------
    def _click_x(self, event, tree, rows, action):
        if tree.identify_region(event.x, event.y) != "cell" or tree.identify_column(event.x) != "#1":
            return
        iid = tree.identify_row(event.y)
        if iid:
            action([rows[int(iid)]["value"]])

    def _selected_values(self, tree, rows):
        return [rows[int(i)]["value"] for i in tree.selection()]

    # ---------- 動作 (都不跳確認) ----------
    def _do_block(self, values):
        if values:
            n = entity_store.block_items(values)
            print(f"[黑名單] 已刪除 {n} 筆並加入黑名單：{', '.join(values[:5])}{' …' if len(values) > 5 else ''}")
            self.refresh_lists()

    def _do_unblock(self, values):
        if values:
            print(f"[黑名單] 已移出黑名單 {entity_store.unblock(values)} 筆")
            self.refresh_lists()

    def _do_unsafe(self, values):
        if values:
            print(f"[安全清單] 已移出 {entity_store.unsafe(values)} 筆")
            self.refresh_lists()

    def block_selected(self):
        self._do_block(self._selected_values(self.items_tree, self._items_rows))

    def safe_selected(self):
        values = self._selected_values(self.items_tree, self._items_rows)
        if values:
            print(f"[安全清單] 已標為安全 {entity_store.mark_safe(values, note='從敏感清單標記')} 筆")
            self.refresh_lists()

    def unblock_selected(self):
        self._do_unblock(self._selected_values(self.blocked_tree, self._blocked_rows))

    def unsafe_selected(self):
        self._do_unsafe(self._selected_values(self.safe_tree, self._safe_rows))

    def add_safe_entry(self):
        v = self.safe_input.get().strip()
        if v:
            print(f"[安全清單] 已加入 {entity_store.mark_safe([v], note='手動新增')} 筆：{v}")
            self.safe_input.set("")
            self.refresh_lists()

    # ---------- 新增敏感資料 ----------
    def _entity_items_from_box(self):
        sel = self.add_label.get().strip()
        default = None if (not sel or sel.startswith("自動")) else sel
        return [(v, l or default) for v, l in entity_store.parse_entity_lines(self.txt_add.get("1.0", "end-1c"))]

    def run_add_entities(self):
        items = self._entity_items_from_box()
        if not items:
            messagebox.showinfo("提示", "請先輸入要新增的資料")
            return
        need_model = any(not l for _, l in items)

        def job():
            return entity_store.add_manual(items, source="UI 手動新增")

        def done(results):
            self.add_tree.delete(*self.add_tree.get_children())
            for r in results:
                self.add_tree.insert("", "end", values=(r["value"], r["label"], r["how"], r["status"], r["note"]))
            n_new = sum(1 for r in results if r["status"] == "新增")
            print(f"[敏感清單] 手動新增完成：新增 {n_new} 筆，共處理 {len(results)} 筆")
            self.txt_add.delete("1.0", "end")
            self.refresh_lists()

        self._start(job, "用模型判斷類型並新增中…" if need_model else "新增中…", done)

    def add_to_safe_from_box(self):
        items = entity_store.parse_entity_lines(self.txt_add.get("1.0", "end-1c"))
        if not items:
            messagebox.showinfo("提示", "請先輸入要加入安全清單的資料")
            return
        n = entity_store.mark_safe([v for v, _ in items], note="手動新增")
        print(f"[安全清單] 已加入 {n} 筆")
        self.txt_add.delete("1.0", "end")
        self.refresh_lists()

    def import_entity_file(self):
        path = filedialog.askopenfilename(title="匯入文字檔 (每行「值」或「類型<Tab>值」)",
                                          filetypes=[("文字檔", "*.txt *.csv *.tsv"), ("所有檔案", "*.*")])
        if not path:
            return
        try:
            with open(path, encoding="utf-8-sig") as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            messagebox.showerror("匯入失敗", str(e))
            return
        self.txt_add.insert("end", ("\n" if self.txt_add.get("1.0", "end-1c") else "") + text)
        print(f"[匯入] 已讀入 {path}，確認內容後按「新增到敏感清單」")

    # ---------- 重新整理 ----------
    def refresh_lists(self):
        if not hasattr(self, "safe_tree"):   # 介面還沒建完
            return
        self._store_sig = entity_store.signature()
        flt = self.item_filter.get().strip().lower()
        self._items_rows = [r for r in entity_store.load_items()
                            if not flt or flt in r["value"].lower() or flt in r.get("label", "").lower()]
        self.items_tree.delete(*self.items_tree.get_children())
        for i, r in enumerate(self._items_rows):
            self.items_tree.insert("", "end", iid=str(i), values=(
                "✕", r["value"], r.get("label", ""), r.get("count", 1), r.get("label_source", ""),
                r.get("last_seen", ""), ", ".join(r.get("sources", []))))
        total = entity_store.items_count()
        self.items_info.set(f"共 {total} 筆" + (f" (顯示 {len(self._items_rows)})" if flt else "") +
                            ("" if core.USE_KNOWN_ITEMS else "　(自動遮蔽已關閉：core.USE_KNOWN_ITEMS = False)"))

        self._blocked_rows = entity_store.load_blocked()
        self.blocked_tree.delete(*self.blocked_tree.get_children())
        for i, r in enumerate(self._blocked_rows):
            self.blocked_tree.insert("", "end", iid=str(i), values=(
                "↩", r["value"], r.get("label", ""), r.get("count", ""), r.get("blocked_at", "")))
        self.blocked_info.set(f"共 {len(self._blocked_rows)} 筆")

        self._safe_rows = entity_store.load_safe()
        self.safe_tree.delete(*self.safe_tree.get_children())
        for i, r in enumerate(self._safe_rows):
            self.safe_tree.insert("", "end", iid=str(i), values=(
                "↩", r["value"], r.get("note", ""), r.get("added_at", "")))
        self.safe_info.set(f"共 {len(self._safe_rows)} 筆")

    # ================= 快速鍵 =================
    def _build_hotkey_tab(self, parent):
        cfg = hotkey_service.load_config()
        self.hk_copy = tk.StringVar(value=cfg["copy"] or "")
        self.hk_paste = tk.StringVar(value=cfg["paste"] or "")
        self.hk_hint = tk.StringVar(value="")
        self.hk_mode = tk.StringVar(value="")
        self.svc_state = tk.StringVar(value="全域快速鍵：未啟動")

        box = ttk.LabelFrame(parent, text="快速鍵設定 (按「錄製」後直接按下想用的組合鍵)")
        box.pack(fill="x", padx=4, pady=8)
        box.columnconfigure(1, weight=1)

        ttk.Label(box, text="複製 / 遮蔽：").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.ent_copy = ttk.Entry(box, textvariable=self.hk_copy, state="readonly", width=32)
        self.ent_copy.grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(box, text="錄製", command=lambda: self._record(self.hk_copy, self.ent_copy)
                   ).grid(row=0, column=2, padx=4)

        ttk.Label(box, text="貼上：").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        self.ent_paste = ttk.Entry(box, textvariable=self.hk_paste, state="readonly", width=32)
        self.ent_paste.grid(row=1, column=1, sticky="we", padx=4)
        ttk.Button(box, text="錄製", command=lambda: self._record(self.hk_paste, self.ent_paste)
                   ).grid(row=1, column=2, padx=4)
        ttk.Button(box, text="清除 (改用模式 B)", command=self._clear_paste).grid(row=1, column=3, padx=4)

        ttk.Label(box, textvariable=self.hk_hint, foreground="#b45309").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 4))
        ttk.Label(box, textvariable=self.hk_mode, wraplength=960, justify="left").grid(
            row=3, column=0, columnspan=4, sticky="w", padx=6, pady=(0, 6))

        for ent in (self.ent_copy, self.ent_paste):
            ent.bind("<KeyPress>", self._on_hotkey_key)
            ent.bind("<FocusOut>", self._stop_recording)
        self.hk_paste.trace_add("write", lambda *_: self._update_mode_label())
        self._update_mode_label()

        bar = ttk.Frame(parent)
        bar.pack(fill="x", padx=4, pady=4)
        ttk.Button(bar, text="套用並儲存", command=self.apply_hotkeys, style="Primary.TButton").pack(side="left")
        ttk.Button(bar, text="還原預設", command=self.reset_hotkeys).pack(side="left", padx=4)
        self.btn_svc = ttk.Button(bar, text="啟動全域快速鍵", command=self.toggle_service)
        self.btn_svc.pack(side="left", padx=(16, 4))
        ttk.Label(bar, textvariable=self.svc_state).pack(side="left", padx=8)

        tip = ("說明：\n"
               "• 模式 A：貼上欄有值。複製只產生覆蓋結果，按貼上快速鍵才會貼出去，原生 Ctrl+C / Ctrl+V 不受影響 (例如 Ctrl+Alt+C / Ctrl+Alt+V)。\n"
               "• 模式 A 且設成 Ctrl+C / Ctrl+V：原生複製貼上會被取代，並監聽剪貼簿——右鍵複製、網頁複製鈕、Ctrl+X 也會處理；\n"
               "  複製後剪貼簿就會是處理後的內容 (不保留原文)，所以右鍵貼上貼出的也是處理後的內容。\n"
               "• 模式 B：貼上欄清空，複製鍵通常設成 Ctrl+C。複製時直接覆寫剪貼簿，之後用原生 Ctrl+V 貼上；"
               "處理完成前就貼上會貼出原始內容。\n"
               "• 設定存在 hotkeys.json，命令列的 main.py 啟動時也會讀取同一份設定。\n"
               "• 全域快速鍵由本視窗提供；關閉視窗後即停止。")
        ttk.Label(parent, text=tip, wraplength=1000, justify="left").pack(fill="x", padx=8, pady=8)

    def _update_mode_label(self):
        if self.hk_paste.get().strip():
            self.hk_mode.set("目前模式：A (專屬快速鍵)。複製鍵產生覆蓋結果，貼上鍵才貼出，剪貼簿貼完會還原。")
        else:
            self.hk_mode.set("目前模式：B (Ctrl+C 直接模式)。複製時直接覆寫剪貼簿，之後按原生 Ctrl+V 貼上。"
                             "若複製鍵設為 Ctrl+C，在終端機按 Ctrl+C 也會觸發。")

    def _record(self, var, entry):
        self._recording = (var, entry)
        self.hk_hint.set("請按下要設定的組合鍵…  (Esc 取消)")
        entry.focus_set()

    def _stop_recording(self, _event=None):
        if self._recording:
            self._recording = None
            self.hk_hint.set("")

    def _clear_paste(self):
        self.hk_paste.set("")
        self._stop_recording()

    def _on_hotkey_key(self, event):
        if not self._recording or self._recording[1] is not event.widget:
            return "break"
        if event.keysym == "Escape":
            self._stop_recording()
            return "break"
        hk, err = hotkey_from_event(event)
        if err:
            self.hk_hint.set(err)
        elif hk:
            self._recording[0].set(hk)
            self._recording = None
            self.hk_hint.set("")
        return "break"

    def _current_cfg(self):
        return {"copy": self.hk_copy.get().strip() or None,
                "paste": self.hk_paste.get().strip() or None}

    def apply_hotkeys(self):
        """驗證 -> 存檔 -> 若服務執行中就用新設定重新綁定。回傳是否成功。"""
        cfg = self._current_cfg()
        err = hotkey_service.validate_config(cfg)
        if err:
            messagebox.showerror("快速鍵設定不合法", err)
            return False
        try:
            hotkey_service.save_config(cfg)
        except OSError as e:
            messagebox.showerror("儲存失敗", str(e))
            return False
        print(f"[設定] 已儲存快速鍵：複製={cfg['copy']}，貼上={cfg['paste'] or '(無，模式 B)'}")
        if self.service.running:
            try:
                self.service.start(cfg)
            except Exception as e:
                messagebox.showerror("重新啟動快速鍵失敗", str(e))
                self._refresh_service_state()
                return False
        else:
            self.service.config = dict(cfg)
        self._refresh_service_state()
        return True

    def reset_hotkeys(self):
        self.hk_copy.set(hotkey_service.DEFAULT_CONFIG["copy"])
        self.hk_paste.set(hotkey_service.DEFAULT_CONFIG["paste"])
        self._stop_recording()

    def toggle_service(self):
        if self.service.running:
            self.service.stop()
        else:
            if not self.apply_hotkeys():
                return
            try:
                self.service.start(self._current_cfg())
            except Exception as e:
                messagebox.showerror("啟動快速鍵失敗", str(e))
        self._refresh_service_state()

    def _refresh_service_state(self):
        if self.service.running:
            mode = "B" if self.service.direct_mode else "A"
            self.svc_state.set(f"全域快速鍵：執行中 (模式 {mode})")
            self.btn_svc.configure(text="停止全域快速鍵")
        else:
            self.svc_state.set("全域快速鍵：未啟動")
            self.btn_svc.configure(text="啟動全域快速鍵")

    # ================= 背景執行 =================
    def _set_busy(self, busy, msg=""):
        self.busy = busy
        for w in self._busy_widgets:
            w.state(["disabled"] if busy else ["!disabled"])
        self.status.set(msg if busy else "就緒")

    def _start(self, func, msg, on_done):
        """在背景執行 func，完成後在主執行緒呼叫 on_done(result)。
        與快速鍵共用 WORK_LOCK，不會同時改對照表或搶剪貼簿。"""
        if self.busy:
            return
        self._on_done = on_done
        self._set_busy(True, msg)

        def worker():
            try:
                with hotkey_service.WORK_LOCK:
                    result = func()
                self.q.put(("done", result, None))
            except Exception as e:
                self.q.put(("done", None, e))
        threading.Thread(target=worker, daemon=True).start()

    def _append_log_batch(self, chunks):
        """One Tk insert per batch; keep UI history bounded to avoid growing costs."""
        if not chunks:
            return
        self.log.configure(state="normal")
        try:
            self.log.insert("end", "".join(chunks))
            try:
                line_count = int(self.log.index("end-1c").split(".")[0])
                if line_count > GUI_LOG_MAX_LINES:
                    self.log.delete("1.0", f"{line_count - GUI_LOG_MAX_LINES + 1}.0")
            except (ValueError, IndexError):
                pass
            self.log.see("end")
        finally:
            self.log.configure(state="disabled")

    def _poll(self):
        """Drain only a bounded amount per tick. UI never loops forever on logs."""
        started = time.perf_counter()
        chunks = []
        handled = 0
        while handled < GUI_LOG_POLL_MAX_ITEMS:
            if handled and (time.perf_counter() - started) * 1000 >= GUI_LOG_POLL_BUDGET_MS:
                break
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            handled += 1
            if item[0] == "log":
                chunks.append(item[1])
            elif item[0] == "status":
                if self.busy:
                    self.status.set(item[1])
            elif item[0] == "done":
                # Show preceding worker output before completion/errors.
                self._append_log_batch(chunks)
                chunks.clear()
                _, result, err = item
                self._set_busy(False)
                cb, self._on_done = self._on_done, None
                if err:
                    print(f"[錯誤] {err}")
                    messagebox.showerror("處理失敗", str(err))
                elif cb:
                    cb(result)
                self.refresh_map_view()
        self._append_log_batch(chunks)

        if len(self.mapping) != self._map_n:
            self.refresh_map_view()
        self._tick += 1
        if self._tick % 10 == 0 and entity_store.signature() != self._store_sig:
            self.refresh_lists()
        # Poll again fairly soon under load, without starving other Tk events.
        self.after(25 if not self.q.empty() else 100, self._poll)

    # ================= 文字 =================
    def read_clipboard_text(self):
        kind, content = core.get_clipboard_content()
        if kind == "FILES":
            if messagebox.askyesno("剪貼簿是檔案", "剪貼簿目前是檔案，要改加入「檔案」清單嗎？"):
                self._add_paths(content)
            return
        self.txt_in.delete("1.0", "end")
        self.txt_in.insert("1.0", content)
        print(f"[讀取] 已從剪貼簿讀入 {len(content)} 個字元")

    def run_text(self):
        """依使用者選擇遮蔽或還原；沒有變動時保留輸入並清楚提示原因。
        處理成功後清空輸入框，結果接在結果框原有內容後面。"""
        text = self.txt_in.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("提示", "輸入框是空的")
            return
        is_code = (self.kind_var.get() == KIND_CODE)
        action = self.text_action_var.get()

        def job():
            print("\n" + "=" * 50)
            return core.process_text(text, self.mapping, action=action, is_code=is_code)

        def done(result):
            level, title, notice = describe_text_action_result(action, text, result, self.mapping)
            self.text_notice_var.set(f"{title}：{notice}")
            self.status.set(title)
            print(f"[文字處理] {title}：{notice}")

            if not result:
                # 沒有變動時不再把原文重複加到結果框，也不清空輸入框，方便切換模式。
                if level == "warning":
                    messagebox.showwarning(title, notice)
                else:
                    messagebox.showinfo(title, notice)
                return

            self._append_result(result)
            self.txt_in.delete("1.0", "end")
            self.refresh_lists()
            # 還原後應明確顯示已還原幾處，以及是否有無法還原的標籤。
            if action == "restore":
                if level == "warning":
                    messagebox.showwarning(title, notice)
                else:
                    messagebox.showinfo(title, notice)

        self._start(job, "處理文字中…", done)

    def _append_result(self, text):
        """把結果接在結果框原有內容後面 (原本的結果不動)。
        原內容結尾沒有換行時先補一個換行，避免兩段黏在同一行。"""
        existing = self.txt_out.get("1.0", "end-1c")
        if existing and not existing.endswith("\n"):
            self.txt_out.insert("end", "\n")
        self.txt_out.insert("end", text)
        self.txt_out.see("end")

    def _toggle_preprocess(self):
        preprocess.ENABLED = self.pp_var.get()
        print("[前處理] " + ("已開啟：全形轉半形 → 隱形字元 → 網頁標籤 → 簡繁統一 → 換行與空白，再送模型"
                          if preprocess.ENABLED else "已關閉：偵測直接使用原文"))

    def preview_preprocess(self):
        """在結果框顯示「送模型的文字」與每個步驟改了什麼 (不會遮蔽、也不會動對照表 / 名單)"""
        text = self.txt_in.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("提示", "輸入框是空的")
            return
        html = self.kind_var.get() != KIND_CODE        # 程式不做網頁標籤刪除，與實際偵測一致
        desc, body = preprocess.describe(text, html=html)
        # 另開視窗顯示，不動結果框 (結果框裡累積的結果不能被預覽蓋掉)
        self._show_text_window("前處理預覽", desc + "\n\n──── 送模型的文字 ────\n" + body)

    def _show_text_window(self, title, content):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry("760x520")
        box = scrolledtext.ScrolledText(win, wrap="word")
        box.pack(fill="both", expand=True)
        box.insert("1.0", content)
        box.configure(state="disabled")

    def copy_result_text(self):
        text = self.txt_out.get("1.0", "end-1c")
        if text:
            core.copy_text_to_clipboard(text)
            self.service.ignore_clipboard_change()   # 這是程式自己寫的，剪貼簿監聽不要當成使用者的複製再處理
            print("[完成] 結果已複製到剪貼簿")

    def result_to_input(self):
        text = self.txt_out.get("1.0", "end-1c")
        self.txt_in.delete("1.0", "end")
        self.txt_in.insert("1.0", text)
        self.txt_out.delete("1.0", "end")

    def clear_text(self):
        self.txt_in.delete("1.0", "end")
        self.txt_out.delete("1.0", "end")

    # ================= 檔案 =================
    def _add_paths(self, paths):
        for p in paths:
            p = os.path.abspath(p)
            if p not in self.files:
                self.files.append(p)
                self.lst_files.insert("end", p)

    def add_files(self):
        exts = (core.CODE_EXTENSIONS | core.MEDIA_EXTENSIONS |
                core.TEXT_FILE_EXTENSIONS | core.TABLE_EXTENSIONS)
        pattern = " ".join(f"*{e}" for e in sorted(exts))
        paths = filedialog.askopenfilenames(
            title="選擇檔案",
            filetypes=[("支援的檔案", pattern), ("所有檔案", "*.*")])
        self._add_paths(paths)

    def read_clipboard_files(self):
        kind, content = core.get_clipboard_content()
        if kind == "FILES":
            self._add_paths(content)
            print(f"[讀取] 已從剪貼簿加入 {len(content)} 個檔案")
        else:
            messagebox.showinfo("提示", "剪貼簿目前不是檔案")

    def remove_selected(self):
        for i in reversed(self.lst_files.curselection()):
            self.lst_files.delete(i)
            del self.files[i]

    def clear_files(self):
        self.lst_files.delete(0, "end")
        self.files.clear()

    def run_files(self):
        if not self.files:
            messagebox.showinfo("提示", "請先新增檔案")
            return
        paths = list(self.files)

        def job():
            print("\n" + "=" * 50)
            return core.dispatch_files(paths, self.mapping)

        def done(outs):
            self.outputs = list(outs)
            self.lst_out.delete(0, "end")
            for p in self.outputs:
                self.lst_out.insert("end", p)
            self.refresh_lists()
            if not outs:
                messagebox.showinfo("結果", "沒有產生任何檔案 (未偵測到內容或副檔名不支援)")

        self._start(job, "處理檔案中…", done)

    def restore_media(self):
        """從檔案頁還原圖片／PDF（同一執行階段的私有 media map）。

        前台負責選檔與提示；真正還原在共用 _start() 工作執行緒中完成。
        公開輸出用 hard link 無覆寫發布，失敗不會留下不完整的結果。
        """
        if self.busy:
            return
        import json
        import tempfile
        import shutil
        import handler_media

        # 先採用使用者在「已產生檔案」清單中明確選取的那一份。
        selected = self.lst_out.curselection()
        source = self.outputs[selected[0]] if len(selected) == 1 else ""
        if not source:
            source = filedialog.askopenfilename(
                parent=self,
                title="選擇要還原的已遮蔽 PDF／圖片",
                filetypes=[("遮蔽 PDF／圖片", "*_masked.pdf *_masked.png *_masked.jpg *_masked.jpeg"),
                           ("PDF", "*.pdf"), ("圖片", "*.png *.jpg *.jpeg"),
                           ("所有檔案", "*.*")],
            )
        if not source:
            return
        source = os.path.abspath(source)
        ext = os.path.splitext(source)[1].lower()
        if (not os.path.isfile(source) or os.path.islink(source)
                or ext not in {".pdf", ".png", ".jpg", ".jpeg"}):
            messagebox.showerror("無法還原", "請選擇存在的已遮蔽 PDF／圖片檔案。", parent=self)
            return

        map_path = os.path.abspath(handler_media.DEFAULT_MAP_FILE)
        try:
            with open(map_path, encoding="utf-8") as f:
                data = json.load(f)
            entries = data.get(os.path.basename(source)) if isinstance(data, dict) else None
            if not isinstance(entries, list) or not entries:
                raise ValueError("missing media restore entries")
        except (OSError, ValueError, TypeError):
            messagebox.showerror("找不到還原對照表",
                "目前找不到這份檔案的還原資料。請先遮蔽後在同一次 Demo UI 執行期間還原。",
                parent=self)
            return

        stem = os.path.splitext(os.path.basename(source))[0]
        if stem.lower().endswith("_masked"):
            stem = stem[:-7]
        from datetime import datetime
        initialfile = f"{stem}_restored_{datetime.now():%Y%m%d_%H%M%S_%f}{ext}"
        dest = filedialog.asksaveasfilename(
            parent=self, title="儲存私有還原文件",
            initialdir=os.path.dirname(source), initialfile=initialfile,
            defaultextension=ext,
            filetypes=[("PDF", "*.pdf")] if ext == ".pdf" else
                      [("圖片", "*.png *.jpg *.jpeg")],
        )
        if not dest:
            return
        dest = os.path.abspath(dest)
        if (os.path.normcase(os.path.realpath(dest)) == os.path.normcase(os.path.realpath(source))
                or os.path.lexists(dest)):
            messagebox.showerror("拒絕覆寫", "輸出檔已存在或與來源相同，請使用新檔名。", parent=self)
            return
        if os.path.splitext(dest)[1].lower() != ext:
            messagebox.showerror("副檔名不符", "還原檔必須使用與來源相同的副檔名。", parent=self)
            return
        if not messagebox.askyesno(
            "確認還原", "還原後的檔案會包含原本的人臉與敏感資訊，只能在私有位置保存。\n\n確定繼續？",
            parent=self,
        ):
            return

        def job():
            # 與其他檔案操作共用 WORK_LOCK，暫存與最終檔都不允許覆寫。
            stage = tempfile.mkdtemp(prefix=".pii_private_restore_", dir=os.path.dirname(dest))
            try:
                try:
                    os.chmod(stage, 0o700)
                except OSError:
                    pass
                candidate = os.path.join(stage, os.path.basename(dest))
                try:
                    handler_media.restore_file(source, candidate, map_path)
                except Exception as exc:
                    raise RuntimeError("媒體還原失敗，請檢查還原對照表與 PDF 格式。") from exc
                if (not os.path.isfile(candidate) or os.path.islink(candidate)
                        or os.path.getsize(candidate) == 0):
                    raise RuntimeError("未產生有效的還原檔案。")
                try:
                    os.link(candidate, dest)  # 同磁碟原子發布，不覆寫現有檔案
                except FileExistsError as exc:
                    raise RuntimeError("目的檔已存在，拒絕覆寫。") from exc
                except OSError as exc:
                    raise RuntimeError("無法安全發布還原檔，請確認 NTFS 與資料夾權限。") from exc
                return dest
            finally:
                shutil.rmtree(stage, ignore_errors=True)

        def done(result):
            print("[還原完成] 已產生私有還原檔案（請勿公開分享）。")
            messagebox.showinfo("還原完成",
                "已完成 PDF／圖片還原：\n" + result +
                "\n\n此檔案包含原始敏感資訊，請勿公開分享。", parent=self)

        self._start(job, "還原 PDF／圖片中…", done)

    def _selected_outputs(self):
        sel = self.lst_out.curselection()
        return [self.outputs[i] for i in sel] if sel else list(self.outputs)

    def copy_outputs(self):
        paths = self._selected_outputs()
        if not paths:
            messagebox.showinfo("提示", "沒有可複製的輸出檔案")
            return
        core.copy_files_to_clipboard(paths)
        self.service.ignore_clipboard_change()
        print(f"[完成] 已把 {len(paths)} 個輸出檔案複製到剪貼簿，可直接在檔案總管貼上")

    def open_output_folder(self):
        paths = self._selected_outputs()
        if paths:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(paths[0])])

    # ================= 對照表 =================
    def refresh_map_view(self):
        self.tree.delete(*self.tree.get_children())
        for tag, val in list(self.mapping.items()):
            self.tree.insert("", "end", values=(tag, val))
        self._map_n = len(self.mapping)

    def clear_map(self):
        if messagebox.askyesno("清除對照表", "清除後，已遮蔽的內容將無法還原。確定嗎？"):
            core.clear_map(self.mapping)
            self.refresh_map_view()
            print("[完成] 對照表已清除")

    # ================= 結束 =================
    def _on_close(self):
        self.service.stop()
        sys.stdout = sys.__stdout__
        core.cleanup_map_files()   # 只刪對照表；三份名單刻意保留
        self.destroy()

# 舊版動態 hook 已由 SEAMLESS_RESTORE_VISIBLE_UI_V2 的固定按鈕取代。

if __name__ == "__main__":
    App().mainloop()