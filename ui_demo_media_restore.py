# -*- coding: utf-8 -*-
"""為美化版 Tk Demo UI 加入媒體還原入口，不改原遮蔽/核心呼叫。

patch_demo_app(App) 必須在 Demo UI 模組中定義 App 後、建立 App 前呼叫。
"""
import json
import os
import queue
import threading
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox


def _find_file_tab(root):
    todo=[root]
    while todo:
        widget=todo.pop()
        if isinstance(widget, ttk.Notebook):
            for tab_id in widget.tabs():
                title=widget.tab(tab_id, "text")
                if "檔案" in title or "PDF" in title or "文件" in title:
                    return widget.nametowidget(tab_id)
        todo.extend(widget.winfo_children())
    return None


def attach_restore_entry(app):
    if getattr(app, "_seamless_restore_installed", False):
        return
    app._seamless_restore_installed=True
    result_q=queue.Queue()
    is_busy=[False]

    def restore_click():
        if is_busy[0]:
            return
        source=filedialog.askopenfilename(
            parent=app, title="選擇已遮蔽的 PDF／圖片",
            filetypes=[("已遮蔽的 PDF / 圖片", "*_masked.pdf *_masked.png *_masked.jpg *_masked.jpeg"),
                       ("PDF", "*.pdf"), ("圖片", "*.png *.jpg *.jpeg"), ("所有檔案", "*.*")])
        if not source:
            return
        if not os.path.isfile(source) or not source.lower().endswith((".pdf", ".png", ".jpg", ".jpeg")):
            messagebox.showerror("無法還原", "請選擇已遮蔽的 PDF 或圖片", parent=app)
            return
        import handler_media
        map_path=os.path.abspath(handler_media.DEFAULT_MAP_FILE)
        try:
            with open(map_path, "r", encoding="utf-8") as f:
                data=json.load(f)
            if not isinstance(data, dict) or not data.get(os.path.basename(source)):
                raise ValueError("missing entry")
        except (FileNotFoundError, OSError, ValueError, TypeError):
            messagebox.showerror("找不到還原資料", "目前沒有對應的媒體還原對照表。請在同一次 Demo UI 執行期間還原。", parent=app)
            return
        stem,ext=os.path.splitext(os.path.basename(source))
        if stem.lower().endswith("_masked"):
            stem=stem[:-len("_masked")]
        suggested=f"{stem}_restored_{datetime.now():%Y%m%d_%H%M%S_%f}{ext}"
        destination=filedialog.asksaveasfilename(parent=app,title="儲存私有還原檔案",
            initialdir=os.path.dirname(source),initialfile=suggested,
            defaultextension=ext, filetypes=[("PDF", "*.pdf")] if ext.lower()==".pdf" else [("圖片", "*.png *.jpg *.jpeg")])
        if not destination:
            return
        if os.path.abspath(destination)==os.path.abspath(source) or os.path.lexists(destination):
            messagebox.showerror("拒絕覆寫", "目的檔已存在，請使用新檔名。", parent=app)
            return
        if os.path.splitext(destination)[1].lower()!=ext.lower():
            messagebox.showerror("副檔名不符", "請使用與原檔相同的副檔名。", parent=app)
            return
        if not messagebox.askyesno("確認還原", "此操作會恢復原始人臉及敏感文字。還原檔只能保存在私有位置，不可公開分享。\n\n是否繼續？", parent=app):
            return
        is_busy[0]=True
        try:
            restore_button.configure(state="disabled")
        except Exception:
            pass
        def worker():
            try:
                handler_media.restore_file(source, destination, map_path)
                result_q.put(("ok", destination))
            except Exception as exc:
                result_q.put(("err", type(exc).__name__))  # 不顯示原始 PII / 裁圖
        threading.Thread(target=worker, daemon=True).start()
        app.after(100, poll)

    def poll():
        try:
            kind,data=result_q.get_nowait()
        except queue.Empty:
            if is_busy[0] and app.winfo_exists():
                app.after(100,poll)
            return
        is_busy[0]=False
        try:
            restore_button.configure(state="normal")
        except Exception:
            pass
        if kind=="ok":
            messagebox.showinfo("還原完成", "已產生私有還原檔案：\n"+data+"\n\n請勿公開分享。", parent=app)
        else:
            messagebox.showerror("還原失敗", "還原未完成（"+data+"）。沒有覆寫現有文件；請檢查 PDF 與對照表。", parent=app)

    file_tab=_find_file_tab(app)
    restore_button=None
    if file_tab is not None:
        restore_button=ttk.Button(file_tab, text="還原 PDF／圖片…", command=restore_click)
        managers={c.winfo_manager() for c in file_tab.winfo_children() if c is not restore_button}
        if "grid" in managers:
            rows=[c.grid_info().get("row",0) for c in file_tab.winfo_children() if c.winfo_manager()=="grid"]
            restore_button.grid(row=max(rows,default=0)+1, column=0, sticky="e", padx=12, pady=6)
        elif "pack" in managers:
            restore_button.pack(side="bottom", anchor="e", padx=12, pady=6)
        else:
            restore_button.place(relx=1.0, rely=0.01, anchor="ne", x=-10, y=10)
    else:
        restore_button=ttk.Button(app, text="還原 PDF／圖片…", command=restore_click)
        restore_button.place(relx=1.0, rely=0.12, anchor="ne", x=-10, y=10)
    # 附加主選單可作為第二個入口；保留原有 menu。
    try:
        current=app.cget("menu")
        menu=app.nametowidget(current) if current else tk.Menu(app)
        extra=tk.Menu(menu, tearoff=False)
        extra.add_command(label="還原 PDF／圖片…", command=restore_click)
        menu.add_cascade(label="媒體還原", menu=extra)
        app.configure(menu=menu)
    except Exception:
        pass


def patch_demo_app(app_class):
    """包裝 App.__init__，保留美化 UI 的 class、繪製與狀態機。"""
    if getattr(app_class, "_seamless_restore_patch_applied", False):
        return
    original=app_class.__init__
    def wrapped(self, *args, **kwargs):
        original(self, *args, **kwargs)
        self.after_idle(lambda: attach_restore_entry(self))
    app_class.__init__=wrapped
    app_class._seamless_restore_patch_applied=True
