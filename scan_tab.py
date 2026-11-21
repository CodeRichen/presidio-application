"""
scan_tab.py - 主題設定 + 「資料夾掃描」分頁
====================================================
這個檔案有兩個用途：
  1. 集中放「顏色 THEME」「尺寸 LAYOUT」「按鈕樣式」，ui.py 也會引用它
       from scan_tab import ScanTab, apply_theme, style_text, style_listbox, THEME, LAYOUT
  2. ScanTab：資料夾掃描分頁
       - 選好資料夾就列出每一個檔案 (待掃描)
       - 掃描時依序變成：掃描中 → 已掃描 → 最後依結果換成 安全 / 中風險 / 高風險 / 錯誤 / 略過
       - 不同狀態用「不同顏色 + 不同形狀」的小圖示，色盲也分得出來
       - 耗時工作交給 app._start 在背景跑；背景只丟訊息進 queue，主執行緒才更新畫面
"""
import os
import queue
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core

# =====================================================================
# 【要改外觀就改這裡】
# =====================================================================
THEME = {
    "bg": "#f1f5f9",            # 視窗底色
    "card": "#ffffff",          # 輸入框 / 表格底色
    "border": "#cbd5e1",        # 邊框
    "text": "#0f172a",          # 主要文字
    "muted": "#64748b",         # 說明文字
    "header": "#1e3a8a",        # 最上方標題列
    "header_sub": "#bfdbfe",    # 標題列小字

    "primary": "#2563eb", "primary_hover": "#1d4ed8", "primary_soft": "#dbeafe",
    "success": "#16a34a", "success_hover": "#15803d",
    "danger":  "#dc2626", "danger_hover":  "#b91c1c",
    "warn":    "#f59e0b", "warn_hover":    "#d97706",
    "purple":  "#7c3aed", "purple_hover":  "#6d28d9",
    "teal":    "#0d9488", "teal_hover":    "#0f766e",
    "neutral": "#e2e8f0", "neutral_hover": "#cbd5e1",

    "tab_idle": "#e2e8f0",      # 未選取的分頁
    "heading": "#e0e7ff",       # 表格標題列
    "row_alt": "#f8fafc",       # 表格隔行底色
    "select": "#bfdbfe",        # 選取列底色

    "log_bg": "#0f172a",        # 記錄區 (深色)
    "log_fg": "#e2e8f0",
}

LAYOUT = {
    "window": "1120x900",       # 視窗大小 "寬x高"
    "min_size": (940, 740),     # 最小可縮到多小
    "font": "Microsoft JhengHei UI",   # 介面字型 (繁體中文 Windows)
    "mono": "Consolas",         # 記錄區 / 程式碼字型
    "font_size": 10,            # 全域字級；想整體放大縮小只改這個
    "pad": 8,                   # 各區塊外距
    "btn_padx": 12,             # 按鈕左右內距 (越大按鈕越寬)
    "btn_pady": 5,              # 按鈕上下內距 (越大按鈕越高)
    "row_height": 26,           # 表格每一列高度
    "icon_size": 16,            # 狀態小圖示大小 (像素)
    "tab_padx": 16,             # 分頁標籤左右內距
    "tab_pady": 7,              # 分頁標籤上下內距
    "wrap": 1000,               # 說明文字幾像素寬後換行
    "text_height": 10,          # 文字輸入/結果框預設行數
    "log_height": 9,            # 記錄區行數
}

# 掃描狀態： key -> (圖示顏色, 形狀, 顯示文字, 該列底色)
STATES = {
    "pending":  ("#9ca3af", "ring",     "待掃描", None),
    "scanning": ("#2563eb", "diamond",  "掃描中", "#eff6ff"),
    "done":     ("#0d9488", "circle",   "已掃描", None),
    "safe":     ("#16a34a", "circle",   "安全",   "#f0fdf4"),
    "mid":      ("#f59e0b", "triangle", "中風險", "#fffbeb"),
    "high":     ("#dc2626", "square",   "高風險", "#fef2f2"),
    "error":    ("#6b7280", "diamond",  "錯誤",   "#f3f4f6"),
    "skipped":  ("#d1d5db", "ring",     "略過",   None),
}
RISK_TO_STATE = {"高": "high", "中": "mid", "錯誤": "error"}

# 按鈕樣式名稱 (用法：ttk.Button(parent, text="…", style=BTN["primary"]))
BTN = {
    "default": "TButton",
    "primary": "Primary.TButton",
    "success": "Success.TButton",
    "danger":  "Danger.TButton",
    "warn":    "Warn.TButton",
    "purple":  "Purple.TButton",
    "teal":    "Teal.TButton",
}


def _font(delta=0, bold=False):
    f = (LAYOUT["font"], LAYOUT["font_size"] + delta)
    return f + ("bold",) if bold else f


# =====================================================================
# 主題套用
# =====================================================================
def apply_theme(root):
    """替整個程式套用顏色、字型、按鈕樣式。在建立任何元件之前呼叫一次。"""
    T, L = THEME, LAYOUT
    s = ttk.Style(root)
    s.theme_use("clam")                      # clam 才能自訂按鈕底色
    root.configure(bg=T["bg"])
    root.option_add("*Font", _font())

    s.configure(".", background=T["bg"], foreground=T["text"], font=_font(),
                bordercolor=T["border"], focuscolor=T["primary_soft"])
    s.configure("TFrame", background=T["bg"])
    s.configure("TLabel", background=T["bg"], foreground=T["text"])
    s.configure("Hint.TLabel", foreground=T["muted"])
    s.configure("Warn.TLabel", foreground="#b45309")
    s.configure("Info.TLabel", foreground=T["primary"], font=_font(0, True))
    s.configure("Title.TLabel", foreground=T["header"], font=_font(1, True))
    s.configure("Status.TLabel", background=T["neutral"], foreground=T["text"], padding=(10, 4))

    s.configure("TLabelframe", background=T["bg"], bordercolor=T["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=T["bg"], foreground=T["primary"], font=_font(0, True))

    for name in ("TCheckbutton", "TRadiobutton"):
        s.configure(name, background=T["bg"], foreground=T["text"])
        s.map(name, background=[("active", T["bg"])], foreground=[("active", T["primary"])])

    s.configure("TEntry", fieldbackground=T["card"], bordercolor=T["border"], padding=4)
    s.configure("TCombobox", fieldbackground=T["card"], bordercolor=T["border"], padding=4)

    # ---- 分頁 ----
    s.configure("TNotebook", background=T["bg"], borderwidth=0, tabmargins=(2, 4, 2, 0))
    s.configure("TNotebook.Tab", background=T["tab_idle"], foreground=T["muted"],
                padding=(L["tab_padx"], L["tab_pady"]), font=_font(0, True), borderwidth=0)
    s.map("TNotebook.Tab",
          background=[("selected", T["card"]), ("active", T["primary_soft"])],
          foreground=[("selected", T["primary"]), ("active", T["primary"])])

    # ---- 表格 ----
    s.configure("Treeview", background=T["card"], fieldbackground=T["card"], foreground=T["text"],
                rowheight=L["row_height"], bordercolor=T["border"], borderwidth=1)
    s.map("Treeview", background=[("selected", T["select"])], foreground=[("selected", T["text"])])
    s.configure("Treeview.Heading", background=T["heading"], foreground=T["header"],
                font=_font(0, True), relief="flat", padding=(6, 5))
    s.map("Treeview.Heading", background=[("active", T["primary_soft"])])

    s.configure("Vertical.TScrollbar", background=T["neutral"], troughcolor=T["bg"],
                bordercolor=T["bg"], arrowcolor=T["muted"])
    s.configure("Horizontal.TProgressbar", troughcolor=T["neutral"], background=T["primary"],
                bordercolor=T["neutral"], lightcolor=T["primary"], darkcolor=T["primary"], thickness=14)

    # ---- 按鈕 ----
    def btn(name, bg, hover, fg="white"):
        s.configure(name, background=bg, foreground=fg, borderwidth=0, relief="flat",
                    focusthickness=0, focuscolor=bg, font=_font(0, True),
                    padding=(L["btn_padx"], L["btn_pady"]))
        s.map(name, background=[("disabled", "#e5e7eb"), ("pressed", hover), ("active", hover)],
              foreground=[("disabled", "#9ca3af")])

    btn("TButton", T["neutral"], T["neutral_hover"], fg=T["text"])
    btn("Primary.TButton", T["primary"], T["primary_hover"])
    btn("Success.TButton", T["success"], T["success_hover"])
    btn("Danger.TButton", T["danger"], T["danger_hover"])
    btn("Warn.TButton", T["warn"], T["warn_hover"], fg="#1f2937")
    btn("Purple.TButton", T["purple"], T["purple_hover"])
    btn("Teal.TButton", T["teal"], T["teal_hover"])


def style_text(widget, mono=False):
    """替 Text / ScrolledText 套用底色、字型、邊框、內距"""
    widget.configure(bg=THEME["card"], fg=THEME["text"], insertbackground=THEME["primary"],
                     selectbackground=THEME["select"], selectforeground=THEME["text"],
                     relief="flat", highlightthickness=1, highlightbackground=THEME["border"],
                     highlightcolor=THEME["primary"], padx=8, pady=6,
                     font=(LAYOUT["mono"], LAYOUT["font_size"]) if mono else _font())


def style_listbox(widget):
    """替 Listbox 套用底色、字型、邊框"""
    widget.configure(bg=THEME["card"], fg=THEME["text"], selectbackground=THEME["select"],
                     selectforeground=THEME["text"], relief="flat", highlightthickness=1,
                     highlightbackground=THEME["border"], highlightcolor=THEME["primary"],
                     activestyle="none", font=_font())


# =====================================================================
# 小圖示 (用程式畫，不需外部圖檔)
# =====================================================================
def make_icon(color, shape="circle", size=None):
    size = size or LAYOUT["icon_size"]
    img = tk.PhotoImage(width=size, height=size)
    c = size / 2
    r = size / 2 - 1

    def inside(dx, dy):
        d2 = dx * dx + dy * dy
        if shape == "circle":
            return d2 <= r * r
        if shape == "ring":
            return r * r * 0.45 <= d2 <= r * r
        if shape == "square":
            return abs(dx) <= r * 0.85 and abs(dy) <= r * 0.85
        if shape == "diamond":
            return abs(dx) + abs(dy) <= r
        if shape == "triangle":
            ty = (dy + r) / (2 * r)
            return 0 <= ty <= 1 and abs(dx) <= r * ty
        return False

    for y in range(size):
        for x in range(size):
            if inside(x + 0.5 - c, y + 0.5 - c):
                img.put(color, to=(x, y, x + 1, y + 1))
    return img


def _norm(path):
    return os.path.normcase(os.path.abspath(path))


# =====================================================================
# 資料夾掃描分頁
# =====================================================================
class ScanTab:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.scan_dir = tk.StringVar(value="")
        self.scan_recursive = tk.BooleanVar(value=True)
        self.scan_summary = tk.StringVar(value="尚未掃描")
        self.progress_text = tk.StringVar(value="")

        self.dest = None
        self.results = []
        self._rel = {}              # iid -> 相對路徑
        self._st = {}               # iid -> 目前狀態
        self._counts = {k: 0 for k in STATES}
        self._count_vars = {}
        self._pq = queue.Queue()    # 背景 -> 主執行緒 的進度訊息
        self._current = None
        self._finalized = set()
        self._draining = False
        # 圖示要留著參照，否則會被回收而不顯示
        self._icons = {k: make_icon(v[0], v[1]) for k, v in STATES.items()}

        self._build()

    # ---------------- 介面 ----------------
    def _build(self):
        p, T, L = self.parent, THEME, LAYOUT
        pad = L["pad"]

        # 第一排：路徑與操作按鈕
        bar = ttk.Frame(p)
        bar.pack(fill="x", pady=(pad, 4))
        ttk.Label(bar, text="📁 資料夾：", style="Info.TLabel").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.scan_dir)
        ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.bind("<Return>", lambda _e: self.refresh_listing())
        ttk.Button(bar, text="📂 瀏覽…", style=BTN["primary"], command=self.pick_dir).pack(side="left", padx=2)
        ttk.Button(bar, text="🔄 重新列出", style=BTN["default"], command=self.refresh_listing).pack(side="left", padx=2)
        ttk.Checkbutton(bar, text="包含子資料夾", variable=self.scan_recursive,
                        command=self.refresh_listing).pack(side="left", padx=8)
        self.btn_scan = ttk.Button(bar, text="🚀 開始掃描並遮蔽", style=BTN["success"], command=self.run)
        self.btn_scan.pack(side="left", padx=2)

        ttk.Label(p, justify="left", wraplength=L["wrap"], style="Hint.TLabel", text=(
            "流程：選好資料夾後會先列出所有檔案。按「開始掃描並遮蔽」後，在選擇的資料夾「同層」建立 <資料夾名>_masked，"
            "先把裡面所有東西複製過去，再只處理複製出來的檔案——檔名不變，有敏感資料的檔案內容會被遮蔽，"
            "其他檔案原樣保留。原資料夾完全不動。")).pack(anchor="w", pady=(0, 4))

        # 摘要 + 進度條
        ttk.Label(p, textvariable=self.scan_summary, style="Title.TLabel").pack(anchor="w", pady=(2, 2))
        prog = ttk.Frame(p)
        prog.pack(fill="x", pady=(0, 4))
        self.pb = ttk.Progressbar(prog, mode="determinate")
        self.pb.pack(side="left", fill="x", expand=True)
        ttk.Label(prog, textvariable=self.progress_text, width=10, anchor="e").pack(side="left", padx=(6, 0))

        # 圖例 (圖示 + 名稱 + 數量)
        legend = ttk.Frame(p)
        legend.pack(fill="x", pady=(0, 4))
        for key, (_c, _s, text, _bg) in STATES.items():
            var = tk.StringVar(value=f"{text} 0")
            self._count_vars[key] = var
            ttk.Label(legend, image=self._icons[key], textvariable=var, compound="left").pack(side="left", padx=(0, 14))

        # 檔案表格
        frame = ttk.Frame(p)
        frame.pack(fill="both", expand=True)
        cols = [("file", "檔案 (相對路徑)", 440, "w"), ("kind", "類型", 80, "center"),
                ("hits", "筆數", 60, "center"), ("labels", "偵測到的類型", 340, "w")]
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="狀態")
        self.tree.column("#0", width=120, stretch=False, anchor="w")
        for cid, text, w, anchor in cols:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid == "file"))
        sb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        # 各狀態的文字顏色 / 底色
        fg = {"high": "#b91c1c", "mid": "#b45309", "error": "#6b7280", "safe": "#15803d",
              "scanning": "#1d4ed8", "skipped": "#9ca3af", "pending": "#475569", "done": "#0f766e"}
        for key, (_c, _s, _t, bg) in STATES.items():
            self.tree.tag_configure(key, foreground=fg[key], **({"background": bg} if bg else {}))

        bar2 = ttk.Frame(p)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="📂 開啟遮蔽後的資料夾", style=BTN["teal"], command=self.open_dest).pack(side="left")
        ttk.Button(bar2, text="🔎 開啟選取檔案的位置", style=BTN["default"], command=self.open_file).pack(side="left", padx=4)

    # ---------------- 檔案列表 ----------------
    def pick_dir(self):
        d = filedialog.askdirectory(title="選擇要掃描的資料夾")
        if d:
            self.scan_dir.set(os.path.normpath(d))
            self.refresh_listing()

    def _list_files(self, folder, recursive):
        out = []
        if recursive:
            for root, _dirs, names in os.walk(folder):
                for n in sorted(names):
                    out.append(os.path.join(root, n))
        else:
            for n in sorted(os.listdir(folder)):
                p = os.path.join(folder, n)
                if os.path.isfile(p):
                    out.append(p)
        return out

    def refresh_listing(self):
        """列出資料夾內所有檔案，全部標成「待掃描」"""
        if self.app.busy:
            return
        folder = self.scan_dir.get().strip()
        self.tree.delete(*self.tree.get_children())
        self._rel.clear()
        self._st.clear()
        self._counts = {k: 0 for k in STATES}
        self._current = None
        self._finalized.clear()
        self.pb.configure(value=0)
        self.progress_text.set("")
        if not folder or not os.path.isdir(folder):
            self.scan_summary.set("尚未選擇有效的資料夾")
            self._refresh_counts()
            return
        try:
            files = self._list_files(folder, self.scan_recursive.get())
        except OSError as e:
            self.scan_summary.set(f"無法讀取資料夾：{e}")
            self._refresh_counts()
            return
        for p in files:
            self._insert(_norm(p), os.path.relpath(p, folder), "pending")
        self.scan_summary.set(f"共 {len(files)} 個檔案，尚未掃描")
        self._refresh_counts()

    def _insert(self, iid, rel, state):
        self._rel[iid] = rel
        self._st[iid] = state
        self._counts[state] += 1
        self.tree.insert("", "end", iid=iid, text=" " + STATES[state][2], image=self._icons[state],
                         tags=(state,), values=(rel, os.path.splitext(rel)[1].lstrip(".").lower(), "", ""))

    def _set_state(self, iid, state):
        if not self.tree.exists(iid):
            return
        old = self._st.get(iid)
        if old:
            self._counts[old] -= 1
        self._counts[state] += 1
        self._st[iid] = state
        self.tree.item(iid, text=" " + STATES[state][2], image=self._icons[state], tags=(state,))
        self._refresh_counts()

    def _refresh_counts(self):
        for key, var in self._count_vars.items():
            var.set(f"{STATES[key][2]} {self._counts[key]}")

    # ---------------- 掃描 ----------------
    def run(self):
        folder = self.scan_dir.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("提示", "請先選擇有效的資料夾")
            return
        recursive = self.scan_recursive.get()
        self.refresh_listing()          # 以開始當下的實際內容為準
        self._start_drain()

        def progress(i, total, path, *extra):
            # 背景執行緒：只丟訊息，不碰 tkinter。若 core 日後在第 4 個參數帶上單檔結果，會即時顯示最終風險
            self.app.q.put(("status", f"掃描中 {i}/{total}：{os.path.basename(path)}"))
            res = extra[0] if extra and isinstance(extra[0], dict) else None
            self._pq.put((i, total, path, res))

        def job():
            print("\n" + "=" * 50)
            return core.scan_folder(folder, self.app.mapping, recursive=recursive, progress=progress)

        def done(res):
            self._drain()               # 先把剩下的進度處理完
            dest, results = res
            self.dest, self.results = dest, results
            self._apply_final(folder, results)
            self.pb.configure(value=self.pb["maximum"])
            danger = [r for r in results if r["output"]]
            high = sum(1 for r in danger if r["risk"] == "高")
            self.scan_summary.set(f"共 {len(results)} 個檔案：危險 {len(danger)} 個 (高風險 {high})，"
                                  f"安全 {len(results) - len(danger)} 個　→　已遮蔽的副本：{dest}")
            print("\n" + core.format_scan_report(results, dest))
            self.app.refresh_lists()
            if danger:
                lines = [f"[{r['risk']}] {os.path.relpath(r['path'], folder)}" for r in danger[:15]]
                more = f"\n… 另外 {len(danger) - 15} 個，請看清單" if len(danger) > 15 else ""
                messagebox.showwarning("發現含敏感資訊的檔案",
                                       f"共 {len(danger)} 個檔案含敏感資訊。\n"
                                       f"遮蔽後的完整資料夾：\n{dest}\n\n" + "\n".join(lines) + more)
            else:
                messagebox.showinfo("掃描完成", f"沒有發現含敏感資訊的檔案。\n(複製的資料夾：{dest})")

        self.app._start(job, "複製並掃描資料夾中…", done)

    # ---------------- 進度 (主執行緒) ----------------
    def _start_drain(self):
        if not self._draining:
            self._draining = True
            self.parent.after(100, self._drain_loop)

    def _drain_loop(self):
        self._drain()
        if self.app.busy:
            self.parent.after(100, self._drain_loop)
        else:
            self._draining = False

    def _drain(self):
        try:
            while True:
                i, total, path, res = self._pq.get_nowait()
                self.pb.configure(maximum=max(total, 1), value=i)
                self.progress_text.set(f"{i}/{total}")
                self._on_progress(_norm(path), res)
        except queue.Empty:
            pass

    def _on_progress(self, iid, res):
        # 前一個「掃描中」的檔案已經處理完 -> 換成「已掃描」
        if self._current and self._current != iid and self._current not in self._finalized:
            self._set_state(self._current, "done")
        if res is not None:
            self._apply_result(iid, res)
            self._current = None
            return
        self._current = iid
        self._set_state(iid, "scanning")
        if self.tree.exists(iid):
            self.tree.see(iid)

    def _state_of(self, r):
        if r.get("output") and r["risk"] not in RISK_TO_STATE:
            return "mid"
        return RISK_TO_STATE.get(r["risk"], "safe")

    def _apply_result(self, iid, r, folder=None):
        state = self._state_of(r)
        if not self.tree.exists(iid):
            rel = os.path.relpath(r["path"], folder) if folder else os.path.basename(r["path"])
            self._insert(iid, rel, "pending")
        labels = ", ".join(f"{k}×{v}" for k, v in r["labels"].items()) or (r["error"] or "")
        self._set_state(iid, state)
        self.tree.item(iid, values=(self._rel.get(iid, ""), r["kind"], r["hits"] or "", labels))
        self._finalized.add(iid)

    def _apply_final(self, folder, results):
        """整批結束：依結果更新所有檔案；沒出現在結果裡的標為「略過」"""
        seen = set()
        for r in results:
            iid = _norm(r["path"])
            seen.add(iid)
            self._apply_result(iid, r, folder)
        for iid in self.tree.get_children():
            if iid not in seen:
                self._set_state(iid, "skipped")
        self._current = None

    # ---------------- 開啟位置 ----------------
    def open_dest(self):
        if self.dest and os.path.isdir(self.dest):
            subprocess.Popen(["explorer", os.path.normpath(self.dest)])
        else:
            messagebox.showinfo("提示", "還沒有掃描結果")

    def open_file(self):
        sel = self.tree.selection()
        if not sel or not self.dest:
            return
        path = os.path.join(self.dest, self._rel.get(sel[0], ""))
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])