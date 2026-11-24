# -*- coding: utf-8 -*-
"""Offline face Ground Truth annotator for image/scanned PDFs.

Usage:
  python face_gt_annotator.py --pdf path/to/original.pdf
  python face_gt_annotator.py --check face_ground_truth.json [--pdf path/to/original.pdf]

Needs: pymupdf, Pillow, tkinter (usually part of Windows Python).
Does not modify the source PDF or use the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

EXPECTED_HINT = [1, 1, 1, 1, 4, 8, 1]  # Earlier *estimates*, not reference labels.
SCHEMA = "face_gt_pdf_points_v1"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fp:
        for data in iter(lambda: fp.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def check_json_file(json_path: Path, pdf_path: Path | None = None) -> tuple[dict[str, Any], list[str]]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    errors = []
    if data.get("schema") != SCHEMA:
        errors.append("不支援的標註 JSON 格式")
    pages = data.get("pages", [])
    if not isinstance(pages, list) or not pages:
        errors.append("pages 欄位缺少資料")
        pages = []
    for index, page in enumerate(pages, 1):
        if page.get("page") != index:
            errors.append(f"第 {index} 頁編號不一致")
        width = float(page.get("width_pt", 0))
        height = float(page.get("height_pt", 0))
        if width <= 0 or height <= 0:
            errors.append(f"第 {index} 頁尺寸不合法")
        for j, rect in enumerate(page.get("faces", []), 1):
            if not isinstance(rect, list) or len(rect) != 4 or not all(isinstance(x, (int, float)) for x in rect):
                errors.append(f"第 {index} 頁第 {j} 個框的格式不合法")
                continue
            x0, y0, x1, y1 = rect
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                errors.append(f"第 {index} 頁第 {j} 個框超出頁面或大小不合法")
    if pdf_path is not None:
        if not pdf_path.is_file():
            errors.append("找不到原始 PDF")
        elif file_sha256(pdf_path) != data.get("source_pdf", {}).get("sha256"):
            errors.append("PDF SHA256 與標註資料不一致，不能混用另一份 PDF")
    return data, errors


def new_ground_truth(pdf_path: Path, pdf_doc) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "coordinate_system": "PDF points; top-left origin; [x0,y0,x1,y1]",
        "source_pdf": {
            "filename": pdf_path.name,
            "sha256": file_sha256(pdf_path),
            "page_count": len(pdf_doc),
        },
        "pages": [
            {"page": i + 1,
             "width_pt": round(float(p.rect.width), 3),
             "height_pt": round(float(p.rect.height), 3),
             "faces": []}
            for i, p in enumerate(pdf_doc)
        ],
    }


def save_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".writing")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
            fp.write("\n")
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def run_gui(pdf_path: Path | None, out_path: Path | None):
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, ttk
        from PIL import Image, ImageTk
        import fitz
    except ImportError as exc:
        raise SystemExit(f"缺少 GUI 相依套件：{exc}。請確認已安裝 pymupdf 與 pillow。")

    root = tk.Tk()
    root.withdraw()
    if pdf_path is None:
        selected = filedialog.askopenfilename(
            title="選擇未遮蔽的原始七頁 PDF", filetypes=[("PDF", "*.pdf")], parent=root)
        if not selected:
            root.destroy()
            return
        pdf_path = Path(selected).resolve()
    if not pdf_path.is_file():
        messagebox.showerror("來源檔案錯誤", f"找不到 PDF：\n{pdf_path}", parent=root)
        root.destroy()
        return
    # A restored output is *not* an independent original. Avoid evaluation leakage.
    if any(part in pdf_path.stem.lower() for part in ("_masked", "_restored", "_fixed")):
        messagebox.showerror(
            "來源 PDF 不適合建立 Ground Truth",
            "請使用遮蔽之前的原始 PDF，不要使用 *_masked、*_restored 或 *_fixed PDF。",
            parent=root)
        root.destroy()
        return
    try:
        document = fitz.open(str(pdf_path))
        if not document.page_count:
            raise ValueError("PDF 沒有頁面")
        if document.needs_pass:
            raise ValueError("PDF 已加密，需要先提供可讀的原始測試資料")
    except Exception as exc:
        messagebox.showerror("PDF 無法開啟", str(exc), parent=root)
        root.destroy()
        return

    if out_path is None:
        out_path = pdf_path.with_name("face_ground_truth.json")
    out_path = out_path.resolve()
    if out_path.exists():
        try:
            data, errors = check_json_file(out_path, pdf_path)
            if errors or len(data.get("pages", [])) != len(document):
                raise ValueError("；".join(errors) or "頁數不一致")
        except Exception as exc:
            messagebox.showerror("現有標註檔案無法續編", f"{out_path}\n\n{exc}\n\n請用 --out 指定新的 JSON 路徑。", parent=root)
            document.close()
            root.destroy()
            return
    else:
        data = new_ground_truth(pdf_path, document)

    class Annotator:
        def __init__(self):
            self.root = root
            self.doc = document
            self.data = data
            self.page_index = 0
            self.zoom = 1.15
            self.photo = None
            self.start = None
            self.preview = None
            self.pending_rect = None
            self.root.title("人臉 Ground Truth 標註（原始 PDF）")
            self.root.geometry("1250x820")
            self.root.minsize(800, 540)

            header = ttk.Frame(root, padding=8)
            header.pack(fill="x")
            ttk.Button(header, text="◀ 上一頁", command=self.prev_page).pack(side="left", padx=3)
            ttk.Button(header, text="下一頁 ▶", command=self.next_page).pack(side="left", padx=3)
            ttk.Button(header, text="縮小 −", command=lambda: self.change_zoom(-0.25)).pack(side="left", padx=(15, 3))
            ttk.Button(header, text="放大 +", command=lambda: self.change_zoom(0.25)).pack(side="left", padx=3)
            ttk.Button(header, text="刪除本頁最後一框", command=self.undo).pack(side="left", padx=(15, 3))
            ttk.Button(header, text="儲存", command=self.save).pack(side="left", padx=3)
            ttk.Button(header, text="檢查進度", command=self.show_summary).pack(side="left", padx=3)

            self.info = ttk.Label(root, text="", padding=(10, 3))
            self.info.pack(fill="x")
            self.tip = ttk.Label(root, text="拖曳滑鼠左鍵框選每張臉（臉部範圍，勿框整個人）。每完成一框即自動儲存。可捲動頁面、放大檢查。", padding=(10, 3))
            self.tip.pack(fill="x")
            body = ttk.Frame(root)
            body.pack(fill="both", expand=True, padx=8, pady=6)
            self.canvas = tk.Canvas(body, bg="#dce1e7", highlightthickness=0)
            vscroll = ttk.Scrollbar(body, orient="vertical", command=self.canvas.yview)
            hscroll = ttk.Scrollbar(body, orient="horizontal", command=self.canvas.xview)
            self.canvas.configure(xscrollcommand=hscroll.set, yscrollcommand=vscroll.set)
            self.canvas.grid(row=0, column=0, sticky="nsew")
            vscroll.grid(row=0, column=1, sticky="ns")
            hscroll.grid(row=1, column=0, sticky="ew")
            body.rowconfigure(0, weight=1)
            body.columnconfigure(0, weight=1)
            self.canvas.bind("<ButtonPress-1>", self.mouse_down)
            self.canvas.bind("<B1-Motion>", self.mouse_drag)
            self.canvas.bind("<ButtonRelease-1>", self.mouse_up)
            self.canvas.bind("<MouseWheel>", self.wheel)
            self.canvas.bind("<Shift-MouseWheel>", self.shift_wheel)
            self.root.bind("<Control-s>", lambda e: self.save())
            self.root.bind("<Control-z>", lambda e: self.undo())
            self.root.bind("<Escape>", self.cancel_draw)
            self.root.protocol("WM_DELETE_WINDOW", self.close)
            self.render_page()

        def p(self):
            return self.data["pages"][self.page_index]

        def total(self):
            return sum(len(p["faces"]) for p in self.data["pages"])

        def save(self):
            save_atomic(out_path, self.data)
            self.refresh_info()

        def refresh_info(self):
            count = len(self.p()["faces"])
            estimate = f"｜先前人工估計本頁 {EXPECTED_HINT[self.page_index]} 張" if len(self.doc) == 7 else ""
            self.info.configure(text=f"原始 PDF：{pdf_path.name}｜第 {self.page_index+1}/{len(self.doc)} 頁｜本頁 {count} 張｜累計 {self.total()} 張｜縮放 {self.zoom:.2f}x{estimate}｜JSON：{out_path}")

        def render_page(self, preserve_view=False):
            old_x = self.canvas.xview()[0] if preserve_view else 0.0
            old_y = self.canvas.yview()[0] if preserve_view else 0.0
            page = self.doc[self.page_index]
            pix = page.get_pixmap(matrix=fitz.Matrix(self.zoom, self.zoom), colorspace=fitz.csRGB, alpha=False)
            mode = "RGB" if pix.n == 3 else "RGBA"
            img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
            self.photo = ImageTk.PhotoImage(img, master=self.root)
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
            self.canvas.configure(scrollregion=(0, 0, pix.width, pix.height))
            for i, rect in enumerate(self.p()["faces"], 1):
                x0, y0, x1, y1 = (v * self.zoom for v in rect)
                self.canvas.create_rectangle(x0,y0,x1,y1, outline="#ff2d1c", width=3, tags="gt")
                self.canvas.create_text(x0 + 3, y0 + 3, anchor="nw", text=str(i),
                                        fill="#ff2d1c", font=("Arial", 12, "bold"), tags="gt")
            self.refresh_info()
            self.canvas.xview_moveto(old_x)
            self.canvas.yview_moveto(old_y)

        def page_change(self, delta):
            nxt = self.page_index + delta
            if 0 <= nxt < len(self.doc):
                self.cancel_draw()
                self.page_index = nxt
                self.render_page()

        def prev_page(self): self.page_change(-1)
        def next_page(self): self.page_change(1)

        def change_zoom(self, delta):
            next_zoom = min(3.0, max(0.5, round(self.zoom + delta, 2)))
            if next_zoom != self.zoom:
                self.cancel_draw()
                self.zoom = next_zoom
                self.render_page()

        def canvas_pos(self, event):
            px = self.canvas.canvasx(event.x)
            py = self.canvas.canvasy(event.y)
            w = self.p()["width_pt"] * self.zoom
            h = self.p()["height_pt"] * self.zoom
            return min(w, max(0.0, px)), min(h, max(0.0, py))

        def mouse_down(self, event):
            self.start = self.canvas_pos(event)
            self.preview = self.canvas.create_rectangle(*self.start, *self.start, outline="#006fff", width=2, dash=(5,3))

        def mouse_drag(self, event):
            if self.start is not None and self.preview is not None:
                x,y = self.canvas_pos(event)
                self.canvas.coords(self.preview, self.start[0], self.start[1], x, y)

        def mouse_up(self, event):
            if self.start is None:
                return
            x1,y1 = self.canvas_pos(event)
            x0,y0 = self.start
            self.cancel_draw()
            x0,x1=sorted((x0/self.zoom,x1/self.zoom))
            y0,y1=sorted((y0/self.zoom,y1/self.zoom))
            if x1-x0 < 5.0 or y1-y0 < 5.0:
                return
            rect = [round(v, 3) for v in (x0,y0,x1,y1)]
            self.p()["faces"].append(rect)
            self.save()
            self.render_page(preserve_view=True)

        def cancel_draw(self, _event=None):
            if self.preview is not None:
                self.canvas.delete(self.preview)
            self.preview = None
            self.start = None

        def undo(self):
            if self.p()["faces"]:
                self.p()["faces"].pop()
                self.save()
                self.render_page(preserve_view=True)

        def wheel(self, event):
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

        def shift_wheel(self, event):
            self.canvas.xview_scroll(-1 if event.delta > 0 else 1, "units")

        def show_summary(self):
            self.save()
            counts = [len(p["faces"]) for p in self.data["pages"]]
            estimated = "\n先前估計（僅供參考）：1 / 1 / 1 / 1 / 4 / 8 / 1" if len(self.doc)==7 else ""
            messagebox.showinfo("Ground Truth 進度",
                                "每頁已標註人臉數：\n" + " / ".join(map(str,counts)) +
                                f"\n合計：{sum(counts)} 張{estimated}\n\n標註結果：\n{out_path}\n\n請以實際可見人臉為準，不必湊成 17 張。",
                                parent=self.root)

        def close(self):
            try:
                self.save()
            finally:
                self.doc.close()
                self.root.destroy()

    app = Annotator()
    root.deiconify()
    root.mainloop()


def main():
    parser = argparse.ArgumentParser(description="離線 PDF 人臉 Ground Truth 標註工具")
    parser.add_argument("--pdf", type=Path, help="遮蔽前的原始 PDF；省略可用選檔視窗")
    parser.add_argument("--out", type=Path, help="標註 JSON 輸出路徑；預設在 PDF 所在資料夾")
    parser.add_argument("--check", type=Path, help="只檢查 JSON 和標註數量，不啟動 GUI")
    args = parser.parse_args()
    if args.check:
        if not args.check.is_file():
            raise SystemExit(f"找不到標註 JSON：{args.check}")
        data, errors = check_json_file(args.check, args.pdf)
        counts = [len(p.get("faces", [])) for p in data.get("pages", [])]
        print("每頁人臉數：", counts)
        print("總人臉數：", sum(counts))
        if errors:
            print("檢查未通過：", "; ".join(errors))
            raise SystemExit(1)
        print("JSON 格式及標註框座標檢查：PASS")
        print("注意：這只是標註檔格式檢查，不是 YuNet Precision/Recall 測試。")
    else:
        run_gui(args.pdf, args.out)


if __name__ == "__main__":
    main()
