"""
ui.py - 剪貼簿敏感資訊防護 圖形介面 (tkinter，不需額外安裝)
====================================================
與 main.py 互相獨立，兩者都只呼叫 core.py。
功能：
  - 文字：貼上/輸入文字 -> 遮蔽 / 還原 / 自動，可指定「程式碼 / 一般文字 / 自動判斷」
  - 檔案：選擇多個檔案 (code / text / media / csv / xlsx) -> 去識別化
  - 剪貼簿：讀取剪貼簿 (文字或檔案)、把結果寫回剪貼簿
  - 檢視 / 清除對照表；關閉視窗時自動刪除對照表檔案
啟動時在背景預熱模型；耗時工作在背景執行緒跑，介面不會卡住；所有 print 顯示在下方記錄區。
"""
import os
import sys
import queue
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

import core


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
        self.geometry("980x780")
        self.minsize(820, 640)

        self.mapping = core.load_map()
        self.q = queue.Queue()
        self.busy = False
        self._on_done = None
        self.files = []
        self.outputs = []

        sys.stdout = QueueWriter(self.q)  # 之後所有 print 都進記錄區

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        print(f"[就緒] 已載入對照表 {len(self.mapping)} 筆")
        self._start(core.warmup, "載入模型中…", None)  # 背景預熱

    # ---------------- 介面 ----------------
    def _build(self):
        nb = ttk.Notebook(self)
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

        # ===== 記錄區 =====
        lg = ttk.LabelFrame(self, text="記錄")
        lg.pack(fill="both", expand=False, padx=8, pady=(0, 8))
        self.log = scrolledtext.ScrolledText(lg, height=11, wrap="none", state="disabled")
        self.log.pack(fill="both", expand=True)
        self.status = tk.StringVar(value="就緒")
        ttk.Label(self, textvariable=self.status, anchor="w").pack(fill="x", padx=8, pady=(0, 4))

        self._busy_widgets = [self.btn_read, self.btn_auto, self.btn_mask,
                              self.btn_restore, self.btn_run_files]

    # ---------------- 背景執行 ----------------
    def _set_busy(self, busy, msg=""):
        self.busy = busy
        for w in self._busy_widgets:
            w.state(["disabled"] if busy else ["!disabled"])
        self.status.set(msg if busy else "就緒")

    def _start(self, func, msg, on_done):
        """在背景執行 func，完成後在主執行緒呼叫 on_done(result)"""
        if self.busy:
            return
        self._on_done = on_done
        self._set_busy(True, msg)

        def worker():
            try:
                self.q.put(("done", func(), None))
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
        for tag, val in self.mapping.items():
            self.tree.insert("", "end", values=(tag, val))

    def clear_map(self):
        if messagebox.askyesno("清除對照表", "清除後，已遮蔽的內容將無法還原。確定嗎？"):
            core.clear_map(self.mapping)
            self.refresh_map_view()
            print("[完成] 對照表已清除")

    # ---------------- 結束 ----------------
    def _on_close(self):
        sys.stdout = sys.__stdout__
        core.cleanup_map_files()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()