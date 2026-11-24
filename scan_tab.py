"""
scan_tab.py - 「資料夾掃描」分頁
====================================================
由 ui.py 建立：  self.scan_tab = ScanTab(self, frame)
- 選好資料夾就列出每一個檔案 (待掃描)
- 可勾選「產生遮蔽後的檔案」：勾選時會建立 <資料夾名>_masked；不勾選時只檢查，掃描完會把這個副本刪掉
- 掃描時依序變成：掃描中 → 已掃描 → 最後只分 安全 / 不安全 / 錯誤 / 略過
- 掃描完在下方列出所有不安全 (含敏感資訊) 的檔案，並顯示整體是否安全
- 耗時工作交給 app._start 在背景跑；背景只丟訊息進 queue，主執行緒才更新畫面
外觀 (顏色 / 狀態 / 尺寸) 都在 theme.py。
"""
import os
import queue
import shutil
import subprocess
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import core
from theme import LAYOUT, STATES, STATE_FG, BTN, hint_label, make_tree


def make_icon(color, shape="circle", size=None):
    """用程式畫小圖示，不需外部圖檔"""
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


def _labels_text(r):
    return ", ".join(f"{k}×{v}" for k, v in r["labels"].items()) or (r["error"] or "")


class ScanTab:
    def __init__(self, app, parent):
        self.app = app
        self.parent = parent
        self.scan_dir = tk.StringVar(value="")
        self.scan_recursive = tk.BooleanVar(value=True)
        self.make_files = tk.BooleanVar(value=True)     # 是否產生遮蔽後的檔案
        self.scan_summary = tk.StringVar(value="尚未掃描")
        self.verdict = tk.StringVar(value="")

        self.src = None             # 本次掃描的原資料夾
        self.dest = None            # 遮蔽後的資料夾 (沒產生檔案時為 None)
        self.results = []
        self._rel = {}              # iid -> 相對路徑
        self._st = {}               # iid -> 目前狀態
        self._res_rel = {}          # 結果表 iid -> 相對路徑
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
        p, pad = self.parent, LAYOUT["pad"]

        # 第一排：路徑與操作按鈕 (輸入框會隨視窗寬度伸縮)
        bar = ttk.Frame(p)
        bar.pack(fill="x", pady=(pad, 4))
        ttk.Label(bar, text="資料夾：", style="Info.TLabel").pack(side="left")
        ent = ttk.Entry(bar, textvariable=self.scan_dir)
        ent.pack(side="left", fill="x", expand=True, padx=4)
        ent.bind("<Return>", lambda _e: self.refresh_listing())
        ttk.Button(bar, text="瀏覽…", style=BTN["primary"], command=self.pick_dir).pack(side="left", padx=2)
        ttk.Button(bar, text="重新列出", style=BTN["default"], command=self.refresh_listing).pack(side="left", padx=2)
        self.btn_scan = ttk.Button(bar, text="開始掃描", style=BTN["success"], command=self.run)
        self.btn_scan.pack(side="left", padx=2)

        # 第二排：選項
        opt = ttk.Frame(p)
        opt.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(opt, text="包含子資料夾", variable=self.scan_recursive,
                        command=self.refresh_listing).pack(side="left")
        ttk.Checkbutton(opt, text="產生遮蔽後的檔案 (在原資料夾旁建立 <資料夾名>_masked)",
                        variable=self.make_files).pack(side="left", padx=16)

        hint_label(p, (
            "勾選「產生遮蔽後的檔案」：會在選擇的資料夾「同層」建立 <資料夾名>_masked，先複製所有內容，"
            "再只遮蔽複本裡含敏感資訊的檔案 (檔名不變)。不勾選：只檢查哪些檔案含敏感資訊，掃描完不留下任何副本。"
            "兩種情況原資料夾都不會被修改。"), pady=(0, 4))

        # 整體結果 + 摘要
        self.verdict_label = ttk.Label(p, textvariable=self.verdict, style="Title.TLabel")
        self.verdict_label.pack(anchor="w", pady=(2, 0))
        hint_label(p, textvariable=self.scan_summary, style="Title.TLabel", pady=(0, 4))

        # 圖例 (圖示 + 名稱 + 數量)
        legend = ttk.Frame(p)
        legend.pack(fill="x", pady=(0, 4))
        for key, (_c, _s, text, _bg) in STATES.items():
            var = tk.StringVar(value=f"{text} 0")
            self._count_vars[key] = var
            ttk.Label(legend, image=self._icons[key], textvariable=var,
                      compound="left").pack(side="left", padx=(0, 14))

        # 底部按鈕先 pack (side=bottom)，視窗縮小時才不會被擠掉
        bar2 = ttk.Frame(p)
        bar2.pack(side="bottom", fill="x", pady=4)
        ttk.Button(bar2, text="開啟遮蔽後的資料夾", style=BTN["teal"], command=self.open_dest).pack(side="left")
        ttk.Button(bar2, text="開啟選取檔案的位置", style=BTN["default"], command=self.open_file).pack(side="left", padx=4)

        # 上：全部檔案；下：掃描結果 (中間分隔線可拖曳)
        self.pane = ttk.PanedWindow(p, orient="vertical")
        self.pane.pack(fill="both", expand=True)

        frame = ttk.Frame(self.pane)
        self.pane.add(frame, weight=3)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        cols = [("file", "檔案 (相對路徑)", 440, "w"), ("kind", "類型", 90, "center"),
                ("hits", "筆數", 80, "center"), ("labels", "偵測到的類型", 340, "w")]
        self.tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="tree headings",
                                 selectmode="extended")
        self.tree.heading("#0", text="狀態")
        self.tree.column("#0", width=130, minwidth=110, stretch=False, anchor="w")
        for cid, text, w, anchor in cols:
            self.tree.heading(cid, text=text)
            self.tree.column(cid, width=w, anchor=anchor, minwidth=70, stretch=(w > 100))
        vs = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="we")
        for key, (_c, _s, _t, bg) in STATES.items():
            self.tree.tag_configure(key, foreground=STATE_FG[key], **({"background": bg} if bg else {}))

        res_box = ttk.LabelFrame(self.pane, text="掃描結果 (只列出不安全或出錯的檔案，雙擊開啟位置)")
        self.pane.add(res_box, weight=2)
        self.res_tree = make_tree(res_box, [
            ("state", "狀態", 90, "center"), ("file", "檔案 (相對路徑)", 360, "w"),
            ("hits", "筆數", 80, "center"), ("labels", "偵測到的類型", 320, "w")])
        for key in ("unsafe", "error", "safe"):
            bg = STATES[key][3]
            self.res_tree.tag_configure(key, foreground=STATE_FG[key], **({"background": bg} if bg else {}))
        self.res_tree.bind("<Double-1>", self._open_result)

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

    def _clear_results(self):
        self.res_tree.delete(*self.res_tree.get_children())
        self._res_rel.clear()
        self.verdict.set("")

    def refresh_listing(self):
        """列出資料夾內所有檔案，全部標成「待掃描」"""
        if self.app.busy:
            return
        folder = self.scan_dir.get().strip()
        self.tree.delete(*self.tree.get_children())
        self._clear_results()
        self._rel.clear()
        self._st.clear()
        self._counts = {k: 0 for k in STATES}
        self._current = None
        self._finalized.clear()
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
        make_files = self.make_files.get()
        # 不產生檔案時，掃描完要刪掉的是「這次新建立」的副本；掃描前就存在的資料夾絕不刪
        expected = os.path.normpath(folder) + "_masked"
        existed_before = os.path.exists(expected)
        self.refresh_listing()          # 以開始當下的實際內容為準
        self._start_drain()

        def progress(i, total, path, *extra):
            # 背景執行緒：只丟訊息，不碰 tkinter。若 core 日後在第 4 個參數帶上單檔結果，會即時顯示結果
            self.app.q.put(("status", f"掃描中 {i}/{total}：{os.path.basename(path)}"))
            res = extra[0] if extra and isinstance(extra[0], dict) else None
            self._pq.put((i, total, path, res))

        def job():
            print("\n" + "=" * 50)
            dest, results = core.scan_folder(folder, self.app.mapping, recursive=recursive, progress=progress)
            kept, note = True, ""
            if not make_files:
                kept, note = self._discard_copy(dest, folder, existed_before)
            return dest, results, kept, note

        def done(res):
            self._drain()               # 先把剩下的進度處理完
            dest, results, kept, note = res
            self.src = folder
            self.dest = dest if kept else None
            self.results = results
            self._apply_final(folder, results)

            unsafe = [r for r in results if self._state_of(r) == "unsafe"]
            errors = [r for r in results if self._state_of(r) == "error"]
            if unsafe:
                self.verdict.set(f"結果：不安全，有 {len(unsafe)} 個檔案含敏感資訊")
                self.verdict_label.configure(style="Unsafe.TLabel")
            else:
                suffix = f"（另有 {len(errors)} 個檔案讀取失敗，未能檢查）" if errors else ""
                self.verdict.set("結果：安全，沒有發現含敏感資訊的檔案" + suffix)
                self.verdict_label.configure(style="Safe.TLabel" if not errors else "Warn.TLabel")

            where = f"　遮蔽後的副本：{dest}" if kept else ("　(未產生檔案)" if not note else f"　({note})")
            self.scan_summary.set(f"共 {len(results)} 個檔案：不安全 {len(unsafe)}，"
                                  f"安全 {len(results) - len(unsafe) - len(errors)}，錯誤 {len(errors)}{where}")
            print("\n" + core.format_scan_report(results, dest))
            if note:
                print(f"[掃描] {note}")
            self.app.refresh_lists()

            if unsafe:
                lines = [f"- {os.path.relpath(r['path'], folder)}" for r in unsafe[:15]]
                more = f"\n… 另外 {len(unsafe) - 15} 個，請看下方結果表" if len(unsafe) > 15 else ""
                tail = f"\n\n遮蔽後的資料夾：\n{dest}" if kept else ""
                messagebox.showwarning("掃描結果：不安全",
                                       f"共 {len(unsafe)} 個檔案含敏感資訊：\n" + "\n".join(lines) + more + tail)
            else:
                messagebox.showinfo("掃描結果：安全", "沒有發現含敏感資訊的檔案。" +
                                    (f"\n(複製的資料夾：{dest})" if kept else ""))

        self.app._start(job, "掃描資料夾中…", done)

    @staticmethod
    def _discard_copy(dest, folder, existed_before):
        """不產生檔案：把這次建立的 _masked 副本刪掉。回傳 (副本是否仍保留, 說明文字)。
        只在『掃描前不存在、位置在原資料夾旁、名稱以 _masked 結尾』時才會刪，其他情況一律保留。"""
        if not dest or not os.path.isdir(dest):
            return False, ""
        if existed_before:
            return True, "掃描前就已有同名的 _masked 資料夾，為避免誤刪已保留，內容可能已被這次掃描更新"
        d, f = _norm(dest), _norm(folder)
        if d == f or not d.endswith("_masked") or os.path.dirname(d) != os.path.dirname(f):
            return True, f"副本位置不是預期的 {os.path.basename(folder)}_masked，為安全起見未刪除：{dest}"
        try:
            shutil.rmtree(dest)
            print(f"[掃描] 未勾選產生檔案，已刪除暫時的副本：{dest}")
            return False, ""
        except OSError as e:
            return True, f"無法刪除暫時副本 ({e})：{dest}"

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
                self.scan_summary.set(f"掃描中… {i}/{total}")
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

    @staticmethod
    def _state_of(r):
        """只分三種：錯誤 / 不安全 (有偵測到敏感資訊) / 安全"""
        if r.get("error") and not (r.get("hits") or r.get("output")):
            return "error"
        if r.get("hits") or r.get("output"):
            return "unsafe"
        return "safe"

    def _apply_result(self, iid, r, folder=None):
        state = self._state_of(r)
        if not self.tree.exists(iid):
            rel = os.path.relpath(r["path"], folder) if folder else os.path.basename(r["path"])
            self._insert(iid, rel, "pending")
        self._set_state(iid, state)
        self.tree.item(iid, values=(self._rel.get(iid, ""), r["kind"], r["hits"] or "", _labels_text(r)))
        self._finalized.add(iid)

    def _apply_final(self, folder, results):
        """整批結束：更新所有檔案的狀態，沒出現在結果裡的標為「略過」，並列出不安全的檔案"""
        seen = set()
        for r in results:
            iid = _norm(r["path"])
            seen.add(iid)
            self._apply_result(iid, r, folder)
        for iid in self.tree.get_children():
            if iid not in seen:
                self._set_state(iid, "skipped")
        self._current = None
        self._fill_results(folder, results)

    def _fill_results(self, folder, results):
        """結果表：只列出不安全或出錯的檔案，不安全排前面"""
        self._clear_results()
        order = {"unsafe": 0, "error": 1}
        rows = [r for r in results if self._state_of(r) in order]
        rows.sort(key=lambda r: order[self._state_of(r)])
        for i, r in enumerate(rows):
            state = self._state_of(r)
            rel = os.path.relpath(r["path"], folder)
            iid = str(i)
            self._res_rel[iid] = rel
            self.res_tree.insert("", "end", iid=iid, tags=(state,),
                                 values=(STATES[state][2], rel, r["hits"] or "", _labels_text(r)))
        if not rows:
            self.res_tree.insert("", "end", tags=("safe",),
                                 values=("安全", "沒有發現含敏感資訊的檔案", "", ""))

    # ---------------- 開啟位置 ----------------
    def open_dest(self):
        if self.dest and os.path.isdir(self.dest):
            subprocess.Popen(["explorer", os.path.normpath(self.dest)])
        else:
            messagebox.showinfo("提示", "沒有遮蔽後的資料夾 (尚未掃描，或掃描時沒有勾選產生檔案)")

    def _reveal(self, rel):
        """有副本就開副本裡的檔案，沒有副本就開原資料夾裡的檔案"""
        base = self.dest if self.dest else self.src
        if not base or not rel:
            return
        path = os.path.join(base, rel)
        if os.path.exists(path):
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])

    def open_file(self):
        sel = self.tree.selection()
        if sel:
            self._reveal(self._rel.get(sel[0], ""))

    def _open_result(self, _event=None):
        sel = self.res_tree.selection()
        if sel:
            self._reveal(self._res_rel.get(sel[0], ""))
