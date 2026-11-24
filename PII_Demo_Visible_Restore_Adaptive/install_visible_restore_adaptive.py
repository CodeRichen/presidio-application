# -*- coding: utf-8 -*-
"""Adaptive / anchor-guarded Demo UI visible restore button installer.

Does not require an exact SHA256 of the on-device UI. Instead inspects the
existing App and updates only known, uniquely matched anchors. --check is
read-only; --apply makes a timestamped backup then uses atomic replacement.
Never touches core.py, OCR, masking logic, or user files.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime
import difflib
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

MARKER = "# SEAMLESS_RESTORE_VISIBLE_UI_V2"
OLD_HOOK = """# SEAMLESS_RESTORE_UI_V1
from ui_demo_media_restore import patch_demo_app as _install_seamless_restore_for_demo
_install_seamless_restore_for_demo(App)"""

RESTORE_METHOD = '    def restore_media(self):\n        """從檔案頁還原圖片／PDF（同一執行階段的私有 media map）。\n\n        前台負責選檔與提示；真正還原在共用 _start() 工作執行緒中完成。\n        公開輸出用 hard link 無覆寫發布，失敗不會留下不完整的結果。\n        """\n        if self.busy:\n            return\n        import json\n        import tempfile\n        import shutil\n        import handler_media\n\n        # 先採用使用者在「已產生檔案」清單中明確選取的那一份。\n        selected = self.lst_out.curselection()\n        source = self.outputs[selected[0]] if len(selected) == 1 else ""\n        if not source:\n            source = filedialog.askopenfilename(\n                parent=self,\n                title="選擇要還原的已遮蔽 PDF／圖片",\n                filetypes=[("遮蔽 PDF／圖片", "*_masked.pdf *_masked.png *_masked.jpg *_masked.jpeg"),\n                           ("PDF", "*.pdf"), ("圖片", "*.png *.jpg *.jpeg"),\n                           ("所有檔案", "*.*")],\n            )\n        if not source:\n            return\n        source = os.path.abspath(source)\n        ext = os.path.splitext(source)[1].lower()\n        if (not os.path.isfile(source) or os.path.islink(source)\n                or ext not in {".pdf", ".png", ".jpg", ".jpeg"}):\n            messagebox.showerror("無法還原", "請選擇存在的已遮蔽 PDF／圖片檔案。", parent=self)\n            return\n\n        map_path = os.path.abspath(handler_media.DEFAULT_MAP_FILE)\n        try:\n            with open(map_path, encoding="utf-8") as f:\n                data = json.load(f)\n            entries = data.get(os.path.basename(source)) if isinstance(data, dict) else None\n            if not isinstance(entries, list) or not entries:\n                raise ValueError("missing media restore entries")\n        except (OSError, ValueError, TypeError):\n            messagebox.showerror("找不到還原對照表",\n                "目前找不到這份檔案的還原資料。請先遮蔽後在同一次 Demo UI 執行期間還原。",\n                parent=self)\n            return\n\n        stem = os.path.splitext(os.path.basename(source))[0]\n        if stem.lower().endswith("_masked"):\n            stem = stem[:-7]\n        from datetime import datetime\n        initialfile = f"{stem}_restored_{datetime.now():%Y%m%d_%H%M%S_%f}{ext}"\n        dest = filedialog.asksaveasfilename(\n            parent=self, title="儲存私有還原文件",\n            initialdir=os.path.dirname(source), initialfile=initialfile,\n            defaultextension=ext,\n            filetypes=[("PDF", "*.pdf")] if ext == ".pdf" else\n                      [("圖片", "*.png *.jpg *.jpeg")],\n        )\n        if not dest:\n            return\n        dest = os.path.abspath(dest)\n        if (os.path.normcase(os.path.realpath(dest)) == os.path.normcase(os.path.realpath(source))\n                or os.path.lexists(dest)):\n            messagebox.showerror("拒絕覆寫", "輸出檔已存在或與來源相同，請使用新檔名。", parent=self)\n            return\n        if os.path.splitext(dest)[1].lower() != ext:\n            messagebox.showerror("副檔名不符", "還原檔必須使用與來源相同的副檔名。", parent=self)\n            return\n        if not messagebox.askyesno(\n            "確認還原", "還原後的檔案會包含原本的人臉與敏感資訊，只能在私有位置保存。\\n\\n確定繼續？",\n            parent=self,\n        ):\n            return\n\n        def job():\n            # 與其他檔案操作共用 WORK_LOCK，暫存與最終檔都不允許覆寫。\n            stage = tempfile.mkdtemp(prefix=".pii_private_restore_", dir=os.path.dirname(dest))\n            try:\n                try:\n                    os.chmod(stage, 0o700)\n                except OSError:\n                    pass\n                candidate = os.path.join(stage, os.path.basename(dest))\n                try:\n                    handler_media.restore_file(source, candidate, map_path)\n                except Exception as exc:\n                    raise RuntimeError("媒體還原失敗，請檢查還原對照表與 PDF 格式。") from exc\n                if (not os.path.isfile(candidate) or os.path.islink(candidate)\n                        or os.path.getsize(candidate) == 0):\n                    raise RuntimeError("未產生有效的還原檔案。")\n                try:\n                    os.link(candidate, dest)  # 同磁碟原子發布，不覆寫現有檔案\n                except FileExistsError as exc:\n                    raise RuntimeError("目的檔已存在，拒絕覆寫。") from exc\n                except OSError as exc:\n                    raise RuntimeError("無法安全發布還原檔，請確認 NTFS 與資料夾權限。") from exc\n                return dest\n            finally:\n                shutil.rmtree(stage, ignore_errors=True)\n\n        def done(result):\n            print("[還原完成] 已產生私有還原檔案（請勿公開分享）。")\n            messagebox.showinfo("還原完成",\n                "已完成 PDF／圖片還原：\\n" + result +\n                "\\n\\n此檔案包含原始敏感資訊，請勿公開分享。", parent=self)\n\n        self._start(job, "還原 PDF／圖片中…", done)\n\n'

def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize(raw: bytes):
    enc = 'utf-8-sig' if raw.startswith(b'\xef\xbb\xbf') else 'utf-8'
    text = raw.decode(enc)
    count_crlf = text.count('\r\n')
    lone_lf = text.count('\n') - count_crlf
    if count_crlf and lone_lf:
        raise ValueError('混合換行格式，拒絕自動修改')
    if '\r' in text.replace('\r\n', ''):
        raise ValueError('非預期 CR 換行，拒絕自動修改')
    newline = '\r\n' if count_crlf else '\n'
    return text.replace('\r\n', '\n'), enc, newline


def reencode(source: str, enc: str, newline: str):
    return source.replace('\n', newline).encode(enc)


def validate_ui_ast(source: str):
    t = ast.parse(source)
    apps = [x for x in t.body if isinstance(x, ast.ClassDef) and x.name == 'App']
    if len(apps) != 1:
        raise ValueError('找不到唯一 App 類別')
    methods = {x.name for x in apps[0].body if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef))}
    required = {'_build', '_start', '_set_busy', '_selected_outputs'}
    if not required.issubset(methods):
        raise ValueError('App 缺少必要的方法：' + ', '.join(sorted(required-methods)))
    return methods


def make_patch(source: str):
    methods = validate_ui_ast(source)
    if MARKER in source:
        if (source.count(MARKER) >= 1 and 'def restore_media(self):' in source and
                'self.btn_restore_media = ttk.Button(' in source and
                'self.btn_restore_media' in source[source.find('self._busy_widgets ='):source.find('self._busy_widgets =')+260]):
            return source, 'ALREADY_INSTALLED'
        raise ValueError('偵測到不完整的先前可見按鈕安裝，停止以免產生重複方法')
    if 'restore_media' in methods or 'self.btn_restore_media' in source:
        raise ValueError('目前已有不同版本的媒體還原程式，請勿重複加入')

    anchor_button = '        self.btn_run_files.pack(side="right")'
    if source.count(anchor_button) != 1:
        raise ValueError('無法唯一定位檔案分頁的「開始處理檔案」操作列')
    preceding = source[:source.index(anchor_button)].splitlines()[-1]
    if not ('self.btn_run_files = ttk.Button(' in preceding and
            'command=self.run_files' in preceding):
        raise ValueError('按鈕宣告不同，未確認上方工具列位置')
    added_button = (anchor_button + '\n'
        '        # SEAMLESS_RESTORE_VISIBLE_UI_V2: 固定顯示在檔案處理操作列。\n'
        '        self.btn_restore_media = ttk.Button(bar, text="還原 PDF／圖片…",\n'
        '                                            command=self.restore_media, style="Success.TButton")\n'
        '        self.btn_restore_media.pack(side="right", padx=(0, 8))')
    source = source.replace(anchor_button, added_button, 1)

    # Existing busy widget list is the safest point to integrate worker disable.
    busy_anchor = re.compile(r'(self\.btn_run_files\s*,\s*)(self\.btn_scan\s*,)')
    if len(busy_anchor.findall(source)) != 1:
        raise ValueError('無法唯一定位 _busy_widgets，拒絕套用')
    source = busy_anchor.sub(r'\g<1>self.btn_restore_media, \g<2>', source, count=1)

    method_anchor = '    def _selected_outputs(self):\n'
    if source.count(method_anchor) != 1:
        raise ValueError('無法唯一定位 _selected_outputs，拒絕加入還原方法')
    source = source.replace(method_anchor, RESTORE_METHOD + method_anchor, 1)

    # Suppress old dynamic hook only if it matches known former installer exactly.
    if '# SEAMLESS_RESTORE_UI_V1' in source:
        if source.count(OLD_HOOK) != 1:
            raise ValueError('舊版動態 UI hook 格式不同，拒絕冒險移除')
        source = source.replace(OLD_HOOK,
            '# 舊版動態 hook 已由 SEAMLESS_RESTORE_VISIBLE_UI_V2 的固定按鈕取代。', 1)

    validate_ui_ast(source)
    ast.parse(source)
    return source, 'PATCH_AVAILABLE'


def _atomic_replace(path: Path, data: bytes):
    fd, temp = tempfile.mkstemp(prefix='.demo_restore_visible_',suffix='.py',dir=str(path.parent))
    try:
        with os.fdopen(fd, 'wb') as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def main():
    ap = argparse.ArgumentParser(description='不依賴舊 SHA256 的精準安全 UI 還原按鈕安裝器')
    ap.add_argument('--project', type=Path, default=Path.cwd())
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='唯讀檢查（預設）')
    mode.add_argument('--apply', action='store_true', help='備份後原子套用 UI 修改')
    ap.add_argument('--diff', action='store_true', help='檢視改動摘要和程式碼差異（不會寫檔）')
    args = ap.parse_args()

    project = args.project.resolve()
    target = project / 'PII_UI_Demo_Refresh' / 'ui_demo.py'
    media = project / 'handler_media.py'
    seam = project / 'seamless_pdf_restore.py'
    if not target.is_file() or target.is_symlink():
        raise SystemExit('REFUSED: 找不到正常的 ui_demo.py')
    if not media.is_file() or media.is_symlink() or not seam.is_file() or seam.is_symlink():
        raise SystemExit('REFUSED: 找不到已整合的無接縫 PDF 還原後端（handler_media.py、seamless_pdf_restore.py）')
    try:
        media_source = media.read_text(encoding='utf-8-sig')
        if 'from seamless_pdf_restore import restore_pdf_flat' not in media_source:
            raise ValueError('handler_media.py 尚未套用無接縫 PDF 還原，請先完成前次後端安裝')
        raw = target.read_bytes()
        source, enc, newline = normalize(raw)
        updated, status = make_patch(source)
        patched = reencode(updated, enc, newline)
        ast.parse(updated)
    except (ValueError, SyntaxError, UnicodeError) as exc:
        raise SystemExit('REFUSED: ' + str(exc))

    print('CURRENT_UI_SHA256:', checksum(raw))
    print('PATCHED_UI_SHA256:', checksum(patched))
    print('STRUCTURE:', status)
    print('BACKEND:', 'READY')
    if args.diff:
        before = source.splitlines(keepends=True)
        after = updated.splitlines(keepends=True)
        for line in difflib.unified_diff(before,after,fromfile='on_device_ui_demo.py',tofile='after_patch_ui_demo.py'):
            print(line,end='')
    if status == 'ALREADY_INSTALLED':
        print('STATUS: ALREADY_INSTALLED (no changes)')
        return
    if not args.apply:
        print('STATUS: CHECK_PASS (read only)')
        return

    backup = target.with_name(target.name+'.backup_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f'))
    if backup.exists():
        raise SystemExit('REFUSED: 備份檔名衝突')
    # Read again: prevent clobber if the user changed the UI between check and apply.
    if checksum(target.read_bytes()) != checksum(raw):
        raise SystemExit('REFUSED: 檔案於檢查期間被修改')
    shutil.copy2(target, backup)
    try:
        if checksum(target.read_bytes()) != checksum(raw):
            raise RuntimeError('備份後原檔被修改，停止套用')
        _atomic_replace(target,patched)
        if checksum(target.read_bytes()) != checksum(patched):
            raise RuntimeError('替換後檔案完整性不符')
    except Exception as exc:
        # Restore ONLY if our candidate is on disk; never overwrite third-party changes.
        if checksum(target.read_bytes()) == checksum(patched):
            _atomic_replace(target,raw)
        raise SystemExit('REFUSED / ROLLBACK: '+type(exc).__name__) from exc
    print('STATUS: PATCH_INSTALLED')
    print('BACKUP:', backup.name)
    print('VISIBLE_BUTTON: 檔案 / PDF 分頁，「開始處理檔案」左側')
    print('只更新 UI。關閉舊視窗、重新啟動後檢查按鈕。')

if __name__ == '__main__':
    main()
