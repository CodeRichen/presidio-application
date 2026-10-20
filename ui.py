"""
ui.py - 剪貼簿敏感資訊防護 圖形介面 (tkinter，不需額外安裝)
====================================================
與 main.py 互相獨立，只 import core.py / entity_store.py / hotkey_service.py。
分頁：
  - 文字      貼上/輸入文字 -> 遮蔽 / 還原 / 自動；送模型前統一先做前處理 (全形轉半形、隱形字元、網頁標籤、簡繁、空白)，
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

import core
import entity_store
import hotkey_service
import preprocess

# ---------- 快速鍵錄製：Tk 按鍵事件 -> pynput 格式 ----------
_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                     "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                     "Caps_Lock", "Num_Lock", "Scroll_Lock"}
_SPECIAL_KEYS = {"space": "<space>", "Return": "<enter>", "Tab": "<tab>", "BackSpace": "<backspace>",
                 "Delete": "<delete>", "Insert": "<insert>", "Home": "<home>", "End": "<end>",
                 "Prior": "<page_up>", "Next": "<page_down>",
                 "Up": "<up>", "Down": "<down>", "Left": "<left>", "Right": "<right>"}

AUTO_LABEL = "自動判斷 (由模型決定)"
LABEL_CHOICES = [AUTO_LABEL, "PERSON", "ORGANIZATION", "ADDRESS", "LOCATION", "POSITION", "TW_PHONE",
                 "EMAIL", "TW_ID", "CREDIT_CARD", "URL", "FILE_NAME", "CREDENTIAL_LIKE", "GENERIC_SECRET"]


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
    """把 print 的輸出導到 queue，由主執行緒更新到記錄區"""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        if s:
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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("剪貼簿敏感資訊防護")
        self.geometry("1080x880")
        self.minsize(900, 720)

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

    # ================= 介面 =================
    def _build(self):
        self.nb = nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        # ===== 文字分頁 =====
        t = ttk.Frame(nb)
        nb.add(t, text="文字")

        bar = ttk.Frame(t)
        bar.pack(fill="x", pady=4)
        ttk.Label(bar, text="內容類型：").pack(side="left")
        self.kind_var = tk.StringVar(value="自動判斷")
        ttk.Combobox(bar, textvariable=self.kind_var, width=10, state="readonly",
                     values=["自動判斷", "一般文字", "程式碼"]).pack(side="left", padx=4)
        self.btn_read = ttk.Button(bar, text="讀取剪貼簿", command=self.read_clipboard_text)
        self.btn_read.pack(side="left", padx=(12, 2))
        self.btn_auto = ttk.Button(bar, text="自動 (有標籤還原/否則遮蔽)", command=lambda: self.run_text("auto"))
        self.btn_auto.pack(side="left", padx=2)
        self.btn_mask = ttk.Button(bar, text="遮蔽", command=lambda: self.run_text("mask"))
        self.btn_mask.pack(side="left", padx=2)
        self.btn_restore = ttk.Button(bar, text="還原", command=lambda: self.run_text("restore"))
        self.btn_restore.pack(side="left", padx=2)
        self.pp_var = tk.BooleanVar(value=preprocess.ENABLED)
        ttk.Checkbutton(bar, text="送模型前先前處理", variable=self.pp_var,
                        command=self._toggle_preprocess).pack(side="left", padx=(14, 2))
        ttk.Button(bar, text="預覽前處理", command=self.preview_preprocess).pack(side="left", padx=2)

        panes = ttk.PanedWindow(t, orient="vertical")
        panes.pack(fill="both", expand=True)
        f1 = ttk.LabelFrame(panes, text="輸入")
        f2 = ttk.LabelFrame(panes, text="結果")
        self.txt_in = scrolledtext.ScrolledText(f1, height=10, wrap="word", undo=True)
        self.txt_in.pack(fill="both", expand=True)
        self.txt_out = scrolledtext.ScrolledText(f2, height=10, wrap="word")
        self.txt_out.pack(fill="both", expand=True)
        panes.add(f1, weight=1)
        panes.add(f2, weight=1)

        bar2 = ttk.Frame(t)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="結果複製到剪貼簿", command=self.copy_result_text).pack(side="left")
        ttk.Button(bar2, text="結果移到輸入框", command=self.result_to_input).pack(side="left", padx=4)
        ttk.Button(bar2, text="清空", command=self.clear_text).pack(side="left")

        # ===== 檔案分頁 =====
        f = ttk.Frame(nb)
        nb.add(f, text="檔案")

        bar = ttk.Frame(f)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="新增檔案…", command=self.add_files).pack(side="left")
        ttk.Button(bar, text="從剪貼簿讀取檔案", command=self.read_clipboard_files).pack(side="left", padx=4)
        ttk.Button(bar, text="移除選取", command=self.remove_selected).pack(side="left")
        ttk.Button(bar, text="清空清單", command=self.clear_files).pack(side="left", padx=4)
        self.btn_run_files = ttk.Button(bar, text="開始處理", command=self.run_files)
        self.btn_run_files.pack(side="right")

        lf = ttk.LabelFrame(f, text="待處理檔案")
        lf.pack(fill="both", expand=True, pady=(0, 4))
        self.lst_files = tk.Listbox(lf, selectmode="extended", height=8)
        self.lst_files.pack(fill="both", expand=True)

        lo = ttk.LabelFrame(f, text="已產生的去識別化檔案")
        lo.pack(fill="both", expand=True)
        self.lst_out = tk.Listbox(lo, selectmode="extended", height=8)
        self.lst_out.pack(fill="both", expand=True)

        bar2 = ttk.Frame(f)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="輸出檔案複製到剪貼簿", command=self.copy_outputs).pack(side="left")
        ttk.Button(bar2, text="開啟所在資料夾", command=self.open_output_folder).pack(side="left", padx=4)

        # ===== 資料夾掃描分頁 =====
        s = ttk.Frame(nb)
        nb.add(s, text="資料夾掃描")
        self._build_scan_tab(s)

        # ===== 名單管理分頁 =====
        lists = ttk.Frame(nb)
        nb.add(lists, text="名單管理")
        self._build_lists_tab(lists)

        # ===== 快速鍵分頁 =====
        h = ttk.Frame(nb)
        nb.add(h, text="快速鍵")
        self._build_hotkey_tab(h)

        # ===== 對照表分頁 =====
        m = ttk.Frame(nb)
        nb.add(m, text="對照表")
        bar = ttk.Frame(m)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="重新整理", command=self.refresh_map_view).pack(side="left")
        ttk.Button(bar, text="清除對照表", command=self.clear_map).pack(side="left", padx=4)
        self.tree = make_tree(m, [("tag", "標籤", 200, "w"), ("val", "原始值 (明碼)", 600, "w")])
        self.refresh_map_view()

        nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_lists())

        # ===== 記錄區 =====
        lg = ttk.LabelFrame(self, text="記錄")
        lg.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(lg, height=9, wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就緒")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        self._busy_widgets = [self.btn_read, self.btn_auto, self.btn_mask, self.btn_restore,
                              self.btn_run_files, self.btn_scan, self.btn_add]
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
        self.btn_scan = ttk.Button(bar, text="開始掃描並遮蔽", command=self.run_scan)
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
        self.txt_add.pack(fill="x")
        bar = ttk.Frame(a)
        bar.pack(fill="x", pady=6)
        ttk.Label(bar, text="類型：").pack(side="left")
        self.add_label = tk.StringVar(value=AUTO_LABEL)
        ttk.Combobox(bar, textvariable=self.add_label, values=LABEL_CHOICES, width=26).pack(side="left", padx=4)
        self.btn_add = ttk.Button(bar, text="新增到敏感清單", command=self.run_add_entities)
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
        ttk.Button(bar, text="套用並儲存", command=self.apply_hotkeys).pack(side="left")
        ttk.Button(bar, text="還原預設", command=self.reset_hotkeys).pack(side="left", padx=4)
        self.btn_svc = ttk.Button(bar, text="啟動全域快速鍵", command=self.toggle_service)
        self.btn_svc.pack(side="left", padx=(16, 4))
        ttk.Label(bar, textvariable=self.svc_state).pack(side="left", padx=8)

        tip = ("說明：\n"
               "• 模式 A：貼上欄有值。複製只產生覆蓋結果，按貼上快速鍵才會貼出去，原生 Ctrl+C / Ctrl+V 不受影響。\n"
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

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", item[1])
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif item[0] == "status":
                    if self.busy:
                        self.status.set(item[1])
                elif item[0] == "done":
                    _, result, err = item
                    self._set_busy(False)
                    cb, self._on_done = self._on_done, None
                    if err:
                        print(f"[錯誤] {err}")
                        messagebox.showerror("處理失敗", str(err))
                    elif cb:
                        cb(result)
                    self.refresh_map_view()
        except queue.Empty:
            pass
        if len(self.mapping) != self._map_n:   # 快速鍵在背景新增了標籤
            self.refresh_map_view()
        self._tick += 1
        if self._tick % 10 == 0 and entity_store.signature() != self._store_sig:   # 名單被 main / 快速鍵改過
            self.refresh_lists()
        self.after(100, self._poll)

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

    def run_text(self, action):
        text = self.txt_in.get("1.0", "end-1c")
        if not text.strip():
            messagebox.showinfo("提示", "輸入框是空的")
            return
        is_code = {"自動判斷": None, "一般文字": False, "程式碼": True}[self.kind_var.get()]

        def job():
            print("\n" + "=" * 50)
            return core.process_text(text, self.mapping, action=action, is_code=is_code)

        def done(result):
            self.txt_out.delete("1.0", "end")
            if result:
                self.txt_out.insert("1.0", result)
            else:
                self.txt_out.insert("1.0", text)
                print("[結果] 未偵測到需要遮蔽 / 還原的內容，結果與輸入相同")
            self.refresh_lists()

        self._start(job, "處理文字中…", done)

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
        html = self.kind_var.get() != "程式碼"          # 程式碼不做網頁標籤刪除，與實際偵測一致
        desc, body = preprocess.describe(text, html=html)
        self.txt_out.delete("1.0", "end")
        self.txt_out.insert("1.0", desc + "\n\n──── 送模型的文字 ────\n" + body)

    def copy_result_text(self):
        text = self.txt_out.get("1.0", "end-1c")
        if text:
            core.copy_text_to_clipboard(text)
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

    def _selected_outputs(self):
        sel = self.lst_out.curselection()
        return [self.outputs[i] for i in sel] if sel else list(self.outputs)

    def copy_outputs(self):
        paths = self._selected_outputs()
        if not paths:
            messagebox.showinfo("提示", "沒有可複製的輸出檔案")
            return
        core.copy_files_to_clipboard(paths)
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


if __name__ == "__main__":
    App().mainloop()