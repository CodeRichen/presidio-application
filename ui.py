"""
ui.py - 剪貼簿敏感資訊防護 圖形介面 (tkinter，不需額外安裝)
====================================================
與 main.py 互相獨立，只 import core.py 與 hotkey_service.py。
功能：
  - 文字：貼上/輸入文字 -> 遮蔽 / 還原 / 自動，可指定「程式碼 / 一般文字 / 自動判斷」
  - 檔案：選擇多個檔案 (code / text / media / csv / xlsx) -> 去識別化
  - 資料夾掃描：選一個資料夾，自動遮蔽裡面的圖片/PDF/文字/程式碼/表格，列出哪些檔案危險
  - 快速鍵：錄製並更改複製/貼上快速鍵、啟動/停止全域快速鍵 (設定存在 hotkeys.json，main.py 也會讀)
  - 敏感清單：檢視每次偵測到的敏感值 (存在 presidio/real/，不會被自動刪除)，可手動移除誤判
  - 檢視 / 清除對照表；關閉視窗時自動刪除對照表檔案 (敏感清單保留)
啟動時在背景預熱模型；耗時工作在背景執行緒跑，介面不會卡住；所有 print 顯示在下方記錄區。
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
import hotkey_service

# ---------- 快速鍵錄製：Tk 按鍵事件 -> pynput 格式 ----------
_MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R", "Shift_L", "Shift_R",
                     "Win_L", "Win_R", "Super_L", "Super_R", "Meta_L", "Meta_R",
                     "Caps_Lock", "Num_Lock", "Scroll_Lock"}
_SPECIAL_KEYS = {"space": "<space>", "Return": "<enter>", "Tab": "<tab>", "BackSpace": "<backspace>",
                 "Delete": "<delete>", "Insert": "<insert>", "Home": "<home>", "End": "<end>",
                 "Prior": "<page_up>", "Next": "<page_down>",
                 "Up": "<up>", "Down": "<down>", "Left": "<left>", "Right": "<right>"}


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("剪貼簿敏感資訊防護")
        self.geometry("1040x860")
        self.minsize(860, 700)

        self.mapping = core.load_map()
        self.q = queue.Queue()
        self.busy = False
        self._on_done = None
        self.files = []
        self.outputs = []
        self.scan_results = []
        self._recording = None      # (StringVar, Entry) 正在錄製快速鍵的欄位
        self._map_n = -1            # 用來偵測快速鍵背景處理造成的對照表變動
        self.service = hotkey_service.HotkeyService(self.mapping)

        sys.stdout = QueueWriter(self.q)  # 之後所有 print 都進記錄區

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        print(f"[就緒] 已載入對照表 {len(self.mapping)} 筆、敏感清單 {core.sensitive_count()} 筆")
        self._start(core.warmup, "載入模型中…", None)  # 背景預熱

    # ---------------- 介面 ----------------
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

        # ===== 快速鍵分頁 =====
        h = ttk.Frame(nb)
        nb.add(h, text="快速鍵")
        self._build_hotkey_tab(h)

        # ===== 敏感清單分頁 =====
        k = ttk.Frame(nb)
        nb.add(k, text="敏感清單")
        self._build_known_tab(k)

        # ===== 對照表分頁 =====
        m = ttk.Frame(nb)
        nb.add(m, text="對照表")
        bar = ttk.Frame(m)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="重新整理", command=self.refresh_map_view).pack(side="left")
        ttk.Button(bar, text="清除對照表", command=self.clear_map).pack(side="left", padx=4)
        self.tree = ttk.Treeview(m, columns=("tag", "val"), show="headings")
        self.tree.heading("tag", text="標籤")
        self.tree.heading("val", text="原始值 (明碼)")
        self.tree.column("tag", width=200, anchor="w")
        self.tree.column("val", width=600, anchor="w")
        self.tree.pack(fill="both", expand=True)
        self.refresh_map_view()

        nb.bind("<<NotebookTabChanged>>", lambda _e: self.refresh_known_view())

        # ===== 記錄區 =====
        lg = ttk.LabelFrame(self, text="記錄")
        lg.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(lg, height=10, wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就緒")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        self._busy_widgets = [self.btn_read, self.btn_auto, self.btn_mask,
                              self.btn_restore, self.btn_run_files, self.btn_scan]

    # ---------------- 資料夾掃描 ----------------
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

        ttk.Label(parent, textvariable=self.scan_summary, font=("", 10, "bold")).pack(anchor="w", pady=(2, 4))

        cols = ("risk", "file", "kind", "hits", "labels", "output")
        self.scan_tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="extended")
        for c, text, w in (("risk", "風險", 60), ("file", "檔案", 340), ("kind", "類型", 70),
                           ("hits", "筆數", 50), ("labels", "偵測到的類型", 220), ("output", "去識別化輸出", 280)):
            self.scan_tree.heading(c, text=text)
            self.scan_tree.column(c, width=w, anchor="w")
        self.scan_tree.tag_configure("高", foreground="#b91c1c")
        self.scan_tree.tag_configure("中", foreground="#b45309")
        self.scan_tree.tag_configure("錯誤", foreground="#6b7280")
        self.scan_tree.pack(fill="both", expand=True)

        bar2 = ttk.Frame(parent)
        bar2.pack(fill="x", pady=4)
        ttk.Button(bar2, text="開啟所在資料夾", command=self.open_scan_folder).pack(side="left")
        ttk.Button(bar2, text="輸出檔案複製到剪貼簿", command=self.copy_scan_outputs).pack(side="left", padx=4)
        ttk.Label(bar2, text="(只有偵測到敏感資訊的檔案才會列為危險並產生 *_masked 檔；選取列則只處理選取的)"
                  ).pack(side="left", padx=8)

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

        def done(results):
            self.scan_results = results
            self.scan_tree.delete(*self.scan_tree.get_children())
            for i, r in enumerate(results):
                rel = os.path.relpath(r["path"], folder)
                labels = ", ".join(f"{k}×{v}" for k, v in r["labels"].items()) or (r["error"] or "")
                self.scan_tree.insert("", "end", iid=str(i), tags=(r["risk"],), values=(
                    r["risk"] if r["risk"] != "無" else "安全", rel, r["kind"],
                    r["hits"] or "", labels, r["output"] or ""))
            danger = [r for r in results if r["output"]]
            high = sum(1 for r in danger if r["risk"] == "高")
            self.scan_summary.set(f"共 {len(results)} 個檔案：危險 {len(danger)} 個 (高風險 {high})，"
                                  f"安全 {len(results) - len(danger)} 個")
            print("\n" + core.format_scan_report(results))
            self.refresh_known_view()
            if danger:
                lines = [f"[{r['risk']}] {os.path.relpath(r['path'], folder)}" for r in danger[:15]]
                more = f"\n… 另外 {len(danger) - 15} 個，請看清單" if len(danger) > 15 else ""
                messagebox.showwarning("發現含敏感資訊的檔案",
                                       f"共 {len(danger)} 個檔案含敏感資訊 (已產生 *_masked 去識別化檔)：\n\n"
                                       + "\n".join(lines) + more)
            else:
                messagebox.showinfo("掃描完成", "沒有發現含敏感資訊的檔案")

        self._start(job, "掃描資料夾中…", done)

    def _selected_scan_results(self):
        sel = self.scan_tree.selection()
        rows = [self.scan_results[int(i)] for i in sel] if sel else self.scan_results
        return [r for r in rows if r["output"]]

    def open_scan_folder(self):
        sel = self.scan_tree.selection()
        if sel:
            path = self.scan_results[int(sel[0])]["path"]
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def copy_scan_outputs(self):
        paths = [r["output"] for r in self._selected_scan_results()]
        if not paths:
            messagebox.showinfo("提示", "沒有可複製的輸出檔案")
            return
        core.copy_files_to_clipboard(paths)
        print(f"[完成] 已把 {len(paths)} 個去識別化檔案複製到剪貼簿")

    # ---------------- 敏感清單 ----------------
    def _build_known_tab(self, parent):
        self.known_info = tk.StringVar(value="")
        bar = ttk.Frame(parent)
        bar.pack(fill="x", pady=4)
        ttk.Button(bar, text="重新整理", command=self.refresh_known_view).pack(side="left")
        ttk.Button(bar, text="移除選取 (誤判的詞)", command=self.remove_known).pack(side="left", padx=4)
        ttk.Label(bar, textvariable=self.known_info).pack(side="left", padx=8)

        cols = ("value", "label", "count", "last", "sources")
        self.known_tree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="extended")
        for c, text, w in (("value", "敏感值 (明碼)", 320), ("label", "類型", 140), ("count", "次數", 50),
                           ("last", "最近出現", 140), ("sources", "來源", 280)):
            self.known_tree.heading(c, text=text)
            self.known_tree.column(c, width=w, anchor="w")
        self.known_tree.pack(fill="both", expand=True)

        ttk.Label(parent, justify="left", wraplength=940, text=(
            f"說明：每次遮蔽偵測到的敏感值都會記錄到 {core.SENSITIVE_FILE}，程式結束時不會刪除。\n"
            "之後文字 / 程式碼 / 表格出現清單裡的值，會被當成正則規則自動遮蔽。"
            "若某個詞被誤判成敏感資訊，在這裡移除即可。檔案內是明碼，請注意存放權限。")
        ).pack(fill="x", padx=4, pady=6)

    def refresh_known_view(self):
        if not hasattr(self, "known_tree"):
            return
        items = core.load_sensitive_items()
        self.known_tree.delete(*self.known_tree.get_children())
        for r in items:
            self.known_tree.insert("", "end", iid=r["value"], values=(
                r["value"], r.get("label", ""), r.get("count", 1), r.get("last_seen", ""),
                ", ".join(r.get("sources", []))))
        self.known_info.set(f"共 {len(items)} 筆" + ("" if core.USE_KNOWN_ITEMS else "  (自動遮蔽已關閉：core.USE_KNOWN_ITEMS = False)"))

    def remove_known(self):
        sel = list(self.known_tree.selection())
        if not sel:
            return
        if messagebox.askyesno("移除", f"從敏感清單移除 {len(sel)} 筆？之後這些值不會再被自動遮蔽。"):
            n = core.remove_sensitive_items(sel)
            print(f"[敏感清單] 已移除 {n} 筆")
            self.refresh_known_view()

    # ---------------- 快速鍵 ----------------
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
        ttk.Label(box, textvariable=self.hk_mode, wraplength=900, justify="left").grid(
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
        ttk.Label(parent, text=tip, wraplength=940, justify="left").pack(fill="x", padx=8, pady=8)

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

    # ---------------- 背景執行 ----------------
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
        self.after(100, self._poll)

    # ---------------- 文字 ----------------
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

        self._start(job, "處理文字中…", done)

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

    # ---------------- 檔案 ----------------
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
            self.refresh_known_view()
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

    # ---------------- 對照表 ----------------
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

    # ---------------- 結束 ----------------
    def _on_close(self):
        self.service.stop()
        sys.stdout = sys.__stdout__
        core.cleanup_map_files()   # 只刪對照表；敏感清單刻意保留
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
