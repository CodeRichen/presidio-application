"""
theme.py - 外觀設定 (顏色 / 尺寸 / 字型 / 按鈕樣式) + 共用小工具
====================================================
想改外觀只需要改這個檔案最上面的 THEME / LAYOUT。
"""
import sys
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

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
    "min_size": (860, 640),     # 最小可縮到多小
    "font": "Microsoft JhengHei UI",   # 介面字型 (中英文統一微軟正黑)；填 "auto" 則自動挑選
    "mono": "Microsoft JhengHei UI",   # 記錄區字型；想等寬可改成 "Consolas"
    "font_size": 10,            # 全域字級；想整體放大縮小只改這個
    "pad": 8,                   # 各區塊外距
    "btn_padx": 12,             # 按鈕左右內距 (越大按鈕越寬)
    "btn_pady": 5,              # 按鈕上下內距 (越大按鈕越高)
    "row_height": 30,           # 表格每一列高度
    "icon_size": 16,            # 狀態小圖示大小 (像素)
    "tab_padx": 16,             # 分頁標籤左右內距
    "tab_pady": 7,              # 分頁標籤上下內距
    "text_height": 8,           # 文字輸入/結果框預設行數 (之後可拖曳調整)
    "log_height": 6,            # 記錄區預設行數 (之後可拖曳調整)
}

# 字型候選：指定的字型不存在時，由前到後用第一個電腦上有的
FONT_CANDIDATES = ["Microsoft JhengHei UI", "Microsoft JhengHei", "Noto Sans TC", "Noto Sans CJK TC",
                   "PingFang TC", "Heiti TC", "Microsoft YaHei UI", "Segoe UI", "Arial"]
MONO_CANDIDATES = ["Microsoft JhengHei UI", "Microsoft JhengHei", "Consolas", "Menlo",
                   "DejaVu Sans Mono", "Courier New"]

# 掃描狀態： key -> (圖示顏色, 形狀, 顯示文字, 該列底色)
# 形狀可選：circle ring square diamond triangle
STATES = {
    "pending":  ("#9ca3af", "ring",    "待掃描", None),
    "scanning": ("#2563eb", "diamond", "掃描中", "#eff6ff"),
    "done":     ("#0d9488", "circle",  "已掃描", None),
    "safe":     ("#16a34a", "circle",  "安全",   "#f0fdf4"),
    "unsafe":   ("#dc2626", "square",  "不安全", "#fef2f2"),
    "error":    ("#6b7280", "diamond", "錯誤",   "#f3f4f6"),
    "skipped":  ("#d1d5db", "ring",    "略過",   None),
}
# 狀態列的文字顏色
STATE_FG = {"unsafe": "#b91c1c", "error": "#6b7280", "safe": "#15803d",
            "scanning": "#1d4ed8", "skipped": "#9ca3af", "pending": "#475569", "done": "#0f766e"}

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

# =====================================================================
# 字型
# =====================================================================
RESOLVED = {"font": "TkDefaultFont", "mono": "TkFixedFont"}


def ui_font(delta=0, bold=False):
    f = (RESOLVED["font"], LAYOUT["font_size"] + delta)
    return f + ("bold",) if bold else f


def mono_font():
    return (RESOLVED["mono"], LAYOUT["font_size"])


def _resolve_fonts(root):
    fams = {f.lower(): f for f in tkfont.families(root)}

    def pick(pref, cands, default):
        order = ([pref] if pref and pref != "auto" else []) + list(cands)
        for c in order:
            if c.lower() in fams:
                return fams[c.lower()]
        return default

    RESOLVED["font"] = pick(LAYOUT["font"], FONT_CANDIDATES, "TkDefaultFont")
    RESOLVED["mono"] = pick(LAYOUT["mono"], MONO_CANDIDATES, "TkFixedFont")

    # 直接改 Tk 的具名字型 (比 option_add 可靠，字型名稱含空白也不會出錯)
    size = LAYOUT["font_size"]
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                 "TkCaptionFont", "TkTooltipFont", "TkIconFont"):
        try:
            tkfont.nametofont(name).configure(family=RESOLVED["font"], size=size)
        except tk.TclError:
            pass
    try:
        tkfont.nametofont("TkFixedFont").configure(family=RESOLVED["mono"], size=size)
    except tk.TclError:
        pass


def font_report():
    return f"[字型] 介面={RESOLVED['font']}，記錄區={RESOLVED['mono']}"


def enable_dpi_awareness():
    """Windows 高解析螢幕避免字體模糊。必須在建立 Tk() 之前呼叫。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# =====================================================================
# 主題套用
# =====================================================================
def apply_theme(root):
    """替整個程式套用顏色、字型、按鈕樣式。在建立任何元件之前呼叫一次。"""
    T, L = THEME, LAYOUT
    _resolve_fonts(root)
    s = ttk.Style(root)
    s.theme_use("clam")                      # clam 才能自訂按鈕底色
    root.configure(bg=T["bg"])

    s.configure(".", background=T["bg"], foreground=T["text"], font=ui_font(),
                bordercolor=T["border"], focuscolor=T["primary_soft"])
    s.configure("TFrame", background=T["bg"])
    s.configure("TLabel", background=T["bg"], foreground=T["text"])
    s.configure("Hint.TLabel", foreground=T["muted"])
    s.configure("Warn.TLabel", foreground="#b45309")
    s.configure("Info.TLabel", foreground=T["primary"], font=ui_font(0, True))
    s.configure("Title.TLabel", foreground=T["header"], font=ui_font(1, True))
    s.configure("Safe.TLabel", foreground="#15803d", font=ui_font(2, True))
    s.configure("Unsafe.TLabel", foreground="#b91c1c", font=ui_font(2, True))
    s.configure("Status.TLabel", background=T["neutral"], foreground=T["text"], padding=(10, 4))

    s.configure("TLabelframe", background=T["bg"], bordercolor=T["border"], relief="solid", borderwidth=1)
    s.configure("TLabelframe.Label", background=T["bg"], foreground=T["primary"], font=ui_font(0, True))

    for name in ("TCheckbutton", "TRadiobutton"):
        s.configure(name, background=T["bg"], foreground=T["text"])
        s.map(name, background=[("active", T["bg"])], foreground=[("active", T["primary"])])

    s.configure("TEntry", fieldbackground=T["card"], bordercolor=T["border"], padding=4)
    s.configure("TCombobox", fieldbackground=T["card"], bordercolor=T["border"], padding=4)
    s.configure("TPanedwindow", background=T["bg"])
    s.configure("Sash", sashthickness=6, background=T["border"])

    # ---- 分頁 ----
    s.configure("TNotebook", background=T["bg"], borderwidth=0, tabmargins=(2, 4, 2, 0))
    s.configure("TNotebook.Tab", background=T["tab_idle"], foreground=T["muted"],
                padding=(L["tab_padx"], L["tab_pady"]), font=ui_font(0, True), borderwidth=0)
    s.map("TNotebook.Tab",
          background=[("selected", T["card"]), ("active", T["primary_soft"])],
          foreground=[("selected", T["primary"]), ("active", T["primary"])])

    # ---- 表格 ----
    s.configure("Treeview", background=T["card"], fieldbackground=T["card"], foreground=T["text"],
                rowheight=L["row_height"], bordercolor=T["border"], borderwidth=1, font=ui_font())
    s.map("Treeview", background=[("selected", T["select"])], foreground=[("selected", T["text"])])
    s.configure("Treeview.Heading", background=T["heading"], foreground=T["header"],
                font=ui_font(0, True), relief="flat", padding=(6, 5))
    s.map("Treeview.Heading", background=[("active", T["primary_soft"])])

    for orient in ("Vertical", "Horizontal"):
        s.configure(f"{orient}.TScrollbar", background=T["neutral"], troughcolor=T["bg"],
                    bordercolor=T["bg"], arrowcolor=T["muted"])

    # ---- 按鈕 ----
    def btn(name, bg, hover, fg="white"):
        s.configure(name, background=bg, foreground=fg, borderwidth=0, relief="flat",
                    focusthickness=0, focuscolor=bg, font=ui_font(0, True),
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


# =====================================================================
# 共用元件 / 小工具
# =====================================================================
def style_text(widget, mono=False):
    """替 Text / ScrolledText 套用底色、字型、邊框、內距"""
    widget.configure(bg=THEME["card"], fg=THEME["text"], insertbackground=THEME["primary"],
                     selectbackground=THEME["select"], selectforeground=THEME["text"],
                     relief="flat", highlightthickness=1, highlightbackground=THEME["border"],
                     highlightcolor=THEME["primary"], padx=8, pady=6,
                     font=mono_font() if mono else ui_font())


def style_listbox(widget):
    """替 Listbox 套用底色、字型、邊框"""
    widget.configure(bg=THEME["card"], fg=THEME["text"], selectbackground=THEME["select"],
                     selectforeground=THEME["text"], relief="flat", highlightthickness=1,
                     highlightbackground=THEME["border"], highlightcolor=THEME["primary"],
                     activestyle="none", font=ui_font())


def auto_wrap(label, pad=12):
    """讓 Label 的換行寬度跟著視窗寬度走 (Label 要用 fill='x' 或 sticky='we' 排版)"""
    def _on(e):
        w = max(e.width - pad, 120)
        if int(float(label.cget("wraplength") or 0)) != w:
            label.configure(wraplength=w)
    label.bind("<Configure>", _on)
    return label


def hint_label(parent, text=None, textvariable=None, style="Hint.TLabel", **pack):
    """建立會自動換行、跟著視窗縮放的說明文字，並直接 pack"""
    kw = {"textvariable": textvariable} if textvariable is not None else {"text": text}
    lb = ttk.Label(parent, style=style, justify="left", anchor="w", **kw)
    auto_wrap(lb)
    opts = {"fill": "x", "anchor": "w"}
    opts.update(pack)
    lb.pack(**opts)
    return lb


def zebra(i):
    """表格隔行底色用的 tag"""
    return ("odd",) if i % 2 else ()


def make_tree(parent, columns, height=None):
    """可縮放的 Treeview (垂直 + 水平捲軸)。columns: [(id, 標題, 寬度, 對齊)]
    最小欄寬 = 標題文字寬度 + 留白，所以文字不會被截掉；所有欄位都能用滑鼠拖曳邊界調整。"""
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True)
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings",
                        selectmode="extended", **({"height": height} if height else {}))
    f = tkfont.Font(font=ui_font(0, True))
    for cid, text, w, anchor in columns:
        need = f.measure(text) + 36
        tree.heading(cid, text=text)
        tree.column(cid, width=max(w, need), anchor=anchor, minwidth=need,
                    stretch=(w > 80))               # 窄欄 (刪除、次數) 固定，其他隨視窗伸縮
    tree.tag_configure("odd", background=THEME["row_alt"])
    vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
    hs = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
    tree.grid(row=0, column=0, sticky="nsew")
    vs.grid(row=0, column=1, sticky="ns")
    hs.grid(row=1, column=0, sticky="we")
    return tree
